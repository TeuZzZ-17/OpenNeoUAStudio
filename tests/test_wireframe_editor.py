import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication

from editor_widgets import MODEL_EDIT_SELECTION_RED
from outline_editor import OutlineCanvas, OutlineEditor
from sklt_parser import create_minimal_sklt_model
from wireframe_editor.window import WireframeEditorWindow


class WireframeEditorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        window = WireframeEditorWindow()
        self.addCleanup(window.close)
        return window

    def test_file_menu_uses_import_and_export_labels(self):
        window = self._window()
        file_menu = window.file_menu
        labels = [
            action.text().replace("&", "")
            for action in file_menu.actions()
            if not action.isSeparator()
        ]
        self.assertEqual(
            labels, ["New", "Import", "Export / Overwrite", "Export As...", "Exit"])
        self.assertIs(window.export_action, window.save_action)
        self.assertIs(window.export_as_action, window.save_as_action)


    def test_export_as_stays_available_without_pending_edits(self):
        window = self._window()
        self.assertFalse(window.outline_editor.is_dirty)
        self.assertFalse(window.save_action.isEnabled())
        self.assertTrue(window.save_as_action.isEnabled())

    def test_wireframe_editor_has_no_mode_menu(self):
        window = self._window()
        labels = [
            action.text().replace("&", "")
            for action in window.menuBar().actions()
        ]
        self.assertNotIn("Mode", labels)
        self.assertFalse(hasattr(window, "rotate_mode_action"))
        self.assertFalse(hasattr(window, "resize_mode_action"))

    def test_no_warnings_is_centered_in_review_panel(self):
        window = self._window()
        alignment = window.warning_status_label.alignment()
        self.assertTrue(alignment & Qt.AlignmentFlag.AlignHCenter)
        self.assertTrue(alignment & Qt.AlignmentFlag.AlignVCenter)

    def test_toolbar_ends_before_vertex_details(self):
        window = self._window()
        widgets = [
            window.edit_toolbar.widgetForAction(action)
            for action in window.edit_toolbar.actions()
            if not action.isSeparator()
            and window.edit_toolbar.widgetForAction(action) is not None
        ]
        self.assertEqual(
            widgets,
            [
                window.outline_editor.undo_button,
                window.outline_editor.redo_button,
                window.outline_editor.reset_button,
                window.outline_editor.show_indices_check,
                window.outline_editor.auto_align_check,
            ],
        )
        self.assertTrue(window.outline_editor.show_indices_check.isChecked())
        self.assertTrue(window.outline_editor.canvas._show_vertex_indices)
        self.assertFalse(hasattr(window, "mode_3d_check"))
        self.assertFalse(hasattr(window, "viewer"))
        self.assertNotIn(window.outline_editor.selected_label, widgets)
        self.assertNotIn(window.outline_editor.x_spin, widgets)
        self.assertNotIn(window.outline_editor.y_spin, widgets)
        self.assertNotIn(window.outline_editor.z_spin, widgets)
        self.assertNotIn(window.outline_editor.status_label, widgets)

    def test_summary_panel_hides_parser_details(self):
        window = self._window()
        self.assertEqual(window.edit_mode_value.text(), "HUD Wireframe")
        self.assertEqual(window.status_value.text(), "Ready")
        self.assertEqual(window.selection_value.text(), "No selection")
        self.assertEqual(window.file_value.text(), "Untitled.SKL")
        self.assertFalse(hasattr(window, "chunk_tree"))
        self.assertFalse(hasattr(window, "rendered_polygons_value"))

    def test_startup_is_immediately_an_editable_empty_new_session(self):
        window = self._window()
        self.assertIsNotNone(window._current_model)
        self.assertIsNone(window._current_file_path)
        self.assertEqual(window.outline_editor.save_mode, "poo2")
        self.assertEqual(window.outline_editor.projected_points, [])
        self.assertEqual(window.outline_editor.polygons, [])
        self.assertFalse(window.outline_editor.is_dirty)
        self.assertTrue(window.add_vertex_action.isEnabled())


    def test_empty_startup_workspace_accepts_wheel_zoom(self):
        window = self._window()
        canvas = window.outline_editor.canvas
        canvas.resize(640, 480)
        self.assertEqual(canvas._points, [])
        zoom_before = canvas._zoom
        position = QPointF(320.0, 240.0)
        event = QWheelEvent(
            position, position, QPointF(0.0, 0.0).toPoint(), QPointF(0.0, 120.0).toPoint(),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        canvas.wheelEvent(event)
        self.assertGreater(canvas._zoom, zoom_before)

    def test_auto_align_magnet_snaps_both_axes_to_unconnected_vertex(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (100.0, 0.0, 100.0)],
            [],
        )
        editor.auto_align_check.setChecked(True)
        editor.move_projected_points({1: (2.0, 3.0)}, True)
        self.assertEqual(editor.projected_points[1], (0.0, 0.0))

    def test_auto_align_never_collapses_connected_link_even_with_two_axis_magnet(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (100.0, 0.0, 100.0)],
            [[0, 1]],
        )
        editor.auto_align_check.setChecked(True)
        editor.move_projected_points({1: (2.0, 3.0)}, True)
        self.assertNotEqual(editor.projected_points[1], editor.projected_points[0])
        self.assertEqual(editor.polygons, [[0, 1]])

    def test_selected_elements_panel_is_taller_than_review_list(self):
        window = self._window()
        self.assertGreater(
            window.selected_elements_list.minimumHeight(),
            window.warning_list.minimumHeight(),
        )


    def test_empty_editor_does_not_show_obsolete_no_data_message(self):
        window = self._window()
        self.assertEqual(window.outline_editor.canvas._message, "")

    def test_empty_context_uses_clicked_world_position_for_first_vertex_here(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view([], [], -1, "", False)
        target = QPointF(500.0, 160.0)
        expected = canvas._from_screen(target)
        emitted = []
        canvas.emptyContextMenuRequested.connect(
            lambda x, z, _global: emitted.append((x, z))
        )

        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, target, target, target,
            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(press)
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease, target, target, target,
            Qt.MouseButton.RightButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(release)

        self.assertEqual(len(emitted), 1)
        self.assertAlmostEqual(emitted[0][0], expected[0])
        self.assertAlmostEqual(emitted[0][1], expected[1])
        self.assertNotEqual(emitted[0], (0.0, 0.0))

    def test_empty_space_right_click_uses_selection_context_when_selection_exists(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view(
            [(0.0, 0.0), (100.0, 0.0)],
            [[0, 1]],
            selected_index=0,
            selected_indices={0},
        )
        selection_menus = []
        empty_menus = []
        canvas.selectionContextMenuRequested.connect(
            lambda x, z, _global: selection_menus.append((x, z))
        )
        canvas.emptyContextMenuRequested.connect(
            lambda x, z, _global: empty_menus.append((x, z))
        )

        target = QPointF(560.0, 420.0)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, target, target, target,
            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(press)
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease, target, target, target,
            Qt.MouseButton.RightButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(release)

        self.assertEqual(len(selection_menus), 1)
        self.assertEqual(empty_menus, [])

    def test_selected_elements_panel_lists_all_selected_vertices_and_connections(self):
        window = self._window()
        model = create_minimal_sklt_model()
        model.points = [
            (0.0, 0.0, 0.0),
            (100.0, 0.0, 25.0),
            (200.0, 0.0, 50.0),
        ]
        model.polygons = [[0, 1], [1, 2]]
        window._current_model = model
        window.outline_editor.set_model(model)
        window.outline_editor.select_box({0, 2}, {(1, 2)}, False)
        window._update_edit_controls()

        items = [
            window.selected_elements_list.item(i).text()
            for i in range(window.selected_elements_list.count())
        ]
        self.assertTrue(any(text.startswith("Vertex 0 ") for text in items))
        self.assertTrue(any(text.startswith("Vertex 2 ") for text in items))
        self.assertIn("Connection 1 ↔ 2", items)

    def test_selection_context_menus_expose_copy(self):
        source = Path(__file__).resolve().parents[1].joinpath("outline_editor.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count('menu.addAction("Copy")'), 3)
        self.assertIn("selectionContextMenuRequested.emit", source)

    def test_wireframe_selected_vertices_use_model_editor_selection_red(self):
        self.assertEqual(MODEL_EDIT_SELECTION_RED, QColor(255, 32, 48))
        source = Path(__file__).resolve().parents[1].joinpath("outline_editor.py").read_text(encoding="utf-8")
        self.assertIn("painter.setBrush(MODEL_EDIT_SELECTION_RED)", source)

    def test_canvas_keeps_view_scale_while_points_move(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view([(0.0, 0.0), (100.0, 100.0)], [[0, 1]], 0)
        transform_before = canvas._make_transform()
        screen_before = canvas._to_screen((0.0, 0.0), transform_before)

        canvas.set_view([(20.0, 0.0), (100.0, 100.0)], [[0, 1]], 0)
        transform_after = canvas._make_transform()
        screen_after = canvas._to_screen((20.0, 0.0), transform_after)

        self.assertEqual(transform_after[1], transform_before[1])
        self.assertAlmostEqual(
            screen_after.x() - screen_before.x(), 20.0 * transform_before[1])
        self.assertAlmostEqual(screen_after.y(), screen_before.y())


    def _poo2_editor(self, points, polygons):
        model = create_minimal_sklt_model()
        model.points = list(points)
        model.polygons = [list(group) for group in polygons]
        editor = OutlineEditor()
        self.addCleanup(editor.close)
        editor.canvas.resize(640, 480)
        editor.set_model(model)
        return editor

    def test_structural_addition_refits_only_when_new_vertices_are_outside_view(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)],
            [[0, 1]],
        )
        old_bounds = editor.canvas._view_bounds
        self.assertIsNotNone(old_bounds)

        editor.add_shape_at_projected("triangle", 5000.0, 0.0)

        self.assertEqual(len(editor.projected_points), 5)
        self.assertEqual(len(editor.polygons), 2)
        self.assertNotEqual(editor.canvas._view_bounds, old_bounds)
        transform = editor.canvas._make_transform()
        visible = editor.canvas.rect().adjusted(20, 20, -20, -20)
        for index in editor._selected_indices:
            point = editor.canvas._to_screen(editor.canvas._points[index], transform).toPoint()
            self.assertTrue(visible.contains(point))

    def test_committed_overlap_does_not_implicitly_delete_vertex(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)],
            [[0, 1]],
        )
        editor.auto_align_check.setChecked(False)

        editor.move_projected_points({1: (0.0, 0.0)}, True)

        self.assertEqual(len(editor.projected_points), 2)
        self.assertEqual(editor.projected_points[0], editor.projected_points[1])
        self.assertEqual(editor.polygons, [[0, 1]])

    def test_generic_delete_of_selected_link_deletes_visually_selected_endpoints_too(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)],
            [[0, 1]],
        )
        editor.select_link(0, 1)

        editor.delete_selection()

        self.assertEqual(editor.projected_points, [])
        self.assertEqual(editor.polygons, [])

    def test_generic_delete_of_mixed_selection_removes_vertices_and_incident_links(self):
        editor = self._poo2_editor(
            [
                (0.0, 0.0, 0.0),
                (100.0, 0.0, 0.0),
                (200.0, 0.0, 0.0),
                (300.0, 0.0, 0.0),
            ],
            [[0, 1], [1, 2], [2, 3]],
        )
        editor.select_box({3}, {(0, 1)}, False)

        editor.delete_selection()

        # Selected edge endpoints 0/1 plus explicitly selected vertex 3 are
        # deleted.  Vertex 2 survives, and no incident link can remain.
        self.assertEqual(len(editor.projected_points), 1)
        self.assertEqual(editor.projected_points[0], (200.0, 0.0))
        self.assertEqual(editor.polygons, [])

    def test_linking_new_vertices_preserves_both_vertices(self):
        editor = self._poo2_editor([(0.0, 0.0, 0.0)], [])
        editor.add_point_at_projected(100.0, 0.0)
        first = editor.selected_index
        editor.add_point_at_projected(200.0, 0.0)
        second = editor.selected_index
        point_count = len(editor.projected_points)

        editor.select_point(first)
        editor.start_link()
        editor.select_point(second)

        self.assertEqual(len(editor.projected_points), point_count)
        self.assertTrue(editor._has_edge(first, second))

    def test_new_document_starts_empty_and_reset_returns_to_empty(self):
        window = self._window()
        window.new_file()

        self.assertEqual(window.outline_editor.save_mode, "poo2")
        self.assertEqual(window.outline_editor.projected_points, [])
        self.assertEqual(window.outline_editor.polygons, [])

        window.outline_editor.add_point_at_projected(100.0, 50.0)
        self.assertEqual(len(window.outline_editor.projected_points), 1)
        window.outline_editor.reset_to_loaded()

        self.assertEqual(window.outline_editor.projected_points, [])
        self.assertEqual(window.outline_editor.polygons, [])

    def test_dragging_linked_vertex_preserves_link_topology(self):
        editor = self._poo2_editor(
            [
                (0.0, 0.0, 0.0),
                (100.0, 0.0, 0.0),
                (200.0, 0.0, 0.0),
                (-100.0, 0.0, 0.0),
            ],
            [[3, 2]],
        )
        before_groups = editor.polygons

        editor.select_point(3)
        editor.move_projected_points({3: (-40.0, 80.0)}, False)
        editor.move_projected_points({3: (25.0, 60.0)}, True)

        self.assertEqual(editor.polygons, before_groups)
        self.assertTrue(editor._has_edge(3, 2))
        self.assertFalse(editor._has_edge(3, 0))

        editor.undo()
        self.assertEqual(editor.polygons, before_groups)
        self.assertTrue(editor._has_edge(3, 2))
        editor.redo()
        self.assertEqual(editor.polygons, before_groups)
        self.assertTrue(editor._has_edge(3, 2))

    def test_mouse_drag_vertex3_does_not_rewrite_its_link(self):
        editor = self._poo2_editor(
            [
                (0.0, 0.0, 0.0),
                (100.0, 0.0, 0.0),
                (200.0, 0.0, 0.0),
                (-100.0, 0.0, 0.0),
            ],
            [[3, 2]],
        )
        canvas = editor.canvas
        canvas.resize(640, 480)
        transform = canvas._make_transform()
        start = canvas._to_screen(canvas._points[3], transform)
        target = canvas._to_screen(canvas._points[0], transform)

        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, start, start, start,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(press)
        move = QMouseEvent(
            QEvent.Type.MouseMove, target, target, target,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseMoveEvent(move)
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease, target, target, target,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(release)

        self.assertEqual(editor.polygons, [[3, 2]])
        self.assertTrue(editor._has_edge(3, 2))
        self.assertFalse(editor._has_edge(3, 0))

    def test_reset_restores_loaded_wireframe_through_shared_history(self):
        editor = self._poo2_editor(
            [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)],
            [[0, 1]],
        )
        loaded_points = editor.projected_points
        loaded_groups = editor.polygons

        editor.select_point(1)
        editor.move_projected_points({1: (250.0, 50.0)}, True)
        editor.add_point_at_projected(300.0, 100.0)
        self.assertNotEqual(editor.projected_points, loaded_points)

        editor.reset_to_loaded()
        self.assertEqual(editor.projected_points, loaded_points)
        self.assertEqual(editor.polygons, loaded_groups)
        self.assertTrue(editor.can_undo)

        editor.undo()
        self.assertNotEqual(editor.projected_points, loaded_points)
        editor.redo()
        self.assertEqual(editor.projected_points, loaded_points)
        self.assertEqual(editor.polygons, loaded_groups)


    def test_auto_align_uses_grid_origin_as_snap_anchor(self):
        editor = self._poo2_editor(
            [(100.0, 0.0, 100.0)],
            [],
        )
        editor.auto_align_check.setChecked(True)
        editor.canvas.resize(640, 480)

        transform = editor.canvas._make_transform()
        scale = max(abs(transform[1]), 1.0e-9)
        near_origin = 5.0 / scale
        editor.select_point(0)
        editor.move_projected_points({0: (near_origin, -near_origin)}, True)

        self.assertAlmostEqual(editor.projected_points[0][0], 0.0)
        self.assertAlmostEqual(editor.projected_points[0][1], 0.0)
        self.assertIn("origin", editor._last_auto_align_message)

    def test_auto_align_near_connected_vertex_does_not_collapse_link(self):
        editor = self._poo2_editor(
            [
                (0.0, 0.0, 0.0),
                (100.0, 0.0, 0.0),
                (200.0, 0.0, 0.0),
                (-100.0, 0.0, 0.0),
            ],
            [[3, 2]],
        )
        editor.auto_align_check.setChecked(True)
        editor.canvas.resize(640, 480)

        # Old Auto Align snapped both coordinates onto v2 at commit, making
        # the v3-v2 connection zero-length and apparently disappear.
        editor.select_point(3)
        editor.move_projected_points({3: (199.0, 0.0)}, True)

        self.assertEqual(editor.polygons, [[3, 2]])
        self.assertTrue(editor._has_edge(3, 2))
        self.assertNotEqual(editor.projected_points[3], editor.projected_points[2])


    def test_first_vertex_here_preserves_empty_workspace_framing(self):
        editor = self._poo2_editor([], [])
        editor.canvas.resize(640, 480)
        before_bounds = editor.canvas._view_bounds
        before_zoom = editor.canvas._zoom
        before_pan = QPointF(editor.canvas._pan)
        target_screen = QPointF(500.0, 160.0)
        target_world = editor.canvas._from_screen(target_screen)

        editor.add_point_at_projected(*target_world)

        self.assertEqual(editor.canvas._view_bounds, before_bounds)
        self.assertEqual(editor.canvas._zoom, before_zoom)
        self.assertEqual(editor.canvas._pan, before_pan)
        transform = editor.canvas._make_transform()
        actual_screen = editor.canvas._to_screen(
            editor.canvas._points[editor.selected_index], transform
        )
        self.assertAlmostEqual(actual_screen.x(), target_screen.x(), delta=1.0)
        self.assertAlmostEqual(actual_screen.y(), target_screen.y(), delta=1.0)

    def test_reset_is_disabled_until_geometry_differs_from_loaded_state(self):
        editor = self._poo2_editor([], [])
        self.assertFalse(editor.can_reset)
        self.assertFalse(editor.reset_button.isEnabled())

        editor.add_point_at_projected(25.0, 40.0)
        self.assertTrue(editor.can_reset)
        self.assertTrue(editor.reset_button.isEnabled())

        editor.reset_to_loaded()
        self.assertFalse(editor.can_reset)
        self.assertFalse(editor.reset_button.isEnabled())

    def test_empty_canvas_keeps_origin_centered_for_grid_workspace(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view([], [], -1, "", False)
        transform = canvas._make_transform()
        origin = canvas._to_screen((0.0, 0.0), transform)

        self.assertAlmostEqual(origin.x(), 320.0, delta=1.0)
        self.assertAlmostEqual(origin.y(), 240.0, delta=1.0)
        self.assertEqual(canvas._message, "")

    def test_grid_paint_defines_axis_pen(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view([(0.0, 0.0), (100.0, 100.0)], [[0, 1]], 0)
        image = QImage(640, 480, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            canvas._draw_grid(painter, canvas._make_transform())
        finally:
            painter.end()

    def test_canvas_drag_uses_mouse_grab_point_and_blocks_wheel_zoom(self):
        canvas = OutlineCanvas()
        canvas.resize(640, 480)
        canvas.set_view([(0.0, 0.0), (100.0, 100.0)], [[0, 1]], 0)
        transform = canvas._make_transform()
        vertex_screen = canvas._to_screen((0.0, 0.0), transform)
        press_position = vertex_screen + QPointF(5.0, 4.0)
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            press_position,
            press_position,
            press_position,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mousePressEvent(press)
        self.assertEqual(canvas._drag_start_screen, press_position)

        moved = []
        canvas.pointsMoved.connect(lambda positions, _commit: moved.append(positions))
        move_position = press_position + QPointF(30.0, -15.0)
        move = QMouseEvent(
            QEvent.Type.MouseMove,
            move_position,
            move_position,
            move_position,
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseMoveEvent(move)
        self.assertTrue(moved)
        self.assertAlmostEqual(
            moved[-1][0][0], 30.0 / transform[1])
        self.assertAlmostEqual(
            moved[-1][0][1], 15.0 / transform[1])

        wheel = type("WheelEvent", (), {"accept": lambda self: None})()
        previous_zoom = canvas._zoom
        canvas.wheelEvent(wheel)
        self.assertEqual(canvas._zoom, previous_zoom)

        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            move_position,
            move_position,
            move_position,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        canvas.mouseReleaseEvent(release)


if __name__ == "__main__":
    unittest.main()
