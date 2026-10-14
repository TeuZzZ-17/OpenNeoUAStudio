import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QSizePolicy

from studio.ui.code_editor import ScriptCodeEditor


class ScriptCodeEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_editor(self, text=""):
        editor = ScriptCodeEditor()
        editor.setPlainText(text)
        editor.show()
        editor.editor.setFocus()
        self.app.processEvents()
        return editor

    def test_keyboard_indent_autoindent_comment_duplicate_and_undo(self):
        widget = self.make_editor("first\nsecond")
        try:
            cursor = QTextCursor(widget.editor.document())
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.End,
                               QTextCursor.MoveMode.KeepAnchor)
            widget.editor.setTextCursor(cursor)
            QTest.keyClick(widget.editor, Qt.Key.Key_Tab)
            self.assertEqual(widget.toPlainText(), "    first\n    second")
            QTest.keyClick(widget.editor, Qt.Key.Key_Tab,
                           Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(widget.toPlainText(), "first\nsecond")
            widget.setPlainText("    body")
            QTest.keyClick(widget.editor, Qt.Key.Key_Backtab)
            self.assertEqual(widget.toPlainText(), "body")

            widget.setPlainText("  speed=1")
            cursor = widget.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            widget.editor.setTextCursor(cursor)
            QTest.keyClick(widget.editor, Qt.Key.Key_Return)
            self.assertEqual(widget.toPlainText(), "  speed=1\n  ")
            QTest.keyClick(widget.editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "  speed=1")

            QTest.keyClick(widget.editor, Qt.Key.Key_Slash,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "  ; speed=1")
            QTest.keyClick(widget.editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "  speed=1")

            QTest.keyClick(widget.editor, Qt.Key.Key_D,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "  speed=1\n  speed=1")
            QTest.keyClick(widget.editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "  speed=1")

            widget.setPlainText("value=12")
            cursor = widget.editor.textCursor()
            cursor.setPosition(0)
            cursor.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
            widget.editor.setTextCursor(cursor)
            QTest.keyClick(widget.editor, Qt.Key.Key_D,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "valuevalue=12")
            self.assertEqual(widget.editor.textCursor().selectedText(), "value")
        finally:
            widget.close()

    def test_alt_arrows_move_lines_and_undo(self):
        widget = self.make_editor("first\nsecond\nthird")
        try:
            cursor = widget.editor.document().findBlockByNumber(1)
            widget.editor.setTextCursor(QTextCursor(cursor))
            QTest.keyClick(widget.editor, Qt.Key.Key_Up,
                           Qt.KeyboardModifier.AltModifier)
            self.assertEqual(widget.toPlainText(), "second\nfirst\nthird")
            QTest.keyClick(widget.editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "first\nsecond\nthird")

            cursor = widget.editor.document().findBlockByNumber(1)
            widget.editor.setTextCursor(QTextCursor(cursor))
            QTest.keyClick(widget.editor, Qt.Key.Key_Down,
                           Qt.KeyboardModifier.AltModifier)
            self.assertEqual(widget.toPlainText(), "first\nthird\nsecond")
        finally:
            widget.close()

    def test_find_replace_case_sensitivity_and_replace_all_undo(self):
        widget = self.make_editor("Alpha alpha ALPHA")
        try:
            QTest.keyClick(widget.editor, Qt.Key.Key_F,
                           Qt.KeyboardModifier.ControlModifier)
            widget.search_edit.setText("alpha")
            QTest.keyClick(widget.editor, Qt.Key.Key_F3)
            self.assertEqual(widget.editor.textCursor().selectedText(), "Alpha")
            QTest.keyClick(widget.editor, Qt.Key.Key_F3)
            self.assertEqual(widget.editor.textCursor().selectedText(), "alpha")
            QTest.keyClick(widget.editor, Qt.Key.Key_F3,
                           Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(widget.editor.textCursor().selectedText(), "Alpha")

            QTest.keyClick(widget.editor, Qt.Key.Key_H,
                           Qt.KeyboardModifier.ControlModifier)
            widget.replace_edit.setText("X")
            QTest.mouseClick(widget.replace_all_button, Qt.MouseButton.LeftButton)
            self.assertEqual(widget.toPlainText(), "X X X")
            QTest.keyClick(widget.editor, Qt.Key.Key_Z,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(widget.toPlainText(), "Alpha alpha ALPHA")

            cursor = widget.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            widget.editor.setTextCursor(cursor)
            widget.search_edit.setText("Alpha")
            widget.replace_edit.setText("First")
            widget.case_check.setChecked(True)
            QTest.mouseClick(widget.replace_one_button, Qt.MouseButton.LeftButton)
            self.assertEqual(widget.toPlainText(), "First alpha ALPHA")

            cursor = widget.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            widget.editor.setTextCursor(cursor)
            widget.search_edit.setText("alpha")
            widget.replace_edit.setText("x")
            QTest.mouseClick(widget.replace_all_button, Qt.MouseButton.LeftButton)
            self.assertEqual(widget.toPlainText(), "First x ALPHA")
        finally:
            widget.close()

    def test_highlighting_themes_line_numbers_error_and_expanding_layout(self):
        widget = self.make_editor(
            'begin_fx\nSpeed=12..34 ; note\nlabel="a;b"\n'
            'label="🚀;x"; note')
        try:
            document = widget.editor.document()

            def color_at(line: int, column: int) -> str:
                block = document.findBlockByNumber(line)
                for item in block.layout().formats():
                    if item.start <= column < item.start + item.length:
                        return item.format.foreground().color().name()
                self.fail(f"No syntax color at line {line + 1}, col {column + 1}")

            self.assertEqual(color_at(0, 0), "#e5bc70")
            self.assertEqual(color_at(1, 0), "#82c9ff")
            self.assertEqual(color_at(1, 6), "#b6dc8b")
            self.assertEqual(color_at(1, 13), "#83918a")
            self.assertEqual(color_at(2, 9), "#d99a88")
            self.assertEqual(color_at(3, 9), "#d99a88")
            self.assertEqual(color_at(3, 12), "#83918a")

            studio_dark = widget.editor.styleSheet()
            widget.theme_combo.setCurrentText("Light")
            self.assertEqual(widget.theme(), "Light")
            self.assertNotEqual(widget.editor.styleSheet(), studio_dark)
            self.assertIn("background-color: #ffffff", widget.editor.styleSheet())
            self.assertEqual(color_at(0, 0), "#795e26")
            self.assertGreater(widget.editor.line_number_area.width(), 0)
            self.assertEqual(widget.sizePolicy().horizontalPolicy(),
                             QSizePolicy.Policy.Expanding)
            self.assertEqual(widget.editor.sizePolicy().verticalPolicy(),
                             QSizePolicy.Policy.Expanding)

            widget.set_error("Invalid value", line=2)
            self.assertEqual(widget.status_label.text(), "Invalid value")
            self.assertEqual(len(widget.editor.extraSelections()), 2)
            widget.clear_error()
            self.assertIn("Line", widget.status_label.text())
            self.assertEqual(len(widget.editor.extraSelections()), 1)
        finally:
            widget.close()

    def test_save_shortcut_emits_request(self):
        widget = self.make_editor("value=1")
        try:
            spy = QSignalSpy(widget.saveRequested)
            text_spy = QSignalSpy(widget.textChanged)
            QTest.keyClick(widget.editor, Qt.Key.Key_X)
            self.assertEqual(widget.toPlainText(), "xvalue=1")
            self.assertEqual(text_spy.count(), 1)
            QTest.keyClick(widget.editor, Qt.Key.Key_S,
                           Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(spy.count(), 1)
            QTest.mouseClick(widget.save_button, Qt.MouseButton.LeftButton)
            self.assertEqual(spy.count(), 2)
        finally:
            widget.close()


if __name__ == "__main__":
    unittest.main()
