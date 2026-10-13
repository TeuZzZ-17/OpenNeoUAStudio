import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from unittest.mock import patch
import pytest
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QTreeWidgetItem, QMainWindow
from model_editor.window import ModelEditorWindow
from entity_designer.editor import CollisionEditorWindow, CollisionProject, WEAPON, validate_project
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
        # Compatibility explanations are kept in the checker.
        assert not hasattr(window, 'name_edit')
        assert not hasattr(window, 'target_combo')
        assert not hasattr(window, 'compat_section')
        assert not hasattr(window, 'source_browser_tabs')
        window.project.target_category = WEAPON
        window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
        window._sync_all()
        assert window.properties_tabs.currentIndex() == window.collision_shape_tab_index
        assert window.properties_tabs.isTabEnabled(window.collision_shape_tab_index)
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        window._sync_all()
        assert window.properties_tabs.currentIndex() == window.fx_tab_index
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

@pytest.mark.parametrize('key', ['model_editor', 'snapshot_studio', 'map_editor', 'entity_designer', 'wireframe_editor'])
def test_checker_is_between_resources_and_change_editor(app, key):
    from studio_shell import install_workspace_shell
    from studio_compat import CompatibilityDialog
    from PySide6.QtWidgets import QTabWidget, QWidget
    window = QMainWindow()
    tabs = QTabWidget(window)
    page = QWidget()
    tabs.addTab(page, 'Script')
    window.palette_tabs = tabs
    window.properties_tabs = tabs
    window._tab_compatibility = {page: ('mixed', 'Vanilla and OpenNeoUA features')}
    try:
        install_workspace_shell(window, key)
        actions = [action.text() for action in window.menuBar().actions()]
        assert actions == ['Resources', 'Vanilla Compatibility Checker', 'Change Editor']
        with patch('studio_compat.CompatibilityDialog.exec', return_value=0) as opened:
            window.studio_compat_action.trigger()
            opened.assert_called_once()
        dialog = CompatibilityDialog(window, key)
        assert dialog.entries and all(note for _, _, note in dialog.entries)
        dialog.show()
        app.processEvents()
        assert dialog.width() == 740
        dialog.close()
    finally:
        window.close()


def test_fx_transport_fits_and_ground_survives_empty_loop_tail(app):
    from tests.test_selection_runtime_v3 import _selection_family
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    window = CollisionEditorWindow()
    try:
        family, obj, _ = _selection_family()
        window.family = family
        window.viewport.load_family(family, primary_owner='root')
        window.viewport.set_ground_alignment(True, False, 0)
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        bar = window.fx_panel.transport_bar
        assert bar.minimumSizeHint().width() < 550
        assert window.width() == 1200
        assert window.properties_scroll.geometry().right() < window.width()
        window.fx_panel.speed_combo.setCurrentIndex(0)
        assert window.fx_panel.speed == 0.1
        viewport = window.viewport
        viewport.resize(500, 350)
        # Empty tail, restart and another empty tail all keep the ground.
        for time_ms in (1000.0, 0.0, 2000.0):
            viewport.set_fx_preview(active=True, items=[], show_unit=False, time_ms=time_ms)
            image = QImage(500, 350, QImage.Format.Format_RGBA8888)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            with patch.object(viewport, '_draw_grid', wraps=viewport._draw_grid) as ground:
                viewport._paint_fx_viewport(painter)
                assert ground.call_count == 1
            painter.end()
            background = QColor(24, 26, 32)
            assert any(image.pixelColor(x, y) != background
                       for x in range(20, 480, 10) for y in range(20, 330, 10))
    finally:
        window._modified = False
        window.close()


def test_resource_bands_are_visible_and_modified_selection_stays_red(app):
    from PySide6.QtWidgets import QTreeWidget, QStyle, QStyleOptionViewItem
    from PySide6.QtGui import QImage, QPainter
    from resource_status import use_resource_delegate, mark_resource_modified
    tree = QTreeWidget()
    tree.setHeaderLabels(['Entity'])
    row = QTreeWidgetItem(['Modified vehicle'])
    tree.addTopLevelItem(row)
    use_resource_delegate(tree)
    tree.resize(320, 160)
    tree.show()
    app.processEvents()
    delegate = tree.itemDelegate()
    index = tree.indexFromItem(row)
    mark_resource_modified(row, True)
    colors = []
    for state in (QStyle.StateFlag.State_MouseOver, QStyle.StateFlag.State_Selected):
        option = QStyleOptionViewItem()
        option.initFrom(tree)
        option.widget = tree
        option.rect = tree.visualRect(index)
        option.state |= state
        image = QImage(tree.size(), QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        delegate.paint(painter, option, index)
        painter.end()
        colors.append(image.pixelColor(option.rect.right() - 10, option.rect.center().y()))
        assert row.foreground(0).color() == MODIFIED_COLOR
    assert colors[0] != colors[1]
    assert all(color.blue() > color.red() for color in colors)
    assert tree.hasMouseTracking()
    tree.close()

@pytest.mark.parametrize('key', ['model_editor', 'snapshot_studio', 'map_editor', 'entity_designer'])
def test_all_workspace_tabs_use_checker_marker_colors(app, key):
    from studio_shell import install_workspace_shell
    from studio_compat import compatibility_entries, compat_color, PREVIEW
    from studio_tabs import PaletteTabStrip
    from snapshot_studio.window import SnapshotStudioWindow
    from map_editor.editor import create_window
    factories = {'model_editor': ModelEditorWindow,
                 'snapshot_studio': SnapshotStudioWindow,
                 'map_editor': lambda: create_window([]),
                 'entity_designer': CollisionEditorWindow}
    from map_editor import bootstrap
    previous_installation = bootstrap.installation()
    window = factories[key]()
    try:
        install_workspace_shell(window, key)
        expected = {name.casefold(): level for names, level, _ in compatibility_entries(window, key)
                    for name in names.split(' / ')}
        expected.update({name: PREVIEW for name in ('log', 'warnings', 'validation')})
        if key == 'map_editor':
            buttons = window.palette_tabs._buttons
        else:
            from PySide6.QtWidgets import QTabWidget
            buttons = [button for tabs in window.findChildren(QTabWidget)
                       if hasattr(tabs, '_palette_strip')
                       for button in tabs._palette_strip.buttons()]
        assert buttons
        for button in buttons:
            assert button.marker is not None, button.text()
            if button.text().casefold() in expected:
                assert button.marker == compat_color(expected[button.text().casefold()])
        assert all(button.marker.name() in ('#5cbf72', '#e3c447', '#e8873a') for button in buttons)
    finally:
        window.close()
        bootstrap.set_installation(previous_installation)


def test_ground_has_only_alignment_controls_and_other_tabs_keep_the_gizmo(app):
    window = CollisionEditorWindow()
    try:
        gizmo = window.transform_box
        window.show()
        window.properties_tabs.setCurrentIndex(window.ground_tab_index)
        app.processEvents()
        assert gizmo.isHidden()
        assert gizmo.parentWidget() is not window.ground_tab
        assert window.ground_tab_layout.indexOf(window.ground_alignment_box) == 0
        assert window.ground_tab_layout.indexOf(gizmo) == -1
        window._sync_all()
        assert gizmo.isHidden()
        window.properties_tabs.setCurrentIndex(window.fire_points_tab_index)
        app.processEvents()
        assert window.transform_box is gizmo and not gizmo.isHidden()
        assert len(window.gizmo.directions) == 6
    finally:
        window.close()


def test_fx_reset_view_restores_scene_camera_without_changing_models(app):
    from types import SimpleNamespace
    from tests.test_selection_runtime_v3 import _selection_family
    from PySide6.QtCore import QPointF
    window = CollisionEditorWindow()
    try:
        family, _, _ = _selection_family()
        viewport = window.viewport
        viewport.load_family(family, primary_owner='root')
        viewport.capture_reset_view()
        normal_home = viewport._home_camera_state
        window.fx_panel.simulation = SimpleNamespace(extent=lambda: ((300., -100., 20.), 600.))
        window._fit_fx_view()
        scene_home = viewport._camera_state()
        faces = viewport._faces
        viewport.set_fx_preview(active=True, time_ms=420.)
        viewport._zoom = 8.
        viewport._pan = QPointF(300., 200.)
        viewport._center = (0., 0., 0.)
        viewport.reset_view()
        assert viewport._camera_state() == scene_home
        assert viewport._faces is faces
        assert viewport._fx_time_ms == 420.
        viewport.set_fx_preview(active=False)
        viewport.reset_view()
        assert viewport._camera_state() == normal_home
    finally:
        window.close()


def test_fx_unit_vanm_uses_toolbar_frames_and_effects_use_scene_clock(app):
    from assembly_viewer import AssetViewport, ViewMaterial
    from entity_designer.editor import CollisionViewport
    from tests.test_selection_runtime_v3 import _selection_family
    from PySide6.QtGui import QImage, QPainter
    family, _, _ = _selection_family()
    viewport = CollisionViewport()
    viewport.load_family(family, primary_owner='root')
    frames = [(16, 0, 0), (16, 0, 0)]
    viewport._materials = [ViewMaterial('unit', anim_frames=frames),
                           ViewMaterial('effect', anim_frames=frames)]
    viewport._faces[0].material = 0
    viewport._faces[1].material = 1
    viewport._faces[1].owner = 'effect'
    viewport._reset_animation_states()
    viewport.set_fx_preview(active=True, owner_by_vp={10: 'root', 20: 'effect'},
                            unit_owners={'root'}, time_ms=0.)
    viewport.play_animation(True)
    viewport._anim_timer.stop()
    viewport._advance_animation()
    assert viewport._anim_states[0] == (1, 1)
    rendered = []
    image = QImage(400, 300, QImage.Format.Format_RGBA8888)
    painter = QPainter(image)
    try:
        with patch.object(AssetViewport, '_paint_viewport',
                          lambda self, painter: rendered.append(dict(self._anim_states))):
            viewport._paint_fx_viewport(painter)
        assert rendered[-1][0] == (1, 1)
        assert rendered[-1][1] == (0, 1)
        viewport.play_animation(False)
        viewport.step_animation()
        with patch.object(AssetViewport, '_paint_viewport',
                          lambda self, painter: rendered.append(dict(self._anim_states))):
            viewport._paint_fx_viewport(painter)
        assert rendered[-1][0] == (0, 1)
        assert rendered[-1][1] == (0, 1)
    finally:
        painter.end()
        viewport.close()


def test_fx_context_menu_only_contains_preview_actions_and_routes_click(app):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu
    window = CollisionEditorWindow()
    try:
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        menu = window._create_fx_context_menu()
        actions = [action for action in menu.actions() if not action.isSeparator()]
        assert [action.text() for action in actions] == [
            'Play', 'Restart', 'Stop', 'Reset View']
        actions[0].trigger()
        assert window.fx_panel.is_playing()
        assert window._create_fx_context_menu().actions()[0].text() == 'Pause'
        actions[2].trigger()
        assert not window.fx_panel.is_playing()
        assert window.fx_panel.time_ms == 0.
        with patch.object(window, '_create_fx_context_menu') as created, \
             patch.object(window, '_create_sphere_context_menu') as spheres:
            window._show_sphere_context_menu(-1, QPoint(100, 100))
            created.return_value.exec.assert_called_once()
            spheres.assert_not_called()
    finally:
        window.close()


def test_effect_list_drag_visibility_ctrl_selection_and_space(app):
    from entity_designer.fx_panel import DestructionFxPanel
    from entity_designer import destruction_fx as fx
    from PySide6.QtTest import QTest
    panel = DestructionFxPanel()
    items = [fx.FxBlock(['begin_fx', 'trigger = destroyed', 'mode = visual',
                         'duration = 1000', 'vp_model = 7', 'end']) for _ in range(4)]
    panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument(items))
    panel.resize(430, 650)
    panel.show()
    app.processEvents()
    tree = panel.effects_list
    try:
        points = [tree.checkbox_rect(tree.item(index)).center() for index in range(4)]
        body_points = [tree.visualItemRect(tree.item(index)).center() for index in range(4)]
        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
        QTest.mouseMove(tree.viewport(), points[2])
        QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[2])
        assert panel.hidden_items == {0, 1, 2}
        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
        QTest.mouseMove(tree.viewport(), points[2])
        QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[2])
        assert panel.hidden_items == set()
        tree.clearSelection()
        for index in (0, 2):
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ControlModifier, body_points[index])
        assert {tree.row(item) for item in tree.selectedItems()} == {0, 2}
        QTest.keyClick(tree, Qt.Key.Key_Space)
        assert panel.hidden_items == {0, 2}
        QTest.keyClick(tree, Qt.Key.Key_Space)
        assert panel.hidden_items == set()
        assert all('\n' in tree.item(index).text() for index in range(4))
        assert '#006d96' in tree.styleSheet() and 'color: #ffffff' in tree.styleSheet()
    finally:
        panel.close()


def test_follow_camera_tracks_falling_unit_and_reset_keeps_current_target(app):
    from dataclasses import replace
    from entity_designer import destruction_fx as fx
    from entity_designer.editor import CollisionViewport
    from tests.test_selection_runtime_v3 import _selection_family
    from PySide6.QtCore import QPointF
    family, _, _ = _selection_family()
    viewport = CollisionViewport()
    viewport.load_family(family, primary_owner='root')
    viewport.set_fx_preview(active=True, owner_by_vp={10: 'root'})
    item = fx.PreviewItem(-1, 'unit', 10, (0., -500., 0.), fx.IDENTITY, (1., 1., 1.))
    viewport.set_fx_follow_unit(True, item, 'root')
    high = viewport._center
    zoom = viewport._zoom
    viewport._fx_home_camera = viewport._camera_state()
    falling = replace(item, position=(0., -100., 0.))
    viewport.set_fx_follow_unit(True, falling, 'root')
    assert viewport._center[1] - high[1] == 400.
    assert viewport._zoom == zoom
    current_target = viewport._center
    viewport._pan = QPointF(200., 100.)
    viewport.reset_view()
    assert viewport._center == current_target
    assert viewport._pan == QPointF(0., 0.)
    viewport.set_fx_follow_unit(False, item, 'root')
    assert viewport._center == current_target
    viewport.close()


def test_transport_has_classic_icons_and_follow_is_default(app):
    from entity_designer.fx_panel import DestructionFxPanel
    panel = DestructionFxPanel()
    assert panel.follow_unit.isChecked()
    for button in (panel.restart_button, panel.play_button, panel.stop_button, panel.loop_button):
        assert not button.text()
        assert not button.icon().isNull()
        assert button.toolTip() and button.accessibleName()
    panel.close()


def test_fx_scale_controls_update_current_unit_without_rewinding(app, tmp_path):
    from tests.test_destruction_fx import UNIT
    from entity_designer.editor import VehicleModelReference, find_script_blocks
    script = tmp_path / 'Vehicles.cfg'
    script.write_text(UNIT, encoding='utf-8')
    window = CollisionEditorWindow()
    try:
        block = find_script_blocks(UNIT)[0]
        reference = VehicleModelReference(block=block, vp_normal=10)
        window._active_script_path = script
        window._active_script_id = 5
        window._active_script_kind = 'new_vehicle'
        window._active_model_reference = reference
        window._load_fx_context(UNIT, block, reference)
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        window.fx_panel.seek(100.)
        before = next(item for item in window.fx_panel.preview_items() if item.kind == 'unit')
        window.model_scale_x_spin.setValue(6.)
        after = next(item for item in window.fx_panel.preview_items() if item.kind == 'unit')
        assert [after.rotation[row][0] for row in range(3)] == pytest.approx(
            [before.rotation[row][0] * 6. for row in range(3)])
        assert [after.rotation[row][1:] for row in range(3)] == [before.rotation[row][1:] for row in range(3)]
        assert window.fx_panel.time_ms == 100.
    finally:
        window._modified = False
        window.close()


def test_fx_auto_reload_handles_atomic_script_save_and_texture_update(app, tmp_path):
    from tests.test_destruction_fx import UNIT
    from entity_designer.editor import VehicleModelReference, find_script_blocks
    from PySide6.QtTest import QTest
    from PySide6.QtGui import QImage
    script_dir = tmp_path / 'Data' / 'Scripts'
    textures = tmp_path / 'Data' / 'Textures'
    script_dir.mkdir(parents=True)
    textures.mkdir()
    texture = textures / 'impact.png'
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor('red'))
    assert image.save(str(texture))
    text = UNIT.replace('    dest_fx = death_40_0_-20_0',
        'begin_fx\ntrigger = destroyed\nmode = ground_decal\ntexture = Data/Textures/impact.png\nsize = 100\nduration = 1000\nend\n    dest_fx = death_40_0_-20_0')
    script = script_dir / 'Vehicles.cfg'
    script.write_text(text, encoding='utf-8')
    window = CollisionEditorWindow()
    try:
        block = find_script_blocks(text)[0]
        reference = VehicleModelReference(block=block, vp_normal=10)
        window._active_script_path = script
        window._active_script_id = 5
        window._active_script_kind = 'new_vehicle'
        window._active_model_reference = reference
        window._load_fx_context(text, block, reference)
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        panel = window.fx_panel
        assert str(script.resolve()) in window._fx_watcher.files()
        assert str(texture.resolve()) in window._fx_watcher.files()
        assert not any(hasattr(panel, name) for name in
                       ('reload_button', 'auto_reload', 'reroll_button', 'fit_button'))
        panel.seek(100.)
        panel.effects_list.item(0).setCheckState(Qt.CheckState.Unchecked)
        updated = text.replace('duration   = 800', 'duration   = 1600')
        replacement = script.with_suffix('.tmp')
        replacement.write_text(updated, encoding='utf-8')
        replacement.replace(script)
        QTest.qWait(800)
        assert any('1600 ms' in panel.effects_list.item(i).text()
                   for i in range(panel.effects_list.count()))
        assert panel.time_ms == 100. and 0 in panel.hidden_items
        assert str(script.resolve()) in window._fx_watcher.files()
        window.viewport._fx_decal_cache['Data/Textures/impact.png'] = image.copy()
        image.fill(QColor('blue'))
        assert image.save(str(texture))
        QTest.qWait(800)
        assert 'Data/Textures/impact.png' not in window.viewport._fx_decal_cache
        assert str(texture.resolve()) in window._fx_watcher.files()
        window.properties_tabs.setCurrentIndex(window.ground_tab_index)
        assert not window._fx_watcher.files()
    finally:
        window._modified = False
        window.close()


def test_fx_options_show_distinct_checked_and_unchecked_indicators(app):
    from entity_designer.fx_panel import DestructionFxPanel, FxEffectDelegate
    panel = DestructionFxPanel()
    panel.resize(430, 650)
    panel.show()
    app.processEvents()
    try:
        assert isinstance(panel.effects_list.itemDelegate(), FxEffectDelegate)
        for checkbox in (panel.show_unit, panel.follow_unit):
            checkbox.setChecked(True)
            on = checkbox.grab().toImage()
            checkbox.setChecked(False)
            off = checkbox.grab().toImage()
            cyan = QColor('#39b8d9').rgb()
            assert any(on.pixel(x, y) == cyan for x in range(min(20, on.width())) for y in range(on.height()))
            assert not any(off.pixel(x, y) == cyan for x in range(min(20, off.width())) for y in range(off.height()))
    finally:
        panel.close()


def test_fx_preview_box_aligns_with_ground_and_timeline_is_passive(app):
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QPoint
    from entity_designer import destruction_fx as fx
    window = CollisionEditorWindow()
    try:
        window.resize(1200, 900)
        window.show()
        window.properties_tabs.setCurrentIndex(window.ground_tab_index)
        app.processEvents()
        ground = window.ground_alignment_box.mapTo(window.properties_tabs, QPoint(0, 0))
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        app.processEvents()
        panel = window.fx_panel
        preview = panel.layout().itemAt(0).widget()
        assert preview.mapTo(window.properties_tabs, QPoint(0, 0)) == ground
        document = fx.FxDocument([fx.FxBlock(['begin_fx', 'trigger = destroyed',
            'mode = visual', 'duration = 1000', 'vp_model = 7', 'end']) for _ in range(2)])
        panel.set_context(fx.CONTEXT_VEHICLE, document)
        panel.seek(200.)
        panel.effects_list.setCurrentRow(1)
        before = (panel.time_ms, panel.highlight, set(panel.hidden_items), panel.effects_list.currentRow())
        timeline = panel.timeline
        assert timeline.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        for point in (QPoint(20, 40), QPoint(timeline.LABEL + 50, 40)):
            QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=point)
            QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(timeline, point + QPoint(30, 0))
            QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
        assert (panel.time_ms, panel.highlight, set(panel.hidden_items), panel.effects_list.currentRow()) == before
    finally:
        window.close()


def test_effect_row_click_double_click_and_drag_do_not_change_visibility(app):
    from entity_designer.fx_panel import DestructionFxPanel
    from entity_designer import destruction_fx as fx
    from PySide6.QtTest import QTest
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument([fx.FxBlock([
        'begin_fx', 'trigger = destroyed', 'mode = visual', 'duration = 1000',
        'vp_model = 7', 'end']) for _ in range(3)]))
    panel.resize(430, 650)
    panel.show()
    app.processEvents()
    tree = panel.effects_list
    try:
        tree.item(1).setCheckState(Qt.CheckState.Unchecked)
        points = [tree.visualItemRect(tree.item(i)).center() for i in range(3)]
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[1])
        assert tree.currentRow() == 1 and panel.hidden_items == {1}
        QTest.mouseDClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
        assert panel.hidden_items == {1}
        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[0])
        QTest.mouseMove(tree.viewport(), points[2])
        QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=points[2])
        assert panel.hidden_items == {1}
        rect = tree.checkbox_rect(tree.item(1))
        assert rect.width() >= 20 and rect.height() >= 20
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert panel.hidden_items == set()
    finally:
        panel.close()


def test_four_effect_visibility_buttons_and_menu_share_actions(app):
    from entity_designer.fx_panel import DestructionFxPanel
    from entity_designer import destruction_fx as fx
    from PySide6.QtTest import QTest
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument([fx.FxBlock([
        'begin_fx', 'trigger = destroyed', 'mode = visual', 'duration = 1000',
        'vp_model = 7', 'end']) for _ in range(4)]))
    panel.resize(430, 650)
    panel.show()
    app.processEvents()
    tree = panel.effects_list
    try:
        assert [action.text() for action in panel._visibility_actions] == [
            'Show All', 'Hide All', 'Hide Selected', 'Show Selected']
        assert not panel.show_selected_button.isEnabled()
        assert not panel.hide_selected_button.isEnabled()
        for i in (0, 2):
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ControlModifier, tree.visualItemRect(tree.item(i)).center())
        assert panel.show_selected_button.isEnabled()
        panel.show_selected_button.click()
        assert panel.hidden_items == {1, 3}
        panel.hide_selected_button.click()
        assert panel.hidden_items == {0, 1, 2, 3}
        panel.show_all_button.click()
        assert not panel.hidden_items
        panel.hide_all_button.click()
        assert panel.hidden_items == {0, 1, 2, 3}
        menu = panel._create_effects_context_menu()
        assert menu.actions() == panel._visibility_actions
        menu.actions()[3].trigger()
        assert panel.hidden_items == {1, 3}
        with patch.object(panel, '_create_effects_context_menu') as created:
            panel._show_effects_context_menu(tree.visualItemRect(tree.item(0)).center())
            assert {tree.row(row) for row in tree.selectedItems()} == {0, 2}
            panel._show_effects_context_menu(tree.visualItemRect(tree.item(3)).center())
            assert [tree.row(row) for row in tree.selectedItems()] == [3]
            assert created.return_value.exec.call_count == 2
        panel.show_selected_button.click()
        assert panel.hidden_items == {0, 1, 2}
    finally:
        panel.close()




def test_effect_checkbox_every_rapid_click_toggles_once(app):
    from entity_designer.fx_panel import DestructionFxPanel
    from entity_designer import destruction_fx as fx
    from PySide6.QtTest import QTest
    panel = DestructionFxPanel()
    panel.set_context(fx.CONTEXT_VEHICLE, fx.FxDocument([fx.FxBlock([
        'begin_fx', 'trigger = destroyed', 'mode = visual', 'duration = 1000',
        'vp_model = 7', 'end'])]))
    panel.resize(430, 650)
    panel.show()
    app.processEvents()
    tree = panel.effects_list
    point = tree.checkbox_rect(tree.item(0)).center()
    try:
        with patch.object(panel, '_rebuild_simulation', wraps=panel._rebuild_simulation) as rebuild:
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert panel.hidden_items == {0} and rebuild.call_count == 1
            QTest.mouseDClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
            assert panel.hidden_items == set() and rebuild.call_count == 2
            for modifier in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                             Qt.KeyboardModifier.ShiftModifier):
                QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, modifier, point)
                assert panel.hidden_items == {0}
                QTest.mouseDClick(tree.viewport(), Qt.MouseButton.LeftButton, modifier, point)
                QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, modifier, point)
                assert panel.hidden_items == set()
    finally:
        panel.close()
