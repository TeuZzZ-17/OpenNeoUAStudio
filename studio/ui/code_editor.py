"""Reusable script editor controls for OpenNeoUA Studio."""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QSyntaxHighlighter,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


_THEME = {
    "background": "#20242a", "foreground": "#e2e6eb",
    "gutter": "#181b20", "line_number": "#77818d",
    "current_line": "#2a3038", "selection": "#40566b",
    "error": "#542c32", "key": "#82c9ff",
    "function": "#e5bc70", "number": "#b6dc8b",
    "string": "#d99a88", "comment": "#83918a",
}

_KEY_PATTERN = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_.-]*)(?=\s*=)")
_FUNCTION_PATTERN = re.compile(r"\b(?:begin|end)(?:_[A-Za-z_][A-Za-z0-9_]*)?\b", re.I)
_STRING_PATTERN = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_RANGE_PATTERN = re.compile(r"(?<=\d)\s*(?:\.\.|-)\s*(?=\d)")


class _ScriptHighlighter(QSyntaxHighlighter):
    def __init__(self, document: QTextDocument, theme: dict[str, str]):
        super().__init__(document)
        self.theme = theme

    def set_theme(self, theme: dict[str, str]) -> None:
        self.theme = theme
        self.rehighlight()

    def _format(self, name: str) -> QTextCharFormat:
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(self.theme[name]))
        if name == "function":
            char_format.setFontWeight(QFont.Weight.DemiBold)
        return char_format

    @staticmethod
    def _qt_range(text: str, start: int, end: int) -> tuple[int, int]:
        prefix = len(text[:start].encode("utf-16-le", errors="surrogatepass")) // 2
        length = len(text[start:end].encode("utf-16-le", errors="surrogatepass")) // 2
        return prefix, length

    def _set_format(self, text: str, start: int, end: int, name: str) -> None:
        qt_start, qt_length = self._qt_range(text, start, end)
        self.setFormat(qt_start, qt_length, self._format(name))

    @staticmethod
    def _comment_start(text: str) -> int:
        quote = ""
        escaped = False
        for index, char in enumerate(text):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ("\"", "'"):
                quote = char
            elif char == ";":
                return index
        return -1

    def highlightBlock(self, text: str) -> None:
        comment_start = self._comment_start(text)
        code = text if comment_start < 0 else text[:comment_start]

        key = _KEY_PATTERN.search(code)
        if key:
            self._set_format(text, key.start(1), key.end(1), "key")
        for match in _FUNCTION_PATTERN.finditer(code):
            # end_size, begin_x ... before "=" are keys, not block keywords.
            if key and match.start() == key.start(1):
                continue
            self._set_format(text, match.start(), match.end(), "function")
        for pattern, name in ((_RANGE_PATTERN, "number"),
                              (_NUMBER_PATTERN, "number"),
                              (_STRING_PATTERN, "string")):
            for match in pattern.finditer(code):
                self._set_format(text, match.start(), match.end(), name)
        if comment_start >= 0:
            self._set_format(text, comment_start, len(text), "comment")


class _LineNumberArea(QWidget):
    def __init__(self, editor: _ScriptTextEdit):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_number_area(event)


class _ScriptTextEdit(QPlainTextEdit):
    def __init__(self, owner: ScriptCodeEditor):
        super().__init__(owner)
        self.owner = owner
        self.line_number_area = _LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(owner._update_cursor_state)
        self.update_line_number_area_width(0)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabChangesFocus(False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        contents = self.contentsRect()
        self.line_number_area.setGeometry(QRect(
            contents.left(), contents.top(), self.line_number_area_width(),
            contents.height()))

    def paint_line_number_area(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(self.owner._theme["gutter"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor(self.owner._theme["line_number"]))
                painter.drawText(
                    0, top, self.line_number_area.width() - 5,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(number + 1))
            block = block.next()
            top = bottom
            if block.isValid():
                bottom = top + int(self.blockBoundingRect(block).height())
            number += 1

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.owner._handle_editor_key(event):
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self.owner._change_zoom(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        find_action = QAction("Find", menu)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(lambda: self.owner._show_search(False))
        menu.addAction(find_action)
        replace_action = QAction("Replace", menu)
        replace_action.setShortcut(QKeySequence.StandardKey.Replace)
        replace_action.triggered.connect(lambda: self.owner._show_search(True))
        menu.addAction(replace_action)
        comment_action = QAction("Toggle Comment", menu)
        comment_action.setShortcut(QKeySequence("Ctrl+/"))
        comment_action.triggered.connect(self.owner._toggle_comment)
        menu.addAction(comment_action)
        duplicate_action = QAction("Duplicate Line / Selection", menu)
        duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        duplicate_action.triggered.connect(self.owner._duplicate_line_or_selection)
        menu.addAction(duplicate_action)
        menu.exec(event.globalPos())


class ScriptCodeEditor(QWidget):
    """Compact UA script editor with syntax colors and common edit tools."""

    textChanged = Signal(str)
    saveRequested = Signal()

    _INDENT = "    "

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._theme = dict(_THEME)
        self._zoom = 10
        self._error_message = ""
        self._error_line: int | None = None
        self._shortcuts: list[QShortcut] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(3)
        self.undo_button = self._tool_button("Undo", "Undo the last edit")
        self.undo_button.clicked.connect(self._undo)
        toolbar.addWidget(self.undo_button)
        self.redo_button = self._tool_button("Redo", "Redo the last edit")
        self.redo_button.clicked.connect(self._redo)
        toolbar.addWidget(self.redo_button)
        self.find_button = self._tool_button("Find", "Find text (Ctrl+F)")
        self.find_button.clicked.connect(lambda: self._show_search(False))
        toolbar.addWidget(self.find_button)
        self.replace_button = self._tool_button(
            "Replace", "Find and replace text (Ctrl+H)")
        self.replace_button.clicked.connect(lambda: self._show_search(True))
        toolbar.addWidget(self.replace_button)
        self.save_button = self._tool_button("Save", "Save script (Ctrl+S)")
        self.save_button.clicked.connect(self.saveRequested.emit)
        toolbar.addWidget(self.save_button)
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        self.search_row = QWidget(self)
        search_layout = QHBoxLayout(self.search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(4)
        self.search_edit = QLineEdit(self.search_row)
        self.search_edit.setPlaceholderText("Find")
        self.search_edit.returnPressed.connect(self.find_next)
        search_layout.addWidget(self.search_edit, 1)
        self.case_check = QCheckBox("Case", self.search_row)
        self.case_check.setToolTip("Match upper and lower case exactly")
        search_layout.addWidget(self.case_check)
        self.find_previous_button = QPushButton("Previous", self.search_row)
        self.find_previous_button.clicked.connect(self.find_previous)
        search_layout.addWidget(self.find_previous_button)
        self.find_next_button = QPushButton("Next", self.search_row)
        self.find_next_button.clicked.connect(self.find_next)
        search_layout.addWidget(self.find_next_button)
        self.close_search_button = self._tool_button(
            "×", "Close search")
        self.close_search_button.clicked.connect(self._hide_search)
        search_layout.addWidget(self.close_search_button)
        root.addWidget(self.search_row)

        self.replace_row = QWidget(self)
        replace_layout = QHBoxLayout(self.replace_row)
        replace_layout.setContentsMargins(0, 0, 0, 0)
        replace_layout.setSpacing(4)
        replace_layout.addWidget(QLabel("Replace with:"))
        self.replace_edit = QLineEdit(self.replace_row)
        self.replace_edit.setPlaceholderText("Replacement")
        self.replace_edit.returnPressed.connect(self.replace_current)
        replace_layout.addWidget(self.replace_edit, 1)
        self.replace_one_button = QPushButton("Replace", self.replace_row)
        self.replace_one_button.clicked.connect(self.replace_current)
        replace_layout.addWidget(self.replace_one_button)
        self.replace_all_button = QPushButton("Replace All", self.replace_row)
        self.replace_all_button.clicked.connect(self.replace_all)
        replace_layout.addWidget(self.replace_all_button)
        root.addWidget(self.replace_row)

        self.editor = _ScriptTextEdit(self)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(self._zoom)
        self.editor.setFont(font)
        self._set_tab_stop()
        self.highlighter = _ScriptHighlighter(self.editor.document(), self._theme)
        self.editor.textChanged.connect(self._emit_text_changed)
        root.addWidget(self.editor, 1)

        self.status_label = QLabel("Line 1, column 1", self)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(18)
        root.addWidget(self.status_label)

        self.search_row.hide()
        self.replace_row.hide()
        self._apply_theme()
        self._bind_shortcut("Ctrl+S", self.saveRequested.emit)
        self._bind_shortcut("Ctrl+F", lambda: self._show_search(False))
        self._bind_shortcut("Ctrl+H", lambda: self._show_search(True))
        self._bind_shortcut("F3", self.find_next)
        self._bind_shortcut("Shift+F3", self.find_previous)

    @staticmethod
    def _tool_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    def _bind_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(callback)
        self._shortcuts.append(shortcut)

    def _emit_text_changed(self) -> None:
        self.textChanged.emit(self.editor.toPlainText())

    def setPlainText(self, text: str) -> None:
        self.editor.setPlainText(str(text))
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)
        self._update_cursor_state()

    def toPlainText(self) -> str:
        return self.editor.toPlainText()

    def set_error(self, message: str, line: int | None = None) -> None:
        self._error_message = str(message or "")
        self._error_line = line if line is not None and line > 0 else None
        self._update_cursor_state()

    def clear_error(self) -> None:
        self.set_error("")

    def _apply_theme(self) -> None:
        self._theme = dict(_THEME)
        self._theme.update(QApplication.instance().property("studioSyntaxColors") or {})
        self.editor.ensurePolished()
        palette = QApplication.instance().palette()
        self._theme.update({
            "background": palette.color(QPalette.ColorRole.Base).name(),
            "foreground": palette.color(QPalette.ColorRole.Text).name(),
            "gutter": palette.color(QPalette.ColorRole.Window).name(),
            "current_line": palette.color(QPalette.ColorRole.AlternateBase).name(),
            "selection": palette.color(QPalette.ColorRole.Highlight).name(),
        })
        self.editor.setPalette(palette)
        self.highlighter.set_theme(self._theme)
        self.status_label.setStyleSheet(
            f"color: {self._theme['foreground']};")

    def refresh_studio_theme(self) -> None:
        """Refresh script colors while preserving text, cursor and undo history."""
        self._apply_theme()
        self._update_cursor_state()
        self.editor.viewport().update()

    def _set_tab_stop(self) -> None:
        metrics = QFontMetricsF(self.editor.font())
        self.editor.setTabStopDistance(metrics.horizontalAdvance(" ") * 4)

    def _change_zoom(self, delta: int) -> None:
        self._zoom = max(7, min(28, self._zoom + delta))
        font = self.editor.font()
        font.setPointSize(self._zoom)
        self.editor.setFont(font)
        self._set_tab_stop()
        self.editor.update_line_number_area_width(0)

    def _update_cursor_state(self) -> None:
        cursor = self.editor.textCursor()
        selections = []
        current = QTextEdit.ExtraSelection()
        current.cursor = QTextCursor(cursor)
        current.cursor.clearSelection()
        current.format.setBackground(QColor(self._theme["current_line"]))
        current.format.setProperty(
            QTextFormat.Property.FullWidthSelection, True)
        selections.append(current)
        if self._error_line is not None:
            block = self.editor.document().findBlockByNumber(
                self._error_line - 1)
            if block.isValid():
                error = QTextEdit.ExtraSelection()
                error.cursor = QTextCursor(block)
                error.format.setBackground(QColor(self._theme["error"]))
                error.format.setProperty(
                    QTextFormat.Property.FullWidthSelection, True)
                selections.append(error)
        self.editor.setExtraSelections(selections)
        self.editor.line_number_area.update()
        if self._error_message:
            self.status_label.setText(self._error_message)
            self.status_label.setStyleSheet(
                f"color: {self._theme['string']};")
        else:
            block = cursor.block()
            column = cursor.position() - block.position() + 1
            self.status_label.setText(
                f"Line {block.blockNumber() + 1}, column {column}")
            self.status_label.setStyleSheet(
                f"color: {self._theme['foreground']};")

    def _show_search(self, replace: bool) -> None:
        selected = self.editor.textCursor().selectedText().replace("\u2029", "\n")
        if selected and not self.search_edit.text():
            self.search_edit.setText(selected)
        self.search_row.show()
        self.replace_row.setVisible(replace)
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def _hide_search(self) -> None:
        self.search_row.hide()
        self.replace_row.hide()
        self.editor.setFocus()

    def _find_flags(self, backward: bool = False):
        flags = QTextDocument.FindFlag(0)
        if self.case_check.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _find(self, backward: bool = False) -> bool:
        needle = self.search_edit.text()
        if not needle:
            return False
        flags = self._find_flags(backward)
        found = self.editor.find(needle, flags)
        if not found:
            cursor = QTextCursor(self.editor.document())
            if backward:
                cursor.movePosition(QTextCursor.MoveOperation.End)
            else:
                cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            found = self.editor.find(needle, flags)
        self.status_label.setText("Found" if found else "No matches")
        return found

    def find_next(self) -> bool:
        return self._find(False)

    def find_previous(self) -> bool:
        return self._find(True)

    def replace_current(self) -> bool:
        needle = self.search_edit.text()
        if not needle:
            return False
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        matches = (selected == needle if self.case_check.isChecked()
                   else selected.casefold() == needle.casefold())
        if not matches:
            if not self.find_next():
                return False
            cursor = self.editor.textCursor()
        cursor.insertText(self.replace_edit.text())
        self.editor.setTextCursor(cursor)
        self.find_next()
        return True

    def replace_all(self) -> int:
        needle = self.search_edit.text()
        if not needle:
            return 0
        document = self.editor.document()
        cursor = QTextCursor(document)
        count = 0
        cursor.beginEditBlock()
        while True:
            found = document.find(needle, cursor, self._find_flags())
            if found.isNull():
                break
            cursor.setPosition(found.selectionStart())
            cursor.setPosition(found.selectionEnd(),
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(self.replace_edit.text())
            count += 1
        cursor.endEditBlock()
        self.status_label.setText(f"Replaced {count} occurrence(s)")
        return count

    def _handle_editor_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if key == Qt.Key.Key_Escape and self.search_row.isVisible():
            self._hide_search()
            return True
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)

        if control and (key == Qt.Key.Key_Slash or event.text() == "/"):
            self._toggle_comment()
            return True
        if control and key == Qt.Key.Key_D:
            self._duplicate_line_or_selection()
            return True
        if alt and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._move_lines(-1 if key == Qt.Key.Key_Up else 1)
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._change_indent(outdent=shift or key == Qt.Key.Key_Backtab)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not control and not alt:
            self._insert_indented_newline()
            return True
        return False

    def _line_bounds(self, cursor: QTextCursor) -> tuple[int, int]:
        start = cursor.selectionStart() if cursor.hasSelection() \
            else cursor.position()
        end = cursor.selectionEnd() if cursor.hasSelection() \
            else cursor.position()
        document = self.editor.document()
        if (cursor.hasSelection() and end > start
                and str(document.characterAt(end - 1)) == "\n"):
            end -= 1
        return (document.findBlock(start).blockNumber(),
                document.findBlock(end).blockNumber())

    def _select_lines(self, start_line: int, end_line: int) -> None:
        document = self.editor.document()
        first = document.findBlockByNumber(start_line)
        last = document.findBlockByNumber(end_line)
        if not first.isValid() or not last.isValid():
            return
        cursor = QTextCursor(first)
        cursor.setPosition(last.position() + last.length() - 1,
                           QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _change_indent(self, outdent: bool) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.hasSelection()
        start_line, end_line = self._line_bounds(cursor)
        if not selected and not outdent:
            cursor.insertText(self._INDENT)
            return

        document = self.editor.document()
        original_line = cursor.block().blockNumber()
        original_column = cursor.position() - cursor.block().position()
        removed_at_cursor = 0
        edit = QTextCursor(document)
        edit.beginEditBlock()
        for line in range(end_line, start_line - 1, -1):
            block = document.findBlockByNumber(line)
            text = block.text()
            if outdent:
                remove = 1 if text.startswith("\t") else min(
                    len(text) - len(text.lstrip(" ")), len(self._INDENT))
                if line == original_line:
                    removed_at_cursor = remove
                if remove:
                    edit.setPosition(block.position())
                    edit.setPosition(block.position() + remove,
                                     QTextCursor.MoveMode.KeepAnchor)
                    edit.removeSelectedText()
            else:
                edit.setPosition(block.position())
                edit.insertText(self._INDENT)
        edit.endEditBlock()
        if selected:
            self._select_lines(start_line, end_line)
        else:
            block = document.findBlockByNumber(original_line)
            cursor = QTextCursor(block)
            cursor.setPosition(min(block.position() + max(
                0, original_column - removed_at_cursor),
                                   block.position() + block.length() - 1))
            self.editor.setTextCursor(cursor)

    def _insert_indented_newline(self) -> None:
        cursor = self.editor.textCursor()
        line = cursor.block().text()
        indentation = re.match(r"[ \t]*", line).group(0)
        cursor.insertText("\n" + indentation)
        self.editor.setTextCursor(cursor)

    def _toggle_comment(self) -> None:
        cursor = self.editor.textCursor()
        selected = cursor.hasSelection()
        original_line = cursor.block().blockNumber()
        original_column = cursor.position() - cursor.block().position()
        start_line, end_line = self._line_bounds(cursor)
        document = self.editor.document()
        blocks = [document.findBlockByNumber(line)
                  for line in range(start_line, end_line + 1)]
        nonempty = [block.text() for block in blocks if block.text().strip()]
        uncomment = bool(nonempty) and all(
            line.lstrip().startswith(";") for line in nonempty)
        edit = QTextCursor(document)
        edit.beginEditBlock()
        current_delta = 0
        for block in reversed(blocks):
            text = block.text()
            indent = len(text) - len(text.lstrip(" \t"))
            edit.setPosition(block.position() + indent)
            if uncomment:
                if text[indent:indent + 2] == "; ":
                    if block.blockNumber() == original_line:
                        current_delta = -2
                    edit.setPosition(block.position() + indent + 2,
                                     QTextCursor.MoveMode.KeepAnchor)
                    edit.removeSelectedText()
                elif text[indent:indent + 1] == ";":
                    if block.blockNumber() == original_line:
                        current_delta = -1
                    edit.setPosition(block.position() + indent + 1,
                                     QTextCursor.MoveMode.KeepAnchor)
                    edit.removeSelectedText()
            else:
                if block.blockNumber() == original_line:
                    current_delta = 2
                edit.insertText("; ")
        edit.endEditBlock()
        if selected:
            self._select_lines(start_line, end_line)
        else:
            block = document.findBlockByNumber(start_line)
            cursor = QTextCursor(block)
            cursor.setPosition(min(
                block.position() + max(0, original_column + current_delta),
                block.position() + block.length() - 1))
            self.editor.setTextCursor(cursor)

    def _duplicate_line_or_selection(self) -> None:
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            value = cursor.selectedText().replace("\u2029", "\n")
            position = cursor.selectionEnd()
            cursor.setPosition(position)
            cursor.insertText(value)
            inserted_end = cursor.position()
            cursor.setPosition(position)
            cursor.setPosition(inserted_end,
                               QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cursor)
            return

        block = cursor.block()
        value = block.text()
        if block.next().isValid():
            position = block.position() + block.length()
            duplicate = value + "\n"
        else:
            position = block.position() + block.length() - 1
            duplicate = "\n" + value
        cursor.setPosition(position)
        cursor.insertText(duplicate)
        self.editor.setTextCursor(cursor)

    def _move_lines(self, direction: int) -> None:
        cursor = self.editor.textCursor()
        start_line, end_line = self._line_bounds(cursor)
        lines = self.editor.toPlainText().split("\n")
        if direction < 0 and start_line == 0:
            return
        if direction > 0 and end_line >= len(lines) - 1:
            return

        original = []
        for position in (cursor.anchor(), cursor.position()):
            block = self.editor.document().findBlock(position)
            original.append((block.blockNumber(), position - block.position()))

        mapping = {line: line for line in range(len(lines))}
        if direction < 0:
            lines[start_line - 1:end_line + 1] = (
                lines[start_line:end_line + 1] + [lines[start_line - 1]])
            mapping[start_line - 1] = end_line
            for line in range(start_line, end_line + 1):
                mapping[line] = line - 1
        else:
            lines[start_line:end_line + 2] = (
                [lines[end_line + 1]] + lines[start_line:end_line + 1])
            mapping[end_line + 1] = start_line
            for line in range(start_line, end_line + 1):
                mapping[line] = line + 1

        document = self.editor.document()
        edit = QTextCursor(document)
        edit.beginEditBlock()
        edit.select(QTextCursor.SelectionType.Document)
        edit.insertText("\n".join(lines))
        edit.endEditBlock()

        positions = []
        for line, column in original:
            block = document.findBlockByNumber(mapping[line])
            positions.append(min(block.position() + column,
                                 block.position() + block.length() - 1))
        cursor = QTextCursor(document)
        cursor.setPosition(positions[0])
        cursor.setPosition(positions[1], QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _undo(self) -> None:
        self.editor.undo()

    def _redo(self) -> None:
        self.editor.redo()
