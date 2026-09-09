import unittest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from uv_editor_widget import UVEditorWidget, UVLoop


class UVEditorToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = UVEditorWidget()
        self.widget.resize(400, 400)
        self.widget.set_loops(None, [
            UVLoop("a", 1, [(80, 80), (120, 80), (120, 120), (80, 120)]),
            UVLoop("b", 2, [(160, 160), (200, 160), (200, 200), (160, 200)]),
        ])

    def tearDown(self):
        self.widget.close()

    def test_alternating_palette_and_opposite_selected_handles(self):
        self.assertEqual(self.widget.loop_color("a").name(), "#ffdc00")
        self.assertEqual(self.widget.loop_color("b").name(), "#00dcff")
        self.widget.select_handle("a", 0)
        self.widget.select_handle("b", 0, additive=True)
        self.assertEqual(
            self.widget._opposite_loop_color(self.widget.loop_color("a")).name(),
            "#00dcff")
        self.assertEqual(
            self.widget._opposite_loop_color(self.widget.loop_color("b")).name(),
            "#ffdc00")

    def test_rotate_scale_and_flip_are_single_transactions(self):
        self.widget.select_handles({("a", 0), ("a", 1)})
        events = []
        self.widget.loopsChanged.connect(lambda value: events.append(value))
        self.widget.editFinished.connect(lambda: events.append("finished"))
        self.assertTrue(self.widget.rotate_selected(90))
        self.assertEqual(self.widget.loop_uvs()["a"][:2], [(100, 60), (100, 100)])
        self.assertEqual(events[-1], "finished")
        self.assertTrue(self.widget.scale_selected(200))
        self.assertFalse(self.widget.flip_selected(horizontal=True))
        self.assertEqual(len(events), 4)

    def test_invalid_transform_is_atomic_and_rejects_collapse(self):
        self.widget.select_all()
        before = self.widget.loop_uvs()
        self.assertFalse(self.widget.scale_selected(-100))
        self.assertEqual(self.widget.loop_uvs(), before)
        self.widget.select_handles({("a", 0), ("a", 1)})
        before = self.widget.loop_uvs()
        self.assertFalse(self.widget.rotate_selected(float("nan")))
        self.assertEqual(self.widget.loop_uvs(), before)

    def test_fit_selection_and_cursor_anchored_zoom(self):
        self.widget.select_handle("a", 0)
        self.widget.fit_selection()
        self.assertGreaterEqual(self.widget._zoom, 1.0)
        self.widget.reset_view()
        cursor = QPointF(100, 140)
        before = self.widget._screen_to_uv(cursor)
        event = QWheelEvent(
            cursor, cursor, QPoint(0, 120), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False)
        self.widget.wheelEvent(event)
        after = self.widget._screen_to_uv(cursor)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
