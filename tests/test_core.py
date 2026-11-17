import json
import os
import shutil
from pathlib import Path

import pytest

import organizer_core as core
from conftest import noop


def make(folder: Path, name: str, content: bytes = b"hello") -> Path:
    path = folder / name
    path.write_bytes(content)
    return path


def folder_of(config, path, seen=None):
    return core.decide(path, config, {} if seen is None else seen)[0]


# ----------------------------------------------------------------- classification


def test_extension_rules(config, desk):
    assert folder_of(config, make(desk, "a.pdf")) == "Documents"
    assert folder_of(config, make(desk, "b.PNG")) == "Images"
    assert folder_of(config, make(desk, "c.mp3")) == "Music"


def test_unknown_low_entropy_goes_to_other(config, desk):
    assert folder_of(config, make(desk, "notes.xyz", b"aaaa" * 100)) == "Other"


def test_high_entropy_unknown_is_quarantined(config, desk):
    make(desk, "blob.bin", os.urandom(50000))
    assert folder_of(config, desk / "blob.bin") == "_Suspicious"


@pytest.mark.parametrize("name", ["game_plan.docx", "epicenter_report.pdf", "gamestop_receipt.pdf"])
def test_documents_with_game_words_are_not_games(config, desk, name):
    assert folder_of(config, make(desk, name)) == "Documents"


@pytest.mark.parametrize("name", ["Steam.lnk", "minecraft_launcher.exe", "EpicGamesLauncher.lnk"])
def test_game_launchers_still_go_to_games(config, desk, name):
    assert folder_of(config, make(desk, name)) == "Games"


def test_games_extensions_empty_applies_keywords_everywhere(config, desk):
    config["games_extensions"] = []
    assert folder_of(config, make(desk, "game_plan.docx")) == "Games"


def test_custom_category_extension_and_keyword(config, desk):
    config["custom_categories"] = core.normalize_custom_categories(
        {"Design": {"extensions": ["psd"], "keywords": ["mockup"]}}
    )
    assert folder_of(config, make(desk, "x.psd")) == "Design"
    assert folder_of(config, make(desk, "new mockup v2.png")) == "Design"


@pytest.mark.parametrize(
    "name",
    ["report.pdf.exe", "photo.jpg .scr", "invoice_march.exe", "URGENT-payment.bat", "invoice\u202egpj.exe"],
)
def test_suspicious_names(config, desk, name):
    assert folder_of(config, make(desk, name)) == "_Suspicious"


def test_plain_executable_is_not_suspicious(config, desk):
    assert folder_of(config, make(desk, "setup.exe")) == "Applications"


# ----------------------------------------------------------------- duplicates


def test_second_copy_goes_to_duplicates(config, desk):
    first = make(desk, "a.txt", b"same content")
    second = make(desk, "b.txt", b"same content")
    seen = {}
    assert core.decide(first, config, seen)[0] == "Documents"
    assert core.decide(second, config, seen) == ("Duplicates", "duplicate hash")


def test_empty_files_are_not_duplicates(config, desk):
    seen = {}
    a, b = make(desk, "a.txt", b""), make(desk, "b.txt", b"")
    assert core.decide(a, config, seen)[0] == "Documents"
    assert core.decide(b, config, seen)[0] == "Documents"


def test_renamed_copy_of_suspicious_file_stays_quarantined(config, desk):
    bad = make(desk, "invoice.exe", b"MZ" + b"x" * 100)
    renamed = make(desk, "totally_fine.txt", b"MZ" + b"x" * 100)
    seen = {}
    assert core.decide(bad, config, seen)[0] == "_Suspicious"
    assert core.decide(renamed, config, seen)[0] == "_Suspicious"


def test_unique_sizes_are_never_hashed(config, desk, monkeypatch):
    files = [make(desk, f"f{i}.txt", b"x" * (i + 1)) for i in range(3)]
    monkeypatch.setattr(core, "file_hash", lambda p: pytest.fail("hashed a unique-size file"))
    counts = core._size_counts(files)
    seen = {}
    for f in files:
        core.decide(f, config, seen, counts)


# ----------------------------------------------------------------- config safety


@pytest.mark.parametrize("bad", ["../evil", "..", "a/b", "a\\b", "C:\\Windows", "/etc", "", "  ", "nul", "COM1", "dots.", "x\x00y"])
def test_sanitize_rejects_unsafe_names(bad):
    assert core.sanitize_folder_name(bad, "Other") == "Other"


def test_sanitize_accepts_normal_names():
    assert core.sanitize_folder_name("My Stuff", "Other") == "My Stuff"


def test_config_cannot_escape_the_folder(config):
    cfg = core.normalize_config(
        {"rules": {".txt": "../../escape"}, "folders": {"games": "/abs"}, "suspicious": {"folder": "..\\x"}}
    )
    assert cfg["rules"][".txt"] == "Other"
    assert cfg["folders"]["games"] == "Games"
    assert cfg["suspicious"]["folder"] == "_Suspicious"


def test_normalize_coerces_bad_types():
    cfg = core.normalize_config({"max_files": "lots", "rules": "nope", "games_keywords": None, "custom_categories": 5})
    assert cfg["max_files"] == 2000
    assert cfg["rules"] == {}
    assert cfg["games_keywords"] == []
    assert cfg["custom_categories"] == {}


def test_load_config_creates_defaults(app_home):
    cfg = core.load_config()
    assert core.CONFIG_PATH.exists()
    assert cfg["rules"][".pdf"] == "Documents"
    assert cfg["default_path"]


def test_load_config_recovers_from_broken_json(app_home):
    core.CONFIG_PATH.write_text("{not json", encoding="utf-8")
    cfg = core.load_config()
    assert cfg["rules"][".pdf"] == "Documents"
    assert list(core.APP_DIR.glob("config.broken_*.json"))
    json.loads(core.CONFIG_PATH.read_text(encoding="utf-8"))  # rewritten as valid JSON


def test_user_config_overrides_defaults(app_home):
    core.CONFIG_PATH.write_text(json.dumps({"rules": {"pdf": "Papers"}, "max_files": 5}), encoding="utf-8")
    cfg = core.load_config()
    assert cfg["rules"][".pdf"] == "Papers"
    assert cfg["max_files"] == 5


def test_default_config_is_not_shared_between_loads(app_home):
    first = core.default_config()
    first["custom_categories"]["X"] = {"extensions": [".x"], "keywords": []}
    first["rules"][".pdf"] = "Changed"
    second = core.default_config()
    assert second["custom_categories"] == {}
    assert second["rules"][".pdf"] == "Documents"
    assert core.DEFAULT_CONFIG["custom_categories"] == {}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "c.json"
    core.atomic_write_text(target, "one")
    core.atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


# ----------------------------------------------------------------- listing


def test_hidden_detection():
    assert core.is_hidden_file(".gitignore")
    assert core.is_hidden_file("desktop.ini", core.FILE_ATTRIBUTE_HIDDEN | core.FILE_ATTRIBUTE_SYSTEM)
    assert not core.is_hidden_file("report.pdf", 0)


def test_list_files_skips_hidden_dirs_symlinks_and_skipped_extensions(config, desk):
    make(desk, "keep.txt")
    make(desk, ".hidden")
    make(desk, "shortcut.lnk")
    (desk / "subdir").mkdir()
    try:
        os.symlink(desk / "keep.txt", desk / "link.txt")
    except (OSError, NotImplementedError):
        pass
    config["skip_extensions"] = [".lnk"]
    assert [p.name for p in core.list_files(desk, config)] == ["keep.txt"]


def test_too_many_files_is_fatal(config, desk):
    config["max_files"] = 2
    for i in range(3):
        make(desk, f"f{i}.txt")
    with pytest.raises(core.SortError):
        core.scan_preview(desk, config, noop)
    with pytest.raises(core.SortError):
        core.apply_sort(desk, config, noop)


def test_preview_does_not_move_anything(config, desk):
    make(desk, "a.pdf")
    preview, errors = core.scan_preview(desk, config, noop)
    assert preview[0][:2] == ("a.pdf", "Documents") and errors == 0
    assert (desk / "a.pdf").exists()


# ----------------------------------------------------------------- apply / undo / redo


def test_apply_undo_redo_roundtrip(config, desk):
    make(desk, "a.pdf")
    make(desk, "b.png", b"img")
    stats, manifest = core.apply_sort(desk, config, noop)
    assert stats["moved"] == 2 and stats["errors"] == 0
    assert (desk / "Documents" / "a.pdf").exists() and (desk / "Images" / "b.png").exists()

    result = core.undo_manifest(manifest, noop)
    assert result["done"] == 2 and result["errors"] == 0
    assert (desk / "a.pdf").exists() and (desk / "b.png").exists()
    assert core.read_manifest(manifest)["status"] == "undone"

    result = core.redo_manifest(manifest, noop)
    assert result["done"] == 2
    assert (desk / "Documents" / "a.pdf").exists()
    assert core.read_manifest(manifest)["status"] == "applied"


def test_name_collisions_never_overwrite(config, desk):
    make(desk, "a.pdf", b"new")
    (desk / "Documents").mkdir()
    make(desk / "Documents", "a.pdf", b"old")
    core.apply_sort(desk, config, noop)
    assert (desk / "Documents" / "a.pdf").read_bytes() == b"old"
    assert (desk / "Documents" / "a_1.pdf").read_bytes() == b"new"


def test_history_survives_restart(config, desk):
    make(desk, "a.pdf")
    _, first = core.apply_sort(desk, config, noop)
    make(desk, "b.pdf", b"other")
    _, second = core.apply_sort(desk, config, noop)

    undo, redo = core.load_history()
    assert undo == [first, second] and redo == []

    core.undo_manifest(second, noop)
    undo, redo = core.load_history()  # simulates restarting the app
    assert undo == [first] and redo == [second]


def test_new_sort_after_undo_drops_old_redo(config, desk):
    make(desk, "a.pdf")
    _, first = core.apply_sort(desk, config, noop)
    core.undo_manifest(first, noop)
    make(desk, "b.pdf", b"other")
    _, second = core.apply_sort(desk, config, noop)
    undo, redo = core.load_history()
    assert undo == [second] and redo == []


def test_partial_run_is_still_recorded_and_undoable(config, desk, monkeypatch):
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        make(desk, name, name.encode())
    real_move = shutil.move
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk went away")
        return real_move(src, dst)

    monkeypatch.setattr(core.shutil, "move", flaky)
    stats, manifest = core.apply_sort(desk, config, noop)
    monkeypatch.setattr(core.shutil, "move", real_move)

    assert stats["moved"] == 2 and stats["errors"] == 1
    assert len(core.read_manifest(manifest)["manifest"]) == 2  # only real moves recorded
    assert core.undo_manifest(manifest, noop)["errors"] == 0
    assert {p.name for p in desk.glob("*.pdf")} == {"a.pdf", "b.pdf", "c.pdf"}


def test_undo_refuses_paths_outside_the_sorted_folder(config, desk, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not move me")
    manifest = core.write_manifest("apply", desk, [(str(desk / "x.txt"), str(victim))], {})
    result = core.undo_manifest(manifest, noop)
    assert result["errors"] == 1 and result["done"] == 0
    assert victim.exists() and not (desk / "x.txt").exists()
    assert core.read_manifest(manifest)["status"] == "applied"  # not marked undone, so it can be retried


def test_undo_with_missing_file_is_skipped_not_fatal(config, desk):
    make(desk, "a.pdf")
    _, manifest = core.apply_sort(desk, config, noop)
    (desk / "Documents" / "a.pdf").unlink()
    result = core.undo_manifest(manifest, noop)
    assert result == {"done": 0, "skipped": 1, "errors": 0}


def test_undo_restores_next_to_a_new_file_with_same_name(config, desk):
    make(desk, "a.pdf", b"original")
    _, manifest = core.apply_sort(desk, config, noop)
    make(desk, "a.pdf", b"newer")  # user created another a.pdf meanwhile
    core.undo_manifest(manifest, noop)
    assert (desk / "a.pdf").read_bytes() == b"newer"
    assert (desk / "a_1.pdf").read_bytes() == b"original"


def test_invalid_manifest_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError):
        core.read_manifest(bad)


def test_load_history_ignores_corrupt_manifests(config, desk):
    make(desk, "a.pdf")
    _, good = core.apply_sort(desk, config, noop)
    (core.MANIFEST_DIR / "apply_00000000_000000_000000.json").write_text("garbage")
    undo, _ = core.load_history()
    assert undo == [good]


def test_entropy_values(tmp_path):
    flat = tmp_path / "flat"
    flat.write_bytes(b"a" * 1000)
    rand = tmp_path / "rand"
    rand.write_bytes(os.urandom(100000))
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert core.entropy(flat) == 0.0
    assert core.entropy(rand) > 7.5
    assert core.entropy(empty) == 0.0
    assert core.entropy(tmp_path / "missing") == 0.0
