"""Orthographic ground-contact guide for the selected model bounds."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from entity_designer.script_model import _number


class GroundProfile(QWidget):
    """Show the model's vertical extent and its distance from flat ground."""

    wheelSteps = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bounds = None
        self.overeof = 0.0
        self.authored = False
        self._wheel_delta = 0
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "Schematic side view of the selected model's scaled bounds. "
            "Contact is estimated against flat ground; animation and terrain "
            "can change the result in-game. Scroll to adjust Overeof.")

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if self.bounds is None or not delta:
            event.ignore()
            return
        self._wheel_delta += delta
        steps = math.trunc(self._wheel_delta / 120)
        self._wheel_delta -= steps * 120
        if steps:
            self.wheelSteps.emit(steps)
        event.accept()

    def set_profile(self, bounds, authored: bool, overeof: float) -> None:
        if bounds is not None and not all(math.isfinite(v) for v in bounds):
            bounds = None
        self.bounds = bounds
        self.authored = bool(authored)
        self.overeof = float(overeof) if authored and math.isfinite(overeof) else 0.0
        self.update()

    def contact_status(self) -> tuple[str, QColor]:
        gap = self.overeof - self.bounds[4]
        if abs(gap) < 1e-9:
            return "Touching ground", QColor(117, 214, 135)
        if gap > 0:
            return f"Above ground: {_number(gap)}", QColor(248, 200, 100)
        return f"Below ground: {_number(-gap)}", QColor(255, 113, 105)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(25, 28, 32))
        painter.setPen(QColor(225, 228, 231))
        painter.drawText(QRectF(10, 6, self.width() - 20, 24),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "Flat ground profile (model bounds)")
        if self.bounds is None:
            painter.setPen(QColor(170, 175, 180))
            painter.drawText(QRectF(10, 35, self.width() - 20, 160),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             "Load a model to see its ground profile.")
            return

        top = self.bounds[1] - self.overeof
        bottom = self.bounds[4] - self.overeof
        low, high = min(top, 0.0), max(bottom, 0.0)
        span = max(high - low, 1.0)
        low -= span * 0.12
        high += span * 0.12
        plot_top, plot_bottom = 39.0, self.height() - 63.0
        scale = (plot_bottom - plot_top) / (high - low)
        y = lambda value: plot_top + (value - low) * scale
        ground_y, top_y, bottom_y = y(0.0), y(top), y(bottom)
        left, right = self.width() * 0.28, self.width() * 0.64

        painter.fillRect(QRectF(1, ground_y, self.width() - 2,
                                max(0.0, plot_bottom - ground_y)),
                         QColor(66, 52, 38))
        painter.setPen(QPen(QColor(104, 183, 118), 2))
        painter.drawLine(QPointF(10, ground_y), QPointF(self.width() - 10, ground_y))
        painter.drawText(QPointF(10, ground_y - 5), "Ground")
        painter.setBrush(QColor(89, 145, 185, 135))
        painter.setPen(QPen(QColor(130, 194, 232), 2))
        painter.drawRect(QRectF(left, top_y, right - left, max(1.0, bottom_y - top_y)))
        painter.setPen(QPen(QColor(248, 188, 77), 2, Qt.PenStyle.DashLine))
        marker_x = (left + right) / 2
        painter.drawLine(QPointF(marker_x, ground_y), QPointF(marker_x, bottom_y))
        painter.setBrush(QColor(248, 188, 77))
        painter.drawEllipse(QPointF(marker_x, bottom_y), 3, 3)

        status, color = self.contact_status()
        painter.setPen(color)
        painter.drawText(QRectF(10, self.height() - 55, self.width() - 20, 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         status)
        painter.setPen(QColor(180, 185, 190))
        label = f"Contact Overeof: {_number(self.bounds[4])}"
        if not self.authored:
            label += "  |  Preview at 0 (not included)"
        painter.drawText(QRectF(10, self.height() - 33, self.width() - 20, 24),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         label)
