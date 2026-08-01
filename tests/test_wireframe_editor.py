import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from outline_editor import OutlineCanvas
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
            labels, ["New", "Import", "Export", "Export As...", "Exit"])

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
        self.assertEqual(window.edit_mode_value.text(), "No editable geometry")
        self.assertEqual(window.status_value.text(), "Ready to import a file")
        self.assertEqual(window.selection_value.text(), "No selection")
        self.assertFalse(hasattr(window, "chunk_tree"))
        self.assertFalse(hasattr(window, "rendered_polygons_value"))

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
