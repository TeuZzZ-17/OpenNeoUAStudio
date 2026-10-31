"""Effect-code editor that slides out of the Destruction FX "Effects" box.

A small tab on the left edge of the box opens and closes the editor. The
editor is attached to the box, with the same height, and overlays the viewport
while it is open. It is a child of the main window and follows the box when
the window or a splitter moves it.
"""

from PySide6.QtCore import (
    QEasingCurve, QEvent, QPoint, QRectF, QSignalBlocker, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import QKeySequence, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from studio.ui.code_editor import ScriptCodeEditor
from studio.ui.cursor_themes import studio_cursor

DRAWER_WIDTH = 640
DRAWER_MIN_WIDTH = 360
DRAWER_MARGIN = 12      # free strip kept at the left edge of the window
SLIDE_MS = 140
TAB_SIZE = (26, 84)
TAB_OVERLAP = 6         # part of the tab that covers the box; the rest sticks out

# These keys belong to the code editor even though the main window may bind
# them to its own actions (Ctrl+D duplicates a line, Ctrl+Z undoes typing, ...).
_EDITOR_OWN_SHORTCUTS = frozenset(
    QKeySequence(text).toString()
    for text in ("Ctrl+S", "Ctrl+F", "Ctrl+H", "F3", "Shift+F3"))
_LAYOUT_EVENTS = frozenset((
    QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide,
    QEvent.Type.LayoutRequest))


class FxDrawerTab(QAbstractButton):
    """Tab with an arrow: points left to open the editor, right to close it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(*TAB_SIZE)
        self.setCursor(studio_cursor("point"))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._open = False
        self.set_open(False)

    def set_open(self, is_open: bool) -> None:
        self._open = is_open
        self.setToolTip("Collapse the effect code editor" if is_open
                        else "Open the effect code editor")
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 8.0
        # Rounded on the outer side only: the other side melts into the box.
        path = QPainterPath()
        path.moveTo(rect.right(), rect.top())
        path.lineTo(rect.left() + radius, rect.top())
        path.quadTo(rect.left(), rect.top(), rect.left(), rect.top() + radius)
        path.lineTo(rect.left(), rect.bottom() - radius)
        path.quadTo(rect.left(), rect.bottom(), rect.left() + radius, rect.bottom())
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        if not self.isEnabled():
            fill = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button)
        elif self.isDown():
            fill = self.palette().color(QPalette.ColorRole.Highlight)
        elif self.underMouse():
            fill = self.palette().color(QPalette.ColorRole.Midlight)
        else:
            fill = self.palette().color(QPalette.ColorRole.Button)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1.0))
        painter.setBrush(fill)
        painter.drawPath(path)
        # The arrow is centred on the part of the tab that sticks out.
        center_x = rect.left() + (rect.width() - TAB_OVERLAP) / 2
        center_y = rect.center().y()
        direction = 1.0 if self._open else -1.0
        arrow = QPainterPath()
        arrow.moveTo(center_x - 4.0 * direction, center_y - 7.0)
        arrow.lineTo(center_x + 4.0 * direction, center_y)
        arrow.lineTo(center_x - 4.0 * direction, center_y + 7.0)
        arrow.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.palette().color(QPalette.ColorRole.ButtonText))
        painter.drawPath(arrow)
        painter.end()


class FxCodeDrawer(QFrame):
    """Code editor of one effect, attached to the left of ``anchor``."""

    def __init__(self, panel, host, anchor):
        super().__init__(host)
        self.panel = panel
        self.anchor = anchor
        self.index = -1
        self._expanded = False
        self._reveal = 0.0
        # Placement waits for the layout to settle. A timer of the drawer is
        # dropped with it, so nothing runs after the window is gone.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync)
        self.setObjectName("FxCodeDrawer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.code = ScriptCodeEditor(self)
        self.code.setToolTip(
            "Visual begin_fx: tint applies to every model in the block, "
            "regardless of position. If repeated, the last value wins.")
        layout.addWidget(self.code, 1)
        # The same controls as the transport bar under the preview.
        transport = QHBoxLayout()
        transport.setSpacing(4)
        self.restart_button = panel.create_mirror_button(self, panel.restart_button)
        self.play_button = panel.create_play_button(self)
        self.stop_button = panel.create_mirror_button(self, panel.stop_button)
        self.loop_button = panel.create_mirror_button(self, panel.loop_button)
        self.mute_button = panel.create_mirror_button(self, panel.mute_button)
        self.shake_button = panel.create_mirror_button(self, panel.shake_button)
        self.pal_button = panel.create_mirror_button(self, panel.pal_button)
        for control in (self.restart_button, self.play_button, self.stop_button,
                        self.loop_button, self.mute_button, self.shake_button,
                        self.pal_button):
            transport.addWidget(control)
        transport.addSpacing(10)
        transport.addWidget(QLabel("Speed"))
        self.speed_combo = panel.create_speed_combo(self)
        transport.addWidget(self.speed_combo)
        transport.addStretch(1)
        layout.addLayout(transport)
        buttons = QHBoxLayout()
        self.revert_button = QPushButton("Revert Effect")
        self.revert_button.clicked.connect(self._revert)
        buttons.addWidget(self.revert_button)
        self.save_button = QPushButton("Save Effect")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.save_all_button = QPushButton("Save All Effects")
        self.save_all_button.clicked.connect(lambda: self._save(all_effects=True))
        buttons.addWidget(self.save_all_button)
        # Nothing to save until the effect is edited.
        self._set_save_enabled(False)
        self.save_all_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.tab = FxDrawerTab(host)
        self.tab.clicked.connect(self.toggle)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._apply)
        self.code.textChanged.connect(self._edited)
        self.code.saveRequested.connect(self._save)
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(SLIDE_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self._reveal_changed)
        self._slide.finished.connect(self._slide_finished)

        # Follow the box whenever the window, a splitter or a tab moves it.
        widget = anchor
        while widget is not None:
            widget.installEventFilter(self)
            if widget is host:
                break
            widget = widget.parentWidget()
        self.code.editor.installEventFilter(self)
        self.hide()
        self.schedule_sync()

    # -- placement ----------------------------------------------------------------------

    @property
    def expanded(self) -> bool:
        return self._expanded

    def eventFilter(self, watched, event):  # noqa: N802
        kind = event.type()
        if watched is self.code.editor:
            if kind == QEvent.Type.ShortcutOverride and \
                    QKeySequence(event.keyCombination()).toString() not in _EDITOR_OWN_SHORTCUTS:
                # Typing in the editor must never trigger a window shortcut.
                event.accept()
            return False
        if kind in _LAYOUT_EVENTS:
            self.schedule_sync()
        return False

    def schedule_sync(self) -> None:
        if not self._sync_timer.isActive():
            self._sync_timer.start(0)

    def _sync(self) -> None:
        host = self.parentWidget()
        anchor = self.anchor
        shown = host is not None and anchor.isVisible()
        self.tab.setVisible(shown)
        self.tab.setEnabled(self.panel.has_rows())
        if not shown:
            self.hide()
            return
        corner = anchor.mapTo(host, QPoint(0, 0))
        room = max(0, corner.x() - DRAWER_MARGIN)
        full = max(DRAWER_MIN_WIDTH, min(DRAWER_WIDTH, room))
        width = round(full * self._reveal)
        self.setGeometry(corner.x() - width, corner.y(), width, anchor.height())
        self.setVisible(width > 0)
        if width > 0:
            self.raise_()
        self.tab.move(corner.x() - TAB_SIZE[0] + TAB_OVERLAP,
                      corner.y() + (anchor.height() - TAB_SIZE[1]) // 2)
        self.tab.raise_()

    # -- open and close -----------------------------------------------------------------

    def toggle(self) -> None:
        if self._expanded:
            self.set_expanded(False)
            return
        panel = self.panel
        if not panel.has_rows():
            return
        if self.index == -1 or not panel.is_valid_index(self.index):
            current = panel.effects_list.currentItem()
            row = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
            first = panel.effects_list.item(0)
            self.index = -1
            if row is None and first is not None:
                row = first.data(Qt.ItemDataRole.UserRole)
            if row is not None:
                self.load_effect(row)
        else:
            self.load_effect(self.index)

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        """Slide the editor out or in; closing applies any pending edit first."""

        if not expanded:
            self.timer.stop()
            if self.index != -1:
                self._apply()
        self._expanded = expanded
        self.tab.set_open(expanded)
        self._slide.stop()
        target = 1.0 if expanded else 0.0
        if animate and self.parentWidget().isVisible() and self.anchor.isVisible():
            self._slide.setStartValue(self._reveal)
            self._slide.setEndValue(target)
            self._slide.start()
            return
        self._reveal = target
        self._sync()

    def _reveal_changed(self, value) -> None:
        self._reveal = float(value)
        self._sync()

    def _slide_finished(self) -> None:
        self._sync()

    def reset(self) -> None:
        """Forget the loaded effect and fold the editor away (new definition)."""

        self.timer.stop()
        self.index = -1
        self.set_expanded(False, animate=False)

    # -- editing ------------------------------------------------------------------------

    def load_effect(self, index):
        self.timer.stop()
        if self.index != -1 and self.index != index:
            self._apply()
        already_open = self.index == index and self._expanded
        self.set_expanded(True)
        if already_open:
            self.code.editor.setFocus()
            return
        self.index = index
        self.panel.effects_view.select_effect(index)
        with QSignalBlocker(self.code):
            self.code.setPlainText(self.panel.session_for(index).drafts[index])
        self._apply()
        self.code.editor.setFocus()

    def _edited(self, text):
        if self.index == -1:
            return
        self.panel.session_for(self.index).drafts[self.index] = text
        self.panel.update_text_status()
        self.timer.start()

    def _apply(self):
        if self.index == -1:
            return False
        session = self.panel.session_for(self.index)
        try:
            item, issues = session.set_draft(self.index, self.code.toPlainText())
        except ValueError as exc:
            self.code.set_error(f"Preview keeps the last valid version. {exc}")
            self._set_save_enabled(False)
            self._sync_title()
            return False
        self.panel.apply_text_item(self.index, item)
        message = "; ".join(issue.message for issue in issues)
        self.code.set_error(message)
        self._set_save_enabled(not session.source_error and session.is_dirty(self.index))
        self.save_all_button.setEnabled(not session.source_error and self.panel.has_unsaved_text)
        self._sync_title()
        return True

    def _set_save_enabled(self, enabled: bool) -> None:
        """Both Save buttons (the drawer's and the editor toolbar's) share one state."""

        self.save_button.setEnabled(enabled)
        self.code.save_button.setEnabled(enabled)

    def _sync_title(self):
        session = self.panel.session_for(self.index)
        dirty = " *" if session.is_dirty(self.index) else ""
        self.title_label.setText(f"{self.panel.edit_title(self.index)}{dirty}")
        source = self.panel.edit_source(self.index)
        self.source_label.setText(source + ("\n" + session.source_error if session.source_error else ""))
        self.revert_button.setEnabled(session.is_dirty(self.index))

    def _save(self, _checked=False, *, all_effects=False):
        self.timer.stop()
        if not self._apply():
            return False
        try:
            if not self.panel.save_rows(None if all_effects else [self.index]):
                return False
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            QMessageBox.warning(self, "Effect was not saved", str(exc))
            return False
        self.panel.update_text_status()
        self._apply()
        return True

    def _revert(self):
        self.timer.stop()
        session = self.panel.session_for(self.index)
        item = session.revert(self.index)
        with QSignalBlocker(self.code):
            self.code.setPlainText(session.drafts[self.index])
        self.panel.apply_text_item(self.index, item)
        self._sync_title()
        self.code.set_error("")
        self._set_save_enabled(False)
