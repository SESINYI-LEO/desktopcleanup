"""Core sorting logic for Desktop Organizer.

This module has no GUI dependencies so it can be unit-tested and reused.
All functions take the configuration explicitly instead of reading a global.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

APP_DIR = Path(os.environ.get("DESKTOP_ORGANIZER_HOME") or (Path.home() / ".desktop_organizer_v5"))
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"
MANIFEST_DIR = APP_DIR / "manifests"

HISTORY_LIMIT = 10

DEFAULT_CONFIG = {
    "default_path": "",  # filled in by default_config() with the real Desktop folder
    "max_files": 2000,
    "skip_hidden_files": True,
    "skip_symlinks": True,
    "skip_extensions": [],
    "rules": {
        ".pdf": "Documents",
        ".docx": "Documents",
        ".txt": "Documents",
        ".rtf": "Documents",
        ".odt": "Documents",
        ".csv": "Documents",
        ".jpg": "Images",
        ".jpeg": "Images",
        ".png": "Images",
        ".gif": "Images",
        ".webp": "Images",
        ".mp4": "Videos",
        ".mkv": "Videos",
        ".avi": "Videos",
        ".mov": "Videos",
        ".mp3": "Music",
        ".wav": "Music",
        ".flac": "Music",
        ".aac": "Music",
        ".zip": "Archives",
        ".rar": "Archives",
        ".7z": "Archives",
        ".tar": "Archives",
        ".exe": "Applications",
        ".msi": "Applications",
        ".bat": "Applications",
        ".lnk": "Applications",
    },
    "games_keywords": ["steam", "minecraft", "valorant", "epic", "roblox", "game", "launcher", "riot"],
    # The games rule only applies to these extensions, so "game_plan.docx" is not a game.
    # Set to [] to apply the keywords to every file type.
    "games_extensions": [".exe", ".lnk", ".url", ".msi", ".bat"],
    "suspicious": {
        "folder": "_Suspicious",
        "entropy_threshold": 7.5,
        "risky_extensions": [
            ".exe", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr", ".msi",
            ".hta", ".wsf", ".jar", ".pif",
        ],
        "double_extension_targets": [
            ".exe", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr", ".hta", ".wsf", ".jar", ".pif",
        ],
        "bait_words": ["invoice", "receipt", "payment", "urgent", "password", "bank"],
    },
    "folders": {"duplicates": "Duplicates", "other": "Other", "games": "Games"},
    "custom_categories": {},
}

# Unicode bidirectional controls, used to disguise extensions (e.g. "invoice‮gpj.exe").
BIDI_CONTROLS = set("‪‫‬‭‮⁦⁧⁨⁩")

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_INVALID_FOLDER_CHARS = set('\\/:*?"<>|')

Callback = Callable[[tuple], None]


class SortError(Exception):
    """A fatal problem that stops a scan or sort (as opposed to a per-file error)."""


# --------------------------------------------------------------------------- app dirs / logging


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)  # no-op on Windows; keeps logs/manifests private on POSIX
    except OSError:
        pass


def setup_logging() -> None:
    ensure_app_dirs()
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[handler],
        force=True,
    )


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + replace so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- desktop detection


def _windows_desktop() -> Optional[Path]:
    """Ask Windows where the Desktop really is (handles OneDrive folder redirection)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        guid = GUID(0xB4BFCC3A, 0xDB2C, 0x424C, (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if result != 0 or not path_ptr.value:
            return None
        try:
            return Path(path_ptr.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(path_ptr, ctypes.c_void_p))
    except Exception:  # pragma: no cover - Windows only
        return None


def detect_desktop() -> Path:
    home = Path.home()
    if sys.platform == "win32":  # pragma: no cover - Windows only
        known = _windows_desktop()
        if known and known.is_dir():
            return known
        for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            base = os.environ.get(env_name)
            if base and (Path(base) / "Desktop").is_dir():
                return Path(base) / "Desktop"
    return home / "Desktop"


# --------------------------------------------------------------------------- config


def sanitize_folder_name(name, fallback: str) -> str:
    """Return a safe single-level folder name, or `fallback` if `name` is unsafe.

    Rejects path separators, "..", absolute paths, control/invalid characters,
    trailing dots/spaces and reserved Windows device names, so a config value can
    never send files outside the folder being organized.
    """
    candidate = str(name).strip() if name is not None else ""
    unsafe = (
        not candidate
        or any(ch in _INVALID_FOLDER_CHARS or ord(ch) < 32 for ch in candidate)
        or candidate.endswith((".", " "))
        or candidate.split(".")[0].strip().lower() in _WINDOWS_RESERVED
    )
    if unsafe:
        logging.warning("Unsafe folder name %r replaced with %r", name, fallback)
        return fallback
    return candidate


def _normalize_ext(ext) -> Optional[str]:
    value = str(ext).strip().lower()
    if not value:
        return None
    return value if value.startswith(".") else f".{value}"


def _ext_list(values) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    for ext in values:
        normalized = _normalize_ext(ext)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _str_list(values) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return [str(v).strip().lower() for v in values if str(v).strip()]


def merge_config(defaults: dict, user: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def normalize_custom_categories(raw) -> dict:
    normalized = {}
    if not isinstance(raw, dict):
        return normalized
    for category, data in raw.items():
        name = str(category).strip()
        if not name:
            continue
        safe_name = sanitize_folder_name(name, "")
        if not safe_name:
            continue
        if isinstance(data, list):
            data = {"extensions": data, "keywords": []}
        if not isinstance(data, dict):
            continue
        extensions = _ext_list(data.get("extensions", []))
        keywords = _str_list(data.get("keywords", []))
        if extensions or keywords:
            normalized[safe_name] = {"extensions": sorted(set(extensions)), "keywords": sorted(set(keywords))}
    return normalized


def normalize_config(config: dict) -> dict:
    cfg = copy.deepcopy(config)

    if not cfg.get("default_path"):
        cfg["default_path"] = str(detect_desktop())

    try:
        max_files = int(cfg.get("max_files", 2000))
    except (TypeError, ValueError):
        max_files = 2000
    cfg["max_files"] = max_files if max_files >= 1 else 2000
    cfg["skip_hidden_files"] = bool(cfg.get("skip_hidden_files", True))
    cfg["skip_symlinks"] = bool(cfg.get("skip_symlinks", True))
    cfg["skip_extensions"] = _ext_list(cfg.get("skip_extensions", []))

    folders_in = cfg.get("folders") if isinstance(cfg.get("folders"), dict) else {}
    cfg["folders"] = {
        key: sanitize_folder_name(folders_in.get(key, default), default)
        for key, default in DEFAULT_CONFIG["folders"].items()
    }
    other_folder = cfg["folders"]["other"]

    rules_in = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else {}
    rules = {}
    for ext, folder in rules_in.items():
        ext_key = _normalize_ext(ext)
        if ext_key:
            rules[ext_key] = sanitize_folder_name(folder, other_folder)
    cfg["rules"] = rules

    cfg["games_keywords"] = _str_list(cfg.get("games_keywords", []))
    cfg["games_extensions"] = _ext_list(cfg.get("games_extensions", []))

    suspicious = merge_config(DEFAULT_CONFIG["suspicious"], cfg.get("suspicious") if isinstance(cfg.get("suspicious"), dict) else {})
    suspicious["folder"] = sanitize_folder_name(suspicious.get("folder"), DEFAULT_CONFIG["suspicious"]["folder"])
    try:
        threshold = float(suspicious.get("entropy_threshold", 7.5))
    except (TypeError, ValueError):
        threshold = 7.5
    suspicious["entropy_threshold"] = threshold if 0 < threshold <= 8 else 7.5
    suspicious["risky_extensions"] = _ext_list(suspicious.get("risky_extensions", []))
    suspicious["double_extension_targets"] = _ext_list(suspicious.get("double_extension_targets", []))
    suspicious["bait_words"] = _str_list(suspicious.get("bait_words", []))
    cfg["suspicious"] = suspicious

    cfg["custom_categories"] = normalize_custom_categories(cfg.get("custom_categories", {}))
    return cfg


def default_config() -> dict:
    return normalize_config(copy.deepcopy(DEFAULT_CONFIG))


def persist_config(config: dict, path: Optional[Path] = None) -> None:
    atomic_write_text(path or CONFIG_PATH, json.dumps(config, indent=2))


def load_config(path: Optional[Path] = None) -> dict:
    config_path = path or CONFIG_PATH
    ensure_app_dirs()
    if not config_path.exists():
        config = default_config()
        persist_config(config, config_path)
        return config

    try:
        user_config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(user_config, dict):
            raise ValueError("config root must be a JSON object")
        return normalize_config(merge_config(DEFAULT_CONFIG, user_config))
    except Exception as exc:
        backup = config_path.with_name(f"{config_path.stem}.broken_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            shutil.copy2(config_path, backup)
        except OSError:
            pass
        logging.exception("Failed to load config, falling back to defaults: %s", exc)
        config = default_config()
        persist_config(config, config_path)
        return config


# --------------------------------------------------------------------------- file inspection


def file_hash(file_path: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        logging.warning("Hash failed for %s: %s", file_path, exc)
        return None


def entropy(file_path: Path, max_bytes: int = 128000) -> float:
    try:
        with file_path.open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError as exc:
        logging.warning("Entropy read failed for %s: %s", file_path, exc)
        return 0.0
    if not data:
        return 0.0

    freq = [0] * 256
    for byte in data:
        freq[byte] += 1
    total = len(data)
    value = 0.0
    for count in freq:
        if count:
            p = count / total
            value -= p * math.log2(p)
    return round(value, 3)


def get_safe_dest(target_dir: Path, filename: str) -> Path:
    destination = target_dir / filename
    if not destination.exists():
        return destination
    stem, ext = destination.stem, destination.suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{ext}"
        if not candidate.exists():
            return candidate
        index += 1


def is_within(root: Path, candidate: Path) -> bool:
    """True if `candidate` resolves to a location inside `root`."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def tokenize_filename(file_path: Path) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", file_path.stem.lower()).split())


def is_game(file_path: Path, config: dict) -> bool:
    game_exts = config.get("games_extensions", [])
    if game_exts and file_path.suffix.lower() not in game_exts:
        return False
    name = file_path.name.lower()
    tokens = tokenize_filename(file_path)
    for keyword in config["games_keywords"]:
        if " " in keyword:
            if keyword in name:
                return True
        elif keyword in tokens or keyword in name:
            return True
    return False


def looks_suspicious(file_path: Path, config: dict) -> bool:
    cfg = config["suspicious"]
    lower_name = file_path.name.lower()

    if any(ch in BIDI_CONTROLS for ch in file_path.name):
        return True

    targets = "|".join(re.escape(ext.lstrip(".")) for ext in cfg["double_extension_targets"])
    if targets and re.search(rf"\.\w+\s*\.({targets})$", lower_name):
        return True

    if file_path.suffix.lower() in set(cfg["risky_extensions"]):
        if any(word in lower_name for word in cfg["bait_words"]):
            return True
    return False


def match_custom_category(file_path: Path, config: dict) -> Optional[tuple[str, str]]:
    categories = config.get("custom_categories", {})
    if not categories:
        return None
    extension = file_path.suffix.lower()
    lower_name = file_path.name.lower()
    tokens = tokenize_filename(file_path)

    for category, data in categories.items():
        if extension and extension in set(data.get("extensions", [])):
            return category, f"custom extension {extension}"
        for keyword in data.get("keywords", []):
            if " " in keyword:
                if keyword in lower_name:
                    return category, f"custom keyword {keyword}"
            elif keyword in tokens or keyword in lower_name:
                return category, f"custom keyword {keyword}"
    return None


def decide(file_path: Path, config: dict, seen: dict, size_counts: Optional[Counter] = None) -> tuple[str, str]:
    """Pick a destination folder for one file.

    `seen` maps content hash -> whether the first copy was suspicious. It is shared across
    the files of one run. `size_counts` (size -> number of files with that size) lets us
    skip hashing files whose size is unique; pass None to always hash.
    """
    suspicious = looks_suspicious(file_path, config)

    digest = None
    try:
        size = file_path.stat().st_size
    except OSError:
        size = -1
    # Empty files are all "identical" but not meaningful duplicates.
    if size > 0 and (size_counts is None or size_counts.get(size, 0) > 1):
        digest = file_hash(file_path)

    if digest:
        if digest not in seen:
            seen[digest] = suspicious
        elif not suspicious:
            if seen[digest]:
                # A renamed copy of a suspicious file must not be filed as a harmless duplicate.
                return config["suspicious"]["folder"], "duplicate of a suspicious file"
            return config["folders"]["duplicates"], "duplicate hash"

    if suspicious:
        return config["suspicious"]["folder"], "suspicious filename"

    custom = match_custom_category(file_path, config)
    if custom:
        return custom

    if is_game(file_path, config):
        return config["folders"]["games"], "game keyword"

    extension = file_path.suffix.lower()
    if extension in config["rules"]:
        return config["rules"][extension], "extension rule"

    if entropy(file_path) > config["suspicious"]["entropy_threshold"]:
        return config["suspicious"]["folder"], "high entropy"

    return config["folders"]["other"], "unknown type"


# --------------------------------------------------------------------------- scanning


FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


def is_hidden_file(name: str, attributes: int = 0) -> bool:
    """Dot-files, plus Windows hidden/system files such as desktop.ini."""
    return name.startswith(".") or bool(attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def list_files(path: Path, config: dict) -> list[Path]:
    skip_extensions = set(config.get("skip_extensions", []))
    files = []
    for entry in path.iterdir():
        try:
            if not entry.is_file():
                continue
            if config.get("skip_hidden_files", True):
                attributes = getattr(entry.stat(), "st_file_attributes", 0)
                if is_hidden_file(entry.name, attributes):
                    continue
            if config.get("skip_symlinks", True) and entry.is_symlink():
                continue
            if entry.suffix.lower() in skip_extensions:
                continue
            files.append(entry)
        except OSError as exc:
            logging.warning("Skipping file during enumeration: %s (%s)", entry, exc)
    return sorted(files, key=lambda item: item.name.lower())


def _collect_files(path: Path, config: dict) -> list[Path]:
    try:
        files = list_files(path, config)
    except PermissionError:
        raise SortError("Permission denied. Choose another folder or run with proper access.")
    except OSError as exc:
        raise SortError(f"Could not read folder: {exc}")
    if len(files) > config.get("max_files", 2000):
        raise SortError(f"Too many files ({len(files)}). Increase max_files in config.")
    return files


def _size_counts(files: list[Path]) -> Counter:
    counts: Counter = Counter()
    for file_path in files:
        try:
            counts[file_path.stat().st_size] += 1
        except OSError:
            pass
    return counts


def scan_preview(path: Path, config: dict, callback: Callback):
    """Return ([(name, folder, size_mb, reason)], error_count). Raises SortError when fatal."""
    files = _collect_files(path, config)
    if not files:
        callback(("progress", 1.0))
        return [], 0

    seen: dict = {}
    counts = _size_counts(files)
    preview = []
    errors = 0
    total = len(files)
    for index, file_path in enumerate(files):
        try:
            size_mb = file_path.stat().st_size / (1024 * 1024)
            folder, reason = decide(file_path, config, seen, counts)
            preview.append((file_path.name, folder, size_mb, reason))
        except Exception as exc:
            errors += 1
            callback(("log", f"Skipped {file_path.name}: {exc}"))
            logging.exception("Preview failed for %s", file_path)
        callback(("progress", (index + 1) / total))
    return preview, errors


# --------------------------------------------------------------------------- manifests / history


def write_manifest(run_type: str, root_path: Path, manifest: list[tuple[str, str]], stats: dict) -> Path:
    now = datetime.now()
    manifest_path = MANIFEST_DIR / f"{run_type}_{now.strftime('%Y%m%d_%H%M%S_%f')}.json"
    payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "path": str(root_path),
        "status": "applied",
        "manifest": [{"source": source, "destination": dest} for source, dest in manifest],
        "stats": stats,
    }
    atomic_write_text(manifest_path, json.dumps(payload, indent=2))
    return manifest_path


def read_manifest(manifest_path: Path) -> dict:
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("manifest"), list) or not data.get("path"):
        raise ValueError(f"Invalid manifest: {manifest_path}")
    return data


def _set_manifest_status(manifest_path: Path, status: str) -> None:
    data = read_manifest(manifest_path)
    data["status"] = status
    atomic_write_text(Path(manifest_path), json.dumps(data, indent=2))


def load_history(limit: int = HISTORY_LIMIT) -> tuple[list[Path], list[Path]]:
    """Rebuild undo/redo stacks from saved manifests so undo survives an app restart.

    Returns (undo_stack, redo_stack); the last element of each is the next one to use.
    """
    if not MANIFEST_DIR.exists():
        return [], []
    applied, undone = [], []
    for manifest_path in sorted(MANIFEST_DIR.glob("apply_*.json")):
        try:
            status = read_manifest(manifest_path).get("status", "applied")
        except Exception:
            logging.warning("Ignoring unreadable manifest %s", manifest_path)
            continue
        (undone if status == "undone" else applied).append(manifest_path)

    newest_applied = applied[-1].name if applied else ""
    redo = [p for p in undone if p.name > newest_applied]
    redo.sort(key=lambda p: p.name, reverse=True)  # oldest undone ends up on top
    return applied[-limit:], redo[-limit:]


def apply_sort(path: Path, config: dict, callback: Callback):
    """Move files into category folders. Returns (stats, manifest_path or None).

    Moves are recorded as they happen and the manifest is written even if the run is
    interrupted, so a partial run can still be undone. Raises SortError when fatal.
    """
    stats = {"moved": 0, "scanned": 0, "errors": 0, "skipped": 0}
    files = _collect_files(path, config)
    if not files:
        callback(("progress", 1.0))
        return stats, None

    seen: dict = {}
    counts = _size_counts(files)
    manifest: list[tuple[str, str]] = []
    total = len(files)
    manifest_path = None

    try:
        for index, file_path in enumerate(files):
            stats["scanned"] += 1
            try:
                folder, reason = decide(file_path, config, seen, counts)
                target_dir = path / folder
                if not is_within(path, target_dir):
                    raise ValueError(f"destination folder {folder!r} is outside the chosen folder")
                target_dir.mkdir(parents=True, exist_ok=True)
                destination = get_safe_dest(target_dir, file_path.name)
                shutil.move(str(file_path), str(destination))
                manifest.append((str(file_path), str(destination)))
                stats["moved"] += 1
                callback(("log", f"Moved: {file_path.name} -> {folder} ({reason})"))
            except Exception as exc:
                stats["errors"] += 1
                callback(("log", f"Failed: {file_path.name} -> {exc}"))
                logging.exception("Sort failed for %s", file_path)
            callback(("progress", (index + 1) / total))
    finally:
        if manifest:
            manifest_path = write_manifest("apply", path, manifest, stats)
            callback(("log", f"Manifest saved: {manifest_path}"))
    return stats, manifest_path


def _run_history(manifest_path: Path, callback: Optional[Callback], undo: bool) -> dict:
    data = read_manifest(manifest_path)
    root = Path(data["path"])
    entries = data["manifest"]
    ordered = list(reversed(entries)) if undo else list(entries)
    result = {"done": 0, "skipped": 0, "errors": 0}
    label = "Undo" if undo else "Redo"

    def emit(message: str) -> None:
        if callback:
            callback(("log", message))

    for index, entry in enumerate(ordered):
        try:
            original, moved = Path(entry["source"]), Path(entry["destination"])
            take_from, put_to = (moved, original) if undo else (original, moved)
            # A manifest is just a JSON file: never trust it to move files outside its own folder.
            if not (is_within(root, take_from) and is_within(root, put_to)):
                result["errors"] += 1
                emit(f"{label} refused (path outside {root}): {take_from.name}")
                continue
            if not take_from.exists():
                result["skipped"] += 1
                continue
            put_to.parent.mkdir(parents=True, exist_ok=True)
            final = get_safe_dest(put_to.parent, put_to.name) if put_to.exists() else put_to
            shutil.move(str(take_from), str(final))
            result["done"] += 1
        except Exception as exc:
            result["errors"] += 1
            emit(f"{label} failed for {entry!r}: {exc}")
            logging.exception("%s move failed", label)
        if callback:
            callback(("progress", (index + 1) / len(ordered)))

    if result["errors"] == 0:
        _set_manifest_status(manifest_path, "undone" if undo else "applied")
    return result


def undo_manifest(manifest_path: Path, callback: Optional[Callback] = None) -> dict:
    """Restore files to where they were. Marked as undone only if every move succeeded."""
    return _run_history(manifest_path, callback, undo=True)


def redo_manifest(manifest_path: Path, callback: Optional[Callback] = None) -> dict:
    return _run_history(manifest_path, callback, undo=False)
