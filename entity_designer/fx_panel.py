"""Destruction FX preview of the Entity Designer.

Effects can be edited as script text, previewed live and explicitly saved.
``DestructionFxPanel`` is the tab page (which effects to show) and owns the
preview clock; ``transport_bar`` is the wide bar the editor places under
the viewport (playback, speed and the visual timeline).
"""

from __future__ import annotations

import math
from dataclasses import replace
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, Signal, QItemSelectionModel
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QMenu,
    QListWidgetItem, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionButton, QStyleOptionViewItem,
    QVBoxLayout, QWidget, QMessageBox,
)

from entity_designer import destruction_fx as fx
from entity_designer.fx_audio import FxSoundPlayer
from entity_designer.fx_authoring import FxTextSession, effect_text
from studio.ui.widgets import LIST_BASE, LIST_ALTERNATE, LIST_SELECTED, LIST_HOVER

SPEEDS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)
MODE_COLORS = {
    fx.MODE_VISUAL: "#5fb7e8",
    fx.MODE_PHYSICAL: "#d98c4a",
    fx.MODE_DECAL: "#9b8be0",
    "dest": "#5cbf72",
}
STATUS_GLYPHS = {
    fx.SEVERITY_FATAL: ("✖", "Breaks the script: the whole file stops loading."),
    fx.SEVERITY_UNKNOWN: ("?", "Contains keys Studio does not know."),
    fx.SEVERITY_IGNORED: ("⦸", "Ignored by the engine."),
    fx.SEVERITY_WARNING: ("⚠", "Loads with fallback values."),
}
# Preview tail after the last effect before a loop restarts.
LOOP_TAIL_MS = 400.0
EMPTY_TIMELINE_MS = 2000.0

# Compact version for the "Hits a unit / Hits the ground" choice.
_SMALL_BUTTON = """
QPushButton {
    background: #252e3a; color: #eef0f4; border: 1px solid #8190a4;
    border-radius: 4px; padding: 1px 8px; font-size: 11px; min-height: 18px;
}
QPushButton:hover:!checked { background: #394759; border-color: #bdd3e7; }
QPushButton:checked { background: #11799e; border: 2px solid #a9edff; color: #ffffff; padding: 0px 7px; }
QPushButton:checked:hover { background: #178bac; }
"""

# Icon-only transport buttons: narrow enough for seven of them in a small window.
_DELETE_BUTTON = """
QPushButton {
    background: #3a2a2e; color: #ffd9d9; border: 1px solid #b06a6a;
    border-radius: 4px; padding: 1px 8px; font-size: 11px; min-height: 18px;
}
QPushButton:hover { background: #5a3036; border-color: #ff9a9a; }
QPushButton:disabled { background: #2a2c31; color: #6b6f78; border-color: #4a4f58; }
"""
_ICON_BUTTON = """
QPushButton {
    background: #252e3a; color: #eef0f4; border: 1px solid #8190a4;
    border-radius: 5px; padding: 3px 4px; font-size: 13px; min-height: 24px;
}
QPushButton:hover:!checked { background: #394759; border-color: #bdd3e7; }
QPushButton:checked { background: #11799e; border: 2px solid #a9edff; color: #ffffff; padding: 2px 3px; }
QPushButton:checked:hover { background: #178bac; }
QPushButton:disabled { color: #6b6f78; }
"""

_BIG_BUTTON = """
QPushButton {
    background: #252e3a; color: #eef0f4; border: 1px solid #8190a4;
    border-radius: 5px; padding: 3px 11px; font-size: 13px; min-height: 24px;
}
QPushButton:hover:!checked { background: #394759; border-color: #bdd3e7; }
QPushButton:checked { background: #11799e; border: 2px solid #a9edff; color: #ffffff; padding: 2px 10px; }
QPushButton:checked:hover { background: #178bac; }
QPushButton:disabled { color: #6b6f78; }
"""



def _palette_icon(muted: bool) -> QIcon:
    """Palette glyph (three color dots); a white slash when switched off."""

    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#8d96a3" if muted else "#eef0f4")
    painter.setPen(QPen(color, 1.4))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(2, 2, 16, 16))
    painter.setPen(Qt.PenStyle.NoPen)
    for center, dot in (((7, 8), "#e0524a"), ((13, 8), "#52c46b"), ((10, 13.5), "#4f8fe8")):
        painter.setBrush(QColor("#6e7782") if muted else QColor(dot))
        painter.drawEllipse(QPointF(*center), 2.3, 2.3)
    if muted:
        painter.setPen(QPen(QColor("#eef0f4"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(3, 17), QPointF(17, 3))
    painter.end()
    return QIcon(pixmap)


def _issues_status(issues) -> str:
    for severity in (fx.SEVERITY_FATAL, fx.SEVERITY_UNKNOWN,
                     fx.SEVERITY_IGNORED, fx.SEVERITY_WARNING):
        if any(issue.severity == severity for issue in issues):
            return severity
    return ""


def _draw_fx_checkbox(painter, rect, checked, enabled=True):
    """Draw the same clear on/off indicator for FX options and effect rows."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(rect).adjusted(1., 1., -1., -1.)
    border = "#b4f0ff" if checked else "#d0d7e2"
    fill = "#39b8d9" if checked else "#151b23"
    if not enabled:
        border, fill = "#7f8b9b", "#566675" if checked else "#252e3a"
    painter.setPen(QPen(QColor(border), 1.5))
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(box, 2., 2.)
    if checked:
        tick = QPainterPath()
        tick.moveTo(box.left() + box.width() * .2, box.top() + box.height() * .5)
        tick.lineTo(box.left() + box.width() * .43, box.top() + box.height() * .74)
        tick.lineTo(box.left() + box.width() * .82, box.top() + box.height() * .25)
        painter.setPen(QPen(QColor("#071b25"), 2., Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(tick)
    painter.restore()


class FxCheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        _draw_fx_checkbox(painter, rect, self.isChecked(), self.isEnabled())
        painter.end()


class FxEffectDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.data(Qt.ItemDataRole.CheckStateRole) is None:
            return
        visible = QStyleOptionViewItem(option)
        self.initStyleOption(visible, index)
        rect = visible.widget.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, visible, visible.widget)
        _draw_fx_checkbox(painter, rect, visible.checkState == Qt.CheckState.Checked,
                          bool(visible.state & QStyle.StateFlag.State_Enabled))


class FxTimeline(QWidget):
    """Ruler, one bar per effect, fades, model switch marks and the playhead."""


    ROW = 18
    RULER = 18
    LABEL = 250

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.tracks: list[fx.PreviewTrack] = []
        self.length = EMPTY_TIMELINE_MS
        self.time = 0.0
        self.highlight: int | None = None
        self.setToolTip(
            "Effect timing overview. Solid = visible, darker ends = fade in/out, "
            "ticks = model switch, hatched = approximated physics.")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_tracks([], EMPTY_TIMELINE_MS)

    def set_tracks(self, tracks, length: float) -> None:
        self.tracks = list(tracks)
        self.length = max(length, 1.0)
        self.setMinimumHeight(self.RULER + self.ROW * max(1, len(self.tracks)) + 6)
        self.update()

    def set_time(self, time_ms: float) -> None:
        self.time = time_ms
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(800, self.minimumHeight())

    def _x(self, time_ms: float) -> float:
        span = max(1.0, self.width() - self.LABEL - 10)
        return self.LABEL + span * max(0.0, min(1.0, time_ms / self.length))

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(22, 24, 28))
        painter.setPen(QColor(120, 126, 136))
        step = 100.0
        while self.length / step > 14:
            step *= 2.0 if str(int(step))[0] in "15" else 2.5
        tick = 0.0
        while tick <= self.length + 1e-6:
            x = self._x(tick)
            painter.drawLine(QPointF(x, self.RULER - 6), QPointF(x, self.RULER))
            painter.drawText(QPointF(x + 3, self.RULER - 8), f"{tick / 1000:g} s")
            tick += step
        for row, track in enumerate(self.tracks):
            top = self.RULER + 2 + row * self.ROW
            selected = track.source == self.highlight
            if selected:
                painter.fillRect(QRectF(0, top, self.width(), self.ROW),
                                 QColor(45, 52, 64))
            painter.setPen(QColor(240, 242, 246) if selected else QColor(180, 186, 196))
            painter.drawText(QRectF(8, top, self.LABEL - 14, self.ROW),
                             Qt.AlignmentFlag.AlignVCenter, track.label)
            end = track.end if math.isfinite(track.end) else self.length
            x0, x1 = self._x(track.start), self._x(end)
            color = QColor("#d98c4a" if track.approximate else "#5fb7e8")
            if track.source < 0:
                color = QColor("#5cbf72")
            bar = QRectF(x0, top + 4, max(3.0, x1 - x0), self.ROW - 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color if selected else color.darker(140))
            painter.drawRoundedRect(bar, 3, 3)
            painter.setBrush(color.darker(260))
            if track.fade_in > 0:
                painter.drawRect(QRectF(
                    x0, bar.top(), self._x(track.start + track.fade_in) - x0,
                    bar.height()))
            if track.fade_out > 0 and math.isfinite(track.end):
                start = self._x(track.end - track.fade_out)
                painter.drawRect(QRectF(start, bar.top(), x1 - start, bar.height()))
            if track.approximate:
                painter.setBrush(Qt.BrushStyle.BDiagPattern)
                painter.drawRect(bar)
            painter.setPen(QPen(QColor(255, 255, 255, 170), 1.2))
            for mark in track.marks:
                x = self._x(mark)
                painter.drawLine(QPointF(x, bar.top()), QPointF(x, bar.bottom()))
        x = self._x(self.time)
        painter.setPen(QPen(QColor(255, 214, 90), 2.0))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        painter.end()



class FxEffectsList(QListWidget):
    """Select rows normally; paint visibility only from their checkboxes."""

    visibilityEdited = Signal()
    toggleSelectionRequested = Signal()
    editRequested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_state = None
        self._drag_row = -1

    def checkbox_rect(self, row):
        """Use the delegate's actual indicator bounds for mouse hit testing."""
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        self.itemDelegate().initStyleOption(option, self.indexFromItem(row))
        option.rect = self.visualItemRect(row)
        return self.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator,
                                           option, self)

    def mousePressEvent(self, event):
        row = self.itemAt(event.position().toPoint())
        painting = (event.button() == Qt.MouseButton.LeftButton
                    and row is not None and row.data(Qt.ItemDataRole.UserRole) is not None
                    and self.checkbox_rect(row).contains(event.position().toPoint()))
        self._drag_state = (Qt.CheckState.Unchecked if row.checkState() == Qt.CheckState.Checked
                            else Qt.CheckState.Checked) if painting else None
        if not painting:
            super().mousePressEvent(event)
            return
        self._drag_row = self.row(row)
        with QSignalBlocker(self):
            super().mousePressEvent(event)
            row.setCheckState(self._drag_state)
        self.visibilityEdited.emit()

    def mouseDoubleClickEvent(self, event):
        row = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and row is not None \
                and row.data(Qt.ItemDataRole.UserRole) is not None \
                and self.checkbox_rect(row).contains(event.position().toPoint()):
            # A second rapid click arrives as a double-click, but still toggles once.
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_state is not None and event.buttons() & Qt.MouseButton.LeftButton:
            row = self.itemAt(event.position().toPoint())
            if row is not None and self.checkbox_rect(row).contains(event.position().toPoint()):
                position = self.row(row)
                with QSignalBlocker(self):
                    for index in range(min(position, self._drag_row), max(position, self._drag_row) + 1):
                        item = self.item(index)
                        if item.data(Qt.ItemDataRole.UserRole) is not None:
                            item.setCheckState(self._drag_state)
                self._drag_row = position
                self.visibilityEdited.emit()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        state = self._drag_state
        self._drag_state = None
        if state is None:
            super().mouseReleaseEvent(event)
            row = self.itemAt(event.position().toPoint())
            if (event.button() == Qt.MouseButton.LeftButton
                    and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    and row is not None and row.data(Qt.ItemDataRole.UserRole) is not None
                    and not self.checkbox_rect(row).contains(event.position().toPoint())):
                self.editRequested.emit(row.data(Qt.ItemDataRole.UserRole))
            return
        # Native checkbox handling on release must not undo the painted state.
        with QSignalBlocker(self):
            super().mouseReleaseEvent(event)
            row = self.itemAt(event.position().toPoint())
            if row is not None and row.data(Qt.ItemDataRole.UserRole) is not None \
                    and self.checkbox_rect(row).contains(event.position().toPoint()):
                row.setCheckState(state)
        self.visibilityEdited.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = self.currentItem()
            if row is not None and row.data(Qt.ItemDataRole.UserRole) is not None:
                self.editRequested.emit(row.data(Qt.ItemDataRole.UserRole))
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space:
            self.toggleSelectionRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class FxEffectsView(QWidget):
    """Effects list with its four visibility buttons.

    A click on an effect's text opens its code in the editor that slides out
    of the box, a click on its checkbox shows or hides it. Which effects are
    hidden lives in the panel.
    """

    HINT = "Click text to edit code · Checkboxes show/hide · Ctrl/Shift select rows"

    def __init__(self, panel, *, menu_source=None, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._menu_source = menu_source or self.create_menu
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        hint = QLabel(self.HINT, self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0aa; font-size: 11px;")
        layout.addWidget(hint)
        self.list = FxEffectsList(self)
        self.list.setItemDelegate(FxEffectDelegate(self.list))
        self.list.setAlternatingRowColors(True)
        self.list.setStyleSheet(
            f"QListWidget {{ font-size: 12px; background: {LIST_BASE}; alternate-background-color: {LIST_ALTERNATE}; border: 1px solid #343b46; border-radius: 5px; }}"
            "QListWidget::indicator { width: 20px; height: 20px; }"
            "QListWidget::item { padding: 8px 5px; border-bottom: 1px solid #303743; color: #edf1f7; }"
            f"QListWidget::item:hover:!selected {{ background: {LIST_HOVER}; color: #ffffff; }}"
            f"QListWidget::item:selected {{ background: {LIST_SELECTED}; color: #ffffff; }}"
            f"QListWidget::item:selected:!active {{ background: {LIST_SELECTED}; color: #ffffff; }}")
        self.list.setToolTip("Click text to edit code. Click or drag over checkboxes to show/hide. "
                             "Ctrl/Shift selects multiple effects; Space toggles their visibility.")
        self.list.setMouseTracking(True)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.visibilityEdited.connect(self.visibility_edited)
        self.list.toggleSelectionRequested.connect(self.toggle_selected_visibility)
        self.list.editRequested.connect(self.panel.edit_effect_code)
        self.list.itemChanged.connect(self._item_toggled)
        self.list.currentRowChanged.connect(self._row_selected)
        layout.addWidget(self.list, 1)
        buttons = QGridLayout()
        self.actions = []
        definitions = (
            ("Show All", "show_all_button", panel.show_all),
            ("Hide All", "hide_all_button", panel.hide_all),
            ("Hide Selected", "hide_selected_button", lambda: self.set_selected_visibility(False)),
            ("Show Selected", "show_selected_button", self.show_selected),
        )
        for index, (text, name, callback) in enumerate(definitions):
            action = QAction(text, self)
            action.triggered.connect(callback)
            self.actions.append(action)
            button = QPushButton(text, self)
            button.setStyleSheet(_SMALL_BUTTON)
            button.clicked.connect(action.trigger)
            action.changed.connect(lambda a=action, b=button: b.setEnabled(a.isEnabled()))
            setattr(self, name, button)
            buttons.addWidget(button, index // 2, index % 2)
        self.show_selected_button.setToolTip("Show only the selected effects and hide all others.")
        # Delete is not a visibility action: it has its own action, shared with the menu.
        self.delete_action = QAction("Delete Effect", self)
        self.delete_action.setToolTip(
            "Remove the selected effects from the script file (asks first).")
        self.delete_action.triggered.connect(self.delete_selected)
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setStyleSheet(_DELETE_BUTTON)
        self.delete_button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.delete_button.setToolTip(self.delete_action.toolTip())
        self.delete_button.clicked.connect(self.delete_action.trigger)
        self.delete_action.changed.connect(
            lambda: self.delete_button.setEnabled(self.delete_action.isEnabled()))
        buttons.addWidget(self.delete_button, 0, 2, 2, 1)
        layout.addLayout(buttons)
        self.list.itemSelectionChanged.connect(self.update_actions)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.show_context_menu)
        self.fill()

    # -- rows ---------------------------------------------------------------------------

    def fill(self, *, keep_selection=False, current=None) -> None:
        panel = self.panel
        selected = {row.data(Qt.ItemDataRole.UserRole) for row in self.list.selectedItems()}
        with QSignalBlocker(self.list):
            self.list.clear()
            for index, item in enumerate(panel.document.items):
                if isinstance(item, fx.FxBlock):
                    color = MODE_COLORS.get(item.mode, "#a0a0a0")
                    text = (f"{item.trigger or 'no trigger'}  ·  "
                            f"{fx.block_summary(item, panel.context)}")
                    issues = fx.interpret_block(item, panel.context).issues
                else:
                    color = MODE_COLORS["dest"]
                    parsed = item.parsed
                    text = (f"{parsed.kind if parsed else '?'}  ·  "
                            f"{fx.dest_summary(item, panel.catalog)}")
                    issues = fx.dest_fx_issues(item)
                glyph, meaning = STATUS_GLYPHS.get(_issues_status(issues), ("", ""))
                parts = text.replace("Vanilla ", "", 1).split("  ·  ", 1)
                trigger = parts[0]
                summary = parts[1] if len(parts) > 1 else ""
                mode, _, detail = summary.partition(" · ")
                title = f"{mode} · {trigger}"
                row = QListWidgetItem(title + (" · Warning" if glyph else "") + "\n" + detail)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Unchecked if index in panel.hidden_items
                                  else Qt.CheckState.Checked)
                dot = QPixmap(12, 12)
                dot.fill(Qt.GlobalColor.transparent)
                painter = QPainter(dot)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(2, 2, 8, 8)
                painter.end()
                row.setIcon(QIcon(dot))
                row.setForeground(QColor("#edf1f7"))
                row.setToolTip("\n".join(
                    [meaning] + [issue.message for issue in issues]).strip())
                row.setData(Qt.ItemDataRole.UserRole, index)
                self.list.addItem(row)
                if keep_selection and index in selected:
                    row.setSelected(True)
            if not panel.document.items:
                placeholder = QListWidgetItem(
                    "No destruction effects in this definition.")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(placeholder)
            if current is not None:
                self.list.setCurrentRow(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.update_actions()

    def mark_dirty(self) -> None:
        """Append a star to the effects with unsaved code."""
        with QSignalBlocker(self.list):
            for position in range(self.list.count()):
                row = self.list.item(position)
                index = row.data(Qt.ItemDataRole.UserRole)
                if index is not None:
                    lines = row.text().split("\n", 1)
                    lines[0] = lines[0].removesuffix(" *") + (
                        " *" if self.panel.text_session.is_dirty(index) else "")
                    row.setText("\n".join(lines))

    def sync_checks(self) -> None:
        with QSignalBlocker(self.list):
            for position in range(self.list.count()):
                row = self.list.item(position)
                index = row.data(Qt.ItemDataRole.UserRole)
                if index is not None:
                    row.setCheckState(Qt.CheckState.Unchecked
                                      if index in self.panel.hidden_items
                                      else Qt.CheckState.Checked)

    def select_effect(self, index) -> None:
        for position in range(self.list.count()):
            if self.list.item(position).data(Qt.ItemDataRole.UserRole) == index:
                self.list.setCurrentRow(position)
                return

    # -- actions ------------------------------------------------------------------------

    def update_actions(self) -> None:
        available = bool(self.panel.document.items)
        selected = any(row.data(Qt.ItemDataRole.UserRole) is not None
                       for row in self.list.selectedItems())
        for index, action in enumerate(self.actions):
            action.setEnabled(available and (index < 2 or selected))
        self.delete_action.setEnabled(available and selected)

    def selected_indexes(self) -> list[int]:
        return sorted(row.data(Qt.ItemDataRole.UserRole) for row in self.list.selectedItems()
                      if row.data(Qt.ItemDataRole.UserRole) is not None)

    def delete_selected(self) -> None:
        self.panel.delete_effects(self.selected_indexes())

    def create_menu(self):
        """Reuse the actions shared with the buttons: Delete and the four visibility ones."""
        menu = QMenu(self)
        edit = menu.addAction("Edit Effect Code")
        current = self.list.currentItem()
        index = current.data(Qt.ItemDataRole.UserRole) if current else None
        edit.setEnabled(index is not None)
        edit.triggered.connect(lambda: self.panel.edit_effect_code(index))
        menu.addAction(self.delete_action)
        menu.addSeparator()
        for action in self.actions:
            menu.addAction(action)
        return menu

    def show_context_menu(self, position) -> None:
        row = self.list.itemAt(position)
        if row is not None and row.data(Qt.ItemDataRole.UserRole) is not None:
            if not row.isSelected():
                self.list.setCurrentItem(row, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            else:
                self.list.setCurrentItem(row, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._menu_source().exec(self.list.viewport().mapToGlobal(position))

    def visibility_edited(self) -> None:
        current = self.list.currentItem()
        self.panel.set_highlight(current.data(Qt.ItemDataRole.UserRole) if current is not None else None)
        self.update_actions()
        self.panel.set_hidden_items({
            row.data(Qt.ItemDataRole.UserRole) for position in range(self.list.count())
            if (row := self.list.item(position)).data(Qt.ItemDataRole.UserRole) is not None
            and row.checkState() != Qt.CheckState.Checked})

    def set_selected_visibility(self, visible) -> None:
        with QSignalBlocker(self.list):
            for row in self.list.selectedItems():
                if row.data(Qt.ItemDataRole.UserRole) is not None:
                    row.setCheckState(Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
        self.visibility_edited()

    def toggle_selected_visibility(self) -> None:
        rows = self.list.selectedItems()
        if rows:
            self.set_selected_visibility(any(row.checkState() != Qt.CheckState.Checked for row in rows))

    def show_selected(self) -> None:
        selected = {row.data(Qt.ItemDataRole.UserRole) for row in self.list.selectedItems()
                    if row.data(Qt.ItemDataRole.UserRole) is not None}
        if selected:
            self.panel.set_hidden_items(set(range(len(self.panel.document.items))) - selected)

    def _item_toggled(self, row: QListWidgetItem) -> None:
        index = row.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        hidden = set(self.panel.hidden_items)
        (hidden.discard if row.checkState() == Qt.CheckState.Checked else hidden.add)(index)
        self.panel.set_hidden_items(hidden)

    def _row_selected(self, position: int) -> None:
        row = self.list.item(position) if position >= 0 else None
        self.panel.set_highlight(row.data(Qt.ItemDataRole.UserRole) if row else None)


class DestructionFxPanel(QWidget):
    """Tab page and preview clock of the Destruction FX preview."""

    frameChanged = Signal()
    fitRequested = Signal()
    documentEdited = Signal()
    draftStateChanged = Signal()
    sourceSaved = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = fx.CONTEXT_VEHICLE
        self.document = fx.FxDocument()
        self.text_session = FxTextSession(self.document, self.context)
        self.code_drawer = None
        self._pending_code_index = None
        self.catalog: dict[int, fx.LegacyFxVehicle] = {}
        self.weapons: dict[int, fx.WeaponDef] = {}
        self.debuffs: dict[int, fx.DebuffSpec] = {}
        self.unit = fx.UnitPhases()
        self.current_trigger = fx.SEQUENCE_TRIGGER
        self.origin = (0.0, 0.0, 0.0)
        self.ground_y = 0.0
        self.unit_radius = 0.0
        self.seed = 1
        self.time_ms = 0.0
        self.speed = 1.0
        self.hidden_items: set[int] = set()
        self.highlight: int | None = None
        self.simulation: fx.PreviewSimulation | None = None
        self.sound = FxSoundPlayer()
        self._sound_events: list = []
        # Clock position the sounds were last advanced to (-1 = before 0 ms).
        self._sound_cursor = -1.0
        self._sound_notes: list[str] = []
        self._last_tick = None
        self._build_page()
        self.transport_bar = self._build_transport_bar()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._rebuild_simulation()

    # -- page ----------------------------------------------------------------------

    def _build_page(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(3)

        outer = layout
        preview = QGroupBox("Preview", self)
        layout = QVBoxLayout(preview)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.weapon_box = QWidget(self)
        weapon_row = QHBoxLayout(self.weapon_box)
        weapon_row.setContentsMargins(0, 0, 0, 0)
        weapon_row.setSpacing(6)
        self.weapon_group = QButtonGroup(self)
        for text, trigger, tip in (
                ("Hits a unit", fx.WEAPON_HIT_UNIT,
                 "Projectile hits a unit: dead model, detonate and dest_fx death."),
                ("Hits the ground", fx.WEAPON_HIT_GROUND,
                 "Projectile hits terrain: megadeth model, impact_world and "
                 "dest_fx megadeth.")):
            button = QPushButton(text, self.weapon_box)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setStyleSheet(_SMALL_BUTTON)
            button.setProperty("trigger", trigger)
            self.weapon_group.addButton(button)
            weapon_row.addWidget(button)
        weapon_row.addStretch(1)
        self.weapon_group.buttonClicked.connect(
            lambda button: self._set_trigger(button.property("trigger")))
        layout.addWidget(self.weapon_box)

        self.altitude_box = QWidget(self)
        altitude_row = QHBoxLayout(self.altitude_box)
        altitude_row.setContentsMargins(0, 0, 0, 0)
        self.altitude_label = QLabel("Flight altitude", self.altitude_box)
        altitude_row.addWidget(self.altitude_label)
        self.altitude_slider = QSlider(Qt.Orientation.Horizontal, self.altitude_box)
        self.altitude_slider.setRange(0, 3000)
        self.altitude_spin = QSpinBox(self.altitude_box)
        self.altitude_spin.setRange(0, 3000)
        self.altitude_spin.setSingleStep(50)
        self.altitude_spin.setMinimumWidth(80)
        altitude_row.addWidget(self.altitude_slider, 1)
        altitude_row.addWidget(self.altitude_spin)
        self.altitude_slider.valueChanged.connect(self.altitude_spin.setValue)
        self.altitude_spin.valueChanged.connect(self._altitude_changed)
        layout.addWidget(self.altitude_box)

        self.show_unit = FxCheckBox("Show the unit and its model changes", self)
        self.show_unit.setChecked(True)
        self.show_unit.toggled.connect(lambda _on: self.frameChanged.emit())
        layout.addWidget(self.show_unit)
        self.follow_unit = FxCheckBox("Follow unit with camera", self)
        self.follow_unit.setChecked(True)
        self.follow_unit.setToolTip("Follow the unit's position during flight and falling. Disable to explore the scene freely.")
        self.follow_unit.toggled.connect(lambda _on: self.fitRequested.emit())
        self.follow_unit.toggled.connect(lambda _on: self.frameChanged.emit())
        layout.addWidget(self.follow_unit)

        outer.addWidget(preview)
        self.effects_box = QGroupBox("Effects", self)
        outer.addWidget(self.effects_box, 1)
        layout = QVBoxLayout(self.effects_box)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.reload_note = QLabel("Auto reload enabled · source files are watched", self)
        self.reload_note.setWordWrap(True)
        self.reload_note.setStyleSheet("color: #9fc3ad; font-size: 11px;")
        layout.addWidget(self.reload_note)
        self.effects_view = FxEffectsView(
            self, menu_source=lambda: self._create_effects_context_menu())
        # The list and its buttons stay reachable under their old names.
        self.effects_list = self.effects_view.list
        self._visibility_actions = self.effects_view.actions
        self.show_all_button = self.effects_view.show_all_button
        self.hide_all_button = self.effects_view.hide_all_button
        self.hide_selected_button = self.effects_view.hide_selected_button
        self.show_selected_button = self.effects_view.show_selected_button
        layout.addWidget(self.effects_view, 1)
        self.notes_label = QLabel(self)
        self.notes_label.setWordWrap(True)
        self.notes_label.setStyleSheet("color: #9aa0aa;")
        layout.addWidget(self.notes_label)

    # -- transport bar ------------------------------------------------------------------

    def _build_transport_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("fxTransport")
        bar.setStyleSheet("QFrame#fxTransport { background: #1b1d22;"
                          " border-top: 1px solid #343842; }")
        bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(4)

        def button(text: str, tip: str, checkable: bool = False) -> QPushButton:
            widget = QPushButton(text, bar)
            widget.setToolTip(tip)
            widget.setCheckable(checkable)
            widget.setStyleSheet(_BIG_BUTTON)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            controls.addWidget(widget)
            return widget

        self.restart_button = button("Restart", "Play again from the start")
        self.play_button = button("Play", "Play / pause", True)
        self.stop_button = button("Stop", "Stop and rewind")
        self.loop_button = button("Loop", "Repeat the preview", True)
        self.loop_button.setChecked(True)
        self.mute_button = button("Mute", "Mute the preview sounds", True)
        self.pal_button = button("Palette", "Switch off the palette flashes (pal_*)", True)
        for widget, symbol in (
                (self.restart_button, QStyle.StandardPixmap.SP_MediaSkipBackward),
                (self.play_button, QStyle.StandardPixmap.SP_MediaPlay),
                (self.stop_button, QStyle.StandardPixmap.SP_MediaStop),
                (self.loop_button, QStyle.StandardPixmap.SP_BrowserReload),
                (self.mute_button, QStyle.StandardPixmap.SP_MediaVolume)):
            widget.setText("")
            widget.setIcon(self._transport_icon(symbol))
            widget.setIconSize(QSize(20, 20))
            widget.setStyleSheet(_ICON_BUTTON)
            widget.setMinimumWidth(32)
        self.restart_button.setAccessibleName("Restart")
        self.play_button.setAccessibleName("Play / pause")
        self.stop_button.setAccessibleName("Stop")
        self.loop_button.setAccessibleName("Loop")
        self.mute_button.setAccessibleName("Mute sound")
        self.mute_button.toggled.connect(self._mute_toggled)
        self.pal_button.setText("")
        self.pal_button.setIcon(_palette_icon(False))
        self.pal_button.setIconSize(QSize(20, 20))
        self.pal_button.setStyleSheet(_ICON_BUTTON)
        self.pal_button.setMinimumWidth(32)
        self.pal_button.toggled.connect(self._palette_toggled)
        self.pal_button.setAccessibleName("Mute palette flashes")
        self.restart_button.clicked.connect(self.restart)
        self.play_button.toggled.connect(self._play_toggled)
        self.stop_button.clicked.connect(self.stop)
        controls.addSpacing(10)
        self.speed_combo = QComboBox(bar)
        self.speed_combo.setToolTip("Playback speed")
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.speed_combo.setMinimumContentsLength(3)
        self.speed_combo.currentIndexChanged.connect(self._speed_changed)
        controls.addWidget(self.speed_combo)
        controls.addStretch(1)
        self.time_label = QLabel(bar)
        self.time_label.setStyleSheet(
            "font-size: 13px; font-family: monospace; color: #ffd65a;")
        controls.addWidget(self.time_label)
        layout.addLayout(controls)

        self.timeline = FxTimeline(bar)
        scroll = QScrollArea(bar)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.timeline)
        # A few rows are visible at once; longer lists scroll.
        scroll.setMinimumHeight(80)
        scroll.setMaximumHeight(122)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(scroll)
        return bar

    # -- data in ------------------------------------------------------------------------

    def set_context(self, context: str, document: fx.FxDocument, *,
                    catalog=None, unit: fx.UnitPhases | None = None,
                    keep_view: bool = False, weapons=None, debuffs=None) -> None:
        """Show one definition; ``keep_view`` keeps time and hidden effects."""

        self.context = context
        self.document = document
        self.text_session = FxTextSession(document, context)
        if self.code_drawer is not None:
            self._pending_code_index = (self.code_drawer.index
                                        if keep_view and self.code_drawer.expanded else None)
            self.code_drawer.reset()
        self.catalog = dict(catalog or {})
        self.weapons = dict(weapons or {})
        self.debuffs = dict(debuffs or {})
        self.unit = unit or fx.UnitPhases(weapon=context == fx.CONTEXT_WEAPON)
        if not keep_view:
            self.hidden_items.clear()
            self.highlight = None
            self.seed = 1
            self.time_ms = 0.0
            self.current_trigger = (fx.WEAPON_HIT_UNIT
                                    if context == fx.CONTEXT_WEAPON
                                    else fx.SEQUENCE_TRIGGER)
        weapon = context == fx.CONTEXT_WEAPON
        self.weapon_box.setVisible(weapon)
        self.show_unit.setText(
            "Show the weapon and its model changes" if weapon
            else "Show the unit and its model changes")
        self.follow_unit.setText(
            "Follow weapon with camera" if weapon
            else "Follow unit with camera")
        self.follow_unit.setToolTip(
            "Follow the weapon's position during flight and impact. Disable "
            "to explore the scene freely." if weapon else
            "Follow the unit's position during flight and falling. Disable "
            "to explore the scene freely.")
        for widget in self.weapon_group.buttons():
            widget.setChecked(widget.property("trigger") == self.current_trigger)
        self.altitude_box.setVisible(weapon or self.unit.flies)
        # Vehicles: kill height of flying units (0 = ground). Weapons: the
        # height the projectile falls from, so it can never be 0.
        minimum = 50 if weapon else 0
        self.altitude_label.setText("Missile altitude" if weapon else "Flight altitude")
        self.altitude_label.setToolTip(
            "Height the weapon starts from before it falls onto the target. "
            "Higher means a longer flight." if weapon else
            "Height of the kill for flying units: the unit explodes in the "
            "air (air_destroyed), falls, and the ground effects start on "
            "impact. 0 = killed on the ground.")
        with QSignalBlocker(self.altitude_spin), QSignalBlocker(self.altitude_slider):
            for widget in (self.altitude_spin, self.altitude_slider):
                widget.setRange(minimum, 3000)
            value = int(self.unit.altitude) if self.unit.altitude > 0 else int(
                fx.WEAPON_LAUNCH_HEIGHT if weapon else 0)
            self.altitude_spin.setValue(value)
            self.altitude_slider.setValue(value)
        count = len(self.document.items)
        self.hidden_items = {index for index in self.hidden_items if index < count}
        self._fill_list()
        self._rebuild_simulation()
        self.seek(min(self.time_ms, self._length()))
        if keep_view and self.is_playing() and self._sound_cursor >= 0:
            # A reload while playing: loops that are in progress start again.
            self.sound.resume(self._sound_events, self.time_ms)

    def set_scene(self, origin, ground_y: float, unit_radius: float, *, transform=None, overeof=None,
                  scale=None, euler=None) -> None:
        values = (tuple(origin), float(ground_y), float(unit_radius))
        unit = replace(self.unit, transform=transform if transform is not None else self.unit.transform,
                       overeof=overeof if overeof is not None else self.unit.overeof,
                       scale=scale if scale is not None else self.unit.scale,
                       euler=euler if euler is not None else self.unit.euler)
        if values == (self.origin, self.ground_y, self.unit_radius) and unit == self.unit:
            return
        self.origin, self.ground_y, self.unit_radius = values
        self.unit = unit
        self._rebuild_simulation()

    def _set_trigger(self, trigger: str) -> None:
        self.current_trigger = trigger
        self._rebuild_simulation()
        self.seek(0.0)

    def _altitude_changed(self, value: int) -> None:
        with QSignalBlocker(self.altitude_slider):
            self.altitude_slider.setValue(value)
        self.unit = replace(self.unit, altitude=float(value))
        self._rebuild_simulation()

    # -- effects list --------------------------------------------------------------------

    def _fill_list(self, **options) -> None:
        self.effects_view.fill(**options)
        if self.code_drawer is not None:
            self.code_drawer.schedule_sync()

    def _create_effects_context_menu(self):
        return self.effects_view.create_menu()

    def _show_effects_context_menu(self, position):
        self.effects_view.show_context_menu(position)

    def set_script_sources(self, origins) -> None:
        self.text_session = FxTextSession(self.document, self.context, origins)
        self.update_text_status()
        if self._pending_code_index is not None and self.document.items:
            index = min(self._pending_code_index, len(self.document.items) - 1)
            self._pending_code_index = None
            self.edit_effect_code(index)

    @property
    def has_unsaved_text(self) -> bool:
        return self.text_session.dirty

    def edit_effect_code(self, index) -> None:
        if index is None or not 0 <= index < len(self.document.items):
            return
        self._ensure_code_drawer().load_effect(index)

    def _ensure_code_drawer(self):
        """Create the editor and its tab once the panel sits in its final window."""
        if self.code_drawer is None:
            from entity_designer.fx_code_drawer import FxCodeDrawer
            self.code_drawer = FxCodeDrawer(self, self.window(), self.effects_box)
        return self.code_drawer

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_code_drawer()

    def delete_effects(self, indexes) -> bool:
        """Remove effects from their script file and from the preview, after asking."""

        indexes = sorted({i for i in indexes if 0 <= i < len(self.document.items)})
        if not indexes:
            return False
        session = self.text_session
        if session.source_error or len(session.sources) != len(self.document.items):
            QMessageBox.warning(
                self, "Effects were not deleted",
                session.source_error or "No script source is attached to these effects.")
            return False
        files = ", ".join(sorted({session.sources[i].path.name for i in indexes}))
        count = len(indexes)
        answer = QMessageBox.question(
            self, "Delete effects",
            f"Delete {count} effect{'s' if count > 1 else ''} from {files}?\n"
            "The script file is changed right away.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        drawer = self.code_drawer
        if drawer is not None:
            drawer.timer.stop()      # an edit waiting for the timer must not revive a deleted effect
        total = len(self.document.items)
        try:
            paths = session.delete(indexes)
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            QMessageBox.warning(self, "Effects were not deleted", str(exc))
            return False
        gone = set(indexes)
        for index in reversed(indexes):
            del self.document.items[index]
        # Effects after a deleted one shift up: hidden marks, highlight and the
        # open editor follow them.
        position = {old: new for new, old in enumerate(
            i for i in range(total) if i not in gone)}
        self.hidden_items = {position[i] for i in self.hidden_items if i in position}
        self.highlight = position.get(self.highlight)
        self.timeline.highlight = self.highlight
        if drawer is not None:
            if drawer.index in position:
                drawer.index = position[drawer.index]
            else:
                drawer.reset()
        self._fill_list()
        self._rebuild_simulation()
        self.update_text_status()
        self.sourceSaved.emit(paths)
        self.documentEdited.emit()
        return True

    def create_play_button(self, parent) -> QPushButton:
        """A Play / Pause button that looks and works like the transport bar's."""

        mirror = QPushButton(parent)
        mirror.setCheckable(True)
        mirror.setStyleSheet(_ICON_BUTTON)
        mirror.setIconSize(QSize(20, 20))
        mirror.setMinimumWidth(32)
        mirror.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        mirror.setAccessibleName("Play / pause")

        def follow(playing: bool) -> None:
            with QSignalBlocker(mirror):
                mirror.setChecked(playing)
            mirror.setIcon(self.play_button.icon())
            mirror.setToolTip(self.play_button.toolTip())

        mirror.toggled.connect(self.play_button.setChecked)
        self.play_button.toggled.connect(follow)
        follow(self.play_button.isChecked())
        return mirror

    def create_speed_combo(self, parent) -> QComboBox:
        """A playback speed list that always shows the transport bar's speed."""

        combo = QComboBox(parent)
        combo.setToolTip("Playback speed")
        for speed in SPEEDS:
            combo.addItem(f"{speed:g}×", speed)
        combo.setCurrentIndex(self.speed_combo.currentIndex())
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(3)
        combo.currentIndexChanged.connect(self.speed_combo.setCurrentIndex)
        self.speed_combo.currentIndexChanged.connect(combo.setCurrentIndex)
        return combo

    def apply_text_item(self, index, item) -> None:
        if effect_text(self.document.items[index]) != effect_text(item):
            self.document.items[index] = item
            self._fill_list(keep_selection=True, current=index)
            self._rebuild_simulation()
            self.documentEdited.emit()
        self.update_text_status()

    def update_text_status(self) -> None:
        self.reload_note.setText(
            "Unsaved effect code · preview updates live · Ctrl+S to save" if self.has_unsaved_text
            else "Auto reload enabled · source files are watched")
        self.effects_view.mark_dirty()
        self.draftStateChanged.emit()

    def confirm_text_changes(self) -> bool:
        if not self.has_unsaved_text:
            return True
        answer = QMessageBox.question(
            self, "Unsaved effect code", "Save the edited effects before leaving this definition?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            try:
                paths = self.text_session.save(range(len(self.document.items)))
                self.sourceSaved.emit(paths)
            except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
                QMessageBox.warning(self, "Effects were not saved", str(exc))
                return False
        else:
            for index in range(len(self.document.items)):
                self.document.items[index] = self.text_session.revert(index)
            self._fill_list()
            self._rebuild_simulation()
        if self.code_drawer is not None:
            self.code_drawer.reset()
        self.update_text_status()
        return True

    def set_hidden_items(self, hidden) -> None:
        """Single place where effect visibility changes."""
        if hidden != self.hidden_items:
            self.hidden_items = set(hidden)
            self._sync_checks()
            self._rebuild_simulation()
        else:
            self.timeline.update()
            self.frameChanged.emit()

    def set_highlight(self, index) -> None:
        self.highlight = index
        self.timeline.highlight = index
        self.timeline.update()
        self.frameChanged.emit()

    def hide_all(self):
        """Disable every effect while keeping the current row selection."""
        self.set_hidden_items(set(range(len(self.document.items))))

    def show_all(self) -> None:
        self.set_hidden_items(set())

    def _sync_checks(self) -> None:
        self.effects_view.sync_checks()

    # -- simulation and clock -----------------------------------------------------------------

    def _rebuild_simulation(self) -> None:
        enabled = set(range(len(self.document.items))) - self.hidden_items
        self.simulation = fx.PreviewSimulation(
            self.document, self.context, self.current_trigger, seed=self.seed,
            origin=self.origin, ground_y=self.ground_y,
            unit_radius=self.unit_radius, legacy_catalog=self.catalog,
            enabled=enabled, unit=self.unit, weapons=self.weapons,
            debuffs=self.debuffs)
        self.timeline.set_tracks(self.simulation.tracks, self._length())
        self.sound.stop_all()
        self._sound_events = self.simulation.sound_events
        self._sound_notes = []
        self.sound.missing.clear()
        self._show_notes()
        self._update_time_label()
        self.frameChanged.emit()

    def _show_notes(self) -> None:
        notes = list(dict.fromkeys(self.simulation.notes)) if self.simulation else []
        if self._sound_notes:
            notes.append("Sound file not found: " + ", ".join(self._sound_notes))
        self.notes_label.setText("\n".join(notes))
        self.notes_label.setVisible(bool(notes))

    def _length(self) -> float:
        duration = self.simulation.duration if self.simulation else 0.0
        return duration if duration > 0 else EMPTY_TIMELINE_MS

    def seek(self, time_ms: float, *, keep_sound: bool = False) -> None:
        self.time_ms = max(0.0, float(time_ms))
        if not keep_sound:
            # A jump in time silences everything; playback restarts from here.
            self.sound.stop_all()
            self._sound_cursor = -1.0 if self.time_ms <= 0 else self.time_ms
        self.timeline.set_time(self.time_ms)
        self._update_time_label()
        self.frameChanged.emit()

    def restart(self) -> None:
        self.seek(0.0)
        self.play_button.setChecked(True)

    def stop(self) -> None:
        self.play_button.setChecked(False)
        self.seek(0.0)

    def is_playing(self) -> bool:
        return self.play_button.isChecked()

    def _transport_icon(self, symbol):
        """Keep classic transport symbols readable against the dark controls."""
        pixmap = self.style().standardIcon(symbol).pixmap(20, 20)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor("#eef0f4"))
        painter.end()
        return QIcon(pixmap)

    def _play_toggled(self, playing: bool) -> None:
        if playing and self.code_drawer is not None and self.code_drawer.timer.isActive():
            self.code_drawer.timer.stop()
            self.code_drawer._apply()
        self.play_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip("Pause destruction preview" if playing else "Play destruction preview")
        if playing:
            if self.time_ms >= self._length():
                self.seek(0.0)
            elif self._sound_cursor >= 0:
                self.sound.resume(self._sound_events, self.time_ms)
            self._last_tick = None
            self._timer.start()
        else:
            self._timer.stop()
            self.sound.stop_all()

    def _palette_toggled(self, muted: bool) -> None:
        self.pal_button.setIcon(_palette_icon(muted))
        self.pal_button.setToolTip(
            "Palette flashes switched off: click to show them" if muted
            else "Switch off the palette flashes (pal_*)")
        self.frameChanged.emit()

    def palette_effects(self) -> list[tuple[int, float]]:
        """[(palette slot, strength)] of the current time; empty when switched off."""

        if self.simulation is None or self.pal_button.isChecked():
            return []
        return self.simulation.palette_effects_at(self.time_ms)

    def _speed_changed(self, index: int) -> None:
        """New playback speed: the sounds follow it and restart their loops."""

        self.speed = float(self.speed_combo.itemData(index))
        self.sound.set_speed(self.speed)
        if self.is_playing() and self._sound_cursor >= 0:
            self.sound.resume(self._sound_events, self.time_ms)

    def _mute_toggled(self, muted: bool) -> None:
        self.mute_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted if muted
            else QStyle.StandardPixmap.SP_MediaVolume))
        self.mute_button.setToolTip(
            "Sound muted: click to hear the preview" if muted
            else "Mute the preview sounds")
        self.sound.set_muted(muted)
        if not muted and self.is_playing() and self._sound_cursor >= 0:
            self.sound.resume(self._sound_events, self.time_ms)

    def _advance_sound(self, now: float) -> None:
        """Start and stop the sounds the clock just passed."""

        previous = self._sound_cursor
        if now < previous:
            # The preview looped back to the start.
            self.sound.stop_all()
            previous = -1.0
        self.sound.step(self._sound_events, previous, now)
        self._sound_cursor = now
        missing = sorted(self.sound.missing)
        if missing and missing != self._sound_notes:
            self._sound_notes = missing
            self._show_notes()

    def _tick(self) -> None:
        now = perf_counter()
        if self._last_tick is None:
            self._last_tick = now
            return
        delta = (now - self._last_tick) * 1000.0 * self.speed
        self._last_tick = now
        time_ms = self.time_ms + delta
        length = self._length()
        if time_ms > length + LOOP_TAIL_MS:
            if self.loop_button.isChecked():
                time_ms = 0.0
            else:
                time_ms = length
                self.play_button.setChecked(False)
        self._advance_sound(time_ms)
        self.seek(time_ms, keep_sound=True)

    def _update_time_label(self) -> None:
        self.time_label.setText(
            f"{self.time_ms / 1000:.2f} / {self._length() / 1000:.2f} s")

    def preview_items(self):
        return self.simulation.items_at(self.time_ms) if self.simulation else []

    def offset_boxes(self):
        return self.simulation.offset_boxes() if self.simulation else []

    def required_vps(self) -> set[int]:
        """Every VP the preview can show, for owner loading."""

        unit = self.unit
        vps: set[int] = {ref.vp for ref in (unit.normal, unit.dead, unit.megadeth)
                         if ref.vp > 0}
        if unit.decoration is not None and unit.decoration.visual.vp > 0:
            vps.add(unit.decoration.visual.vp)
        if not unit.weapon and unit.genesis.vp > 0:
            vps.add(unit.genesis.vp)
        if unit.weapon and unit.launch.vp > 0:
            vps.add(unit.launch.vp)
        if unit.weapon:
            # Cluster children bring their own models and effects.
            vps |= fx.weapon_vps(unit, self.document, self.weapons, None,
                                 self.debuffs)
        for item in self.document.items:
            if isinstance(item, fx.FxBlock):
                spec = fx.interpret_block(item, self.context)
                vps.update(visual.vp for visual in spec.visuals if visual.vp > 0)
                vps.update(vp for vp in spec.vp_models if vp > 0)
                vps.update(vp for vp in spec.vp_impacts if vp > 0)
            else:
                dest = item.parsed
                if dest is not None:
                    vps.update(self._legacy_vps(dest.vehicle_id, 0))
        return vps

    def _legacy_vps(self, vehicle_id: int, depth: int) -> set[int]:
        vehicle = self.catalog.get(vehicle_id)
        if vehicle is None or depth >= fx.LEGACY_NESTING_LIMIT:
            return set()
        vps = {vp for vp in (vehicle.vp_dead, vehicle.vp_megadeth) if vp > 0}
        if vehicle.scale_fx is not None:
            vps.update(vehicle.scale_fx.vps)
        for child in vehicle.dest_fx:
            vps.update(self._legacy_vps(child.vehicle_id, depth + 1))
        return vps
