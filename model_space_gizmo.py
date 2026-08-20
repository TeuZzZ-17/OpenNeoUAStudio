"""Compact model-space Move, Rotate and Scale gizmo for OpenNeoUAStudio."""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget


DIRECTIONS_26 = tuple(
    direction
    for direction in itertools.product((-1, 0, 1), repeat=3)
    if direction != (0, 0, 0)
)
AXIS_DIRECTIONS = (
    (1, 0, 0), (-1, 0, 0),
    (0, 1, 0), (0, -1, 0),
    (0, 0, 1), (0, 0, -1),
)
SCALE_DIRECTIONS = AXIS_DIRECTIONS + ((1, 1, 1), (-1, -1, -1))
TRANSFORM_MODES = ("move", "rotate", "scale")


@dataclass(frozen=True)
class GizmoHandle:
    direction: tuple[int, int, int]
    position: QPointF
    depth: float
    radius: float


class ModelSpaceGizmo(QWidget):
    """Perspective model-space control whose handles depend on the active mode.

    A click performs one nudge. Holding a handle repeats the same nudge. The
    amount is controlled by the separate step control in the editor;
    dragging handles deliberately has no analogue-speed meaning.
    """

    nudgeStarted = Signal(object)
    directionTriggered = Signal(object)
    nudgeFinished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 240)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self._mode = "move"
        self._update_tooltip()
        self._yaw = -35.0
        self._pitch = 20.0
        self._model_matrix = (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )
        self._hover: tuple[int, int, int] | None = None
        self._active: tuple[int, int, int] | None = None
        self._first_repeat = False
        self._interaction_state = "idle"
        self._repeat_timer = QTimer(self)
        self._repeat_timer.timeout.connect(self._repeat)
        self._handles: tuple[GizmoHandle, ...] = ()
        # Optional visual tuning.  Defaults preserve the established gizmo
        # geometry used by the other OpenNeoUAStudio editors.
        self._visual_extent_ratio = 0.72
        self._visual_margin = 22.0
        self._handle_scale = 1.0
        self._line_scale = 1.0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(300, 300)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(240, 240)

    @property
    def directions(self) -> tuple[tuple[int, int, int], ...]:
        if self._mode == "rotate":
            return AXIS_DIRECTIONS
        if self._mode == "scale":
            return SCALE_DIRECTIONS
        return DIRECTIONS_26

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        normalized = str(mode).lower()
        if normalized not in TRANSFORM_MODES:
            raise ValueError(f"unsupported transform mode: {mode}")
        if normalized == self._mode:
            return
        self._repeat_timer.stop()
        self._active = None
        self._hover = None
        self._interaction_state = "idle"
        self._mode = normalized
        self._handles = ()
        self._update_tooltip()
        self.update()

    def _update_tooltip(self) -> None:
        if self._mode == "rotate":
            text = (
                "Rotate around the selected model-space axis. "
                "Positive and negative use the right-hand rule.")
        elif self._mode == "scale":
            text = (
                "Scale on X, Y, Z or uniformly on all axes. "
                "Positive grows; negative applies the safe inverse.")
        else:
            text = (
                "Move in model space. Red=X, green=Y, blue=Z. "
                "Click once for one nudge or hold to repeat. "
                "-Y is Up and +Y is Down in Urban Assault model space.")
        self.setToolTip(text)

    @property
    def handles(self) -> tuple[GizmoHandle, ...]:
        self._handles = self._project_handles()
        return self._handles

    def set_camera_orientation(self, yaw: float, pitch: float) -> None:
        if self._yaw == yaw and self._pitch == pitch:
            return
        self._yaw = float(yaw)
        self._pitch = float(pitch)
        self.update()

    def set_model_matrix(self, matrix) -> None:
        values = tuple(tuple(float(value) for value in row) for row in matrix)
        if values == self._model_matrix:
            return
        self._model_matrix = values
        self.update()

    def set_visual_scale(
            self, *, extent_ratio: float | None = None,
            margin: float | None = None, handle_scale: float | None = None,
            line_scale: float | None = None) -> None:
        """Tune the gizmo drawing without changing transform semantics.

        The Collision Editor uses a deliberately larger presentation, while
        every existing caller keeps the historical defaults above.
        """

        if extent_ratio is not None:
            self._visual_extent_ratio = max(0.25, min(1.2, float(
                extent_ratio)))
        if margin is not None:
            self._visual_margin = max(4.0, float(margin))
        if handle_scale is not None:
            self._handle_scale = max(0.5, min(3.0, float(handle_scale)))
        if line_scale is not None:
            self._line_scale = max(0.5, min(3.0, float(line_scale)))
        self._handles = ()
        self.update()

    def hold_multiplier(self) -> float:
        return 1.0

    def direction_text(self, direction: tuple[int, int, int]) -> str:
        if self._mode == "scale" and abs(direction[0]) == abs(direction[1]) \
                == abs(direction[2]) == 1:
            return ("Uniform +  (grow all axes)" if direction[0] > 0
                    else "Uniform -  (shrink all axes)")
        names = []
        for value, axis in zip(direction, "XYZ"):
            if value:
                names.append(f"{'+' if value > 0 else '-'}{axis}")
        vector = ", ".join(str(value) for value in direction)
        text = " ".join(names)
        if self._mode == "rotate":
            text += " rotation  (right-hand rule)"
        elif self._mode == "scale":
            text += " scale"
        else:
            text += f"  ({vector})"
        if self._mode == "move" and direction == (0, -1, 0):
            text += "  -Y = Up in UA model space"
        elif self._mode == "move" and direction == (0, 1, 0):
            text += "  +Y = Down in UA model space"
        return text

    def _camera_direction(self, direction):
        matrix = self._model_matrix
        world = tuple(
            sum(matrix[row][axis] * direction[axis] for axis in range(3))
            for row in range(3))
        x, y, z = world[0], -world[1], world[2]
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        xz_x = x * math.cos(yaw) + z * math.sin(yaw)
        xz_z = -x * math.sin(yaw) + z * math.cos(yaw)
        return (
            xz_x,
            y * math.cos(pitch) - xz_z * math.sin(pitch),
            y * math.sin(pitch) + xz_z * math.cos(pitch),
        )

    def _project_handles(self) -> tuple[GizmoHandle, ...]:
        center = QPointF(self.width() * 0.5, self.height() * 0.5)
        projected = []
        for direction in self.directions:
            cx, cy, depth = self._camera_direction(direction)
            denominator = max(1.45, 2.75 - depth * 0.36)
            active_axes = sum(value != 0 for value in direction)
            radius = (14.0 if active_axes == 1 else (
                11.0 if active_axes == 2 else 9.0)) * self._handle_scale
            projected.append((
                direction, cx / denominator, -cy / denominator,
                depth, radius))

        # Fit the complete hit geometry inside compact layouts. Text labels
        # are clamped separately at paint time.
        max_x = max((abs(item[1]) for item in projected), default=1.0)
        max_y = max((abs(item[2]) for item in projected), default=1.0)
        margin = self._visual_margin
        horizontal = max(1.0, self.width() * 0.5 - margin)
        vertical = max(1.0, self.height() * 0.5 - margin)
        extent = min(
            min(self.width(), self.height()) * self._visual_extent_ratio,
            horizontal / max(max_x, 1e-9),
            vertical / max(max_y, 1e-9),
        )
        handles = []
        for direction, offset_x, offset_y, depth, radius in projected:
            point = QPointF(
                center.x() + offset_x * extent,
                center.y() + offset_y * extent,
            )
            handles.append(GizmoHandle(direction, point, depth, radius))
        return tuple(handles)

    def handle_for_direction(
            self, direction: tuple[int, int, int]) -> GizmoHandle | None:
        return next(
            (handle for handle in self.handles
             if handle.direction == tuple(direction)), None)

    def hit_test(self, point: QPointF) -> tuple[int, int, int] | None:
        candidates = []
        for handle in self.handles:
            dx = point.x() - handle.position.x()
            dy = point.y() - handle.position.y()
            distance = math.hypot(dx, dy)
            if distance <= handle.radius + 4.0 * self._handle_scale:
                candidates.append((distance, -handle.depth, handle.direction))
        return min(candidates)[2] if candidates else None

    @staticmethod
    def _axis_color(direction) -> QColor:
        active = [index for index, value in enumerate(direction) if value]
        colors = (
            QColor(235, 82, 82),
            QColor(78, 205, 102),
            QColor(74, 132, 245),
        )
        if len(active) == 1:
            return QColor(colors[active[0]])
        return QColor(
            sum(colors[index].red() for index in active) // len(active),
            sum(colors[index].green() for index in active) // len(active),
            sum(colors[index].blue() for index in active) // len(active),
        )

    def _draw_label(self, painter: QPainter, position: QPointF,
                    dx: float, dy: float, text: str) -> None:
        """Draw a handle label wholly inside even the compact 240 px widget."""

        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text)
        left = (
            position.x() + 8.0 if dx >= 0.0
            else position.x() - width - 8.0)
        baseline = (
            position.y() - 7.0 if dy >= 0.0
            else position.y() + metrics.height())
        left = max(2.0, min(self.width() - width - 2.0, left))
        baseline = max(
            metrics.ascent() + 2.0,
            min(self.height() - metrics.descent() - 2.0, baseline),
        )
        painter.drawText(QPointF(left, baseline), text)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(self.width() * 0.5, self.height() * 0.5)
        handles = sorted(self.handles, key=lambda item: item.depth)
        depth_min = min((handle.depth for handle in handles), default=-1.0)
        depth_max = max((handle.depth for handle in handles), default=1.0)
        depth_span = max(1e-9, depth_max - depth_min)
        for handle in handles:
            color = self._axis_color(handle.direction)
            front = (handle.depth - depth_min) / depth_span
            alpha = int(80 + front * 150)
            if handle.direction == self._hover:
                alpha = 255
                color = color.lighter(135)
            if handle.direction == self._active:
                color = QColor(255, 245, 145)
                alpha = 255
            position = handle.position
            style = (Qt.PenStyle.SolidLine if front >= 0.42
                     else Qt.PenStyle.DashLine)
            painter.setPen(QPen(QColor(
                color.red(), color.green(), color.blue(), alpha),
                (3.0 if sum(v != 0 for v in handle.direction) == 1
                 else 1.35) * self._line_scale,
                style))
            painter.drawLine(center, position)
            painter.setBrush(QColor(
                color.red(), color.green(), color.blue(), alpha))
            painter.setPen(QPen(QColor(20, 23, 29, 220), 1.0))
            active_axes = sum(value != 0 for value in handle.direction)
            dx = position.x() - center.x()
            dy = position.y() - center.y()
            label = self.direction_text(handle.direction).split("  ")[0]
            if self._mode == "rotate":
                painter.drawEllipse(position, handle.radius + 2,
                                    handle.radius + 2)
                painter.setPen(QPen(QColor(238, 240, 245), 1.0))
                self._draw_label(painter, position, dx, dy, label)
            elif self._mode == "scale":
                r = handle.radius + (3 if active_axes == 3 else 0)
                painter.drawRect(QRectF(
                    position.x() - r, position.y() - r, r * 2, r * 2))
                painter.setPen(QPen(QColor(238, 240, 245), 1.0))
                display = (
                    "All +" if handle.direction == (1, 1, 1) else
                    "All -" if handle.direction == (-1, -1, -1) else label)
                self._draw_label(painter, position, dx, dy, display)
            elif active_axes == 1:
                length = max(1e-9, math.hypot(dx, dy))
                ux, uy = dx / length, dy / length
                side_x, side_y = -uy, ux
                tip = position
                arrow_length = 15.0 * self._handle_scale
                arrow_half_width = 7.0 * self._handle_scale
                base = QPointF(
                    tip.x() - ux * arrow_length,
                    tip.y() - uy * arrow_length)
                arrow = QPolygonF([
                    tip,
                    QPointF(base.x() + side_x * arrow_half_width,
                            base.y() + side_y * arrow_half_width),
                    QPointF(base.x() - side_x * arrow_half_width,
                            base.y() - side_y * arrow_half_width),
                ])
                painter.drawPolygon(arrow)
                painter.setPen(QPen(QColor(238, 240, 245), 1.0))
                self._draw_label(painter, position, dx, dy, label)
            elif active_axes == 2:
                painter.drawEllipse(position, handle.radius, handle.radius)
            else:
                r = handle.radius
                painter.drawPolygon(QPolygonF([
                    position + QPointF(0, -r),
                    position + QPointF(r, 0),
                    position + QPointF(0, r),
                    position + QPointF(-r, 0),
                ]))

        painter.setPen(QPen(
            QColor(235, 238, 245), 1.4 * self._line_scale))
        painter.setBrush(QColor(67, 72, 84))
        center_half = 8.0 * self._handle_scale
        painter.drawRect(QRectF(
            center.x() - center_half, center.y() - center_half,
            center_half * 2, center_half * 2))
        painter.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Dragging deliberately has no analogue movement mode. Once a handle
        # is pressed, only click/hold semantics apply until release.
        if self._active is not None \
                and event.buttons() & Qt.MouseButton.LeftButton:
            event.accept()
            return
        direction = self.hit_test(event.position())
        if direction != self._hover:
            self._hover = direction
            if direction is not None:
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    self.direction_text(direction), self)
            else:
                QToolTip.hideText()
            self.update()
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._active is None:
            self._hover = None
            QToolTip.hideText()
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            event.ignore()
            return
        direction = self.hit_test(event.position())
        if direction is None:
            event.ignore()
            return
        self._active = direction
        self._hover = direction
        self._first_repeat = True
        self._interaction_state = "pressed"
        self.nudgeStarted.emit(direction)
        self._repeat_timer.start(350)
        self.update()
        event.accept()

    def _repeat(self) -> None:
        if self._active is None \
                or self._interaction_state not in ("pressed", "hold"):
            self._repeat_timer.stop()
            return
        if self._first_repeat:
            self._first_repeat = False
            self._interaction_state = "hold"
            self._repeat_timer.setInterval(70)
        self.directionTriggered.emit(self._active)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._active is None:
            event.ignore()
            return
        self._repeat_timer.stop()
        self._repeat_timer.setInterval(350)
        if self._interaction_state == "pressed":
            self.directionTriggered.emit(self._active)
        self._active = None
        self._interaction_state = "idle"
        self.nudgeFinished.emit()
        self._hover = self.hit_test(event.position())
        self.update()
        event.accept()
