import pytest

import organizer_core as core


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    """Redirect config, logs and manifests into a temp dir so tests never touch ~."""
    home = tmp_path / "app_home"
    monkeypatch.setattr(core, "APP_DIR", home)
    monkeypatch.setattr(core, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(core, "LOG_PATH", home / "app.log")
    monkeypatch.setattr(core, "MANIFEST_DIR", home / "manifests")
    core.ensure_app_dirs()
    return home


@pytest.fixture
def config(app_home):
    return core.default_config()


@pytest.fixture
def desk(tmp_path):
    folder = tmp_path / "desk"
    folder.mkdir()
    return folder


def noop(_event):
    pass
