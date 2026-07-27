import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from uv_editor_widget import UVEditorWidget


class UVMarqueeV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = UVEditorWidget()
        self.widget.resize(360, 360)
        self.widget.set_data(
            None,
            [(40, 40), (100, 40), (100, 100), (40, 100)],
            True,
            "test",
        )
        self.widget.show()
        QApplication.processEvents()

    def tearDown(self):
        self.widget.close()

    def _drag(self, start: QPointF, end: QPointF, modifiers=None):
        modifiers = modifiers or Qt.KeyboardModifier.NoModifier
        QTest.mousePress(
            self.widget, Qt.MouseButton.LeftButton, modifiers,
            start.toPoint())
        QTest.mouseMove(self.widget, end.toPoint(), delay=1)
        QTest.mouseRelease(
            self.widget, Qt.MouseButton.LeftButton, modifiers,
            end.toPoint())

    def test_marquee_replaces_ctrl_adds_and_empty_click_clears(self):
        first = self.widget._uv_to_screen((40, 40))
        second = self.widget._uv_to_screen((100, 40))
        third = self.widget._uv_to_screen((100, 100))

        self._drag(
            QPointF(first.x() - 18, first.y() - 18),
            QPointF(second.x() + 18, second.y() + 18))
        self.assertEqual(self.widget.selected_points(), {0, 1})

        self._drag(
            QPointF(third.x() - 14, third.y() - 14),
            QPointF(third.x() + 14, third.y() + 14),
            Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.widget.selected_points(), {0, 1, 2})

        QTest.mouseClick(
            self.widget, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            QPointF(self.widget.width() - 8, 8).toPoint())
        self.assertEqual(self.widget.selected_points(), set())

    def test_group_drag_and_keyboard_nudge_survive_marquee(self):
        first = self.widget._uv_to_screen((40, 40))
        second = self.widget._uv_to_screen((100, 40))
        self._drag(
            QPointF(first.x() - 16, first.y() - 16),
            QPointF(second.x() + 16, second.y() + 16))
        before = self.widget.uvs()
        self._drag(first, first + QPointF(22, 15))
        after = self.widget.uvs()
        delta0 = (after[0][0] - before[0][0],
                  after[0][1] - before[0][1])
        delta1 = (after[1][0] - before[1][0],
                  after[1][1] - before[1][1])
        self.assertEqual(delta0, delta1)
        self.assertNotEqual(delta0, (0, 0))
        self.assertEqual(after[2:], before[2:])

        self.widget.nudge_selected(1, -1)
        nudged = self.widget.uvs()
        self.assertEqual(nudged[0][0], after[0][0] + 1)
        self.assertEqual(nudged[1][0], after[1][0] + 1)

    def test_marquee_uses_screen_coordinates_after_zoom_and_pan(self):
        self.widget._zoom = 2.4
        self.widget._pan = QPointF(31.0, -22.0)
        first = self.widget._uv_to_screen((40, 40))
        fourth = self.widget._uv_to_screen((40, 100))
        self._drag(
            QPointF(first.x() - 15, first.y() - 15),
            QPointF(fourth.x() + 15, fourth.y() + 15))
        self.assertEqual(self.widget.selected_points(), {0, 3})

    def test_click_and_ctrl_click_handle_semantics(self):
        first = self.widget._uv_to_screen((40, 40)).toPoint()
        second = self.widget._uv_to_screen((100, 40)).toPoint()
        QTest.mouseClick(
            self.widget, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, first)
        self.assertEqual(self.widget.selected_points(), {0})
        QTest.mouseClick(
            self.widget, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier, second)
        self.assertEqual(self.widget.selected_points(), {0, 1})
        QTest.mouseClick(
            self.widget, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier, first)
        self.assertEqual(self.widget.selected_points(), {1})


if __name__ == "__main__":
    unittest.main()
