"""File Backup menu: ``.bak`` safety copies are off by default and follow the menu choice."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMainWindow

from entity_designer.script_model import write_script_file
from studio.exchange.portable import (
    backup_enabled, backup_existing_file, commit_verified_files, set_backup_enabled)
from studio.ui.shell import TOOL_OPTIONS, install_workspace_shell
from studio.ui.style import install_backup_menu, set_backup_option


@pytest.fixture
def isolated_settings(tmp_path):
    """Keep the Backup choice out of the real user settings."""
    QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    set_backup_enabled(False)
    yield
    set_backup_enabled(False)
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def _commit(tmp_path: Path, name: str, payload: bytes) -> Path:
    stage = tmp_path / f"stage_{name}"
    stage.write_bytes(payload)
    target = tmp_path / name
    commit_verified_files([(stage, target)])
    return target


def test_backup_is_disabled_by_default(tmp_path):
    set_backup_enabled(False)
    assert not backup_enabled()
    target = _commit(tmp_path, "model.BASE", b"old")
    _commit(tmp_path, "model.BASE", b"new")
    assert target.read_bytes() == b"new"
    assert not list(tmp_path.glob("*.bak"))


def test_enabled_backup_keeps_every_previous_version(tmp_path):
    set_backup_enabled(True)
    try:
        target = _commit(tmp_path, "model.BASE", b"v1")
        assert not list(tmp_path.glob("*.bak"))           # nothing to back up on first write
        _commit(tmp_path, "model.BASE", b"v2")
        _commit(tmp_path, "model.BASE", b"v3")
    finally:
        set_backup_enabled(False)
    assert target.read_bytes() == b"v3"
    assert (tmp_path / "model.BASE.bak").read_bytes() == b"v1"
    assert (tmp_path / "model.BASE.1.bak").read_bytes() == b"v2"


def test_direct_writers_back_up_only_when_enabled(tmp_path):
    script = tmp_path / "unit.scr"
    script.write_bytes(b"old")
    write_script_file(script, "new", "utf-8", False)
    assert not list(tmp_path.glob("*.bak"))
    set_backup_enabled(True)
    try:
        script.write_bytes(b"old")
        write_script_file(script, "new", "utf-8", False)
        assert backup_existing_file(tmp_path / "missing.txt") is None
    finally:
        set_backup_enabled(False)
    assert (tmp_path / "unit.scr.bak").read_bytes() == b"old"
    assert script.read_bytes() == b"new"


def test_backup_menu_defaults_to_disable_and_follows_the_choice(isolated_settings):
    windows = [QMainWindow(), QMainWindow()]
    for window in windows:
        install_backup_menu(window)
    for window in windows:
        actions = window.studio_backup_menu.actions()
        assert [a.text() for a in actions] == ["Enable", "Disable"]
        assert [a.isChecked() for a in actions] == [False, True]
    assert not backup_enabled()

    windows[0].studio_backup_menu.actions()[0].trigger()
    assert backup_enabled()
    for window in windows:                                # every open workspace stays in step
        assert [a.isChecked() for a in window.studio_backup_menu.actions()] == [True, False]
    assert QSettings("OpenNeoUA", "Studio").value("backup/enabled", False, type=bool) is True

    set_backup_option(False)
    assert not backup_enabled()
    assert [a.isChecked() for a in windows[1].studio_backup_menu.actions()] == [False, True]


def test_every_workspace_has_the_backup_menu_before_theme(isolated_settings):
    for option in TOOL_OPTIONS:
        window = QMainWindow()
        install_workspace_shell(window, option.key)
        titles = [action.text() for action in window.menuBar().actions()]
        assert titles.index("File Backup") == titles.index("Theme") - 1
        window.close()
