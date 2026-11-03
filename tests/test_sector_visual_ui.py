"""Visual cell selection, transient model previews and native edit history."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu

from test_sector_editor import real_window  # noqa: F401 - shared Qt fixture


def cell_point(view, cell):
    x, z = 600 + (cell % 3 - 1) * 300, -600 + (1 - cell // 3) * 300
    return QPointF(*view.camera.world_to_screen((x, 0, z))[0])


def test_top_view_shows_real_models_and_selects_cells_in_grid_order(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    view = window.cell_grid
    assert view.camera.pitch == 90 and view.camera.yaw == 180
    assert view.width() <= window.cells_page.viewport().width()
    assert view.lib.mesh(142).faces
    assert "Building 16" in view.cell_details[0]
    assert "Destroyed:" in view.cell_details[0]
    for cell in range(9):
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=cell_point(view, cell).toPoint())
        assert window.selected_cells == {cell}
        assert view.selected_cells == window.view.selected_cells == {cell}
    window.select_cell(0)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     cell_point(view, 8).toPoint())
    assert window.selected_cells == {0, 8}
    window.select_cell(0)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                     cell_point(view, 4).toPoint())
    assert window.selected_cells == {0, 1, 3, 4}


def test_model_browsing_ghost_is_grey_and_never_changes_tables(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.tabs.setCurrentWidget(window.models_page)
    before, history = deepcopy(window.document.sdf), len(window.document._undo)
    window.models.setCurrentRow(3)
    view = window.view
    assert view.target_cells == {4} and view._ghost_mesh.faces
    from map_editor.render.map_scene import scene_polygons, render_scene
    frame = render_scene(scene_polygons(view.lib, view.doc, view.terrain, view.camera),
                         view.camera, view.lib.tables, preview_codes=(-2,))
    rgba = frame.rgba
    visible = frame.cell_ids == -2
    assert visible.any()
    assert np.all(rgba[:, :, 0][visible] == rgba[:, :, 1][visible])
    assert np.all(rgba[:, :, 1][visible] == rgba[:, :, 2][visible])
    assert all(view.cell_index(ox, oz) != 4 or id(face) in view.lib.preview_face_codes
               for face, ox, oz in view.lib.mesh(142).faces)
    assert window.document.sdf == before and len(window.document._undo) == history
    window.activateWindow()
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._ghost_mesh is None and not view.target_cells
    assert window.document.sdf == before


def drag_event(window, view, cell, *, foreign=False):
    mime = QMimeData()
    mime.setData("application/x-openneoua-sector-model", b"3")
    state = {"accepted": False}
    event = SimpleNamespace(source=lambda: None if foreign else window.models,
        mimeData=lambda: mime, position=lambda: cell_point(view, cell),
        acceptProposedAction=lambda: state.update(accepted=True),
        ignore=lambda: state.update(accepted=False), accept=lambda: state.update(accepted=True))
    return event, state


def test_drop_previews_then_applies_only_target_cells_and_selected_damage_state(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.state.setCurrentIndex(1)
    before = deepcopy(window.document.sdf)
    view = window.view
    event, result = drag_event(window, view, 8)
    view.dragEnterEvent(event)
    assert result["accepted"]
    view.dragMoveEvent(event)
    assert view.target_cells == {8}
    assert window.document.sdf == before
    view.dropEvent(event)
    assert result["accepted"] and window.selected_cells == {8}
    sector = window.document.sdf.sectors[142]
    assert sector.subsects[:8] == before.sectors[142].subsects[:8]
    old = before.buildings[before.sectors[142].subsects[8]]
    new = window.document.sdf.buildings[sector.subsects[8]]
    assert new.damaged == 3
    assert (new.intact, new.heavy, new.flat) == (old.intact, old.heavy, old.flat)
    assert len(window.document._undo) == 1
    window.undo()
    assert window.document.sdf == before
    window.redo()
    assert window.document.sdf.sectors[142] == sector


def test_drop_rejects_foreign_sources_and_drag_leave_cancels_preview(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    before = deepcopy(window.document.sdf)
    event, result = drag_event(window, window.cell_grid, 0, foreign=True)
    window.cell_grid.dragEnterEvent(event)
    assert not result["accepted"]
    window.cell_grid.dropEvent(event)
    assert window.document.sdf == before
    event, result = drag_event(window, window.cell_grid, 0)
    window.cell_grid.dragMoveEvent(event)
    assert window.cell_grid.target_cells == {0}
    window.cell_grid.dragLeaveEvent(event)
    assert not window.cell_grid.target_cells and window.cell_grid._ghost_mesh is None
    assert window.document.sdf == before


def test_group_move_copy_cut_and_paste_share_history(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell(0)
    window.select_cell(1, True)
    before = deepcopy(window.document.sdf)
    window.start_cell_placement()
    window.preview_cell_placement(3)
    assert window.view.target_cells == {3, 4}
    assert window.document.sdf == before
    window.finish_cell_placement(3)
    assert window.selected_cells == {3, 4}
    assert window.document.sdf.sectors[142].subsects[3:5] == before.sectors[142].subsects[:2]
    assert window.document.sdf.sectors[142].subsects[:2] == (0, 0)
    window.undo()
    assert window.document.sdf == before
    window.select_cell(0)
    window.copy_cells()
    window.preview_cell_placement(8)
    window.cancel_preview()
    assert window.document.sdf == before and window._placement is None
    window.paste_cells()
    window.finish_cell_placement(8)
    assert window.document.sdf.sectors[142].subsects[8] == before.sectors[142].subsects[0]
    window.undo()
    window.select_cell(0)
    window.cut_cells()
    assert window.document.sdf.sectors[142].subsects[0] == 0
    window.select_cell(8)
    window.paste_cells()
    window.confirm_placement()
    assert window.document.sdf.sectors[142].subsects[8] == before.sectors[142].subsects[0]
    window.undo()
    window.undo()
    assert window.document.sdf == before


def test_connection_family_applies_to_selected_sectors_and_automatic_picks_values(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.sectors.item(141).setSelected(True)
    before = deepcopy(window.document.sdf)
    sample = window.document.sdf.sectors[142].ground
    window.ground.setValue((sample + 1) % 6)
    assert window._selected_sector_numbers() == [141, 142]
    assert window.document.sdf.buildings == before.buildings
    for number in (141, 142):
        assert window.document.sdf.sectors[number].subsects == before.sectors[number].subsects
        assert window.document.sdf.sectors[number].ground == (sample + 1) % 6
    assert len(window.document._undo) == 1
    window.undo()
    assert window.document.sdf == before
    window.sectors.clearSelection()
    window.sectors.setCurrentRow(142)
    window.symbol.setValue(window.symbol.minimum())
    assert window.symbol.specialValueText() == "Automatic"
    expected = window.document.suggest_fields(142)[1]
    assert window.document.sdf.sectors[142].symbol == expected == window.symbol.value()


def test_context_menu_uses_shared_actions_and_right_drag_still_orbits(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    view = window.view
    seen = []
    def read_and_close_menu():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            seen.extend(a.text() for a in menu.actions())
            menu.close()
    QTimer.singleShot(50, read_and_close_menu)
    QTest.mouseClick(view, Qt.MouseButton.RightButton, pos=cell_point(view, 4).toPoint())
    assert seen == ["Copy cell", "Cut cell", "Paste cell", "Clear cell", "Rotate cell" + chr(0x2026),
                    "Select all cells", "Change cell state"]
    previous = view.camera.copy()
    point = cell_point(view, 4).toPoint()
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    QTest.mousePress(view, Qt.MouseButton.RightButton, pos=point)
    moved = QPointF(point + QPoint(40, 25))
    event = QMouseEvent(QEvent.Type.MouseMove, moved, moved,
                       Qt.MouseButton.NoButton, Qt.MouseButton.RightButton,
                       Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, event)
    seen.clear()
    QTimer.singleShot(50, read_and_close_menu)
    QTest.mouseRelease(view, Qt.MouseButton.RightButton, pos=moved.toPoint())
    assert not seen
    assert view.camera.yaw != previous.yaw or view.camera.pitch != previous.pitch


def test_cut_shortcut_respects_text_fields_and_drop_fills_states(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.activateWindow()
    window.description.setText("Cut text")
    window.description.selectAll()
    window.description.setFocus()
    before = window.document.sdf.sectors[142].subsects
    QTest.keyClick(window.description, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    assert not window.description.text()
    assert window.document.sdf.sectors[142].subsects == before
    window.select_cell(0)
    window.select_cell(8, True)
    window.drop_model(3, 8)
    chain = window.document.state_chain(3)
    for cell in (0, 8):
        sector = window.document.sdf.sectors[142]
        states = tuple(window.document.sdf.buildings[sector.subsects[cell]].state(s) for s in range(4))
        assert states == (chain[0] if chain else (3,) * 4)


def test_whole_sector_model_is_centered_and_converts_the_sector(real_window):
    from sector_editor.viewport import covers_sector
    window = real_window
    window.sectors.setCurrentRow(142)
    big = next(m.id for m in window.document.sdf.models if covers_sector(window.library, m.id))
    assert not covers_sector(window.library, 3)
    window.select_cell(0)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(big)
    assert window.view.target_cells == set(range(9))
    assert {(ox, oz) for _face, ox, oz in window.view._ghost_mesh.faces} == {(0, 0)}
    window.apply_model()
    sector = window.document.sdf.sectors[142]
    assert sector.single and window.document.sdf.buildings[sector.subsects[0]].intact == big
    assert window.selected_cells == {4}


def test_ghost_missing_model_is_reported_and_export_excludes_preview(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    before = deepcopy(window.document.sdf)
    image = window.view.capture_preview(256)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(3)
    preview = window.view.capture_preview(256)
    assert image == preview
    window.view.set_ghost()
    with patch.object(window.library, "_base_faces", return_value=None):
        window.view.set_ghost(3, {4})
    assert window.view._ghost_mesh is None
    assert "Preview unavailable" in window.statusBar().currentMessage()
    assert window.document.sdf == before


def test_sector_context_menu_preserves_group_when_clicked_member_is_not_current(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.sectors.item(141).setSelected(True)
    QApplication.processEvents()
    item = window.sectors.item(141)
    window.sectors.scrollToItem(item)
    seen = []
    def close_menu():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            seen.extend(a.text() for a in menu.actions() if a.text())
            menu.close()
    QTimer.singleShot(50, close_menu)
    window._sector_menu(window.sectors.visualItemRect(item).center())
    assert window.current_sector == 141
    assert window._selected_sector_numbers() == [141, 142]
    assert seen == ["Import sector definition" + chr(0x2026), "Export sector definition" + chr(0x2026),
                    "Wipe sector" + chr(0x2026), "Clear sector", "Copy sector", "Cut sector"]


def test_copied_sectors_follow_the_mouse_and_land_on_the_released_slot(real_window):
    window = real_window
    window.sectors.setCurrentRow(141)
    window.sectors.item(142).setSelected(True)
    before = deepcopy(window.document.sdf)
    window.copy_sectors()
    assert window.sectors.carrying
    target = window.document.free_sector()
    window.sectors.scrollToItem(window.sectors.item(target))
    QApplication.processEvents()
    QTest.mouseClick(window.sectors.viewport(), Qt.MouseButton.LeftButton,
                     pos=window.sectors.visualItemRect(window.sectors.item(target)).center())
    assert not window.sectors.carrying
    assert window.document.sdf.sectors[target].subsects == before.sectors[141].subsects
    assert window.document.sdf.sectors[target + 1].subsects == before.sectors[142].subsects
    assert window.current_sector == target and 141 in window.document.sdf.sectors
    window.undo()
    window.sectors.setCurrentRow(141)
    window.copy_sectors(cut=True)
    window.cancel_interaction()
    assert not window.sectors.carrying and window.document.sdf == before


def test_model_inspection_releases_mouse_without_changing_sector(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.model_combo.setCurrentIndex(3)
    window.preview_mode.setCurrentIndex(1)
    before = deepcopy(window.document.sdf)
    QTest.mouseClick(window.view, Qt.MouseButton.LeftButton, pos=window.view.rect().center())
    assert window.view._drag_button is None
    assert window.document.sdf == before


def test_double_click_places_the_previewed_model(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(3)
    view = window.cell_grid
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 0).toPoint())
    sector = window.document.sdf.sectors[142]
    assert window.document.sdf.buildings[sector.subsects[0]].intact == 3
    assert len(window.document._undo) == 1
