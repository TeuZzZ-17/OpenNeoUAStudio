"""Event-level regressions for transforms, clipboard placement and shared commands."""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLineEdit, QMenu, QMessageBox

from sklt_parser import create_minimal_sklt_model, parse_sklt_bytes, parse_sklt_file, save_sklt_with_poo2_pol2_structure
from wireframe_editor.window import WireframeEditorWindow


class WireframeTransformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = WireframeEditorWindow()
        self.editor = self.window.outline_editor
        self.canvas = self.editor.canvas
        model = create_minimal_sklt_model()
        model.points = [(-100.0, 7.0, 0.0), (100.0, 9.0, 0.0), (200.0, 13.0, 100.0)]
        model.polygons = [[0, 1], [1, 2]]
        self.window._show_model(model, Path("test.SKL"))
        self.editor.auto_align_check.setChecked(False)
        self.window.show()
        self.window.activateWindow()
        self.canvas.setFocus()
        self.app.processEvents()
        self.addCleanup(self.window.close)
        self.addCleanup(self.editor.mark_clean)

    def mouse(self, kind, position, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        global_pos = QPointF(self.canvas.mapToGlobal(position.toPoint()))
        event = QMouseEvent(kind, position, position, global_pos, button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.canvas, event)
        self.app.processEvents()

    def screen(self, point):
        return self.canvas._to_screen(point, self.canvas._make_transform())

    def begin(self, point):
        self.mouse(QEvent.Type.MouseButtonPress, self.screen(point), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)

    def move(self, point):
        self.mouse(QEvent.Type.MouseMove, self.screen(point), buttons=Qt.MouseButton.LeftButton)

    def release(self, point):
        self.mouse(QEvent.Type.MouseButtonRelease, self.screen(point), Qt.MouseButton.LeftButton)

    def key(self, key, modifiers=Qt.KeyboardModifier.NoModifier, widget=None):
        QTest.keyClick(widget or self.canvas, key, modifiers)
        self.app.processEvents()

    def assert_points(self, expected):
        self.assertEqual(len(self.editor.projected_points), len(expected))
        for actual, target in zip(self.editor.projected_points, expected):
            for a, b in zip(actual, target):
                self.assertAlmostEqual(a, b, places=5)

    def test_mouse_transforms_share_history_and_preserve_shared_topology(self):
        for mode, destination, expected in (
            ("move", (150, 40), [(-50, 7, 40), (150, 9, 40), (200, 13, 100)]),
            ("rotate", (0, 100), [(0, 7, -100), (0, 9, 100), (200, 13, 100)]),
            ("scale", (200, 0), [(-200, 7, 0), (200, 9, 0), (200, 13, 100)]),
        ):
            with self.subTest(mode=mode):
                self.editor.select_link(0, 1)
                self.editor.set_transform_mode(mode)
                before = self.editor.projected_points
                self.assertEqual(self.canvas.selection_pivot(), (0, 0))
                self.begin((100, 0))  # Endpoint of selected edge retains both endpoints.
                self.move(destination)
                self.move(destination)
                self.assertEqual(len(self.editor._undo_stack), 0)
                self.release(destination)
                self.assert_points(expected)
                self.assertEqual(self.editor.polygons, [[0, 1], [1, 2]])
                self.assertEqual(len(self.editor._undo_stack), 1)
                self.assertFalse(self.canvas._snap_preview_pairs)
                self.editor.undo()
                self.assertEqual(self.editor.projected_points, before)
                self.editor.redo()
                self.assert_points(expected)
                self.editor.undo()

    def test_single_vertex_move_and_cancel_each_transform(self):
        self.editor.select_point(0)
        self.begin((-100, 0))
        self.move((-70, 25))
        self.release((-70, 25))
        self.assert_points([(-70, 7, 25), (100, 9, 0), (200, 13, 100)])
        self.editor.undo()
        self.editor.select_box({0, 1}, set(), False)
        for mode in ("move", "rotate", "scale"):
            with self.subTest(mode=mode):
                self.editor.set_transform_mode(mode)
                before = self.editor._snapshot()
                self.begin((100, 0))
                self.move((150, 50))
                self.key(Qt.Key.Key_Escape)
                self.release((150, 50))
                self.assertEqual(self.editor._snapshot(), before)
                self.assertFalse(self.editor.is_dirty)
                self.assertEqual(len(self.editor._undo_stack), 0)

    def test_click_without_drag_does_not_snap_or_dirty(self):
        self.editor.auto_align_check.setChecked(True)
        before = self.editor.projected_points
        self.begin((100, 0))
        self.release((100, 0))
        self.assertEqual(self.editor.projected_points, before)
        self.assertFalse(self.editor.can_undo)
        self.assertIsNone(self.editor._drag_start_state)

    def test_modes_panel_edit_context_and_shortcuts_are_synchronized(self):
        before = self.editor.projected_points
        self.window.transform_buttons["rotate"].click()
        self.assertEqual(self.canvas.transform_mode, "rotate")
        self.assertIn(self.editor.transform_actions["rotate"], self.window.edit_menu.actions())
        self.editor.transform_actions["scale"].trigger()
        self.assertTrue(self.window.transform_buttons["scale"].isChecked())

        def choose_move(menu, *args):
            action = self.editor.transform_actions["move"]
            self.assertIn(action, menu.actions())
            action.trigger()
            return action

        self.editor.select_link(0, 1)
        with patch("outline_editor.QMenu", type("TestMenu", (QMenu,), {"exec": choose_move})):
            for handler, args in (
                (self.editor._show_selection_context_menu, (0, 0, QPoint())),
                (self.editor._show_point_context_menu, (0, -100, 0, QPoint())),
                (self.editor._show_link_context_menu, (0, 1, 0, 0, QPoint())),
                (self.editor._show_empty_context_menu, (0, 0, QPoint())),
            ):
                handler(*args)
                self.assertEqual(self.editor.selected_edges, ((0, 1),))
        self.assertTrue(self.window.transform_buttons["move"].isChecked())
        for key, mode in ((Qt.Key.Key_R, "rotate"), (Qt.Key.Key_S, "scale"), (Qt.Key.Key_G, "move")):
            self.key(key)
            self.assertEqual(self.canvas.transform_mode, mode)
            self.assertEqual([m for m, a in self.editor.transform_actions.items() if a.isChecked()], [mode])
        self.assertEqual(self.editor.projected_points, before)
        self.assertFalse(self.editor.is_dirty)

    def test_copy_ghost_follows_mouse_pastes_at_preview_and_esc_keeps_clipboard(self):
        self.editor.select_link(0, 1)
        before = self.editor.projected_points
        self.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.editor.projected_points, before)
        self.assertFalse(self.editor.is_dirty)
        self.mouse(QEvent.Type.MouseMove, self.screen((35, 80)))
        ghost = list(self.canvas.ghost_points)
        self.assertEqual(len(ghost), 2)
        self.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual([(p[0], p[2]) for p in self.editor.projected_points[3:]], ghost)
        self.assertFalse(self.canvas.ghost_points)
        self.assertEqual(self.editor.polygons[-1], [3, 4])
        self.editor.undo()
        self.assertEqual(self.editor.projected_points, before)
        self.editor.copy_selection()
        payload = self.editor._clipboard
        self.key(Qt.Key.Key_Escape)
        self.assertIs(self.editor._clipboard, payload)
        self.assertFalse(self.canvas.ghost_points)

    def test_cut_empty_canvas_ghost_context_paste_and_undo_redo(self):
        self.key(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        original = self.editor.projected_points
        self.key(Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.editor.projected_points, [])
        self.mouse(QEvent.Type.MouseMove, self.screen((70, 80)))
        ghost = list(self.canvas.ghost_points)
        self.assertEqual(len(ghost), 3)
        rendered = QImage(self.canvas.size(), QImage.Format.Format_ARGB32)
        self.canvas.render(rendered)
        self.assertFalse(rendered.isNull())

        def choose_paste(menu, *args):
            return next(action for action in menu.actions() if action.text() == "Paste")

        with patch("outline_editor.QMenu", type("TestMenu", (QMenu,), {"exec": choose_paste})):
            self.editor._show_empty_context_menu(-500, -500, QPoint())
        self.assertEqual([(p[0], p[2]) for p in self.editor.projected_points], ghost)
        self.assertEqual(len(self.editor._undo_stack), 2)
        self.editor.undo()
        self.assertFalse(self.editor.projected_points)
        self.editor.undo()
        self.assertEqual(self.editor.projected_points, original)
        self.editor.redo()
        self.editor.redo()
        self.assertEqual([(p[0], p[2]) for p in self.editor.projected_points], ghost)

    def test_auto_align_guides_and_ghost_respect_toggle(self):
        self.editor.auto_align_check.setChecked(True)
        self.editor.select_point(0)
        self.begin((-100, 0))
        self.move((198, 98))
        self.assert_points([(200, 7, 100), (100, 9, 0), (200, 13, 100)])
        self.assertIn(((200, 100), (200, 100)), self.canvas._snap_preview_pairs)
        self.editor.auto_align_check.setChecked(False)
        self.assertFalse(self.canvas._snap_preview_pairs)
        self.move((198, 98))
        self.assertAlmostEqual(self.editor.projected_points[0][0], 198)
        self.key(Qt.Key.Key_Escape)
        self.editor.copy_selection()
        self.mouse(QEvent.Type.MouseMove, self.screen((198, 98)))
        self.assertAlmostEqual(self.editor._ghost_target[0], 198)
        self.editor.auto_align_check.setChecked(True)
        self.assertEqual(self.editor._ghost_target, (200, 100))

    def test_scale_never_collapses_and_rotation_ignores_move_snap(self):
        self.editor.select_link(0, 1)
        self.editor.set_transform_mode("scale")
        self.begin((100, 0))
        self.move((0, 0))
        self.release((0, 0))
        self.assertNotEqual(self.editor.projected_points[0][0], self.editor.projected_points[1][0])
        self.editor.undo()
        self.editor.auto_align_check.setChecked(True)
        self.editor.set_transform_mode("rotate")
        self.begin((100, 0))
        self.move((100, 1))
        self.release((100, 1))
        self.assertNotEqual(self.editor.projected_points[1][2], 0)
        self.assertFalse(self.canvas._snap_preview_pairs)

    def test_shortcuts_leave_text_and_numeric_editing_to_control(self):
        line = QLineEdit(self.window.centralWidget())
        line.show()
        line.setFocus()
        line.setText("original")
        line.selectAll()
        self.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier, line)
        self.assertEqual(QApplication.clipboard().text(), "original")
        self.assertFalse(self.editor.has_clipboard)
        for key in (Qt.Key.Key_R, Qt.Key.Key_S, Qt.Key.Key_G):
            self.key(key, widget=line)
        self.assertEqual(line.text(), "rsg")
        self.assertEqual(self.canvas.transform_mode, "move")
        self.key(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier, line)
        self.key(Qt.Key.Key_Delete, widget=line)
        self.assertEqual(line.text(), "")
        self.assertEqual(self.editor.editable_point_count, 3)
        spin = QDoubleSpinBox(self.window.centralWidget())
        spin.show()
        spin.setFocus()
        spin.selectAll()
        self.key(Qt.Key.Key_S, widget=spin)
        self.key(Qt.Key.Key_Escape, widget=spin)
        self.assertEqual(self.canvas.transform_mode, "move")
        self.assertEqual(self.editor.selected_vertex_indices, (0,))
        self.assertFalse(self.editor.is_dirty)

    def test_export_shortcuts_roundtrip_overwrite_undo_redo_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "roundtrip.SKL"
            source = Path(directory) / "source.SKL"
            save_sklt_with_poo2_pol2_structure(create_minimal_sklt_model(), self.editor.projected_points,
                                              self.editor.polygons, source)
            self.window._show_model(parse_sklt_file(source), source)
            self.editor.copy_selection()  # A visible ghost must never enter the exported document.
            with patch("wireframe_editor.window.QFileDialog") as dialog, patch("wireframe_editor.window.QMessageBox"), patch("outline_editor.QMessageBox") as message:
                dialog.getSaveFileName.return_value = (str(target), "")
                message.StandardButton = QMessageBox.StandardButton
                message.question.return_value = QMessageBox.StandardButton.Yes
                self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
                self.assertTrue(target.exists())
                self.assertEqual(parse_sklt_file(target).points, self.editor.projected_points)
                self.editor.select_point(0)
                self.begin((-100, 0))
                self.move((-80, 20))
                self.release((-80, 20))
                edited = self.editor.projected_points
                self.key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
                self.assertNotEqual(self.editor.projected_points, edited)
                self.key(Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
                self.assertEqual(self.editor.projected_points, edited)
                self.key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
                self.key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
                self.assertEqual(self.editor.projected_points, edited)
                self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
                self.assert_points(edited)
                self.assertEqual(parse_sklt_file(target).points, self.editor.projected_points)
                self.assertFalse(self.editor.is_dirty)
                self.assertEqual(parse_sklt_file(target).polygons, [[0, 1], [1, 2]])
                self.key(Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                self.key(Qt.Key.Key_Delete)
                self.assertFalse(self.editor.projected_points)
                self.key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
                self.assert_points(edited)

    def test_warning_status_uses_parser_warnings_and_open_is_clean(self):
        self.window._current_model.warnings = ["First warning", "Second warning"]
        self.window._show_model(self.window._current_model, Path("warnings.SKL"))
        self.assertEqual(self.window.warning_status_label.toolTip(), "First warning\nSecond warning")
        self.assertFalse(self.window.warning_status_label.isHidden())
        self.assertFalse(self.editor.is_dirty)

    def test_otl2_transform_limits_history_and_byte_preserving_export(self):
        def chunk(tag, payload):
            return tag + struct.pack(">I", len(payload)) + payload

        contents = b"SKLT" + chunk(b"OTL2", bytes([80, 100, 120, 100])) + chunk(b"OLPL", struct.pack(">3H", 2, 0, 1))
        contents += chunk(b"TEST", b"keep")
        raw = b"FORM" + struct.pack(">I", len(contents)) + contents
        model = parse_sklt_bytes(raw)
        self.window._show_model(model, Path("outline.SKL"))
        self.editor.select_link(0, 1)
        self.assertEqual(self.editor.selected_edges, ((0, 1),))
        self.editor.set_transform_mode("move")
        self.begin((120, 100))
        self.move((130, 110))
        self.release((130, 110))
        self.assertEqual(self.editor.outline_points, [(90, 110), (130, 110)])
        self.editor.undo()
        self.editor.set_transform_mode("rotate")
        self.begin((120, 100))
        self.move((100, 120))
        self.release((100, 120))
        self.assertEqual(self.editor.outline_points, [(100, 80), (100, 120)])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "outline.SKL"
            self.window._write_edited_file(target)
            parsed = parse_sklt_file(target)
            self.assertEqual(parsed.outline_points, self.editor.outline_points)
            self.assertEqual(parsed.outline_groups, model.outline_groups)
            data = target.read_bytes()
            start = model.otl2_payload_offset
            end = start + model.otl2_payload_size
            self.assertEqual(raw[:start] + raw[end:], data[:start] + data[end:])
        self.editor.undo()
        self.editor.set_transform_mode("scale")
        original = self.editor.outline_points
        self.begin((120, 100))
        self.move((100, 100))  # Byte rounding would collapse endpoints.
        self.release((100, 100))
        self.assertEqual(self.editor.outline_points, original)
        self.assertFalse(self.editor.can_undo)
        self.begin((120, 100))
        self.move((500, 100))  # Reject the sample rather than clipping the shape.
        self.release((500, 100))
        self.assertEqual(self.editor.outline_points, original)
        self.assertFalse(self.editor.can_undo)

    def test_mode_change_cancels_drag_and_empty_transform_is_inert(self):
        self.editor.select_link(0, 1)
        before = self.editor.projected_points
        self.begin((100, 0))
        self.move((150, 30))
        self.key(Qt.Key.Key_R)
        self.release((150, 30))
        self.assertEqual(self.editor.projected_points, before)
        self.assertFalse(self.editor.can_undo)
        self.editor.clear_selection()
        self.key(Qt.Key.Key_S)
        self.assertIsNone(self.canvas.selection_pivot())
        self.assertEqual(self.editor.projected_points, before)

    def test_text_edit_save_shortcuts_do_not_export(self):
        line = QLineEdit(self.window.centralWidget())
        line.show()
        line.setFocus()
        line.setText("clipboard text")
        line.selectAll()
        with patch("wireframe_editor.window.QFileDialog") as dialog:
            self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier, line)
            self.key(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, line)
            dialog.getSaveFileName.assert_not_called()
        self.key(Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier, line)
        self.assertEqual(line.text(), "")
        self.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier, line)
        self.assertEqual(line.text(), "clipboard text")
        self.assertFalse(self.editor.has_clipboard)

    def test_scale_both_axes_around_non_origin_centroid(self):
        self.editor.select_box({0, 2}, set(), False)
        self.assertEqual(self.canvas.selection_pivot(), (50, 50))
        self.editor.set_transform_mode("scale")
        self.begin((200, 100))
        self.move((350, 150))
        self.release((350, 150))
        self.assert_points([(-250, 7, -50), (100, 9, 0), (350, 13, 150)])
        self.assertEqual(self.canvas.selection_pivot(), (50, 50))
        self.assertEqual(self.editor.polygons, [[0, 1], [1, 2]])


if __name__ == "__main__":
    unittest.main()
