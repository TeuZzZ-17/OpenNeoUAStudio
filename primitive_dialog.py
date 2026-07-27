"""Compact parameter dialog for one-shot normal geometry primitives."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from primitive_geometry import (
    DEFAULT_RADIUS,
    DEFAULT_SEGMENTS,
    DEFAULT_SIZE,
    MAX_DIMENSION,
    MAX_SEGMENTS,
    MIN_SEGMENTS,
)


_LABELS = {
    "triangle": "Triangle",
    "square": "Square",
    "rectangle": "Rectangle",
    "circle": "Circle / Disc",
    "plane": "Plane",
    "cube": "Cube / Box",
    "pyramid": "Pyramid",
    "cylinder": "Cylinder",
}


class PrimitiveDialog(QDialog):
    """Collect only the dimensions needed by the selected V1 shape."""

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.kind = str(kind).casefold()
        self.setWindowTitle(f"Add {_LABELS.get(self.kind, self.kind)}")
        layout = QVBoxLayout(self)
        note = QLabel(
            "The shape uses the current static material. After this dialog, "
            "choose its final position in the viewport.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        layout.addLayout(form)
        self._values: dict[str, object] = {}

        if self.kind in ("triangle", "square", "plane", "cube"):
            self._add_dimension(form, "size", "Size", DEFAULT_SIZE)
        elif self.kind == "rectangle":
            self._add_dimension(form, "width", "Width", DEFAULT_SIZE)
            self._add_dimension(form, "height", "Height", DEFAULT_SIZE)
        elif self.kind == "circle":
            self._add_dimension(form, "radius", "Radius", DEFAULT_RADIUS)
            self._add_segments(form)
        elif self.kind == "pyramid":
            self._add_dimension(
                form, "base_size", "Base size", DEFAULT_SIZE)
            self._add_dimension(form, "height", "Height", DEFAULT_SIZE)
        elif self.kind == "cylinder":
            self._add_dimension(form, "radius", "Radius", DEFAULT_RADIUS)
            self._add_dimension(form, "height", "Height", DEFAULT_SIZE)
            self._add_segments(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_dimension(
            self, form: QFormLayout, key: str, label: str,
            default: float) -> None:
        spin = QDoubleSpinBox()
        spin.setRange(0.01, MAX_DIMENSION)
        spin.setDecimals(2)
        spin.setValue(default)
        spin.setSuffix(" model units")
        form.addRow(label, spin)
        self._values[key] = spin

    def _add_segments(self, form: QFormLayout) -> None:
        spin = QSpinBox()
        spin.setRange(MIN_SEGMENTS, MAX_SEGMENTS)
        spin.setValue(DEFAULT_SEGMENTS)
        form.addRow("Segments", spin)
        self._values["segments"] = spin

    def parameters(self) -> dict[str, float | int]:
        return {
            key: widget.value()
            for key, widget in self._values.items()
        }


__all__ = ["PrimitiveDialog"]
