"""Sector / building placement: list drag & drop, and the click -> grey preview -> click flow."""
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from .test_special_objects import app, win  # noqa: F401  (fixtures)

LEFT = Qt.MouseButton.LeftButton
NO_MOD = Qt.KeyboardModifier.NoModifier


def _select(win, tool):
    """Pick the first usable entry of the sector or building list, as a click on the list does."""
    widget = win.sector_list if tool == 'sector' else win.building_list
    items = win._icon_items if tool == 'sector' else win._bicon_items
    key = 6 if tool == 'sector' else 54
    widget.setCurrentItem(items[key])
    (win._sector_selected if tool == 'sector' else win._building_selected)(items[key])
    return key


def _click(win, cell, monkeypatch, *, button=LEFT, move_to=None):
    """Press/release on the map as if the pointer were over ``cell`` (``move_to`` = cell after dragging)."""
    where = [cell]
    monkeypatch.setattr(win.view, 'pick_cell', lambda x, y: where[0])
    QTest.mousePress(win.view, button, NO_MOD, QPoint(40, 40))
    if move_to is not None:
        where[0] = move_to
        QTest.mouseMove(win.view, QPoint(120, 80))
    QTest.mouseRelease(win.view, button, NO_MOD, QPoint(40 if move_to is None else 120, 40 if move_to is None else 80))


def test_nothing_is_selected_at_start(win):
    assert win.sel_typ is None and win.sel_building is None
    assert win.sector_list.currentItem() is None and not win.sector_list.selectedItems()
    assert win.building_list.currentItem() is None and not win.building_list.selectedItems()


def test_click_without_selection_places_nothing(win, monkeypatch):
    win.set_tool('sector')
    before = win.doc.snapshot()
    _click(win, (4, 4), monkeypatch)
    assert win._stamp is None and win._ghost is None
    assert win.doc.snapshot() == before


@pytest.mark.parametrize('tool', ['sector', 'building'])
def test_two_click_placement_with_grey_preview(win, monkeypatch, tool):
    value = _select(win, tool)
    layer = 'type' if tool == 'sector' else 'blg'
    expected = f'{value:02x}' if tool == 'sector' else f'{win.buildings[value].id:02x}'
    before = win.doc.snapshot()
    _click(win, (4, 4), monkeypatch)
    # First click: grey preview on the cell, nothing written yet.
    assert win._stamp == tool and win._ghost == (tool, value, (4, 4))
    assert win.doc.snapshot() == before
    assert win.view.doc is not win.doc and win.view.preview_cells == {(4, 4)}
    assert win.view.doc.grids[layer][4][4] == expected
    assert win.view.placing
    # The preview follows the pointer.
    win._hovered(6, 5)
    assert win._ghost == (tool, value, (6, 5)) and win.view.preview_cells == {(6, 5)}
    # Second click places it and the preview stays for the next one.
    _click(win, (6, 5), monkeypatch)
    assert win.doc.grids[layer][5][6] == expected
    assert win._stamp == tool
    assert not win.view.selection
    win.undo()
    assert win.doc.snapshot() == before


@pytest.mark.parametrize('tool', ['sector', 'building'])
def test_escape_leaves_preview_then_clears_selection(win, monkeypatch, tool):
    _select(win, tool)
    _click(win, (4, 4), monkeypatch)
    win.view.selection = {(2, 2)}
    win._cancel_operation()
    assert win._stamp is None and win._ghost is None and not win.view.placing
    assert win.view.doc is win.doc and not win.view.preview_cells
    assert win.view.selection == {(2, 2)}, 'the first Esc only closes the preview'
    widget = win.sector_list if tool == 'sector' else win.building_list
    assert widget.currentItem() is not None, 'the entry stays selected'
    win._cancel_operation()
    assert not win.view.selection


def test_dragging_on_the_map_still_paints(win, monkeypatch):
    value = _select(win, 'sector')
    _click(win, (3, 3), monkeypatch, move_to=(6, 3))
    assert win._stamp is None
    assert [win.doc.grids['type'][3][c] for c in (3, 4, 5, 6)] == [f'{value:02x}'] * 4


def test_cells_with_a_building_get_no_preview(win, monkeypatch):
    _select(win, 'building')
    _click(win, (4, 4), monkeypatch)
    _click(win, (4, 4), monkeypatch)
    assert win.doc.grids['blg'][4][4] != '00'
    win._hovered(4, 4)
    assert win._ghost is None
    win._hovered(0, 0)
    assert win._ghost is None, 'border cells cannot hold sectors or buildings'


def test_right_click_keeps_preview_visible_and_can_cancel_it(win, monkeypatch):
    _select(win, 'sector')
    _click(win, (4, 4), monkeypatch)
    monkeypatch.setattr(win.view, 'pick_cell', lambda x, y: (4, 4))
    win._map_context_menu(QPointF(40, 40), QPoint(40, 40))
    menu = win._context_menu
    assert win.view.preview_cells == {(4, 4)} and win._stamp == 'sector'
    menu.show()
    win._hovered(-1, -1)  # the pointer leaves the map when the menu opens
    assert win.view.preview_cells == {(4, 4)}
    menu.hide()
    QTest.qWait(30)
    assert win.view.preview_cells == {(4, 4)} and win._stamp == 'sector'
    win._map_context_menu(QPointF(40, 40), QPoint(40, 40))
    assert not win.view.selection
    action = next(a for a in win._context_menu.actions() if a.text() == 'Cancel Placement')
    action.trigger()
    assert win._stamp is None and win._ghost is None and not win.view.preview_cells
    assert win.sector_list.currentItem() is not None


def test_right_click_without_preview_has_no_cancel_entry(win, monkeypatch):
    _select(win, 'sector')
    monkeypatch.setattr(win.view, 'pick_cell', lambda x, y: (4, 4))
    win._map_context_menu(QPointF(40, 40), QPoint(40, 40))
    assert 'Cancel Placement' not in [a.text() for a in win._context_menu.actions()]


@pytest.mark.parametrize('tool', ['sector', 'building'])
def test_drag_and_drop_from_the_list(win, tool):
    value = _select(win, tool)
    payload = {'kind': tool, 'value': value}
    layer = 'type' if tool == 'sector' else 'blg'
    expected = f'{value:02x}' if tool == 'sector' else f'{win.buildings[value].id:02x}'
    before = win.doc.snapshot()
    win._map_drag_moved(payload, (5, 5))
    assert win._ghost == (tool, value, (5, 5)) and win.view.doc.grids[layer][5][5] == expected
    assert win.doc.snapshot() == before
    win._map_drag_moved(payload, None)
    assert win._ghost is None and win.view.doc is win.doc
    assert not win._drop_allowed(payload, (0, 0))
    win._map_drag_moved(payload, (5, 5))
    win._map_dropped(payload, (5, 5))
    assert win._ghost is None and win.view.doc is win.doc
    assert win.doc.grids[layer][5][5] == expected
    win.undo()
    assert win.doc.snapshot() == before


def test_unknown_drop_value_is_ignored(win):
    before = win.doc.snapshot()
    win._map_dropped({'kind': 'sector', 'value': 9999}, (5, 5))
    win._map_dropped({'kind': 'building', 'value': 'x'}, (5, 5))
    assert win.doc.snapshot() == before


def test_list_drag_payload_round_trip():
    from PySide6.QtCore import QMimeData
    from map_editor.ui.map_drag import decode_payload, encode_payload
    payload = {'kind': 'special', 'object': 'gem', 'slot': 2}
    assert decode_payload(encode_payload(payload)) == payload
    assert decode_payload(QMimeData()) is None


def _gem_in_slot_one(win):
    panel = win.special_panels['gem']
    win._start_special_draft('gem', panel.placement_values())
    win._special_changed('gem', panel.slot, {'actions': [
        dict(target_type='modify_vehicle', id=1, param='enable', val='1')]})
    win._confirm_special((3, 3))
    assert win.doc.gems[1]['x'] == 3
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gem'])
    panel.list.setCurrentRow(0)
    assert panel.slot == 1
    return panel


def test_special_tab_starts_with_nothing_selected(win):
    win.palette_tabs.setCurrentIndex(win.special_tab_indices['gem'])
    assert win.special_panels['gem'].slot == 0


def test_special_object_two_click_placement(win, monkeypatch):
    _gem_in_slot_one(win)
    # An empty cell starts the grey preview, the second click moves the gem.
    win._select_cell((6, 6), NO_MOD)
    assert win._special_draft is not None and win.view.placing
    win._confirm_placement((6, 6))
    assert win._special_draft is None and not win.view.placing
    assert (win.doc.gems[1]['x'], win.doc.gems[1]['y']) == (6, 6)
    # Esc after the first click leaves the gem where it was.
    win._select_cell((8, 8), NO_MOD)
    win._cancel_operation()
    assert win._special_draft is None
    assert (win.doc.gems[1]['x'], win.doc.gems[1]['y']) == (6, 6)


def test_special_object_right_click_cancels_without_menu(win):
    _gem_in_slot_one(win)
    win._select_cell((6, 6), NO_MOD)
    assert win._special_draft is not None
    win._context_menu = None
    win._map_context_menu(QPointF(40, 40), QPoint(40, 40))
    assert win._context_menu is None
    assert win._special_draft is None and not win.view.placing
    assert (win.doc.gems[1]['x'], win.doc.gems[1]['y']) == (3, 3)


def test_special_object_drag_and_drop(win):
    _gem_in_slot_one(win)
    payload = {'kind': 'special', 'object': 'gem', 'slot': 1}
    win._map_drag_moved(payload, (7, 7))
    assert win._special_draft is not None
    win._map_drag_moved(payload, None)
    assert win._special_draft is None and (win.doc.gems[1]['x'], win.doc.gems[1]['y']) == (3, 3)
    win._map_drag_moved(payload, (7, 7))
    win._map_dropped(payload, (7, 7))
    assert win._special_draft is None
    assert (win.doc.gems[1]['x'], win.doc.gems[1]['y']) == (7, 7)


def test_list_press_and_move_starts_a_drag_with_the_entry(win, monkeypatch):
    from map_editor.ui.map_drag import MapDragSource
    started = []
    monkeypatch.setattr(MapDragSource, '_start', lambda self, item, payload: started.append(payload))
    win.show()
    QTest.qWait(50)
    widget = win.sector_list
    item = widget.item(0)
    center = widget.visualItemRect(item).center()
    QTest.mousePress(widget.viewport(), LEFT, NO_MOD, center)
    QTest.mouseMove(widget.viewport(), center + QPoint(2, 0))
    assert not started, 'a tiny movement is still a click'
    QTest.mouseMove(widget.viewport(), center + QPoint(40, 40))
    QTest.mouseRelease(widget.viewport(), LEFT, NO_MOD, center + QPoint(40, 40))
    assert started == [{'kind': 'sector', 'value': item.data(Qt.ItemDataRole.UserRole)}]
    assert widget.currentItem() is item, 'the pressed entry becomes the selected one'


def test_viewport_drag_events_drive_the_preview_and_the_drop(win, monkeypatch):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDragLeaveEvent, QDropEvent
    from map_editor.ui.map_drag import encode_payload
    value = _select(win, 'sector')
    cell = [(5, 5)]
    monkeypatch.setattr(win.view, 'pick_cell', lambda x, y: cell[0])
    mime = encode_payload({'kind': 'sector', 'value': value})
    args = (QPoint(30, 30), Qt.DropAction.CopyAction, mime, LEFT, NO_MOD)
    assert win.view.acceptDrops()
    enter = QDragEnterEvent(*args)
    QApplication.sendEvent(win.view, enter)
    assert enter.isAccepted()
    move = QDragMoveEvent(*args)
    QApplication.sendEvent(win.view, move)
    assert move.isAccepted() and win._ghost == ('sector', value, (5, 5))
    cell[0] = (0, 0)  # border: not a valid target
    move = QDragMoveEvent(*args)
    QApplication.sendEvent(win.view, move)
    assert not move.isAccepted() and win._ghost is None
    cell[0] = (5, 5)
    QApplication.sendEvent(win.view, QDragMoveEvent(*args))
    QApplication.sendEvent(win.view, QDragLeaveEvent())
    assert win._ghost is None
    drop = QDropEvent(QPointF(30, 30), Qt.DropAction.CopyAction, mime, LEFT, NO_MOD)
    win.view.dropEvent(drop)  # a synthetic QDropEvent is not routed by sendEvent
    assert drop.isAccepted() and win.doc.grids['type'][5][5] == f'{value:02x}'
    # Foreign drags (files, text) are ignored.
    foreign = QMimeData()
    foreign.setText('x')
    enter = QDragEnterEvent(QPoint(30, 30), Qt.DropAction.CopyAction, foreign, LEFT, NO_MOD)
    QApplication.sendEvent(win.view, enter)
    assert not enter.isAccepted()


@pytest.mark.parametrize('kind', ['gem', 'item', 'gate'])
def test_original_special_object_leaves_its_cell_while_the_copy_is_shown(win, kind):
    from map_editor.core.special_objects import add_special, update_special
    from map_editor.render.special_scene import special_members
    slot = add_special(win.doc, kind)
    values = {'x': 3, 'y': 3}
    if kind == 'gem':
        values['actions'] = [dict(target_type='modify_vehicle', id=1, param='enable', val='1')]
    if kind != 'gem':
        values['keys'] = [(5, 3)]
    update_special(win.doc, kind, slot, values, win.buildings)
    win._refresh_specials()
    win.palette_tabs.setCurrentIndex(win.special_tab_indices[kind])
    win.special_panels[kind].list.setCurrentRow(0)
    real = win.doc.snapshot()
    win.view.hover = (6, 6)
    win._select_cell((6, 6), NO_MOD)
    cells = {m.cell: m for m in special_members(win.view.doc, win.view.lib)}
    assert (3, 3) not in cells, 'the original main object is gone from the preview'
    assert cells[(6, 6)].preview
    if kind == 'gem':
        assert win.doc.grids['blg'][3][3] != '00' and win.view.doc.grids['blg'][3][3] == '00'
    if kind != 'gem':
        assert (5, 3) not in cells, 'the original keys are gone too'
    assert win.doc.snapshot() == real, 'the real map is untouched until the second click'
    win._cancel_operation()
    assert win.view.doc is win.doc
    assert {m.cell for m in special_members(win.doc, win.view.lib)} >= {(3, 3)}
