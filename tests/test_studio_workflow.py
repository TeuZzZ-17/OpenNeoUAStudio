import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel, QTreeWidget
from map_editor import bootstrap
from map_editor.core.game_installation import GameInstallation


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_snapshot_set_change_preserves_page_and_model_starts_on_shared_set(app, tmp_path):
    from snapshot_studio.window import SnapshotStudioWindow
    from model_editor.window import ModelEditorWindow
    for number in (1, 4):
        (tmp_path / f'Set{number}/Objects').mkdir(parents=True)
        (tmp_path / f'Set{number}/Scripts').mkdir()
        (tmp_path / f'Set{number}/Objects/SET.BAS').touch()
        (tmp_path / f'Set{number}/Scripts/set.sdf').touch()
    previous = bootstrap.installation()
    bootstrap.set_installation(GameInstallation(tmp_path, {'sets': tmp_path}))
    loaded = []
    def fake_open(self, path):
        self._setbas = SimpleNamespace(path=Path(path), resources=[])
        loaded.append(Path(path))
        self._raise_setbas_tab()
    try:
        with patch('studio.workbench.window.AssemblyWindow.open_setbas', fake_open):
            snapshot = SnapshotStudioWindow()
            snapshot._right_tabs.setCurrentWidget(snapshot._entity_panel)
            snapshot._set_combo.setCurrentIndex(snapshot._set_combo.findData(4))
            assert snapshot._right_tabs.currentWidget() is snapshot._entity_panel
            before = len(loaded)
            model = ModelEditorWindow()
            assert model._set_combo.currentData() == 4
            assert loaded[before:] == [tmp_path / 'Set4/Objects/SET.BAS']
            from studio.ui.browsers import GameObjectBrowser
            browser = GameObjectBrowser()
            assert browser.set_number == 4
            browser.close()
            model.close()
            snapshot.close()
    finally:
        bootstrap.set_installation(previous)


def test_entity_context_menu_only_copies_and_never_opens_on_right_click(app):
    from studio.ui.browsers import GameObjectBrowser
    from studio.assets.catalog import GameObject
    browser = GameObjectBrowser()
    obj = GameObject('weapon', 7, Path('Weapons.cfg'), {'name': 'Rocket'})
    browser.objects = [obj]
    browser.fill()
    opened = []
    browser.selected.connect(opened.append)
    menu = browser._context_menu(browser.tree.topLevelItem(0))
    actions = {action.text(): action for action in menu.actions()}
    assert not opened
    assert list(actions) == ['Copy Name', 'Copy ID', 'Copy Script Path']
    actions['Copy Name'].trigger()
    assert QApplication.clipboard().text() == 'Rocket'
    actions['Copy ID'].trigger()
    assert QApplication.clipboard().text() == '7'
    assert not opened
    browser.close()


def test_entity_designer_dirty_script_row_clears_after_save_or_reset(app, tmp_path):
    from entity_designer.editor import CollisionEditorWindow
    from studio.assets.catalog import GameObject
    from studio.ui.widgets import MODIFIED_ROLE
    window = CollisionEditorWindow()
    try:
        obj = GameObject('weapon', 7, tmp_path / 'Weapons.cfg', {'name': 'Rocket'})
        window.game_object_browser.objects = [obj]
        window.game_object_browser.fill()
        window._active_script_id = 7
        window._active_script_kind = 'new_weapon'
        window._active_script_path = obj.source
        window._set_modified(True)
        item = window.game_object_browser.tree.topLevelItem(0)
        assert item.data(0, MODIFIED_ROLE)
        window._set_modified(False)
        assert not item.data(0, MODIFIED_ROLE)
    finally:
        window.close()


def test_modified_selected_row_uses_dark_background_instead_of_purple(app):
    from studio.ui.browsers import configure_resource_tree
    from studio.ui.widgets import mark_resource_modified
    from PySide6.QtWidgets import QTreeWidgetItem
    tree = QTreeWidget()
    tree.setHeaderLabels(['Resource'])
    configure_resource_tree(tree)
    tree.setStyleSheet('QTreeWidget::item:selected { background: #aa44bb; color: white; }')
    item = QTreeWidgetItem(['Modified'])
    tree.addTopLevelItem(item)
    mark_resource_modified(item, True)
    tree.setCurrentItem(item)
    tree.resize(400, 120)
    tree.show()
    app.processEvents()
    rectangle = tree.visualItemRect(item)
    pixel = tree.viewport().grab().toImage().pixelColor(QPoint(rectangle.right() - 15, rectangle.center().y()))
    # The selection color of the standard list style (dark blue), not the purple of the stylesheet.
    assert pixel.red() < 60 and pixel.blue() > pixel.red()
    tree.close()


def test_actual_import_bas_archive_command_displays_loading_feedback(app):
    from studio.workbench.window import AssemblyWindow
    window = AssemblyWindow()
    try:
        observed = []
        def load(path):
            observed.append(any(label.isVisible() and 'Loading BAS' in label.text() for label in window.findChildren(QLabel)))
        with patch('studio.workbench.window.choose_bas_archive', return_value='SET.BAS'), patch.object(window, 'open_setbas', side_effect=load):
            window.open_bas_archive_dialog()
        assert observed == [True]
    finally:
        window.close()
