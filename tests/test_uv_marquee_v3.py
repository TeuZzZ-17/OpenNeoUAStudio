import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from uv_editor_widget import UVEditorWidget, UVLoop


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

    def test_auto_align_coordinates_spacing_and_conservative_square(self):
        self.widget.set_data(
            None, [(40, 40), (100, 40), (70, 100)], True, "triangle")
        handle = (0, 2)
        self.widget._drag_start_uvs = {handle: (70, 100)}
        delta = self.widget._snap_drag_delta(
            {handle}, 27, -57)
        self.assertEqual(delta, (30, -60))
        self.assertEqual(
            {(axis, value) for axis, value, _label
             in self.widget.snap_guides},
            {("u", 100), ("v", 40)})

        self.widget.set_data(
            None, [(40, 40), (80, 90), (130, 140)], True, "spacing")
        handle = (0, 2)
        self.widget._drag_start_uvs = {handle: (130, 140)}
        delta = self.widget._snap_drag_delta(
            {handle}, -9, 0, allow_v=False)
        self.assertEqual(delta[0], -10)
        self.assertTrue(any(
            label.endswith("to equal spacing")
            for _axis, _value, label in self.widget.snap_guides))

        loop = UVLoop(
            "square", 0,
            [(14, 12), (50, 10), (50, 50), (10, 50)])
        self.assertEqual(
            self.widget._near_square_target(
                loop, 0, (12, 11), threshold_uv=6.0),
            (10, 10))
        self.assertIsNone(self.widget._near_square_target(
            loop, 0, (30, 30), threshold_uv=6.0))
        rectangle = UVLoop(
            "rectangle", 0,
            [(14, 12), (90, 10), (90, 50), (10, 50)])
        self.assertEqual(
            self.widget._near_quad_target(
                rectangle, 0, (12, 11), threshold_uv=6.0),
            ((10, 10), "rectangle"))

    def test_alt_bypasses_uv_snap_for_current_drag(self):
        self.widget.select_handle(0, 0)
        handle = (0, 0)
        self.widget._dragging = True
        self.widget._drag_anchor_uv = (40, 40)
        self.widget._drag_start_uvs = {handle: (40, 40)}
        target = self.widget._uv_to_screen((43, 42))
        event = QMouseEvent(
            QEvent.Type.MouseMove, target, target, target,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.AltModifier)
        self.widget.mouseMoveEvent(event)
        self.assertEqual(self.widget.uvs()[0], (43, 42))
        self.assertEqual(self.widget.snap_guides, ())

    def test_multi_loop_select_all_nudge_and_colors(self):
        loops = [
            UVLoop(("root", 0, 0), 3,
                   [(20, 30), (80, 30), (50, 90)]),
            UVLoop(("root", 1, 0), 7,
                   [(130, 140), (190, 140), (190, 200), (130, 200)]),
        ]
        self.widget.set_loops(None, loops, "multi")
        finished = []
        changed = []
        self.widget.editFinished.connect(lambda: finished.append(True))
        self.widget.loopsChanged.connect(lambda payload: changed.append(payload))

        self.assertNotEqual(
            self.widget.loop_color(loops[0].key).name(),
            self.widget.loop_color(loops[1].key).name())
        self.widget.select_all()
        self.assertEqual(len(self.widget.selected_handles()), 7)
        before = self.widget.loop_uvs()
        self.widget.nudge_selected(4, -3)
        after = self.widget.loop_uvs()

        for key, old_loop in before.items():
            self.assertEqual(
                after[key],
                [(u + 4, v - 3) for u, v in old_loop])
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(finished), 1)

    def test_right_click_handle_emits_context_identity(self):
        key = ("root", 4, 2)
        self.widget.set_loops(
            None, [UVLoop(key, 9, [(40, 40), (100, 40), (70, 100)])],
            "context")
        requested = []
        self.widget.handleContextMenuRequested.connect(
            lambda loop_key, index, position:
            requested.append((loop_key, index, position)))
        point = self.widget._uv_to_screen((100, 40)).toPoint()

        QTest.mouseClick(
            self.widget, Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier, point)

        self.assertEqual(requested[0][:2], (key, 1))


if __name__ == "__main__":
    unittest.main()
