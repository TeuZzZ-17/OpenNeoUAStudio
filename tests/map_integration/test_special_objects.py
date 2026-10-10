import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QPoint
from PySide6.QtGui import QValidator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QMenu

from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.special_objects import add_special, remove_special, special_store, update_special
from map_editor.render.camera import IsoCamera
from map_editor.render.gpu_scene import WorldScene
from map_editor.render.map_scene import render_scene, scene_polygons
from map_editor.render.special_scene import actor_code, scene_object_styles, special_members
from map_editor.render.terrain_mesh import TerrainMesh
from map_editor.ui.main_window import MainWindow
from map_editor.ui.special_panel import DurationSpinBox, format_duration


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    window = MainWindow(LdfDocument(mw=9, mh=9))
    window._icons.stop()
    yield window
    window._cancel_operation(clear=False)
    window._released(-1, -1, Qt.KeyboardModifier.NoModifier)
    window.dirty = False
    window._icons.stop()
    window._repeat.stop()
    window._script_timer.stop()
    window._live_timer.stop()
    window.view._render_timer.stop()
    window.view._settle.stop()
    window.view._nav_timer.stop()
    app.processEvents()
    window._icons.stop()
    window._repeat.stop()
    window._script_timer.stop()
    window._live_timer.stop()
    window.view._render_timer.stop()
    window.view._settle.stop()
    window.view._nav_timer.stop()
    for receiver in (window, *window.findChildren(QObject)):
        QCoreApplication.removePostedEvents(receiver)
    window.close()
    window._icon_pool.waitForDone(10000)
    window.view._pool.waitForDone(10000)
    window._briefing_pool.waitForDone(10000)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_special_objects_place_and_round_trip_all_effect_targets():
    doc = LdfDocument(mw=9, mh=9)
    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {
        'x': 2, 'y': 2, 'target': 19, 'keys': [(3, 2)], 'hidden': True,
    })
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {
        'x': 4, 'y': 2, 'countdown': 45000, 'type': 8,
        'inactive_bp': 68, 'active_bp': 69, 'trigger_bp': 70,
        'keys': [(5, 2)],
    })
    gem = add_special(doc, 'gem')
    actions = [
        {'target_type': 'modify_vehicle', 'id': 222, 'param': 'add_energy', 'val': '-12.5'},
        {'target_type': 'modify_building', 'id': 199, 'param': 'enable', 'val': '0'},
        {'target_type': 'modify_weapon', 'id': 7, 'param': 'add_energy_tank', 'val': '0x10'},
    ]
    update_special(doc, 'gem', gem, {
        'x': 6, 'y': 3, 'blg': 65, 'type': 42, 'hidden': True,
        'actions': actions,
    })

    restored = loads_ldf(dumps_ldf(doc))
    assert restored.visible_gate_slots == restored.visible_item_slots == restored.visible_gem_slots == 1
    assert (restored.gates[1]['x'], restored.gates[1]['y']) == (2, 2)
    assert restored.gates[1]['keys'] == [(3, 2)] and restored.gates[1]['hidden']
    assert (restored.items[1]['x'], restored.items[1]['y']) == (4, 2)
    assert restored.items[1]['keys'] == [(5, 2)]
    assert (restored.items[1]['countdown'], restored.items[1]['type']) == (45000, 8)
    assert [restored.items[1][f] for f in ('inactive_bp', 'active_bp', 'trigger_bp')] == [68, 69, 70]
    assert (restored.gems[1]['x'], restored.gems[1]['y']) == (6, 3)
    assert restored.gems[1]['blg'] == 65 and restored.gems[1]['type'] == 42
    assert restored.gems[1]['hidden'] and restored.gems[1]['actions'] == actions
    assert restored.grids['blg'][3][6] == '41'
    assert restored.grids['type'][2][5] == 'f3'


def test_gate_and_item_visuals_restore_underlay_and_respect_authored_buildings():
    doc = LdfDocument(mw=9, mh=9)
    for x, y in ((2, 2), (3, 3)):
        doc.grids['type'][y][x] = 'ab'

    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {'x': 2, 'y': 2})
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('03', '19')
    update_special(doc, 'gate', gate, {'x': 3, 'y': 3})
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
    assert (doc.grids['type'][3][3], doc.grids['blg'][3][3]) == ('03', '19')
    remove_special(doc, 'gate', gate)
    assert (doc.grids['type'][3][3], doc.grids['blg'][3][3]) == ('ab', '00')

    doc.grids['type'][4][4] = '12'
    doc.grids['blg'][4][4] = '37'
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {'x': 4, 'y': 4})
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('12', '37')
    remove_special(doc, 'item', item)
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('12', '37')


def test_special_validation_is_atomic_and_keys_match_legacy_occupancy():
    doc = LdfDocument(mw=9, mh=9)
    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {'x': 2, 'y': 2})
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {'x': 3, 'y': 3})

    # A key may share a cell with another main object, but not its own parent.
    update_special(doc, 'gate', gate, {'keys': [(3, 3)]})
    before = doc.snapshot()
    with pytest.raises(ValueError):
        update_special(doc, 'item', item, {'keys': [(3, 3)]})
    assert doc.snapshot() == before

    update_special(doc, 'item', item, {'keys': [(4, 4)]})
    for changes in (
        {'x': 0, 'y': 5},                    # map border
        {'x': 3, 'y': 3},                    # another main object
        {'keys': [(0, 4)]},                 # key on map border
        {'keys': [(4, 4), (4, 4)]},          # duplicate within this object
        {'keys': [(3, 3), (4, 4)]},          # duplicate key across objects
        {'keys': [(2, 2)]},                 # own parent cell
    ):
        before = doc.snapshot()
        with pytest.raises(ValueError):
            update_special(doc, 'gate', gate, changes)
        assert doc.snapshot() == before

    gem = add_special(doc, 'gem')
    before = doc.snapshot()
    with pytest.raises(ValueError):
        update_special(doc, 'gem', gem, {'x': 6, 'y': 6})
    assert doc.snapshot() == before


def test_special_slot_limit_and_delete_compacts_slots():
    doc = LdfDocument(mw=9, mh=9)
    for index in range(1, 11):
        slot = add_special(doc, 'gate')
        update_special(doc, 'gate', slot, {'target': index})
    before = doc.snapshot()
    with pytest.raises(ValueError):
        add_special(doc, 'gate')
    assert doc.snapshot() == before

    remove_special(doc, 'gate', 4)
    assert doc.visible_gate_slots == 9
    assert [special_store(doc, 'gate')[i]['target'] for i in range(1, 10)] == [1, 2, 3, 5, 6, 7, 8, 9, 10]
    assert add_special(doc, 'gate') == 10
    assert doc.visible_gate_slots == 10


def test_special_panel_escape_undo_key_edit_filter_and_delete_all(win, monkeypatch):
    gate_panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    baseline = win.doc.snapshot()
    undo_depth, dirty = len(win.history._undo), win.dirty
    gate_panel.add_button.click()
    assert gate_panel.slot == 1
    assert win.palette_tabs.currentIndex() == win.special_tab_indices['gate']
    assert win._special_placement == ('gate', 1, 'object', -1)
    assert win.doc.visible_gate_slots == 0
    assert win.view.doc.visible_gate_slots == 1  # virtual preview row
    QTest.keyClick(win.view, Qt.Key.Key_Escape)
    assert win._special_draft is None and win._special_placement is None
    assert win.doc.snapshot() == baseline
    assert win.dirty == dirty and len(win.history._undo) == undo_depth
    assert win.view.doc.visible_gate_slots == 0

    gate_panel.add_button.click()
    win._special_click((2, 2))
    assert win.doc.visible_gate_slots == 1
    assert (win.doc.gates[1]['x'], win.doc.gates[1]['y']) == (2, 2)
    win.undo()
    assert win.doc.visible_gate_slots == 0
    win.redo()
    assert (win.doc.gates[1]['x'], win.doc.gates[1]['y']) == (2, 2)

    item_panel = win.special_panels['item']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['item'])
    item_panel.add_button.click()
    item_slot = item_panel.slot
    win._special_click((4, 4))
    next(button for button in item_panel.findChildren(QPushButton) if button.text() == 'Add on map').click()
    win._special_click((5, 4))
    assert win.doc.items[item_slot]['keys'] == [(5, 4)]
    item_panel.keys.setCurrentRow(0)
    item_panel.key_x.setValue(6)
    item_panel.key_y.setValue(4)
    item_panel.key_x.editingFinished.emit()
    assert win.doc.items[item_slot]['keys'] == [(6, 4)]
    assert win.doc.grids['type'][4][6] == 'f3'
    item_panel.key_road.setChecked(True)
    assert win.doc.grids['type'][4][6] == 'f4'

    item_panel.add_button.click()
    second_item = item_panel.slot
    win._special_click((4, 6))
    item_panel.search.setText('Super item 2')
    assert item_panel.list.item(0).isHidden()
    assert not item_panel.list.item(1).isHidden()

    monkeypatch.setattr('map_editor.ui.main_window.QMessageBox.question',
                        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    item_panel.clear_button.click()
    assert win.doc.visible_item_slots == 0
    win.undo()
    assert win.doc.visible_item_slots == 2
    assert win.doc.items[1]['keys'] == [(6, 4)]
    assert second_item == 2


def test_special_drag_is_one_history_step_and_copy_keeps_source_keys(win):
    panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    panel.add_button.click()
    win._special_click((2, 2))
    slot = panel.slot
    win._special_changed('gate', slot, {'keys': [(3, 2)]})
    original = deepcopy(win.doc.gates[slot])
    members = list(special_members(win.doc, win.view.lib))
    member_index = next(i for i, member in enumerate(members)
                        if member.kind == 'gate' and member.slot == slot and member.key == -1)
    style_index = len(win.doc.squads) + len(win.doc.host_stations) + member_index
    style_before = scene_object_styles(win.view)[style_index][3]

    win._begin_actor_drag('special', (2, 2))
    assert win._special_drag is not None
    assert scene_object_styles(win.view)[style_index][3] == 3
    win._move_actor_drag((4, 4))
    assert scene_object_styles(win.view)[style_index][3] == 3
    win._finish_actor_drag()
    assert scene_object_styles(win.view)[style_index][3] == style_before
    assert (win.doc.gates[slot]['x'], win.doc.gates[slot]['y']) == (4, 4)
    assert win.doc.gates[slot]['keys'] == [(3, 2)]
    win.undo()
    assert (win.doc.gates[slot]['x'], win.doc.gates[slot]['y']) == (2, 2)
    win.redo()
    assert (win.doc.gates[slot]['x'], win.doc.gates[slot]['y']) == (4, 4)

    win._begin_actor_drag('special', (4, 4))
    win._move_actor_drag((5, 5))
    win._cancel_operation(clear=False)
    assert (win.doc.gates[slot]['x'], win.doc.gates[slot]['y']) == (4, 4)
    win.undo()
    assert (win.doc.gates[slot]['x'], win.doc.gates[slot]['y']) == (2, 2)
    win.redo()

    win._copy_elements()
    assert win._special_draft is not None
    assert win.doc.gates[slot] == original | {'x': 4, 'y': 4}
    win._special_click((6, 6))
    assert win.doc.visible_gate_slots == 2
    assert (win.doc.gates[2]['x'], win.doc.gates[2]['y']) == (6, 6)
    assert win.doc.gates[2]['keys'] == [(5, 4)]
    assert win.doc.gates[slot] == original | {'x': 4, 'y': 4}



def test_special_template_survives_deselect_cancelling_draft(win):
    panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    panel.fields['target'].setValue(23)
    panel.fields['target'].editingFinished.emit()
    before = win.doc.snapshot()
    panel.add_button.click()
    assert win._special_draft[1]['target'] == 23
    panel.deselect_button.click()
    assert win._special_draft is None
    assert win.doc.snapshot() == before
    assert win.doc.visible_gate_slots == 0
    assert panel.slot == 0 and panel.template['target'] == 23
    assert panel.fields['target'].value() == 23


def test_special_context_menus_cancel_unplaced_previews_and_gate_key_actions(win):
    expected = {'Add preview', 'Move selected object', 'Focus',
                'Delete selected object', 'Deselect'}
    key_actions = {'Add key preview', 'Move selected key', 'Delete selected key'}
    for kind, panel in win.special_panels.items():
        assert panel.list.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
        menu = QMenu(win)
        win._special_context_actions(menu, kind)
        labels = {action.text() for action in menu.actions()}
        assert labels == expected | (key_actions if kind != 'gem' else set())

    panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    baseline = win.doc.snapshot()
    undo_depth = len(win.history._undo)
    redo_depth = len(win.history._redo)
    dirty = win.dirty

    def menu_actions():
        menu = QMenu(win)
        win._special_context_actions(menu, 'gate')
        return {action.text(): action for action in menu.actions()}

    menu_actions()['Add preview'].trigger()
    assert win._special_draft is not None and win._special_draft[0] == 'gate'
    assert not panel.keys_group.isEnabled()
    assert win.doc.snapshot() == baseline
    assert (len(win.history._undo), len(win.history._redo), win.dirty) == (
        undo_depth, redo_depth, dirty)
    draft_actions = menu_actions()
    assert all(not draft_actions[label].isEnabled() for label in key_actions)

    # Deselect cancels a draft without creating an LDF record or history entry.
    draft_actions['Deselect'].trigger()
    assert win._special_draft is None
    assert win.doc.snapshot() == baseline
    assert (len(win.history._undo), len(win.history._redo), win.dirty) == (
        undo_depth, redo_depth, dirty)

    # The delete command also cancels a preview instead of deleting data.
    menu_actions()['Add preview'].trigger()
    draft_actions = menu_actions()
    assert draft_actions['Delete selected object'].isEnabled()
    draft_actions['Delete selected object'].trigger()
    assert win._special_draft is None
    assert win.doc.snapshot() == baseline
    assert (len(win.history._undo), len(win.history._redo), win.dirty) == (
        undo_depth, redo_depth, dirty)

    # Once the object is placed, adding a key is available; with a key selected,
    # all three key actions are enabled outside any preview draft.
    menu_actions()['Add preview'].trigger()
    win._special_click((2, 2))
    assert win._special_draft is None and win.doc.visible_gate_slots == 1
    assert panel.keys_group.isEnabled()
    menu_actions()['Add key preview'].trigger()
    assert win._special_placement == ('gate', 1, 'key', -1)
    win._special_click((3, 2))
    panel.keys.setCurrentRow(0)
    placed_actions = menu_actions()
    assert all(placed_actions[label].isEnabled() for label in key_actions)


def test_delete_shortcut_reuses_gate_key_and_object_callbacks(win, app):
    slot = add_special(win.doc, 'gate')
    update_special(win.doc, 'gate', slot, {'x': 2, 'y': 2, 'keys': [(3, 2)]})
    panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    panel.refresh(win.doc, slot)
    win.show()
    app.processEvents()

    panel.keys.setCurrentRow(0)
    panel.keys.setFocus()
    QTest.keyClick(panel.keys, Qt.Key.Key_Delete)
    assert win.doc.visible_gate_slots == 1
    assert win.doc.gates[slot]['keys'] == []

    panel.list.setFocus()
    QTest.keyClick(panel.list, Qt.Key.Key_Delete)
    assert win.doc.visible_gate_slots == 0
    win.undo()
    assert win.doc.visible_gate_slots == 1 and win.doc.gates[slot]['keys'] == []

    before = win.doc.snapshot()
    win._add_special('gate')
    assert win._special_draft is not None
    panel.list.setFocus()
    QTest.keyClick(panel.list, Qt.Key.Key_Delete)
    assert win._special_draft is None
    assert win.doc.snapshot() == before


def test_copy_special_preview_shifts_keys_and_cancel_keeps_source(win):
    panel = win.special_panels['gate']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    panel.add_button.click()
    slot = panel.slot
    win._special_click((2, 2))
    win._special_changed('gate', slot, {'keys': [(3, 2)]})
    source = deepcopy(win.doc.gates[slot])
    baseline = win.doc.snapshot()
    undo_depth, dirty = len(win.history._undo), win.dirty

    win._copy_elements()
    assert win._special_draft is not None
    assert win._special_draft[1]['keys'] == [(5, 4)]
    assert win._special_draft[1]['x'] == 4 and win._special_draft[1]['y'] == 4
    assert win.doc.snapshot() == baseline
    assert win.doc.gates[slot] == source
    QTest.keyClick(win.view, Qt.Key.Key_Escape)
    assert win._special_draft is None
    assert win.doc.snapshot() == baseline
    assert win.doc.gates[slot] == source
    assert win.dirty == dirty and len(win.history._undo) == undo_depth

def test_gem_action_editor_keeps_template_and_draft_effects_until_placement(win):
    panel = win.special_panels['gem']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gem'])
    panel.target.setCurrentIndex(panel.target.findData('modify_vehicle'))
    panel.target_id.setValue(222)
    panel.param.setCurrentText('enable')
    panel.action_value.setText('arbitrary-token')
    panel._save_action(False)
    assert panel.template['actions'][0]['val'] == 'arbitrary-token'

    baseline = win.doc.snapshot()
    dirty = win.dirty
    panel.add_button.click()
    slot = panel.slot
    assert win.doc.snapshot() == baseline
    assert win._special_draft[1]['actions'][0]['val'] == 'arbitrary-token'
    for target, obj_id, param, value in (
        ('modify_building', 199, 'enable', '0'),
        ('modify_weapon', 7, 'add_energy_tank', '2.5'),
    ):
        panel.target.setCurrentIndex(panel.target.findData(target))
        panel.target_id.setValue(obj_id)
        panel.param.setCurrentText(param)
        panel.action_value.setText(value)
        panel._save_action(False)

    actions = deepcopy(win._special_draft[1]['actions'])
    assert [action['target_type'] for action in actions] == [
        'modify_vehicle', 'modify_building', 'modify_weapon']
    assert actions[0]['val'] == 'arbitrary-token'
    assert win.doc.snapshot() == baseline and win.dirty == dirty
    panel.actions.setCurrentRow(0)
    assert panel.action_value.text() == 'arbitrary-token'
    panel.action_value.setText('')
    panel._save_action(True)
    assert win._special_draft[1]['actions'] == actions

    win._special_click((4, 4))
    assert win.doc.gems[slot]['actions'] == actions




def test_gem_visual_uses_building_sector_type_and_restores_underlay():
    from types import SimpleNamespace

    from map_editor.core.special_objects import update_special

    doc = LdfDocument(mw=9, mh=9)
    doc.grids['type'][2][2], doc.grids['blg'][2][2] = 'ab', '00'
    doc.grids['type'][4][4], doc.grids['blg'][4][4] = 'cd', '00'
    buildings = {
        50: SimpleNamespace(sec_type=123),
        51: SimpleNamespace(sec_type=124),
    }
    gem = add_special(doc, 'gem')
    actions = [{'target_type': 'modify_vehicle', 'id': 1, 'param': 'enable', 'val': '1'}]
    update_special(doc, 'gem', gem, {'actions': actions, 'x': 2, 'y': 2}, buildings=buildings)
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('7b', '32')

    update_special(doc, 'gem', gem, {'x': 4, 'y': 4}, buildings=buildings)
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('7b', '32')

    update_special(doc, 'gem', gem, {'blg': 51}, buildings=buildings)
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('7c', '33')
    restored = loads_ldf(dumps_ldf(doc))
    assert (restored.grids['type'][4][4], restored.grids['blg'][4][4]) == ('7c', '33')

    remove_special(doc, 'gem', gem, buildings=buildings)
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('cd', '00')

    fallback = LdfDocument(mw=9, mh=9)
    fallback.grids['type'][3][3], fallback.grids['blg'][3][3] = 'ef', '00'
    fallback_gem = add_special(fallback, 'gem')
    update_special(fallback, 'gem', fallback_gem, {'actions': actions, 'x': 3, 'y': 3})
    assert (fallback.grids['type'][3][3], fallback.grids['blg'][3][3]) == ('ef', '32')
    remove_special(fallback, 'gem', fallback_gem)
    assert (fallback.grids['type'][3][3], fallback.grids['blg'][3][3]) == ('ef', '00')


def _loaded_item_with_key_visual(visual='f4'):
    source = LdfDocument(mw=9, mh=9)
    source.visible_item_slots = 1
    source.items[1].update(x=2, y=2, keys=[(3, 3)])
    source.grids['type'][3][3] = visual
    return loads_ldf(dumps_ldf(source))


def test_loaded_item_key_road_toggle_delete_and_move_preserve_marker_metadata(win):
    from map_editor.core.special_objects import update_special

    loaded = _loaded_item_with_key_visual()
    win._new_doc(loaded, None)
    panel = win.special_panels['item']
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['item'])
    panel.list.setCurrentRow(0)
    panel.keys.setCurrentRow(0)
    assert panel.key_road.isChecked()
    panel.key_road.setChecked(False)
    assert loaded.grids['type'][3][3] == 'f3'
    assert loaded.items[1]['_key_visuals'][(3, 3)] == {'before': 'f4', 'applied': 'f3'}
    panel.delete_button.click()
    assert loaded.visible_item_slots == 0
    assert loaded.grids['type'][3][3] == 'f4'

    moved = _loaded_item_with_key_visual()
    moved.grids['type'][5][5] = 'ab'
    update_special(moved, 'item', 1, {'keys': [(5, 5)]})
    assert moved.grids['type'][3][3] == '00'
    assert moved.grids['type'][5][5] == 'f4'
    assert moved.items[1]['_key_visuals'][(5, 5)] == {'before': 'ab', 'applied': 'f4'}

    authored = LdfDocument(mw=9, mh=9)
    slot = add_special(authored, 'item')
    update_special(authored, 'item', slot, {'x': 2, 'y': 2, 'keys': [(3, 3)]})
    authored.grids['type'][3][3] = 'ef'  # a manual map edit supersedes the key marker
    remove_special(authored, 'item', slot)
    assert authored.grids['type'][3][3] == 'ef'


def _loaded_legacy_gem(type_id='05'):
    source = LdfDocument(mw=9, mh=9)
    source.visible_gem_slots = 1
    source.gems[1].update(x=2, y=2, blg=50, actions=[
        {'target_type': 'modify_vehicle', 'id': 1, 'param': 'enable', 'val': '1'}])
    source.grids['type'][2][2], source.grids['blg'][2][2] = type_id, '32'
    return loads_ldf(dumps_ldf(source))


def test_loaded_and_saved_gem_visual_restore_preserves_type_map():
    from types import SimpleNamespace

    from map_editor.core.special_objects import remove_special, update_special

    buildings = {50: SimpleNamespace(sec_type=123)}
    loaded = _loaded_legacy_gem('05')
    remove_special(loaded, 'gem', 1, buildings=buildings)
    assert (loaded.grids['type'][2][2], loaded.grids['blg'][2][2]) == ('05', '00')

    moved = _loaded_legacy_gem('05')
    moved.grids['type'][4][4], moved.grids['blg'][4][4] = '06', '00'
    update_special(moved, 'gem', 1, {'x': 4, 'y': 4}, buildings=buildings)
    assert (moved.grids['type'][2][2], moved.grids['blg'][2][2]) == ('05', '00')
    assert (moved.grids['type'][4][4], moved.grids['blg'][4][4]) == ('7b', '32')

    authored = LdfDocument(mw=9, mh=9)
    authored.grids['type'][2][2], authored.grids['blg'][2][2] = 'ab', '00'
    slot = add_special(authored, 'gem')
    actions = [{'target_type': 'modify_vehicle', 'id': 1, 'param': 'enable', 'val': '1'}]
    update_special(authored, 'gem', slot, {'actions': actions, 'x': 2, 'y': 2}, buildings=buildings)
    reloaded = loads_ldf(dumps_ldf(authored))
    assert (reloaded.grids['type'][2][2], reloaded.grids['blg'][2][2]) == ('7b', '32')
    remove_special(reloaded, 'gem', 1, buildings=buildings)
    assert (reloaded.grids['type'][2][2], reloaded.grids['blg'][2][2]) == ('7b', '00')


def test_special_preview_defaults_and_item_countdown_mm_ss(win):
    from PySide6.QtWidgets import QLabel

    for kind in ('gate', 'item', 'gem'):
        panel = win.special_panels[kind]
        assert panel.list.preview_level == 2
        assert any(label.text() == '3/3' for label in panel.findChildren(QLabel))
    assert win.host_panel.list.preview_level == 1
    assert any(label.text() == '2/3' for label in win.host_panel.findChildren(QLabel))

    panel = win.special_panels['item']
    slot = add_special(win.doc, 'item')
    update_special(win.doc, 'item', slot, {'countdown': 45001})
    panel.refresh(win.doc, slot)
    countdown = panel.fields['countdown']
    assert isinstance(countdown, DurationSpinBox)
    assert countdown.singleStep() == 1000
    assert countdown.text() == '00:45'
    assert countdown.value() == 45001
    assert format_duration(45001) == '00:45'
    assert '00:45' in panel.list.item(0).toolTip()
    assert countdown.validate('12:', 3)[0] == QValidator.State.Intermediate
    assert countdown.validate('12:3', 4)[0] == QValidator.State.Acceptable
    assert countdown.validate('12:60', 5)[0] == QValidator.State.Invalid
    assert countdown.validate('35792:2', 7)[0] == QValidator.State.Invalid

    # Interpreting an untouched rounded label must retain its source milliseconds.
    countdown.interpretText()
    countdown.editingFinished.emit()
    assert countdown.value() == 45001
    assert win.doc.items[slot]['countdown'] == 45001

    countdown.lineEdit().selectAll()
    QTest.keyClicks(countdown.lineEdit(), '12:34')
    countdown.interpretText()
    countdown.editingFinished.emit()
    assert countdown.value() == 754000
    assert win.doc.items[slot]['countdown'] == 754000
    countdown.lineEdit().selectAll()
    QTest.keyClicks(countdown.lineEdit(), '13:')
    countdown.interpretText()
    assert countdown.value() == 754000
    update_special(win.doc, 'item', slot, {'x': 2, 'y': 2})
    assert loads_ldf(dumps_ldf(win.doc)).items[slot]['countdown'] == 754000


def test_item_preview_hover_preserves_uncommitted_countdown(win):
    panel = win.special_panels['item']
    panel.template['countdown'] = 45001
    before = win.doc.snapshot()
    history = (len(win.history._undo), len(win.history._redo))
    dirty = win.dirty

    panel.add_button.click()
    assert win._special_draft is not None and win._special_draft[0] == 'item'
    slot = panel.slot
    countdown = panel.fields['countdown']
    line_edit = countdown.lineEdit()
    card = panel.list.currentItem()
    icon_key = card.icon().cacheKey()
    line_edit.selectAll()
    QTest.keyClicks(line_edit, '12:34')
    pending_text, pending_value = line_edit.text(), countdown.value()
    assert pending_text == '12:34' and pending_value == 754000
    assert win._special_draft[1]['countdown'] == 754000

    win._move_special_draft((2, 2))
    assert line_edit.text() == pending_text
    assert countdown.value() == pending_value
    assert panel.list.currentItem() is card
    assert panel.list.currentItem().icon().cacheKey() == icon_key

    # Commit the typed duration into the virtual draft only. The real LDF and
    # undo stack stay untouched until the user places the preview.
    countdown.interpretText()
    countdown.editingFinished.emit()
    assert countdown.value() == 754000
    assert win._special_draft[1]['countdown'] == 754000
    assert win.doc.snapshot() == before
    assert (len(win.history._undo), len(win.history._redo)) == history
    assert win.dirty == dirty

    win._special_click((3, 2))
    assert win._special_draft is None
    assert win.doc.items[slot]['countdown'] == 754000
    assert len(win.history._undo) == history[0] + 1



def test_real_special_descriptors_actor_ids_and_rendered_pixel_selection(win, app):
    from map_editor.core.special_objects import update_special

    doc, lib = win.doc, win._lib()
    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {'x': 2, 'y': 2}, win.buildings)
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {'x': 3, 'y': 3}, win.buildings)
    gem = add_special(doc, 'gem')
    actions = [{'target_type': 'modify_vehicle', 'id': 1, 'param': 'enable', 'val': '1'}]
    # Keep this as an LDF-style legacy gem: the map carries its sector/building visual.
    update_special(doc, 'gem', gem, {'x': 4, 'y': 4, 'actions': actions}, win.buildings)
    doc = loads_ldf(dumps_ldf(doc))  # strips editor-only underlay metadata
    win.doc = doc
    win._refresh_squads(fields=False)
    win._refresh_specials()

    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    members = list(special_members(doc, lib))
    assert [(member.kind, member.slot, member.key) for member in members] == [
        ('gate', 1, -1), ('item', 1, -1), ('gem', 1, -1)]
    assert all(member.typ >= 0 and member.building > 0 for member in members)
    codes = {member.kind: actor_code(doc, i) for i, member in enumerate(members)}

    gpu = WorldScene()
    gpu.set_library(lib)
    gpu.update(doc, terrain)
    gpu_ids = np.concatenate([
        instances[:, 3].astype(np.int32)
        for chunk in gpu.chunks.values()
        for _template, instances in chunk.instances.values()
        if len(instances)
    ])
    assert all(code in gpu_ids for code in codes.values())

    win.resize(1100, 800)
    win.show()
    app.processEvents()
    view = win.view
    view._render_timer.stop()
    original_view_state = (getattr(view, '_software_fallback', None), view.camera, view.doc,
                           view.terrain, view._frame, view._frame_key)
    width, height = view.width(), view.height()
    assert width > 0 and height > 0
    camera = IsoCamera(yaw=0, pitch=90, zoom=.12, width=width, height=height,
                       center=(5400, 0, -5400))
    polygons = scene_polygons(lib, doc, terrain, camera)
    cell_codes = {member.kind: member.cell[1] * doc.mw + member.cell[0] + 1
                  for member in members}
    # CPU terrain sectors retain their positive cell IDs; pick_scene_object maps
    # those rendered pixels back to the special descriptor when no model face exists.
    assert all(any(polygon.payload[1] in (codes[kind], cell_code) for polygon in polygons)
               for kind, cell_code in cell_codes.items())
    frame = render_scene(polygons, camera, lib.tables)
    visible = {kind: bool(np.any(frame.cell_ids == cell_codes[kind])
                           or np.any(frame.cell_ids == codes[kind])) for kind in codes}
    assert all(visible.values()), visible

    view.doc, view.lib, view.terrain, view.camera = doc, lib, terrain, camera
    view._frame = frame
    view._frame_key = view._render_key()
    for kind, code in codes.items():
        pick_code = code if np.any(frame.cell_ids == code) else cell_codes[kind]
        y, x = np.argwhere(frame.cell_ids == pick_code)[0]
        picked = view.pick_scene_object(int(x), int(y))
        assert picked == ('special', (kind, 1, -1))
        # Click the rendered pixel from a different tab and verify it selects
        # the special record in its owning panel.
        win.palette_tabs.setCurrentIndex(win.host_tab_index)
        view._frame_key = view._render_key()
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(int(x), int(y)))
        assert win.palette_tabs.currentIndex() == win.special_tab_indices[kind]
        assert win.special_panels[kind].slot == 1
        assert view.selected_special == (kind, 1, -1)
        view._frame_key = view._render_key()

    # Pixel hits for ordinary actors must remain selectable from a special
    # panel too. Exercise their real CPU-rendered silhouettes and the same
    # white selection contour that the viewport paints over that frame.
    from PySide6.QtGui import QImage, QPainter
    from map_editor.core.ldf_model import ensure_host_defaults

    doc.squads = [dict(owner=1, veh=1, num=1, x=6, y=6,
                       hidden=False, useable=False, custom_name=None)]
    host = dict(owner=1, veh=56, x=2, y=6, energy=500000, pos_y=-700,
                hidden=False, custom_name=None)
    ensure_host_defaults(host)
    doc.host_stations = [host]
    win.doc = doc
    win._refresh_squads()
    win._refresh_hosts()
    win._icons.stop()
    win._icon_pool.waitForDone(10000)
    view.selected_host = -1
    view.selected_special = None
    win.squad_panel.set_selection(set())
    win.host_panel.deselect()
    from map_editor.core.ldf_model import SECTOR_SIZE
    zoom = .8 * min(width / (doc.mw * SECTOR_SIZE), height / (doc.mh * SECTOR_SIZE))
    camera = IsoCamera(yaw=0, pitch=90, zoom=zoom, width=width, height=height,
                       center=(doc.mw * SECTOR_SIZE / 2, 0,
                               -doc.mh * SECTOR_SIZE / 2))
    frame = render_scene(scene_polygons(lib, doc, terrain, camera), camera, lib.tables)
    view.doc, view.terrain, view.camera = doc, terrain, camera
    view._frame = frame
    view._frame_key = view._render_key()
    actor_codes = {'squad': -(doc.mw * doc.mh + 1),
                   'host': -(doc.mw * doc.mh + len(doc.squads) + 1)}
    actor_tabs = {'squad': win.squad_tab_index, 'host': win.host_tab_index}
    view._software_fallback = True
    for kind, code in actor_codes.items():
        assert np.any(frame.cell_ids == code), f'{kind} silhouette is missing from the CPU frame'
        y, x = np.argwhere(frame.cell_ids == code)[0]
        win.palette_tabs.setCurrentIndex(win.special_tab_indices['gem'])
        view._frame_key = view._render_key()
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(int(x), int(y)))
        view._render_timer.stop()
        assert win.palette_tabs.currentIndex() == actor_tabs[kind]
        assert scene_object_styles(view)[0 if kind == 'squad' else 1][3] == 2

        # Paint the production CPU contour overlay over the real frame and
        # check that its white outline surrounds the selected actor pixels.
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        win.squad_overlay.draw(painter, camera, terrain)
        painter.end()
        del painter
        mask = frame.cell_ids == code
        expanded = np.pad(mask, 1)
        expanded = (mask | expanded[:-2, 1:-1] | expanded[2:, 1:-1]
                    | expanded[1:-1, :-2] | expanded[1:-1, 2:])
        expanded2 = np.pad(expanded, 1)
        contour = (expanded | expanded2[:-2, 1:-1] | expanded2[2:, 1:-1]
                   | expanded2[1:-1, :-2] | expanded2[1:-1, 2:]) & ~mask
        ys, xs = np.where(contour)
        assert any((lambda color: color.red() >= 250 and color.green() >= 250
                    and color.blue() >= 250)(image.pixelColor(int(px), int(py)))
                   for py, px in zip(ys[:1000], xs[:1000])), (
                       f'{kind} selection has no white CPU contour')
    software_fallback, view.camera, view.doc, view.terrain, view._frame, view._frame_key = original_view_state
    if software_fallback is None:
        del view._software_fallback
    else:
        view._software_fallback = software_fallback
    view._render_timer.stop()
    view._settle.stop()
    view._nav_timer.stop()


def test_clear_view_filters_cpu_actor_geometry_without_document_edits(win, monkeypatch):
    from PySide6.QtCore import QRunnable, Signal
    from map_editor.core.ldf_model import dumps_ldf
    from map_editor.render import map_viewport

    view, doc = win.view, win.doc
    lib = win._lib()
    assert lib is not None
    view._render_timer.stop()
    view._settle.stop()
    view._nav_timer.stop()
    view._pool.waitForDone(10000)
    view._job = None
    view._software_fallback = True
    view.doc, view.lib = doc, lib
    view.resize(640, 480)

    doc.squads = [dict(owner=1, veh=1, num=1, x=4, y=4,
                       hidden=False, useable=False, custom_name=None)]
    baseline = dumps_ldf(doc)
    terrain = view.terrain
    polygons = scene_polygons(lib, doc, terrain, view.camera.copy())
    actor_code = -(doc.mw * doc.mh + 1)
    assert any(int(polygon.payload[1]) == actor_code for polygon in polygons)

    before_key = view._render_key()
    history = (len(win.history._undo), len(win.history._redo), win.dirty)
    assert not win.clear_view_action.isChecked()
    win.clear_view_action.trigger()
    assert view.clear_view and win.clear_view_action.isChecked()
    assert dumps_ldf(doc) == baseline
    assert (len(win.history._undo), len(win.history._redo), win.dirty) == history
    current_key = view._render_key()
    assert current_key[1] == before_key[1] and current_key[-1] == before_key[-1]
    assert current_key[2] is True

    captured = []

    class _Signals(QObject):
        finished = Signal(object)

    class _CaptureJob(QRunnable):
        def __init__(self, polygons, camera, tables, key, generation, terrain,
                     preview_cells=(), preview_codes=()):
            super().__init__()
            self.polygons, self.camera, self.tables, self.key = polygons, camera, tables, key
            self.signals = _Signals()
            captured.append(self)

        def run(self):
            pass

    monkeypatch.setattr(map_viewport, '_RenderJob', _CaptureJob)
    view._launch_render()
    assert captured
    assert all(int(polygon.payload[1]) > 0 for polygon in captured[-1].polygons)
    frame = render_scene(captured[-1].polygons, captured[-1].camera,
                         captured[-1].tables, fast=captured[-1].key[-1])
    assert np.any(frame.cell_ids > 0)
    assert not np.any(frame.cell_ids < 0)


def test_dragging_sector_over_ordinary_building_replaces_it_and_supports_undo(win):
    win.doc.grids['type'][2][2] = 'a1'
    win.doc.grids['blg'][4][4] = '02'
    win.doc.grids['type'][4][4] = 'b2'
    before = win.doc.snapshot()
    win.set_tool('select')
    win._begin_actor_drag('building', (2, 2))
    win._move_actor_drag((4, 4))
    assert win._building_drop_valid
    win.view._moved = True
    win._finish_building_drag()
    assert (win.doc.grids['type'][4][4], win.doc.grids['blg'][4][4]) == ('a1', '00')
    assert (win.doc.grids['type'][2][2], win.doc.grids['blg'][2][2]) == ('00', '00')
    win.undo()
    assert win.doc.snapshot() == before
