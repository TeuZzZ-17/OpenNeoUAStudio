import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from tests.test_geometry_paste_preview import _fixture
from tests.test_selection_runtime_v3 import _prepare_window


def mouse(view, kind, point, modifiers=Qt.KeyboardModifier.NoModifier):
    button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
    event = QMouseEvent(kind, QPointF(point), QPointF(point), button, buttons, modifiers)
    {QEvent.Type.MouseButtonPress: view.mousePressEvent,
     QEvent.Type.MouseMove: view.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


class ModelMouseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_paste_short_click_confirms_only_on_release(self):
        view, model, clipboard = _fixture()
        self.addCleanup(view.close)
        view.begin_paste_preview(clipboard, QPoint(100, 100))
        confirmed = []
        view.pastePreviewConfirmRequested.connect(lambda: confirmed.append(True))
        with patch('assembly_viewer.time.monotonic', return_value=10):
            mouse(view, QEvent.Type.MouseButtonPress, QPoint(100, 100))
        self.assertEqual(confirmed, [])
        with patch('assembly_viewer.time.monotonic', return_value=10.1):
            mouse(view, QEvent.Type.MouseButtonRelease, QPoint(100, 100))
        self.assertEqual(confirmed, [True])

    def test_hold_orbits_without_commit_or_geometry_change(self):
        view, model, clipboard = _fixture()
        self.addCleanup(view.close)
        before = list(model.points)
        view.begin_paste_preview(clipboard, QPoint(100, 100))
        translation = view.paste_preview_delta
        yaw = view._yaw
        confirmed = []
        view.pastePreviewConfirmRequested.connect(lambda: confirmed.append(True))
        with patch('assembly_viewer.time.monotonic', return_value=10):
            mouse(view, QEvent.Type.MouseButtonPress, QPoint(100, 100))
        with patch('assembly_viewer.time.monotonic', return_value=10.4):
            mouse(view, QEvent.Type.MouseMove, QPoint(125, 110))
            mouse(view, QEvent.Type.MouseButtonRelease, QPoint(125, 110))
        self.assertNotEqual(view._yaw, yaw)
        self.assertTrue(view.paste_preview_active)
        self.assertEqual(view.paste_preview_delta, translation)
        self.assertEqual(model.points, before)
        self.assertEqual(confirmed, [])
        with patch('assembly_viewer.time.monotonic', return_value=11):
            mouse(view, QEvent.Type.MouseButtonPress, QPoint(125, 110))
        with patch('assembly_viewer.time.monotonic', return_value=11.1):
            mouse(view, QEvent.Type.MouseButtonRelease, QPoint(125, 110))
        self.assertEqual(confirmed, [True])

    def test_plain_drag_selects_vertices_and_ctrl_extends(self):
        view, model, _ = _fixture()
        self.addCleanup(view.close)
        view.drag_select_enabled = True
        points = [QPointF(30, 30), QPointF(60, 60), QPointF(150, 150)]
        yaw = view._yaw
        with patch.object(view, '_edit_screen_points', return_value=points), \
                patch.object(view, '_available_edit_vertices', return_value={0, 1, 2}):
            mouse(view, QEvent.Type.MouseButtonPress, QPoint(20, 20))
            mouse(view, QEvent.Type.MouseMove, QPoint(80, 80))
            mouse(view, QEvent.Type.MouseButtonRelease, QPoint(80, 80))
            self.assertEqual(view.edit_session.selection, {0, 1})
            ctrl = Qt.KeyboardModifier.ControlModifier
            mouse(view, QEvent.Type.MouseButtonPress, QPoint(140, 140), ctrl)
            mouse(view, QEvent.Type.MouseMove, QPoint(170, 170), ctrl)
            mouse(view, QEvent.Type.MouseButtonRelease, QPoint(170, 170), ctrl)
        self.assertEqual(view.edit_session.selection, {0, 1, 2})
        self.assertEqual(view._yaw, yaw)
        self.assertFalse(view.edit_session.dirty)

    def test_move_action_uses_existing_modal_and_cancel_restores_points(self):
        window, obj, model = _prepare_window()
        self.addCleanup(window.close)
        view = window.viewport
        view.set_auto_align_enabled(False)
        view.edit_session.selection = {0, 1}
        before = list(model.points)
        window._sync_edit_action_states()
        self.assertTrue(window.edit_move_action.isEnabled())
        window.edit_move_action.trigger()
        self.assertEqual(view._modal_op, 'grab')
        view._update_modal(view._modal_start + QPoint(25, 10))
        self.assertNotEqual(view.edit_session.points(), before)
        self.assertEqual(model.points, before)
        view._cancel_modal()
        self.assertEqual(model.points, before)
        window.edit_move_action.trigger()
        view._update_modal(view._modal_start + QPoint(25, 10))
        view._commit_modal()
        self.assertNotEqual(model.points, before)
        self.assertTrue(view.edit_session.can_undo)
        moved = list(model.points)
        window._undo_edit()
        self.assertEqual(model.points, before)
        window._redo_edit()
        self.assertEqual(model.points, moved)

    def test_alt_drag_keeps_camera_navigation_in_model_edit_mode(self):
        view, model, _ = _fixture()
        self.addCleanup(view.close)
        view.drag_select_enabled = True
        yaw = view._yaw
        alt = Qt.KeyboardModifier.AltModifier
        mouse(view, QEvent.Type.MouseButtonPress, QPoint(20, 20), alt)
        mouse(view, QEvent.Type.MouseMove, QPoint(80, 80), alt)
        mouse(view, QEvent.Type.MouseButtonRelease, QPoint(80, 80), alt)
        self.assertNotEqual(view._yaw, yaw)
        self.assertFalse(view.edit_session.selection)
        self.assertFalse(view.edit_session.dirty)


if __name__ == '__main__':
    unittest.main()
