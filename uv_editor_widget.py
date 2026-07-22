"""Interactive OLPL UV editor for one polygon over its texture.

Coordinates remain native amesh bytes (0..255).  The widget only changes
in-memory data through signals and never writes asset files itself.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

HANDLE_RADIUS = 6.0


class UVEditorWidget(QWidget):
    uvChanged = Signal(list)
    editFinished = Signal()
    pointSelected = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(280, 280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._image: QImage | None = None
        self._uvs: list[tuple[int, int]] = []
        self._editable = False
        self._active_point = -1
        self._selected_points: set[int] = set()
        self._dragging = False
        self._drag_axis: str | None = None
        self._drag_start_uvs: dict[int, tuple[int, int]] = {}
        self._drag_anchor_uv: tuple[int, int] | None = None
        self._drag_changed = False
        self._box_start: QPointF | None = None
        self._box_rect: QRectF | None = None
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._pan_last: QPoint | None = None

    def set_data(self, image: QImage | None, uvs: list[tuple[int, int]],
                 editable: bool, message: str = "") -> None:
        self._image = image
        self._uvs = [tuple(uv) for uv in uvs]
        self._editable = editable and bool(uvs)
        self.setToolTip(message)
        self._active_point = -1
        self._selected_points.clear()
        self._dragging = False
        self._box_start = None
        self._box_rect = None
        self.update()

    def uvs(self) -> list[tuple[int, int]]:
        return list(self._uvs)

    def active_point(self) -> int:
        return self._active_point

    def selected_points(self) -> set[int]:
        return set(self._selected_points)

    def select_all(self) -> None:
        self._selected_points = set(range(len(self._uvs)))
        self._active_point = min(self._selected_points, default=-1)
        self.pointSelected.emit(self._active_point)
        self.update()

    def select_none(self) -> None:
        self._selected_points.clear()
        self._active_point = -1
        self.pointSelected.emit(-1)
        self.update()

    def _finish_programmatic_edit(self, before) -> None:
        if before == self._uvs:
            return
        self.uvChanged.emit(self.uvs())
        self.editFinished.emit()

    def align_selected_horizontal(self) -> None:
        """Give selected handles the same V coordinate."""

        indices = sorted(self._selected_points)
        if not self._editable or len(indices) < 2:
            return
        before = list(self._uvs)
        value = round(sum(self._uvs[index][1] for index in indices)
                      / len(indices))
        for index in indices:
            self._uvs[index] = (self._uvs[index][0], value)
        self.update()
        self._finish_programmatic_edit(before)

    def align_selected_vertical(self) -> None:
        """Give selected handles the same U coordinate."""

        indices = sorted(self._selected_points)
        if not self._editable or len(indices) < 2:
            return
        before = list(self._uvs)
        value = round(sum(self._uvs[index][0] for index in indices)
                      / len(indices))
        for index in indices:
            self._uvs[index] = (value, self._uvs[index][1])
        self.update()
        self._finish_programmatic_edit(before)

    def nudge_selected(self, du: int, dv: int) -> None:
        indices = sorted(self._selected_points)
        if not self._editable or not indices:
            return
        before = list(self._uvs)
        for index in indices:
            u, v = self._uvs[index]
            self._uvs[index] = (max(0, min(255, u + du)),
                                max(0, min(255, v + dv)))
        self.update()
        self._finish_programmatic_edit(before)

    def set_point(self, index: int, u: int, v: int,
                  notify: bool = True) -> None:
        if not (0 <= index < len(self._uvs)):
            return
        self._uvs[index] = (max(0, min(255, u)), max(0, min(255, v)))
        self.update()
        if notify:
            self.uvChanged.emit(self.uvs())

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def _canvas_rect(self) -> tuple[float, float, float]:
        size = min(self.width(), self.height()) - 8
        size = max(64, size) * self._zoom
        ox = (self.width() - size) / 2 + self._pan.x()
        oy = (self.height() - size) / 2 + self._pan.y()
        return ox, oy, size

    def _uv_to_screen(self, uv: tuple[int, int]) -> QPointF:
        ox, oy, size = self._canvas_rect()
        return QPointF(ox + uv[0] / 256.0 * size,
                       oy + uv[1] / 256.0 * size)

    def _screen_to_uv(self, point: QPointF) -> tuple[int, int]:
        ox, oy, size = self._canvas_rect()
        u = round((point.x() - ox) / size * 256.0)
        v = round((point.y() - oy) / size * 256.0)
        return max(0, min(255, u)), max(0, min(255, v))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 32, 38))
        ox, oy, size = self._canvas_rect()
        cell = size / 16
        for row in range(16):
            for col in range(16):
                light = (row + col) % 2 == 0
                painter.fillRect(
                    int(ox + col * cell), int(oy + row * cell),
                    int(cell) + 1, int(cell) + 1,
                    QColor(70, 72, 80) if light else QColor(54, 56, 62),
                )
        if self._image is not None and not self._image.isNull():
            painter.drawImage(
                int(ox), int(oy),
                self._image.scaled(int(size), int(size),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.FastTransformation),
            )
        if self._uvs:
            points = [self._uv_to_screen(uv) for uv in self._uvs]
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(255, 255, 90), 1.6))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(QPolygonF(points))
            for index, point in enumerate(points):
                active = index == self._active_point
                selected = index in self._selected_points
                painter.setBrush(QColor(90, 230, 255) if active else
                                 QColor(255, 145, 55) if selected else
                                 QColor(255, 255, 90))
                painter.setPen(QPen(QColor(255, 255, 255) if selected
                                    else QColor(20, 20, 20), 1.4))
                painter.drawEllipse(point, HANDLE_RADIUS, HANDLE_RADIUS)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(point + QPointF(8, -6),
                                 f"{index} ({self._uvs[index][0]},"
                                 f"{self._uvs[index][1]})")
        if self._box_rect is not None:
            painter.setPen(QPen(QColor(90, 230, 255), 1.0,
                                Qt.PenStyle.DashLine))
            painter.setBrush(QColor(90, 230, 255, 40))
            painter.drawRect(self._box_rect)
        if not self._editable and self._uvs:
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(self.rect().adjusted(6, 4, -6, -4),
                             Qt.AlignmentFlag.AlignTop
                             | Qt.AlignmentFlag.AlignRight, "[READ-ONLY]")
        painter.end()

    def _hit_point(self, pos: QPointF) -> int:
        for index, uv in enumerate(self._uvs):
            point = self._uv_to_screen(uv)
            if (point - pos).manhattanLength() <= HANDLE_RADIUS * 2:
                return index
        return -1

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_point(pos)
            additive = bool(event.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
            if hit >= 0:
                if additive:
                    if hit in self._selected_points:
                        self._selected_points.remove(hit)
                    else:
                        self._selected_points.add(hit)
                elif hit not in self._selected_points:
                    self._selected_points = {hit}
                self._active_point = (hit if hit in self._selected_points
                                      else min(self._selected_points,
                                               default=-1))
                self.pointSelected.emit(self._active_point)
            elif additive:
                self._box_start = pos
                self._box_rect = QRectF(pos, pos)
            else:
                self.select_none()
            if hit >= 0 and hit in self._selected_points and self._editable:
                self._dragging = True
                self._drag_axis = None
                self._drag_start_uvs = {
                    index: self._uvs[index]
                    for index in self._selected_points
                }
                self._drag_anchor_uv = self._uvs[hit]
                self._drag_changed = False
            self.update()
        elif event.button() in (Qt.MouseButton.MiddleButton,
                                Qt.MouseButton.RightButton):
            self._pan_last = event.position().toPoint()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._dragging and self._active_point >= 0 \
                and self._drag_anchor_uv is not None:
            u, v = self._screen_to_uv(pos)
            du = u - self._drag_anchor_uv[0]
            dv = v - self._drag_anchor_uv[1]
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if self._drag_axis is None and (abs(du) > 2 or abs(dv) > 2):
                    self._drag_axis = "u" if abs(du) >= abs(dv) else "v"
                if self._drag_axis == "u":
                    dv = 0
                elif self._drag_axis == "v":
                    du = 0
            for index, (start_u, start_v) in self._drag_start_uvs.items():
                self._uvs[index] = (
                    max(0, min(255, start_u + du)),
                    max(0, min(255, start_v + dv)),
                )
            self._drag_changed = self._uvs != [
                self._drag_start_uvs.get(index, uv)
                for index, uv in enumerate(self._uvs)
            ]
            self.uvChanged.emit(self.uvs())
            self.update()
        elif self._box_start is not None:
            self._box_rect = QRectF(self._box_start, pos).normalized()
            self.update()
        elif self._pan_last is not None:
            delta = event.position().toPoint() - self._pan_last
            self._pan_last = event.position().toPoint()
            self._pan += QPointF(delta.x(), delta.y())
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            self._drag_axis = None
            if self._drag_changed:
                self.editFinished.emit()
            self._drag_start_uvs.clear()
            self._drag_anchor_uv = None
            self._drag_changed = False
        if self._box_start is not None:
            rect = self._box_rect or QRectF(self._box_start, self._box_start)
            hits = {
                index for index, uv in enumerate(self._uvs)
                if rect.contains(self._uv_to_screen(uv))
            }
            self._selected_points |= hits
            self._active_point = min(hits, default=self._active_point)
            self.pointSelected.emit(self._active_point)
            self._box_start = None
            self._box_rect = None
            self.update()
        self._pan_last = None
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_A \
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.select_all()
            event.accept()
            return
        delta = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }.get(event.key())
        if delta is not None:
            step = 5 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier \
                else 1
            self.nudge_selected(delta[0] * step, delta[1] * step)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom = max(1.0, min(6.0, self._zoom * factor))
        if self._zoom == 1.0:
            self._pan = QPointF(0.0, 0.0)
        self.update()
        event.accept()
