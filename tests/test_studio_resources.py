import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from map_editor import bootstrap
from map_editor.core.game_installation import GameInstallation, load_remembered
from studio.ui.shell import TOOL_OPTIONS
from studio.ui.shell import ResourcesPanel
from studio.ui.shell import install_workspace_shell


def test_only_complete_existing_sets_are_offered(tmp_path):
    for name in ("Set1", "sEt7", "Set234", "Set300", "other"):
        (tmp_path / "Sets" / name / "Objects").mkdir(parents=True)
        (tmp_path / "Sets" / name / "Scripts").mkdir()
    for name in ("Set1", "sEt7", "Set300", "other"):
        (tmp_path / "Sets" / name / "Objects" / "SET.BAS").touch()
        (tmp_path / "Sets" / name / "Scripts" / "set.sdf").touch()
    install = GameInstallation.suggest(tmp_path)
    assert install.available_sets() == [1, 7]
    assert install.inventory()["sets"] == 2


def test_resource_paths_are_shared_and_remembered(tmp_path):
    app = QApplication.instance() or QApplication([])
    data = tmp_path / "Data"
    (data / "Sets").mkdir(parents=True)
    custom = tmp_path / "My scripts"
    custom.mkdir()
    settings = tmp_path / "settings.json"
    before = bootstrap.installation()
    try:
        panel = ResourcesPanel(GameInstallation.suggest(tmp_path))
        panel.edits["scripts"].setText(str(custom))
        with patch("map_editor.core.game_installation.settings_path", return_value=settings):
            selected = panel.apply()
        assert bootstrap.installation() == selected
        assert load_remembered(settings).folder("scripts") == custom
        panel.close()
    finally:
        bootstrap.set_installation(before)


def test_resource_selection_is_required_and_shell_is_shared(tmp_path):
    app = QApplication.instance() or QApplication([])
    panel = ResourcesPanel()
    with patch("studio.ui.shell.QMessageBox.warning") as warning:
        assert panel.apply() is None
        warning.assert_called_once()
    window = QMainWindow()
    install_workspace_shell(window, "wireframe_editor")
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles == ["Resources", "Theme", "Change Editor", "Vanilla Compatibility Checker"]
    assert window.menuBar().cornerWidget() is None
    tool_titles = [action.text() for action in window.studio_change_menu.actions()]
    assert "Sector Editor" in tool_titles and "Map Editor" in tool_titles
    install_workspace_shell(window, "wireframe_editor")
    assert len(window.menuBar().actions()) == 4
    panel.close()
    window.close()


def test_change_editor_menu_follows_resources_in_every_tool():
    app = QApplication.instance() or QApplication([])
    for option in TOOL_OPTIONS:
        window = QMainWindow()
        window.menuBar().addMenu("File")
        install_workspace_shell(window, option.key)
        titles = [action.text() for action in window.menuBar().actions()]
        # Change Set (only in the editors that use a set) sits before Change Editor.
        assert titles[:3] == ["File", "Resources", "Theme"]
        assert titles[3:] in (
            ["Change Editor", "Vanilla Compatibility Checker"],
            ["Change Set", "Change Editor", "Vanilla Compatibility Checker"])
        current = [a for a in window.studio_change_menu.actions() if a.isChecked()]
        assert [a.text() for a in current] == [option.title]
        assert not current[0].isEnabled()
        window.close()


class _Tool(QMainWindow):
    def __init__(self, accept_close=True):
        super().__init__()
        self.accept_close = accept_close

    def closeEvent(self, event):
        event.accept() if self.accept_close else event.ignore()


def _switch_to(source, target):
    action = next(a for a in source.studio_change_menu.actions() if a.text() == "Map Editor")
    with patch("main.create_workspace", return_value=target) as create:
        action.trigger()
    create.assert_called_once_with("map_editor")


def test_switching_editor_closes_the_previous_one():
    app = QApplication.instance() or QApplication([])
    source, target = _Tool(), _Tool()
    install_workspace_shell(source, "wireframe_editor")
    source.show()
    target.show()
    _switch_to(source, target)
    assert not source.isVisible()
    assert target.isVisible()
    target.close()


def test_switching_editor_is_cancelled_when_the_old_one_refuses_to_close():
    app = QApplication.instance() or QApplication([])
    source, target = _Tool(accept_close=False), _Tool()
    install_workspace_shell(source, "wireframe_editor")
    source.show()
    target.show()
    _switch_to(source, target)
    assert source.isVisible()
    assert not target.isVisible()
    source.accept_close = True
    source.close()


def test_sector_editor_opens_its_workspace():
    app = QApplication.instance() or QApplication([])
    source = _Tool()
    install_workspace_shell(source, "wireframe_editor")
    action = next(a for a in source.studio_change_menu.actions() if a.text() == "Sector Editor")
    target = _Tool()
    with patch("main.create_workspace", return_value=target) as create, patch("studio.ui.shell.QMessageBox.information") as info:
        action.trigger()
    create.assert_called_once_with("sector_editor")
    info.assert_not_called()
    target.close()
    source.close()


def test_reset_configuration_asks_then_clears_everything(tmp_path):
    app = QApplication.instance() or QApplication([])
    data = tmp_path / "Data"
    (data / "Sets").mkdir(parents=True)
    settings = tmp_path / "settings.json"
    before = bootstrap.installation()
    try:
        with patch("map_editor.core.game_installation.settings_path", return_value=settings):
            panel = ResourcesPanel(GameInstallation.suggest(tmp_path))
            panel.apply()
            assert settings.is_file()
            resets = []
            panel.reset_done.connect(lambda: resets.append(True))
            yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
            with patch("studio.ui.shell.QMessageBox.question", return_value=no) as ask:
                assert panel.reset_configuration() is False
                ask.assert_called_once()
            assert settings.is_file() and panel.root.text() and not resets
            with patch("studio.ui.shell.QMessageBox.question", return_value=yes):
                assert panel.reset_configuration() is True
            assert not settings.exists()
            assert load_remembered(settings) is None
            assert panel.root.text() == "" and all(not e.text() for e in panel.edits.values())
            assert panel.installation() is None
            assert panel.inventory_label.text() == "Select the game installation."
            assert resets == [True]
            panel.close()
    finally:
        bootstrap.set_installation(before)


def test_reconfirming_same_game_keeps_manual_folder_overrides(tmp_path):
    app = QApplication.instance() or QApplication([])
    (tmp_path / "Data" / "Sets").mkdir(parents=True)
    panel = ResourcesPanel(GameInstallation.suggest(tmp_path))
    custom = tmp_path / "Custom Models"
    panel.edits["models"].setText(str(custom))
    panel.root.setText(str(tmp_path))
    panel._suggest()
    assert panel.edits["models"].text() == str(custom)
    panel.close()
