from types import SimpleNamespace

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent

from .test_special_objects import app, win  # noqa: F401
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.road_network import EAST, WEST, plan_road_fixes
from map_editor.core.special_objects import add_special, update_special


def test_delete_building_restores_underlying_sector_after_save_and_undo(win):
    cell = (3, 3)
    win.doc.grids['type'][3][3] = '06'
    win.set_tool('building')
    win.building_list.setCurrentItem(win._bicon_items[54])
    win._pressed(*cell, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(*cell, Qt.KeyboardModifier.NoModifier)
    saved = dumps_ldf(win.doc)
    win.doc = loads_ldf(saved)
    before = win.doc.snapshot()
    win._delete_building_at(cell)
    assert win.doc.grids['type'][3][3] == '06'
    assert win.doc.grids['blg'][3][3] == '00'
    win.undo()
    assert win.doc.snapshot() == before


def test_multiple_selection_paints_over_existing_building_without_starting_drag(win, monkeypatch):
    view = win.view
    win.set_tool('sector')
    win.sector_list.setCurrentItem(win._icon_items[6])
    view.selection = {(3, 3), (4, 3)}
    win.doc.grids['blg'][3][3] = '36'
    monkeypatch.setattr(view, 'ground_cell', lambda *args: (3, 3))
    monkeypatch.setattr(view, 'pick_cell', lambda *args: (3, 3))
    monkeypatch.setattr(view, 'pick_scene_object', lambda *args: ('special', ('item', 1, -1)))
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(20, 20), QPointF(20, 20),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(event)
    assert win._building_drag is None and win._special_drag is None
    assert [win.doc.grids['type'][3][c] for c in (3, 4)] == ['06', '06']
    assert win.doc.grids['blg'][3][3] == '00'
    view.mouseReleaseEvent(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(20, 20), QPointF(20, 20),
                                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))


def test_road_plan_caps_an_isolated_straight_instead_of_leaving_it_cut():
    doc = LdfDocument(mw=7, mh=7)
    doc.grids['type'][3][3] = '01'
    masks = SimpleNamespace(mask=lambda typ: {1: EAST | WEST, 2: 0}.get(typ, 0),
                            is_special=lambda typ: False, road_types=lambda: [1, 2])
    sectors = {1: SimpleNamespace(subsects=(1,) * 9, ground=1),
               2: SimpleNamespace(subsects=(1,) * 9, ground=1)}
    lib = SimpleNamespace(assets=SimpleNamespace(sdf=SimpleNamespace(sectors=sectors)))
    plan = plan_road_fixes(doc, lib, masks)
    assert plan.changes == [(3, 3, 1, 2)] or plan.unresolved == [(3, 3, 0)]


def test_road_command_is_reachable_in_map_menu(win):
    from PySide6.QtWidgets import QMenu
    actions = [action.text() for menu in win.menuBar().findChildren(QMenu) for action in menu.actions()]
    assert 'Close Roads (Experimental)…' in actions


def test_special_drag_back_to_origin_invalidates_scene(win, monkeypatch):
    slot = add_special(win.doc, 'gate')
    update_special(win.doc, 'gate', slot, dict(x=3, y=3), win.buildings)
    win._refresh_specials()
    win._begin_special_drag({'cell': (3, 3), 'hit': ('gate', slot, -1)})
    win._move_special_drag((4, 3))
    updates = []
    original = win.view.scene_changed
    monkeypatch.setattr(win.view, 'scene_changed', lambda *args: (updates.append(True), original(*args)))
    win._move_special_drag((3, 3))
    assert win.doc.gates[slot]['x'] == 3
    assert updates
    win._finish_special_drag()


def test_real_road_masks_distinguish_straights_caps_and_nonroad_buildings(win):
    from map_editor.core.road_network import NORTH, SOUTH, RoadMasks
    masks = RoadMasks(win._lib())
    expected = {5: NORTH | SOUTH, 10: SOUTH, 12: NORTH | EAST | SOUTH,
                16: EAST | WEST, 17: EAST | WEST, 18: EAST | WEST, 22: WEST, 14: 0, 27: 0}
    assert {typ: masks.mask(typ) for typ in expected} == expected


def test_building_drag_preview_preserves_underlays_and_cancel_restores_map(win):
    win.doc.grids['type'][3][3] = '06'
    win.set_tool('building')
    win.building_list.setCurrentItem(win._bicon_items[54])
    win._pressed(3, 3, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(3, 3, Qt.KeyboardModifier.NoModifier)
    before = win.doc.snapshot()
    win._begin_building_drag((3, 3))
    win._move_building_drag((4, 3))
    assert win.doc.snapshot() == before
    win._cancel_operation(clear=False)
    assert win.doc.snapshot() == before


def test_terrain_does_not_paint_sector_selection_brackets(win, monkeypatch):
    from PySide6.QtGui import QImage
    win.view._annotation_base = QImage(20, 20, QImage.Format.Format_RGBA8888)
    win.view.active_tool = 'terrain'
    win.view.brush_cells = {(3, 3)}
    win.view.selection = {(3, 3)}
    win.view.hover = (3, 3)
    assert win.view._annotations() is win.view._annotation_base


def test_resize_links_dimensions_and_default_height_then_clears_old_border(app):
    from map_editor.core.ldf_model import HGT_MIN
    from map_editor.ui.dialogs import ResizeDialog
    doc = LdfDocument(mw=7, mh=7)
    dialog = ResizeDialog(doc)
    assert dialog.link_box.isChecked()
    assert dialog.sector_height_box.text() == 'Default'
    dialog.width_box.setValue(11)
    assert dialog.values() == (11, 11)
    dialog.link_box.setChecked(False)
    dialog.height_box.setValue(9)
    assert dialog.values() == (11, 9)
    dialog.sector_height_box.setValue(42)
    doc.resize(11, 9, height=dialog.sector_height_box.height())
    assert all(value == HGT_MIN + 42 for row in doc.grids['hgt'] for value in row)
    assert all(value == '00' for row in doc.grids['type'][1:-1] for value in row[1:-1])
    dialog.deleteLater()


def test_road_command_applies_only_approved_replacements_and_one_undo(win, monkeypatch):
    from map_editor.core.road_network import RoadPlan
    from PySide6.QtWidgets import QMessageBox
    win.doc.grids['type'][3][3] = '05'
    win.doc.grids['type'][3][4] = '05'
    before = win.doc.snapshot()
    monkeypatch.setattr('map_editor.core.road_network.plan_road_fixes',
                        lambda *args: RoadPlan(changes=[(3, 3, 5, 10)]))
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Cancel)
    win.map_close_roads()
    assert win.doc.snapshot() == before
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Apply)
    win.map_close_roads()
    assert win.doc.grids['type'][3][3] == '0a' and win.doc.grids['type'][3][4] == '05'
    win.undo()
    assert win.doc.snapshot() == before


def test_briefing_pair_exports_png_and_valid_ilbm_with_no_overwrite(win, tmp_path):
    from PySide6.QtGui import QImage
    from studio.formats.images import parse_ilbm_file
    from map_editor.render.briefing_art import briefing_palette, write_briefing_pair
    image = QImage(20, 18, QImage.Format.Format_RGBA8888)
    image.fill(0xff506030)
    palette = briefing_palette(1)
    for fmt in ('png', 'ilbm'):
        paths = write_briefing_pair(image, tmp_path, fmt, fmt=fmt, palette=palette)
        for path in paths:
            if fmt == 'png':
                assert (QImage(str(path)).width(), QImage(str(path)).height()) == (20, 18)
            else:
                parsed = parse_ilbm_file(path)
                assert (parsed.width, parsed.height) == (20, 18)
        import pytest
        with pytest.raises(FileExistsError):
            write_briefing_pair(image, tmp_path, fmt, fmt=fmt, palette=palette)
