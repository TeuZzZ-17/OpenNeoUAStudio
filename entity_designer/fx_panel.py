"""Destruction FX preview of the Entity Designer.

Preview only: effects are edited in the script files and reloaded here.
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
    QVBoxLayout, QWidget,
)

from entity_designer import destruction_fx as fx

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
        if event.key() == Qt.Key.Key_Space:
            self.toggleSelectionRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class DestructionFxPanel(QWidget):
    """Tab page and preview clock of the Destruction FX preview."""

    frameChanged = Signal()
    fitRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context = fx.CONTEXT_VEHICLE
        self.document = fx.FxDocument()
        self.catalog: dict[int, fx.LegacyFxVehicle] = {}
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
            button.setStyleSheet(_BIG_BUTTON)
            button.setProperty("trigger", trigger)
            self.weapon_group.addButton(button)
            weapon_row.addWidget(button, 1)
        self.weapon_group.buttonClicked.connect(
            lambda button: self._set_trigger(button.property("trigger")))
        layout.addWidget(self.weapon_box)

        self.altitude_box = QWidget(self)
        altitude_row = QHBoxLayout(self.altitude_box)
        altitude_row.setContentsMargins(0, 0, 0, 0)
        altitude_label = QLabel("Flight altitude", self.altitude_box)
        altitude_label.setToolTip(
            "Height of the kill for flying units: the unit explodes in the "
            "air (air_destroyed), falls, and the ground effects start on "
            "impact. 0 = killed on the ground.")
        altitude_row.addWidget(altitude_label)
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
        hint = QLabel("Checkboxes show/hide · Ctrl/Shift select rows", self)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0aa; font-size: 11px;")
        layout.addWidget(hint)
        self.effects_list = FxEffectsList(self)
        self.effects_list.setItemDelegate(FxEffectDelegate(self.effects_list))
        self.effects_list.setStyleSheet(
            "QListWidget { font-size: 12px; background: #1b1e24; border: 1px solid #343b46; border-radius: 5px; }"
            "QListWidget::indicator { width: 20px; height: 20px; }"
            "QListWidget::item { padding: 8px 5px; border-bottom: 1px solid #303743; color: #edf1f7; }"
            "QListWidget::item:hover:!selected { background: #234e64; color: #ffffff; }"
            "QListWidget::item:selected { background: #006d96; color: #ffffff; }"
            "QListWidget::item:selected:!active { background: #006d96; color: #ffffff; }")
        self.effects_list.setToolTip("Click text to select. Click or drag over checkboxes to show/hide. "
                                     "Ctrl/Shift selects multiple effects; Space toggles their visibility.")
        self.effects_list.setMouseTracking(True)
        self.effects_list.setWordWrap(True)
        self.effects_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.effects_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.effects_list.visibilityEdited.connect(self._visibility_edited)
        self.effects_list.toggleSelectionRequested.connect(self.toggle_selected_visibility)
        self.effects_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel)
        self.effects_list.itemChanged.connect(self._item_toggled)
        self.effects_list.currentRowChanged.connect(self._row_selected)
        layout.addWidget(self.effects_list, 1)
        buttons = QGridLayout()
        self._visibility_actions = []
        definitions = (
            ("Show All", "show_all_button", self.show_all),
            ("Hide All", "hide_all_button", self.hide_all),
            ("Hide Selected", "hide_selected_button", lambda: self.set_selected_visibility(False)),
            ("Show Selected", "show_selected_button", self.show_selected),
        )
        for index, (text, name, callback) in enumerate(definitions):
            action = QAction(text, self)
            action.triggered.connect(callback)
            self._visibility_actions.append(action)
            button = QPushButton(text, self)
            button.clicked.connect(action.trigger)
            action.changed.connect(lambda a=action, b=button: b.setEnabled(a.isEnabled()))
            setattr(self, name, button)
            buttons.addWidget(button, index // 2, index % 2)
        self.show_selected_button.setToolTip("Show only the selected effects and hide all others.")
        layout.addLayout(buttons)
        self.effects_list.itemSelectionChanged.connect(self._update_visibility_actions)
        self.effects_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.effects_list.customContextMenuRequested.connect(self._show_effects_context_menu)
        self._update_visibility_actions()
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
        controls.setSpacing(6)

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
        for widget, symbol in (
                (self.restart_button, QStyle.StandardPixmap.SP_MediaSkipBackward),
                (self.play_button, QStyle.StandardPixmap.SP_MediaPlay),
                (self.stop_button, QStyle.StandardPixmap.SP_MediaStop),
                (self.loop_button, QStyle.StandardPixmap.SP_BrowserReload)):
            widget.setText("")
            widget.setIcon(self._transport_icon(symbol))
            widget.setIconSize(QSize(20, 20))
            widget.setMinimumWidth(36)
        self.restart_button.setAccessibleName("Restart")
        self.play_button.setAccessibleName("Play / pause")
        self.stop_button.setAccessibleName("Stop")
        self.loop_button.setAccessibleName("Loop")
        self.restart_button.clicked.connect(self.restart)
        self.play_button.toggled.connect(self._play_toggled)
        self.stop_button.clicked.connect(self.stop)
        controls.addSpacing(10)
        self.speed_combo = QComboBox(bar)
        self.speed_combo.setToolTip("Playback speed")
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.currentIndexChanged.connect(
            lambda index: setattr(self, "speed", float(self.speed_combo.itemData(index))))
        controls.addWidget(self.speed_combo)
        controls.addStretch(1)
        self.time_label = QLabel(bar)
        self.time_label.setStyleSheet(
            "font-size: 15px; font-family: monospace; color: #ffd65a;")
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
                    keep_view: bool = False) -> None:
        """Show one definition; ``keep_view`` keeps time and hidden effects."""

        self.context = context
        self.document = document
        self.catalog = dict(catalog or {})
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
        for widget in self.weapon_group.buttons():
            widget.setChecked(widget.property("trigger") == self.current_trigger)
        self.altitude_box.setVisible(not weapon and self.unit.flies)
        with QSignalBlocker(self.altitude_spin), QSignalBlocker(self.altitude_slider):
            self.altitude_spin.setValue(int(self.unit.altitude))
            self.altitude_slider.setValue(int(self.unit.altitude))
        count = len(self.document.items)
        self.hidden_items = {index for index in self.hidden_items if index < count}
        self._fill_list()
        self._rebuild_simulation()
        self.seek(min(self.time_ms, self._length()))

    def set_scene(self, origin, ground_y: float, unit_radius: float, *, transform=None, overeof=None) -> None:
        values = (tuple(origin), float(ground_y), float(unit_radius))
        unit = replace(self.unit, transform=transform if transform is not None else self.unit.transform,
                       overeof=overeof if overeof is not None else self.unit.overeof)
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
        unit = self.unit
        self.unit = fx.UnitPhases(
            unit.normal, unit.dead, unit.megadeth, unit.model, unit.weapon,
            unit.transform, unit.overeof, float(value))
        self._rebuild_simulation()

    # -- effects list --------------------------------------------------------------------

    def _fill_list(self) -> None:
        with QSignalBlocker(self.effects_list):
            self.effects_list.clear()
            for index, item in enumerate(self.document.items):
                if isinstance(item, fx.FxBlock):
                    color = MODE_COLORS.get(item.mode, "#a0a0a0")
                    text = (f"{item.trigger or 'no trigger'}  ·  "
                            f"{fx.block_summary(item, self.context)}")
                    issues = fx.interpret_block(item, self.context).issues
                else:
                    color = MODE_COLORS["dest"]
                    parsed = item.parsed
                    text = (f"{parsed.kind if parsed else '?'}  ·  "
                            f"{fx.dest_summary(item, self.catalog)}")
                    issues = fx.dest_fx_issues(item)
                glyph, meaning = STATUS_GLYPHS.get(_issues_status(issues), ("", ""))
                parts = text.replace("Vanilla ", "", 1).split("  ·  ", 1)
                trigger = parts[0]
                summary = parts[1] if len(parts) > 1 else ""
                mode, _, detail = summary.partition(" · ")
                title = f"{mode} · {trigger}"
                row = QListWidgetItem(title + (" · Warning" if glyph else "") + "\n" + detail)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Unchecked if index in self.hidden_items
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
                self.effects_list.addItem(row)
            if not self.document.items:
                placeholder = QListWidgetItem(
                    "No destruction effects in this definition.")
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self.effects_list.addItem(placeholder)

        self._update_visibility_actions()

    def _update_visibility_actions(self):
        available = bool(self.document.items)
        selected = any(row.data(Qt.ItemDataRole.UserRole) is not None
                       for row in self.effects_list.selectedItems())
        for index, action in enumerate(self._visibility_actions):
            action.setEnabled(available and (index < 2 or selected))

    def _create_effects_context_menu(self):
        """Reuse the four visibility actions shared with the buttons."""
        menu = QMenu(self)
        for action in self._visibility_actions:
            menu.addAction(action)
        return menu

    def _show_effects_context_menu(self, position):
        row = self.effects_list.itemAt(position)
        if row is not None and row.data(Qt.ItemDataRole.UserRole) is not None:
            if not row.isSelected():
                self.effects_list.setCurrentItem(row, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            else:
                self.effects_list.setCurrentItem(row, QItemSelectionModel.SelectionFlag.NoUpdate)
        self._create_effects_context_menu().exec(self.effects_list.viewport().mapToGlobal(position))

    def _visibility_edited(self):
        current = self.effects_list.currentItem()
        self.highlight = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.timeline.highlight = self.highlight
        self._update_visibility_actions()
        hidden = {row.data(Qt.ItemDataRole.UserRole)
                             for index in range(self.effects_list.count())
                             if (row := self.effects_list.item(index)).data(Qt.ItemDataRole.UserRole) is not None
                             and row.checkState() != Qt.CheckState.Checked}
        if hidden != self.hidden_items:
            self.hidden_items = hidden
            self._rebuild_simulation()
        else:
            self.timeline.update()
            self.frameChanged.emit()

    def set_selected_visibility(self, visible):
        with QSignalBlocker(self.effects_list):
            for row in self.effects_list.selectedItems():
                if row.data(Qt.ItemDataRole.UserRole) is not None:
                    row.setCheckState(Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
        self._visibility_edited()

    def toggle_selected_visibility(self):
        rows = self.effects_list.selectedItems()
        if rows:
            self.set_selected_visibility(any(row.checkState() != Qt.CheckState.Checked for row in rows))

    def show_selected(self):
        selected = {row.data(Qt.ItemDataRole.UserRole) for row in self.effects_list.selectedItems()
                    if row.data(Qt.ItemDataRole.UserRole) is not None}
        if selected:
            self.hidden_items = set(range(len(self.document.items))) - selected
            self._sync_checks()
            self._rebuild_simulation()

    def _item_toggled(self, row: QListWidgetItem) -> None:
        index = row.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        if row.checkState() == Qt.CheckState.Checked:
            self.hidden_items.discard(index)
        else:
            self.hidden_items.add(index)
        self._rebuild_simulation()

    def hide_all(self):
        """Disable every effect while keeping the current row selection."""
        self.hidden_items = set(range(len(self.document.items)))
        self._sync_checks()
        self._rebuild_simulation()

    def show_all(self) -> None:
        self.hidden_items.clear()
        self._sync_checks()
        self._rebuild_simulation()

    def _sync_checks(self) -> None:
        with QSignalBlocker(self.effects_list):
            for position in range(self.effects_list.count()):
                row = self.effects_list.item(position)
                index = row.data(Qt.ItemDataRole.UserRole)
                if index is not None:
                    row.setCheckState(Qt.CheckState.Unchecked
                                      if index in self.hidden_items
                                      else Qt.CheckState.Checked)

    def _row_selected(self, position: int) -> None:
        row = self.effects_list.item(position) if position >= 0 else None
        self.highlight = row.data(Qt.ItemDataRole.UserRole) if row else None
        self.timeline.highlight = self.highlight
        self.timeline.update()
        self.frameChanged.emit()

    # -- simulation and clock -----------------------------------------------------------------

    def _rebuild_simulation(self) -> None:
        enabled = set(range(len(self.document.items))) - self.hidden_items
        self.simulation = fx.PreviewSimulation(
            self.document, self.context, self.current_trigger, seed=self.seed,
            origin=self.origin, ground_y=self.ground_y,
            unit_radius=self.unit_radius, legacy_catalog=self.catalog,
            enabled=enabled, unit=self.unit)
        self.timeline.set_tracks(self.simulation.tracks, self._length())
        notes = list(dict.fromkeys(self.simulation.notes))
        self.notes_label.setText("\n".join(notes))
        self.notes_label.setVisible(bool(notes))
        self._update_time_label()
        self.frameChanged.emit()

    def _length(self) -> float:
        duration = self.simulation.duration if self.simulation else 0.0
        return duration if duration > 0 else EMPTY_TIMELINE_MS

    def seek(self, time_ms: float) -> None:
        self.time_ms = max(0.0, float(time_ms))
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
        self.play_button.setIcon(self._transport_icon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip("Pause destruction preview" if playing else "Play destruction preview")
        if playing:
            if self.time_ms >= self._length():
                self.seek(0.0)
            self._last_tick = None
            self._timer.start()
        else:
            self._timer.stop()

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
        self.seek(time_ms)

    def _update_time_label(self) -> None:
        self.time_label.setText(
            f"{self.time_ms / 1000:5.2f} / {self._length() / 1000:4.2f} s")

    def preview_items(self):
        return self.simulation.items_at(self.time_ms) if self.simulation else []

    def offset_boxes(self):
        return self.simulation.offset_boxes() if self.simulation else []

    def required_vps(self) -> set[int]:
        """Every VP the preview can show, for owner loading."""

        unit = self.unit
        vps: set[int] = {ref.vp for ref in (unit.normal, unit.dead, unit.megadeth)
                         if ref.vp > 0}
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
