"""Direct editing acceptance: placement, camera previews, empty and box selections."""

from copy import deepcopy

import numpy as np
from studio.ui.cursor_themes import cursor_kind
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_sector_editor import real_window  # noqa: F401 - shared Qt fixture
from test_sector_visual_ui import cell_point


def move_mouse(view, point, button=Qt.MouseButton.LeftButton):
    point = QPointF(point)
    event = QMouseEvent(QEvent.Type.MouseMove, point, point,
                       Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, event)


def test_wasd_browses_models_and_enter_confirms_without_affecting_other_cells(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(3)
    before = deepcopy(window.document.sdf)
    QTest.keyClick(window.models, Qt.Key.Key_S)
    assert window.models.currentRow() == 4 and window.model_combo.currentData() == 4
    QTest.keyClick(window.models, Qt.Key.Key_A)
    assert window.models.currentRow() == 3
    assert window.document.sdf == before
    QTest.keyClick(window.view, Qt.Key.Key_D)
    assert window.model_combo.currentData() == 4
    QTest.keyClick(window.view, Qt.Key.Key_Return)
    sector = window.document.sdf.sectors[142]
    assert window.document.sdf.buildings[sector.subsects[4]].intact == 4
    assert sector.subsects[:4] + sector.subsects[5:] == before.sectors[142].subsects[:4] + before.sectors[142].subsects[5:]
    window.undo()
    assert window.document.sdf == before


def test_copy_starts_ghost_and_paste_confirms_position_and_relative_shape(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell_box({0, 1, 3})
    before = deepcopy(window.document.sdf)
    window.copy_cells()
    assert window._placement["kind"] == "paste"
    assert window.view._ghost_mesh is not None
    window.preview_cell_placement(3)
    assert window.view.target_cells == {3, 4, 6}
    assert window.document.sdf == before
    window.paste_cells()
    sector = window.document.sdf.sectors[142]
    for source, target in ((0, 3), (1, 4), (3, 6)):
        assert sector.subsects[target] == before.sectors[142].subsects[source]
    assert window._placement is None
    assert len(window.document._undo) == 1
    assert "duplicate" not in window.actions
    window.undo()
    assert window.document.sdf == before
    assert window._copied_cells is not None


def test_copy_cancellation_and_outside_pattern_do_not_edit_or_lose_clipboard(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell_box({0, 8})
    before = deepcopy(window.document.sdf)
    window.copy_cells()
    window.preview_cell_placement(1)
    assert not window.view.target_cells
    assert cursor_kind(window.view.cursor()) == "forbidden"
    window.finish_cell_placement(1)
    assert window.document.sdf == before and window._placement is not None
    window.cancel_preview()
    assert window._placement is None and window._copied_cells is not None
    assert window.view._ghost_mesh is None
    window.paste_cells()
    assert window._placement["kind"] == "paste" and window.document.sdf == before


def test_double_click_background_deselects_including_single_cell_layout(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    view = window.cell_grid
    window._select_all()
    before = deepcopy(window.document.sdf)
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert not window.selected_cells and not view.selected_cells
    assert not window.apply_model_button.isEnabled()
    assert window.ground.isEnabled() and window.symbol.isEnabled()
    window.confirm_placement()
    window.apply_states()
    assert window.document.sdf == before
    window._layout_changed(1)
    before = deepcopy(window.document.sdf)
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert not window.selected_cells
    window._refresh()
    assert not window.selected_cells and window.document.sdf == before


def test_left_drag_draws_selection_rectangle_without_changing_document(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    view = window.cell_grid
    start = (view.cell_polygon(0).boundingRect().topLeft() - QPointF(5, 5)).toPoint()
    end = cell_point(view, 4).toPoint()
    before = deepcopy(window.document.sdf)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    move_mouse(view, end)
    assert view._selection_box is not None
    assert window.document.sdf == before
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)
    assert window.selected_cells == {0, 1, 3, 4}
    assert view._selection_box is None and window.document.sdf == before


def test_dragging_selected_cells_moves_them_with_the_grabbed_cell_as_anchor(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell_box({0, 1})
    view = window.cell_grid
    before = deepcopy(window.document.sdf)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 1).toPoint())
    move_mouse(view, cell_point(view, 4))
    assert window._placement["kind"] == "move"
    assert view.target_cells == {3, 4}
    assert window.document.sdf == before
    assert all(view.cell_index(ox, oz) not in (0, 1) for face, ox, oz in view.lib.mesh(142).faces)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 4).toPoint())
    sector = window.document.sdf.sectors[142]
    assert sector.subsects[:2] == (0, 0)
    assert sector.subsects[3:5] == before.sectors[142].subsects[:2]
    assert window.selected_cells == {3, 4}
    assert len(window.document._undo) == 1
    window.undo()
    assert window.document.sdf == before


def test_rotation_keeps_ghost_in_shared_scene_and_replaces_original_target(real_window):
    from map_editor.render.map_scene import render_scene, scene_polygons
    window = real_window
    window.sectors.setCurrentRow(142)
    window.select_cell(0)
    window.tabs.setCurrentWidget(window.models_page)
    window.models.setCurrentRow(3)
    view = window.view
    library, mesh = view.lib, view._ghost_mesh
    before = deepcopy(window.document.sdf)
    assert library.preview_styles == [(160, 160, 160, 3)]
    for yaw in (0, 37, 90, 143, 270):
        view.camera.yaw = yaw
        view.cameraChanged.emit()
        assert view.lib is library and view._ghost_mesh is mesh
        camera = view.camera.copy()
        camera.width, camera.height, camera.pan = 300, 260, (0, 0)
        camera.zoom = .14
        frame = render_scene(scene_polygons(library, view.doc, view.terrain, camera),
                             camera, library.tables, fast=True, preview_codes=(-2,))
        visible = frame.cell_ids == -2
        assert visible.any(), yaw
        rgb = frame.rgba[visible, :3]
        assert np.all(rgb[:, 0] == rgb[:, 1]) and np.all(rgb[:, 1] == rgb[:, 2])
        assert all(view.cell_index(ox, oz) != 0 or id(face) in library.preview_face_codes
                   for face, ox, oz in library.mesh(142).faces)
    assert window.document.sdf == before


def test_validation_is_a_report_not_a_persistent_tab_and_zip_label_is_clear(real_window):
    window = real_window
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Cells", "Models", "Definitions"]
    assert window.issues.isHidden() and window.issues.toPlainText()
    assert window.validation_action.text().startswith("Validation Report")
    assert window.actions["zip"].text() == "Export Complete Set as ZIP" + chr(0x2026)


def test_dragging_an_unselected_building_selects_and_moves_it_directly(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    view = window.cell_grid
    assert window.selected_cells == {4} and 8 in view.occupied_cells
    before = deepcopy(window.document.sdf)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 8).toPoint())
    move_mouse(view, cell_point(view, 4))
    assert window._placement["kind"] == "move" and window.selected_cells == {8}
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 4).toPoint())
    sector = window.document.sdf.sectors[142]
    assert sector.subsects[8] == 0 and sector.subsects[4] == before.sectors[142].subsects[8]
    window.undo()
    assert window.document.sdf == before


def test_wasd_navigates_context_menu_and_enter_triggers_selected_action(real_window):
    from PySide6.QtWidgets import QMenu
    window = real_window
    menu = QMenu(window)
    choices = []
    first = menu.addAction("First", lambda: choices.append(1))
    second = menu.addAction("Second", lambda: choices.append(2))
    window._prepare_menu(menu)
    menu.popup(window.mapToGlobal(QPoint(50, 50)))
    menu.setActiveAction(first)
    QTest.keyClick(menu, Qt.Key.Key_S)
    assert menu.activeAction() is second
    QTest.keyClick(menu, Qt.Key.Key_W)
    assert menu.activeAction() is first
    QTest.keyClick(menu, Qt.Key.Key_Return)
    assert choices == [1]
    menu.close()


def test_escape_cancels_mouse_rectangle_before_release(real_window):
    window = real_window
    window.sectors.setCurrentRow(142)
    window.activateWindow()
    view = window.cell_grid
    view.setFocus()
    original = set(window.selected_cells)
    before = deepcopy(window.document.sdf)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    move_mouse(view, cell_point(view, 4))
    assert view._selection_box is not None
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._selection_box is None and view._cell_press is None
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=cell_point(view, 4).toPoint())
    assert window.selected_cells == original and window.document.sdf == before
