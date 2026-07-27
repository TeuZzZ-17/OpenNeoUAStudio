"""Small non-modal transform dialogs used by the model editor."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
)


class LiveScaleDialog(QDialog):
    """Uniform scale slider whose changes are previewed live."""

    factorChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scale")
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)
        self.value_label = QLabel("Scale: 1.00x")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(10, 400)
        self.slider.setValue(100)
        self.slider.setTickInterval(25)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider.valueChanged.connect(self._value_changed)
        layout.addWidget(self.slider)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def factor(self) -> float:
        return self.slider.value() / 100.0

    def _value_changed(self, value: int) -> None:
        factor = value / 100.0
        self.value_label.setText(f"Scale: {factor:.2f}x")
        self.factorChanged.emit(factor)


class LiveRotateDialog(QDialog):
    """Model-space axis/angle controls with a live rotation preview."""

    rotationChanged = Signal(str, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rotate")
        self.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(self)

        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("Axis"))
        self.axis_combo = QComboBox()
        self.axis_combo.addItems(["X", "Y", "Z"])
        axis_row.addWidget(self.axis_combo, 1)
        layout.addLayout(axis_row)

        angle_row = QHBoxLayout()
        angle_row.addWidget(QLabel("Angle"))
        self.angle_spin = QDoubleSpinBox()
        self.angle_spin.setRange(-360.0, 360.0)
        self.angle_spin.setDecimals(2)
        self.angle_spin.setSingleStep(1.0)
        self.angle_spin.setSuffix("°")
        angle_row.addWidget(self.angle_spin, 1)
        layout.addLayout(angle_row)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-3600, 3600)
        self.slider.setValue(0)
        self.slider.setTickInterval(450)
        self.slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        layout.addWidget(self.slider)

        self.axis_combo.currentTextChanged.connect(self._emit_rotation)
        self.angle_spin.valueChanged.connect(self._spin_changed)
        self.slider.valueChanged.connect(self._slider_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, self.sizeHint().height())

    def axis(self) -> str:
        return self.axis_combo.currentText()

    def angle_degrees(self) -> float:
        return self.angle_spin.value()

    def _spin_changed(self, value: float) -> None:
        slider_value = round(value * 10.0)
        if self.slider.value() != slider_value:
            self.slider.blockSignals(True)
            self.slider.setValue(slider_value)
            self.slider.blockSignals(False)
        self._emit_rotation()

    def _slider_changed(self, value: int) -> None:
        angle = value / 10.0
        if self.angle_spin.value() != angle:
            self.angle_spin.blockSignals(True)
            self.angle_spin.setValue(angle)
            self.angle_spin.blockSignals(False)
        self._emit_rotation()

    def _emit_rotation(self, _value=None) -> None:
        self.rotationChanged.emit(self.axis(), self.angle_degrees())
