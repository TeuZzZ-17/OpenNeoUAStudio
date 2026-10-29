"""Fire Area tab: controls for the ai_fire_trigger_* values of a weapon.

Only the widgets live here. The editor owns the data, undo and script writing, and
the viewport draws the volume; this panel reports edits through signals so those
parts keep one single source of truth.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QSlider,
    QSpinBox,
    QSizePolicy,
    QVBoxLayout,
)

from entity_designer import fire_area as fa
from entity_designer.widgets import CompactScaleSpinBox

TAB_TITLE = "Fire Area"
_NO_VOLUME = "No volume (vanilla fire control)"
_NOTE_STYLE = "font-size: 10px; color: #aeb6c2;"
_WARNING_STYLE = "font-size: 10px; color: #e0a860;"


def _spin(low: float, high: float, step: float, decimals: int = 3) -> CompactScaleSpinBox:
    spin = CompactScaleSpinBox()
    spin.setRange(low, high)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setKeyboardTracking(True)
    spin.setMinimumWidth(72)
    return spin


class FireAreaPanel(QGroupBox):
    """Shape, size, dead zone and tilt of the AI fire volume, plus preview controls."""

    shapeChosen = Signal(object)         # a fire_area shape name, or None
    unitChosen = Signal(object)          # vehicle id of the firing unit, or None
    valueEdited = Signal(str, float)     # field, value
    editFinished = Signal(str)
    frameRequested = Signal()
    frameUnitRequested = Signal()
    resetRequested = Signal()
    aimChosen = Signal(str)              # fa.AIM_LOW / fa.AIM_HIGH

    def __init__(self, parent=None) -> None:
        super().__init__("Fire Area", parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet(_NOTE_STYLE)
        layout.addWidget(self.notice)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(7)
        grid.addWidget(QLabel("Volume"), 0, 0)
        self.shape_combo = QComboBox()
        self.shape_combo.addItem(_NO_VOLUME, None)
        for shape in fa.SHAPES:
            self.shape_combo.addItem(fa.SHAPE_LABELS[shape], shape)
        self.shape_combo.setToolTip(
            "A weapon uses exactly one volume. Choosing another one keeps only "
            "that volume in the script. Bombs use the Below volumes, other "
            "projectile weapons the Forward ones.")
        self.shape_combo.currentIndexChanged.connect(
            lambda _index: self.shapeChosen.emit(self.shape_combo.currentData()))
        grid.addWidget(self.shape_combo, 0, 1, 1, 2)

        self.size_labels = []
        self.size_spins = []
        self.size_sliders = []
        for row in range(3):
            label = QLabel("")
            spin = _spin(0.1, 1_000_000.0, 10.0)
            field = f"size{row}"
            spin.valueChanged.connect(
                lambda value, name=field: self.valueEdited.emit(name, value))
            spin.editingFinished.connect(
                lambda name=field: self.editFinished.emit(name))
            grid.addWidget(label, row + 1, 0)
            slider = self._add_slider(grid, row + 1, spin, field)
            self.size_labels.append(label)
            self.size_spins.append(spin)
            self.size_sliders.append(slider)

        grid.addWidget(QLabel("Firing unit (preview)"), 4, 0)
        self.unit_combo = QComboBox()
        self.unit_combo.setToolTip(
            "Preview only, never written to the script: a vehicle that fires this "
            "weapon. The volume starts at its fire point (fire_x/y/z).")
        self.unit_combo.currentIndexChanged.connect(
            lambda _index: self.unitChosen.emit(self.unit_combo.currentData()))
        grid.addWidget(self.unit_combo, 4, 1, 1, 2)

        grid.addWidget(QLabel("Dead zone (min_depth)"), 5, 0)
        self.min_depth_spin = _spin(0.0, fa.MAX_MIN_DEPTH, 10.0)
        self.min_depth_spin.setDecimals(0)
        self.min_depth_spin.setToolTip(
            "ai_fire_trigger_min_depth: distance from the fire point before the "
            "target can trigger the shot. 0 = no dead zone.")
        self.min_depth_spin.valueChanged.connect(
            lambda value: self.valueEdited.emit("min_depth", value))
        self.min_depth_spin.editingFinished.connect(
            lambda: self.editFinished.emit("min_depth"))
        self.min_depth_slider = self._add_slider(grid, 5, self.min_depth_spin, "min_depth")

        grid.addWidget(QLabel("Max tilt (forward)"), 6, 0)
        self.inclination_spin = _spin(0.0, fa.MAX_INCLINATION_LIMIT - 0.1, 1.0, 1)
        self.inclination_spin.setToolTip(
            "ai_fire_trigger_forward_max_inclination: furthest a forward volume "
            "tilts toward a ground target, in degrees. 0 = no tilt. Not used by "
            "Below volumes.")
        self.inclination_spin.valueChanged.connect(
            lambda value: self.valueEdited.emit("max_inclination", value))
        self.inclination_spin.editingFinished.connect(
            lambda: self.editFinished.emit("max_inclination"))
        self.inclination_slider = self._add_slider(grid, 6, self.inclination_spin, "max_inclination")

        grid.addWidget(QLabel("Weapon height"), 7, 0)
        self.height_slider = QSlider(Qt.Orientation.Horizontal)
        self.height_slider.setRange(0, 5000)
        self.height_slider.setSingleStep(10)
        self.height_slider.setPageStep(100)
        self.height_slider.setToolTip(
            "Preview only, never written to the script: how high above the "
            "ground the firing unit (or the bare weapon) is.")
        self.height_value = QSpinBox()
        self.height_value.setRange(0, 5000)
        self.height_value.setKeyboardTracking(False)
        self.height_value.setToolTip(self.height_slider.toolTip())
        height_row = QHBoxLayout()
        height_row.addWidget(self.height_slider, 1)
        height_row.addWidget(self.height_value)
        self.height_slider.valueChanged.connect(self.height_value.setValue)
        self.height_value.valueChanged.connect(self.height_slider.setValue)
        self.height_value.valueChanged.connect(
            lambda value: self.valueEdited.emit("height", float(value)))
        grid.addLayout(height_row, 7, 1, 1, 2)

        grid.addWidget(QLabel("Shot goes to"), 8, 0)
        self.aim_combo = QComboBox()
        self.aim_combo.addItem("Low zone (toward the ground)", fa.AIM_LOW)
        self.aim_combo.addItem("High zone (straight ahead)", fa.AIM_HIGH)
        self.aim_combo.setToolTip(
            "Preview only, never written to the script: where a forward weapon "
            "flies when you press play. Low follows the tilted (dashed) volume "
            "down to the ground, High follows the untilted volume. Below "
            "weapons always fall straight down.")
        self.aim_combo.currentIndexChanged.connect(
            lambda _index: self.aimChosen.emit(self.aim_combo.currentData()))
        grid.addWidget(self.aim_combo, 8, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(5)
        self.frame_button = QPushButton("Focus Volume")
        self.frame_button.setToolTip("Move the camera so the whole volume is visible.")
        self.frame_button.clicked.connect(self.frameRequested)
        buttons.addWidget(self.frame_button, 0, 0)
        self.frame_unit_button = QPushButton("Focus Unit")
        self.frame_unit_button.setToolTip(
            "Close in on the firing unit and its fire point, where the volume starts.")
        self.frame_unit_button.clicked.connect(self.frameUnitRequested)
        buttons.addWidget(self.frame_unit_button, 0, 1)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setToolTip(
            "Bring everything in this tab back exactly as when the weapon was "
            "opened: the volume, sizes, dead zone and tilt as the script has them "
            "(none if it has none), the firing unit, height, shot zone and camera.")
        self.reset_button.clicked.connect(self.resetRequested)
        buttons.addWidget(self.reset_button, 0, 2)
        layout.addLayout(buttons)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(_WARNING_STYLE)
        layout.addWidget(self.warning)

        output_box = QGroupBox("Script parameters")
        output_layout = QVBoxLayout(output_box)
        output_layout.setContentsMargins(8, 8, 8, 8)
        self.output = QLabel()
        self.output.setTextFormat(Qt.TextFormat.PlainText)
        self.output.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.output.setFrameShape(QLabel.Shape.NoFrame)
        self.output.setMinimumHeight(56)
        output_layout.addWidget(self.output)
        layout.addWidget(output_box)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _add_slider(self, grid, row, spin, field):
        """Add a slider beside an existing editable numeric field."""
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setProperty("factor", 1)
        slider.setRange(0, 5000)
        def change(value):
            spin.setValue(value / slider.property("factor"))
            if not slider.isSliderDown():
                self.editFinished.emit(field)
        slider.valueChanged.connect(change)
        slider.sliderReleased.connect(lambda: self.editFinished.emit(field))
        grid.addWidget(slider, row, 1)
        grid.addWidget(spin, row, 2)
        return slider

    def set_state(self, area: fa.FireArea, shape: str | None, *, available: bool,
                  height: float, lines: list[str], note: str, warning: str,
                  units: list[tuple[int, str]] = (), unit_id: int | None = None,
                  aim: str = fa.AIM_LOW) -> None:
        """Show the project's area without sending edit signals back."""

        blocked = [QSignalBlocker(widget) for widget in (
            self.shape_combo, self.unit_combo, self.aim_combo, self.min_depth_spin,
            self.inclination_spin, self.height_slider, self.height_value,
            self.min_depth_slider, self.inclination_slider,
            *self.size_spins, *self.size_sliders)]
        self.setEnabled(available)
        self.unit_combo.clear()
        self.unit_combo.addItem("None (weapon only)", None)
        for vehicle_id, label in units:
            self.unit_combo.addItem(label, vehicle_id)
        self.unit_combo.setCurrentIndex(
            0 if unit_id is None else max(0, self.unit_combo.findData(unit_id)))
        self.shape_combo.setCurrentIndex(
            0 if shape is None else max(0, self.shape_combo.findData(shape)))
        size = area.size(shape) if shape else None
        fields = fa.SIZE_FIELDS.get(shape, ())
        for row, (label, spin, slider) in enumerate(
                zip(self.size_labels, self.size_spins, self.size_sliders)):
            visible = size is not None and row < len(fields)
            label.setVisible(visible)
            spin.setVisible(visible)
            slider.setVisible(visible)
            if not visible:
                continue
            name, index = fields[row]
            label.setText(name)
            is_angle = fa.is_cone(shape) and index < 2
            spin.setSuffix("°" if is_angle else "")
            spin.setMaximum(fa.CONE_ANGLE_LIMIT - 0.1 if is_angle else 1_000_000.0)
            spin.setValue(size[index])
            slider.setProperty("factor", 10)
            slider.setRange(1,
                            int((fa.CONE_ANGLE_LIMIT - 0.1) * 10)
                            if is_angle else max(50000, math.ceil(spin.value() * 10)))
            slider.setValue(round(spin.value() * slider.property("factor")))
        self.min_depth_spin.setValue(area.min_depth)
        self.min_depth_spin.setEnabled(shape is not None)
        self.min_depth_slider.setEnabled(shape is not None)
        self.min_depth_slider.setMaximum(max(5000, math.ceil(self.min_depth_spin.value())))
        self.min_depth_slider.setValue(round(self.min_depth_spin.value()))
        self.inclination_spin.setValue(area.max_inclination)
        self.inclination_spin.setEnabled(shape is not None and not fa.is_below(shape))
        self.inclination_slider.setProperty("factor", 10)
        self.inclination_slider.setRange(0, int((fa.MAX_INCLINATION_LIMIT - 0.1) * 10))
        self.inclination_slider.setValue(round(self.inclination_spin.value() * 10))
        self.inclination_slider.setEnabled(shape is not None and not fa.is_below(shape))
        self.height_slider.setValue(round(height))
        self.height_value.setValue(self.height_slider.value())
        self.aim_combo.setEnabled(shape is not None and not fa.is_below(shape))
        self.aim_combo.setCurrentIndex(max(0, self.aim_combo.findData(aim)))
        self.frame_button.setEnabled(shape is not None)
        self.frame_unit_button.setEnabled(shape is not None)
        self.reset_button.setEnabled(available)
        self.notice.setText(note)
        self.warning.setText(warning)
        self.warning.setVisible(bool(warning))
        self.output.setText("\n".join(lines))
        del blocked
