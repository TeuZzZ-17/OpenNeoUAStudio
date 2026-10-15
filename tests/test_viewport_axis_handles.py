import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.test_selection_runtime_v3 import _prepare_window


def _point_on_handle(viewport, axis, fraction=0.7):
    center, tip = viewport._gizmo_handles()[axis]
    return QPoint(
        round(center.x() + (tip.x() - center.x()) * fraction),
        round(center.y() + (tip.y() - center.y()) * fraction))


class ViewportAxisHandleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        window, obj, model = _prepare_window()
        window.resize(1200, 800)
        window.show()
        for _ in range(5):
            self.app.processEvents()
        window.viewport.select_all_edit_vertices()
        window.viewport.set_direct_transform("move", 0.1)
        return window, model

    def test_handles_exist_only_with_a_selection(self):
        window, _model = self._window()
        try:
            self.assertTrue(window.viewport._gizmo_handles())
            window.viewport.select_no_edit_vertices()
            self.assertEqual(window.viewport._gizmo_handles(), {})
        finally:
            window.close()

    def test_dragging_a_handle_moves_only_along_that_axis_and_undo_restores(self):
        window, model = self._window()
        try:
            viewport = window.viewport
            original = list(model.points)
            start = _point_on_handle(viewport, "X")
            center, tip = viewport._gizmo_handles()["X"]
            direction = tip - center
            end = start + QPoint(round(direction.x() * 0.8),
                                 round(direction.y() * 0.8))
            self.assertEqual(viewport._gizmo_axis_at(start), "X")

            QTest.mousePress(viewport, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, start)
            self.assertEqual(viewport._gizmo_drag_axis, "X")
            QTest.mouseMove(viewport, end)
            QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier, end)

            moved = [tuple(round(a - b, 6) for a, b in zip(new, old))
                     for new, old in zip(model.points, original)]
            self.assertTrue(any(offset[0] != 0.0 for offset in moved))
            self.assertTrue(all(offset[1] == 0.0 and offset[2] == 0.0
                                for offset in moved))
            self.assertEqual(len(set(moved)), 1)
            self.assertEqual(len(window._edit_undo_stack), 1)
            self.assertIsNone(viewport._gizmo_drag_axis)

            window._undo_edit()
            self.assertEqual(model.points, original)
        finally:
            window.close()

    def test_dead_zone_next_to_the_pivot_is_not_grabbable(self):
        window, _model = self._window()
        try:
            viewport = window.viewport
            center, tip = viewport._gizmo_handles()["Y"]
            near = QPoint(round(center.x() + (tip.x() - center.x()) * 0.1),
                          round(center.y() + (tip.y() - center.y()) * 0.1))
            self.assertIsNone(viewport._gizmo_axis_at(near))
        finally:
            window.close()

    def test_ctrl_click_keeps_additive_selection_instead_of_grabbing(self):
        window, _model = self._window()
        try:
            viewport = window.viewport
            point = _point_on_handle(viewport, "Z")
            QTest.mousePress(viewport, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ControlModifier, point)
            self.assertIsNone(viewport._gizmo_drag_axis)
            QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.ControlModifier, point)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
