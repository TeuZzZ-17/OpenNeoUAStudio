import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from unittest.mock import patch
import pytest
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QTreeWidgetItem, QMainWindow
from model_editor.window import ModelEditorWindow
from collision_editor.editor import CollisionEditorWindow, CollisionProject, WEAPON, validate_project
from game_object_catalog import GameObject
from game_visuals import VisualChoice
from resource_status import MODIFIED_ROLE, MODIFIED_COLOR
from loading_feedback import loading_resources


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_entity_state_control_precedes_tree_and_state_column_tracks_selection(app):
    window = ModelEditorWindow()
    try:
        browser = window.entity_browser
        window._resources_tabs.setCurrentWidget(window._entity_panel)
        assert browser.layout().indexOf(browser.search) == 0
        assert browser.layout().itemAt(1).layout() is window._entity_controls
        assert window._entity_controls.indexOf(browser.set_combo) == 1
        assert window._entity_controls.indexOf(window.entity_state) == 3
        assert window._entity_controls.indexOf(browser.category) == 5
        assert browser.tree.headerItem().text(1) == 'State'
        obj = GameObject('building', 3, Path('building.cfg'), {'name': 'Building'})
        browser.objects = [obj]
        browser.fill()
        browser.set_visual_state(obj, 'Flat')
        browser.fill()
        assert browser.tree.topLevelItem(0).text(1) == 'Flat'
        assert browser.tree.topLevelItem(0).text(2) == 'Building'
        assert browser.tree.topLevelItem(0).text(3) == '3'
    finally:
        window.close()


def test_geometry_edit_colors_both_browsers_and_undo_restores_color(app):
    from tests.test_selection_runtime_v3 import _selection_family
    from base_mapping_editor import MappingIndex
    from assembly_window import _BAS_NAME_ROLE, _BAS_KIND_ROLE
    window = ModelEditorWindow()
    try:
        family, obj, model = _selection_family()
        window._family = family
        window._base_entry_names.clear()
        window._owner_to_obj = {'root': obj}
        window._selected_owner = 'root'
        window._workbench_obj = obj
        window._mapping_index = MappingIndex(obj)
        window.viewport.load_family(family, primary_owner='root')
        window.edit_toggle_action.blockSignals(True)
        window.edit_toggle_action.setChecked(True)
        window.edit_toggle_action.blockSignals(False)
        window.viewport.enter_edit_mode('root')
        entity = GameObject('vehicle', 1, Path('Vehicles.cfg'), {'name': 'Selection', 'vp_normal': '1'})
        window.entity_browser.objects = [entity]
        with QSignalBlocker(window.entity_browser.set_combo):
            window.entity_browser.set_combo.clear()
            window.entity_browser.set_combo.addItem('Set 1', 1)
        window.entity_browser.fill()
        key = (window.entity_browser.set_number, 'vehicle', 1)
        window._entity_choices[key] = [VisualChoice('Normal', Path('SET.BAS'), 'SELECT')]
        group = QTreeWidgetItem(['Models'])
        resource = QTreeWidgetItem(['SELECT'])
        resource.setData(0, _BAS_NAME_ROLE, 'SELECT')
        resource.setData(0, _BAS_KIND_ROLE, 'base')
        group.addChild(resource)
        window.setbas_tree.clear()
        window.setbas_tree.addTopLevelItem(group)
        original = list(model.points)
        window._remember_geometry_original('root')
        model.points[0] = (.2, 0, 0)
        window._on_geometry_edited('root')
        window._record_edit_command({'kind': 'geometry', 'owner': 'root', 'before': original, 'after': list(model.points)})
        row = window.entity_browser.tree.topLevelItem(0)
        assert row.data(0, MODIFIED_ROLE) and resource.data(0, MODIFIED_ROLE)
        assert row.foreground(0).color() == MODIFIED_COLOR
        window._undo_edit()
        assert model.points == original
        assert not row.data(0, MODIFIED_ROLE) and not resource.data(0, MODIFIED_ROLE)
        window._redo_edit()
        assert row.data(0, MODIFIED_ROLE) and resource.data(0, MODIFIED_ROLE)
        window._undo_edit()
    finally:
        window._geom_dirty.clear()
        window.close()


def test_entity_designer_identity_is_read_only_and_weapon_shape_tab_stays_active(app):
    window = CollisionEditorWindow()
    try:
        assert window.name_edit.isReadOnly()
        assert not window.target_combo.isEnabled()
        assert not hasattr(window, 'source_browser_tabs')
        window.project.target_category = WEAPON
        window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
        window._sync_all()
        assert window.properties_tabs.currentIndex() == window.collision_shape_tab_index
        assert window.properties_tabs.isTabEnabled(window.collision_shape_tab_index)
        _, warnings = validate_project(CollisionProject(name='Projectile', source_model='test.sklt', target_category=WEAPON), None)
        assert not warnings
    finally:
        window.close()


def test_loading_feedback_is_visible_and_closes_after_failure(app):
    window = QMainWindow()
    try:
        with pytest.raises(ValueError):
            with loading_resources(window, 'Loading Set 2'):
                dialogs = window.findChildren(QDialog)
                assert len(dialogs) == 1 and dialogs[0].isVisible()
                assert dialogs[0].findChild(QLabel).text() == 'Loading Set 2'
                with loading_resources(window, 'Nested load'):
                    assert len(window.findChildren(QDialog)) == 1
                raise ValueError('Failed load')
        assert not window._resource_load_active
        assert not dialogs[0].isVisible()
    finally:
        window.close()


def test_change_editor_opens_maximized(app):
    from studio_shell import switch_workspace
    source, target = QMainWindow(), QMainWindow()
    with patch('main.create_workspace', return_value=target):
        switch_workspace(source, 'model_editor')
        assert target.isMaximized()
    target.close()


def test_unselected_wireframe_vertex_is_yellow(app):
    from outline_editor import OutlineCanvas
    canvas = OutlineCanvas()
    canvas.resize(300, 300)
    canvas._points = [(0., 0.)]
    canvas.show()
    app.processEvents()
    point = canvas._to_screen(canvas._points[0], canvas._make_transform()).toPoint()
    assert canvas.grab().toImage().pixelColor(point) == QColor(255, 230, 0)
    canvas.close()


def test_snapshot_uses_the_same_compact_entity_list_as_model(app):
    from snapshot_studio.window import SnapshotStudioWindow
    from entity_list import EntityListMixin
    window = SnapshotStudioWindow()
    try:
        window._right_tabs.setCurrentWidget(window._entity_panel)
        assert window._entity_controls.indexOf(window._set_combo) == 1
        assert window._entity_controls.indexOf(window.entity_state) == 3
        assert window.entity_browser.tree.headerItem().text(1) == 'State'
        assert window._entity_selected.__func__ is EntityListMixin._entity_selected
        assert ModelEditorWindow._entity_selected is EntityListMixin._entity_selected
        assert not window.edit_toggle_action.isEnabled()
    finally:
        window.close()


def test_imported_archive_syncs_the_installed_set_without_reloading(app, tmp_path):
    from set_tabs import sync_archive_set_selector, fill_set_combo
    from map_editor import bootstrap
    from map_editor.core.game_installation import GameInstallation
    from PySide6.QtWidgets import QComboBox
    for number in (1, 4):
        directory = tmp_path / f'Set{number}'
        (directory / 'Objects').mkdir(parents=True)
        (directory / 'Scripts').mkdir()
        (directory / 'Objects/SET.BAS').touch()
        (directory / 'Scripts/set.sdf').touch()
    install = GameInstallation(tmp_path, {'sets': tmp_path})
    previous = bootstrap.installation()
    bootstrap.set_installation(install)
    try:
        combo = QComboBox()
        fill_set_combo(combo, install)
        signals = []
        combo.currentIndexChanged.connect(signals.append)
        assert sync_archive_set_selector(combo, tmp_path / 'Set4/Objects/SET.BAS') == 4
        assert combo.currentData() == 4 and not signals
    finally:
        bootstrap.set_installation(previous)


def test_wireframe_new_and_unlisted_file_clear_the_browser_selection(app, tmp_path):
    from wireframe_editor.window import WireframeEditorWindow
    from sklt_parser import create_minimal_sklt_model
    window = WireframeEditorWindow()
    try:
        listed = tmp_path / 'listed.skl'
        listed.touch()
        row = QTreeWidgetItem(['listed.skl'])
        row.setData(0, Qt.ItemDataRole.UserRole, listed)
        window.game_file_browser.tree.clear()
        window.game_file_browser.tree.addTopLevelItem(row)
        window.game_file_browser.select_path(listed)
        assert window.game_file_browser.tree.currentItem() is row
        window.new_file()
        assert window.game_file_browser.tree.currentItem() is None
        window._current_file_path = tmp_path / 'outside.skl'
        window._show_model(create_minimal_sklt_model('outside.skl'), window._current_file_path)
        assert window.game_file_browser.tree.currentItem() is None
    finally:
        window.outline_editor.mark_clean()
        window.close()


def test_manual_bas_import_clears_entity_selection_but_entity_workflow_preserves_it(app):
    from game_browser import GAME_OBJECT_ROLE
    window = ModelEditorWindow()
    try:
        obj = GameObject('vehicle', 1, Path('Vehicles.cfg'), {'name': 'Entity', 'vp_normal': '1'})
        window.entity_browser.objects = [obj]
        window.entity_browser.fill(obj)
        window._current_entity = obj
        window._entity_loading = True
        window._sync_imported_set(Path('unknown.bas'))
        assert window._current_entity is obj
        assert window.entity_browser.tree.currentItem().data(0, GAME_OBJECT_ROLE) is obj
        window._entity_loading = False
        window._sync_imported_set(Path('unknown.bas'))
        assert window._current_entity is None
        assert window.entity_browser.tree.currentItem() is None
    finally:
        window.close()
