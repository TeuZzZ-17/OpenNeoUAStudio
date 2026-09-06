"""Simple 2D editor for OTL2/OLPL or projected POO2/POL2 data."""

from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sklt_parser import OutlineGroup, Point2D, Point3D, Polygon, SkltModel


ProjectedPoint = tuple[float, float]
EditablePoint = tuple[float, float, float]
StatePoint2D = tuple[int, int]
StatePoint3D = tuple[float, float, float]

HUD_PROJECTION_AXES = (0, 2)


@dataclass(frozen=True)
class EditorState:
    mode: str
    points_2d: tuple[StatePoint2D, ...]
    points_3d: tuple[StatePoint3D, ...]
    groups: tuple[tuple[int, ...], ...]
    selected_index: int
    selected_indices: tuple[int, ...]
    selected_edges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ClipboardPayload:
    points: tuple[EditablePoint, ...]
    groups: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class HistoryEdit:
    before: EditorState
    after: EditorState


class OutlineCanvas(QWidget):
    pointSelected = Signal(int)
    pointMoved = Signal(int, float, float, bool)
    lineSelected = Signal(int, int)
    pointSelectionRequested = Signal(int, bool)
    pointsMoved = Signal(object, bool)
    lineSelectionRequested = Signal(int, int, bool)
    emptySelectionRequested = Signal()
    boxSelectionRequested = Signal(object, object, bool)
    pointContextMenuRequested = Signal(int, float, float, QPoint)
    lineContextMenuRequested = Signal(int, int, float, float, QPoint)
    emptyContextMenuRequested = Signal(float, float, QPoint)
    selectionContextMenuRequested = Signal(float, float, QPoint)
    selectionDragStarted = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._points: list[ProjectedPoint] = []
        self._groups: list[OutlineGroup] = []
        self._selected_index = -1
        self._selected_indices: set[int] = set()
        self._selected_edge: tuple[int, int] | None = None
        self._selected_edges: set[tuple[int, int]] = set()
        self._message = ""
        self._fixed_byte_space = False
        self._dragging_point = False
        self._panning = False
        self._pending_empty_context_menu = False
        self._drag_start_index = -1
        self._drag_start_indices: list[int] = []
        self._drag_start_screen = QPointF()
        self._drag_start_points: dict[int, ProjectedPoint] = {}
        self._drag_transform: tuple[QRectF, float, float, float, float, float] | None = None
        self._last_pan_pos = QPointF()
        self._empty_context_screen = QPointF()
        self._empty_context_world: ProjectedPoint = (0.0, 0.0)
        self._empty_context_global = QPoint()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._view_bounds: tuple[float, float, float, float] | None = None
        self._view_bounds_fixed: bool | None = None
        self._show_vertex_indices = False
        self._drag_snap_message = ""
        self._box_selecting = False
        self._box_select_start = QPointF()
        self._box_select_current = QPointF()
        self._box_select_additive = False
        self._box_select_moved = False
        self._auto_align_enabled = True
        self._snap_preview_pairs: list[tuple[ProjectedPoint, ProjectedPoint]] = []
        self._link_mode_active = False
        self.setMinimumSize(360, 360)
        self.setMouseTracking(True)

    def set_view(
        self,
        points: list[ProjectedPoint],
        groups: list[OutlineGroup],
        selected_index: int = -1,
        message: str = "",
        fixed_byte_space: bool = False,
        selected_edge: tuple[int, int] | None = None,
        selected_indices: set[int] | None = None,
        selected_edges: set[tuple[int, int]] | None = None,
    ) -> None:
        self._points = list(points)
        self._groups = [list(group) for group in groups]
        self._selected_index = selected_index if 0 <= selected_index < len(points) else -1
        self._selected_edge = self._validated_edge(selected_edge)
        if selected_indices is None:
            self._selected_indices = {self._selected_index} if self._selected_index >= 0 else set()
        else:
            self._selected_indices = {
                index for index in selected_indices if 0 <= index < len(self._points)
            }
        if selected_edges is None:
            self._selected_edges = {self._selected_edge} if self._selected_edge is not None else set()
        else:
            self._selected_edges = {
                edge for edge in (self._validated_edge(edge) for edge in selected_edges) if edge is not None
            }
        if self._selected_edge is None and self._selected_edges:
            self._selected_edge = sorted(self._selected_edges)[0]
        self._message = message
        self._fixed_byte_space = fixed_byte_space
        if (
            self._view_bounds is None
            or self._view_bounds_fixed != fixed_byte_space
        ):
            self._view_bounds = self._calculate_view_bounds(
                self._points, fixed_byte_space)
            self._view_bounds_fixed = fixed_byte_space
        self.update()

    def set_selected_index(self, index: int) -> None:
        self._selected_index = index if 0 <= index < len(self._points) else -1
        self._selected_indices = {self._selected_index} if self._selected_index >= 0 else set()
        self._selected_edge = None
        self._selected_edges.clear()
        self.update()

    def set_selected_edge(self, edge: tuple[int, int] | None) -> None:
        self._selected_edge = self._validated_edge(edge)
        self._selected_edges = {self._selected_edge} if self._selected_edge is not None else set()
        self._selected_index = -1
        self._selected_indices.clear()
        self.update()

    def set_selection(
        self,
        selected_indices: set[int],
        selected_edges: set[tuple[int, int]],
        primary_index: int = -1,
    ) -> None:
        self._selected_indices = {
            index for index in selected_indices if 0 <= index < len(self._points)
        }
        self._selected_edges = {
            edge for edge in (self._validated_edge(edge) for edge in selected_edges) if edge is not None
        }
        self._selected_index = primary_index if primary_index in self._selected_indices else -1
        self._selected_edge = sorted(self._selected_edges)[0] if self._selected_edges else None
        self.update()

    def set_show_vertex_indices(self, enabled: bool) -> None:
        self._show_vertex_indices = enabled
        self.update()

    def set_auto_align_enabled(self, enabled: bool) -> None:
        self._auto_align_enabled = bool(enabled)
        if not self._auto_align_enabled:
            self._snap_preview_pairs.clear()
            self.update()

    def set_link_mode_active(self, enabled: bool) -> None:
        self._link_mode_active = bool(enabled)

    def reset_view(self, fit_content: bool = True) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        if fit_content:
            self._view_bounds = self._calculate_view_bounds(
                self._points, self._fixed_byte_space)
            self._view_bounds_fixed = self._fixed_byte_space
        else:
            self._view_bounds = None
            self._view_bounds_fixed = None
        self.update()

    def set_empty_workspace_view(self) -> None:
        """Use the neutral empty-wireframe framing without creating geometry.

        Keeping explicit bounds here is important: if the empty workspace leaves
        ``_view_bounds`` as ``None``, the first added vertex makes ``set_view``
        auto-fit around that single point.  That looks like an unexpected zoom/
        teleport even though the vertex itself was created at the clicked world
        position.  Explicit neutral bounds let the first edit reuse the exact
        same grid framing.
        """
        if self._fixed_byte_space:
            return
        self._view_bounds = (-100.0, 100.0, -100.0, 100.0)
        self._view_bounds_fixed = False
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def ensure_points_visible(self, indices: object, margin: float = 28.0) -> None:
        """Keep newly-created vertices visible without reframing normal moves.

        The canvas intentionally keeps its original view bounds while existing
        vertices move, so editing does not make the whole wireframe jump around.
        Structural additions are different: a new primitive can legitimately
        fall outside those cached bounds.  Refit only when one of the supplied
        new vertices is actually outside the usable viewport.
        """
        if not self._points or not isinstance(indices, (set, list, tuple)):
            return
        valid = [
            index for index in indices
            if isinstance(index, int) and 0 <= index < len(self._points)
        ]
        if not valid:
            return

        transform = self._make_transform()
        visible = QRectF(
            margin,
            margin,
            max(1.0, float(self.width()) - margin * 2.0),
            max(1.0, float(self.height()) - margin * 2.0),
        )
        if all(visible.contains(self._to_screen(self._points[index], transform)) for index in valid):
            return
        self.reset_view(fit_content=True)

    def view_center_world(self) -> ProjectedPoint:
        if not self._points:
            return (0.0, 0.0)
        return self._from_screen(QPointF(self.width() * 0.5, self.height() * 0.5))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(24, 26, 31))

        # The grid is the editing workspace itself, so keep it visible even
        # when the current wireframe contains no vertices yet.
        transform = self._make_transform()
        self._draw_grid(painter, transform)

        if not self._points:
            if self._message:
                painter.setPen(QColor(180, 185, 192))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return

        normal_pen = QPen(QColor(100, 210, 255), 1.5)
        selected_pen = QPen(QColor(245, 245, 245), 2.5)

        selected_pairs: list[tuple[int, int]] = []
        for first, second in self._iter_edges():
            edge = _edge_key(first, second)
            if edge in self._selected_edges:
                selected_pairs.append((first, second))
                continue
            painter.setPen(normal_pen)
            painter.drawLine(
                self._to_screen(self._points[first], transform),
                self._to_screen(self._points[second], transform),
            )

        for first, second in selected_pairs:
            painter.setPen(selected_pen)
            painter.drawLine(
                self._to_screen(self._points[first], transform),
                self._to_screen(self._points[second], transform),
            )

        edge_endpoint_indices: set[int] = set()
        for first, second in self._selected_edges:
            edge_endpoint_indices.add(first)
            edge_endpoint_indices.add(second)
        visual_selected_indices = self._selected_indices | edge_endpoint_indices

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(210, 215, 220))
        for index, point in enumerate(self._points):
            if index in visual_selected_indices:
                continue
            screen = self._to_screen(point, transform)
            painter.drawEllipse(screen, 3.0, 3.0)

        for index in sorted(visual_selected_indices):
            if 0 <= index < len(self._points):
                painter.setBrush(QColor(255, 204, 70))
                screen = self._to_screen(self._points[index], transform)
                radius = 7.0 if index == self._selected_index else 5.5
                painter.drawEllipse(screen, radius, radius)

        if self._snap_preview_pairs:
            preview_pen = QPen(QColor(255, 204, 70, 170), 1.3, Qt.PenStyle.DashLine)
            painter.setPen(preview_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for moving_point, target_point in self._snap_preview_pairs:
                moving_screen = self._to_screen(moving_point, transform)
                target_screen = self._to_screen(target_point, transform)
                painter.drawLine(moving_screen, target_screen)
                painter.drawEllipse(target_screen, 8.0, 8.0)

        if self._box_selecting and self._box_select_moved:
            rect = QRectF(self._box_select_start, self._box_select_current).normalized()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            box_pen = QPen(QColor(255, 204, 70), 1.5, Qt.PenStyle.DashLine)
            painter.setPen(box_pen)
            painter.drawRect(rect)

        if self._show_vertex_indices:
            painter.setPen(QColor(230, 230, 210))
            for index, point in enumerate(self._points):
                screen = self._to_screen(point, transform)
                painter.drawText(screen + QPointF(6.0, -6.0), str(index))

        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            toggle = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            index = self._nearest_point(event.position(), 24.0 if self._link_mode_active else 12.0)
            if self._link_mode_active:
                if index >= 0:
                    self.pointSelectionRequested.emit(index, False)
                    event.accept()
                    return
                # While linking, do not let nearby existing lines steal the click.
                event.accept()
                return
            if index >= 0:
                if toggle:
                    self.pointSelectionRequested.emit(index, True)
                    event.accept()
                    return

                if index not in self._selected_indices:
                    self._selected_indices = {index}
                    self._selected_edges.clear()
                    self.pointSelectionRequested.emit(index, False)
                self._selected_index = index
                self._selected_edge = None
                self._dragging_point = True
                self._drag_start_index = index
                self._drag_start_indices = self._drag_indices_for(index)
                self._drag_transform = self._make_transform()
                # Keep the original grab offset so the vertex remains attached
                # to the cursor instead of jumping on the first move event.
                self._drag_start_screen = event.position()
                self._drag_start_points = {
                    drag_index: self._points[drag_index] for drag_index in self._drag_start_indices
                }
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                event.accept()
                return

            edge = self._nearest_line(event.position())
            if edge is not None:
                edge_key = _edge_key(*edge)
                if toggle:
                    self.lineSelectionRequested.emit(edge_key[0], edge_key[1], True)
                    self.update()
                    event.accept()
                    return

                if edge_key not in self._selected_edges:
                    self._selected_index = -1
                    self._selected_indices.clear()
                    self._selected_edge = edge_key
                    self._selected_edges = {edge_key}
                    self.lineSelectionRequested.emit(edge_key[0], edge_key[1], False)
                else:
                    self._selected_index = -1
                    self._selected_edge = edge_key
                self._begin_selection_drag(event.position())
                self.update()
                event.accept()
                return

            self._box_selecting = True
            self._box_select_start = event.position()
            self._box_select_current = event.position()
            self._box_select_additive = toggle
            self._box_select_moved = False
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            index = self._nearest_point(event.position())
            edge = self._nearest_line(event.position()) if index < 0 else None

            if index >= 0:
                if index not in self._selected_indices:
                    self._selected_indices = {index}
                    self._selected_edges.clear()
                    self._selected_index = index
                    self._selected_edge = None
                    self.pointSelectionRequested.emit(index, False)
                x_value, z_value = self._from_screen(event.position())
                self.pointContextMenuRequested.emit(index, x_value, z_value, event.globalPosition().toPoint())
                event.accept()
                return

            edge = self._nearest_line(event.position())
            if edge is not None:
                edge_key = _edge_key(*edge)
                if edge_key not in self._selected_edges:
                    self._selected_index = -1
                    self._selected_indices.clear()
                    self._selected_edge = edge_key
                    self._selected_edges = {edge_key}
                    self.lineSelectionRequested.emit(edge_key[0], edge_key[1], False)
                x_value, z_value = self._from_screen(event.position())
                self.lineContextMenuRequested.emit(
                    edge_key[0], edge_key[1], x_value, z_value, event.globalPosition().toPoint()
                )
                self.update()
                event.accept()
                return

            self._panning = True
            self._pending_empty_context_menu = True
            self._last_pan_pos = event.position()
            self._empty_context_screen = event.position()
            # Context-menu placement must use the clicked world position even
            # for a brand-new empty wireframe.  The empty canvas already has a
            # neutral transform, so forcing (0, 0) here made the first
            # "Vertex Here" appear at the origin regardless of the click.
            self._empty_context_world = self._from_screen(event.position())
            self._empty_context_global = event.globalPosition().toPoint()
            event.accept()
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._last_pan_pos = event.position()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._dragging_point and self._drag_start_indices:
            positions = self._drag_positions_from_screen(event)
            self._update_snap_preview(positions)
            self.pointsMoved.emit(positions, False)
            event.accept()
            return

        if self._box_selecting:
            self._box_select_current = event.position()
            if (self._box_select_current - self._box_select_start).manhattanLength() > 4.0:
                self._box_select_moved = True
            self.update()
            event.accept()
            return

        if self._panning:
            delta = event.position() - self._last_pan_pos
            self._last_pan_pos = event.position()
            if (
                event.position() - self._empty_context_screen
            ).manhattanLength() > 4.0:
                self._pending_empty_context_menu = False
            self._pan += delta
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_point:
            self._dragging_point = False
            if self._drag_start_indices:
                self.pointsMoved.emit(self._drag_positions_from_screen(event), True)
            self._drag_start_index = -1
            self._drag_start_indices = []
            self._drag_start_points.clear()
            self._drag_transform = None
            self._snap_preview_pairs.clear()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._box_selecting:
            self._box_select_current = event.position()
            if (self._box_select_current - self._box_select_start).manhattanLength() > 4.0:
                self._box_select_moved = True
            if self._box_select_moved:
                selected_indices, selected_edges = self._selection_in_box()
                self.boxSelectionRequested.emit(
                    selected_indices, selected_edges, self._box_select_additive
                )
            elif not self._box_select_additive:
                self._selected_index = -1
                self._selected_indices.clear()
                self._selected_edge = None
                self._selected_edges.clear()
                self.emptySelectionRequested.emit()
            self._box_selecting = False
            self._box_select_moved = False
            self.update()
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton and self._panning:
            self._panning = False
            if self._pending_empty_context_menu:
                x, y = self._empty_context_world
                self.emptyContextMenuRequested.emit(x, y, self._empty_context_global)
            self._pending_empty_context_menu = False
            event.accept()
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        if (
            self._dragging_point
            or self._box_selecting
            or self._panning
        ):
            # Trackpads can emit wheel events while a button is held. Never
            # let that change the view while the user is manipulating geometry.
            # An empty New session is still a real editable workspace, though,
            # so wheel zoom must work before the first vertex exists.
            event.accept()
            return

        cursor = event.position()
        world_under_cursor = self._from_screen(cursor)
        zoom_factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
        new_zoom = max(0.1, min(30.0, self._zoom * zoom_factor))
        if new_zoom == self._zoom:
            event.accept()
            return

        self._zoom = new_zoom
        transform = self._make_transform()
        screen_after = self._to_screen(world_under_cursor, transform)
        self._pan += cursor - screen_after
        self.update()
        event.accept()

    def _nearest_point(self, screen_pos: QPointF, radius: float = 12.0) -> int:
        if not self._points:
            return -1

        transform = self._make_transform()
        best_index = -1
        best_distance_sq = radius * radius
        for index, point in enumerate(self._points):
            screen = self._to_screen(point, transform)
            delta = screen - screen_pos
            distance_sq = delta.x() * delta.x() + delta.y() * delta.y()
            if distance_sq <= best_distance_sq:
                best_distance_sq = distance_sq
                best_index = index
        return best_index

    def _nearest_line(self, screen_pos: QPointF) -> tuple[int, int] | None:
        if not self._points:
            return None

        transform = self._make_transform()
        best_edge: tuple[int, int] | None = None
        best_distance_sq = 8.0 * 8.0
        for first, second in self._iter_edges():
            start = self._to_screen(self._points[first], transform)
            end = self._to_screen(self._points[second], transform)
            distance_sq = _distance_sq_to_segment(screen_pos, start, end)
            if distance_sq <= best_distance_sq:
                best_distance_sq = distance_sq
                best_edge = (first, second)
        return best_edge

    def _iter_edges(self) -> list[tuple[int, int]]:
        edges: list[tuple[int, int]] = []
        point_count = len(self._points)
        for group in self._groups:
            valid_group = [index for index in group if 0 <= index < point_count]
            pairs = list(zip(valid_group, valid_group[1:]))
            if len(valid_group) > 2:
                pairs.append((valid_group[-1], valid_group[0]))
            edges.extend(pairs)
        return edges

    def _validated_edge(self, edge: tuple[int, int] | None) -> tuple[int, int] | None:
        if edge is None:
            return None
        first, second = edge
        if not (0 <= first < len(self._points) and 0 <= second < len(self._points)):
            return None
        edge_key = _edge_key(first, second)
        for existing_first, existing_second in self._iter_edges():
            if _edge_key(existing_first, existing_second) == edge_key:
                return edge_key
        return None

    def _begin_selection_drag(self, screen_pos: QPointF) -> None:
        drag_indices = self._drag_indices_for_selection()
        if not drag_indices:
            return
        self._dragging_point = True
        self._drag_start_index = drag_indices[0]
        self._drag_start_indices = drag_indices
        self._drag_start_screen = screen_pos
        self._drag_start_points = {
            drag_index: self._points[drag_index] for drag_index in self._drag_start_indices
        }
        self._drag_transform = self._make_transform()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _drag_indices_for(self, clicked_index: int) -> list[int]:
        drag_indices = set(self._selected_indices)
        for first, second in self._selected_edges:
            drag_indices.add(first)
            drag_indices.add(second)
        if clicked_index not in drag_indices:
            drag_indices = {clicked_index}
        return sorted(index for index in drag_indices if 0 <= index < len(self._points))

    def _connected_vertices(self, index: int) -> set[int]:
        connected: set[int] = set()
        for first, second in self._iter_edges():
            if first == index:
                connected.add(second)
            elif second == index:
                connected.add(first)
        return connected

    def _drag_indices_for_selection(self) -> list[int]:
        drag_indices = set(self._selected_indices)
        for first, second in self._selected_edges:
            drag_indices.add(first)
            drag_indices.add(second)
        return sorted(index for index in drag_indices if 0 <= index < len(self._points))

    def _selection_in_box(self) -> tuple[set[int], set[tuple[int, int]]]:
        rect = QRectF(self._box_select_start, self._box_select_current).normalized()
        if rect.width() < 2.0 or rect.height() < 2.0:
            return set(), set()

        transform = self._make_transform()
        selected_indices: set[int] = set()
        for index, point in enumerate(self._points):
            if rect.contains(self._to_screen(point, transform)):
                selected_indices.add(index)

        selected_edges: set[tuple[int, int]] = set()
        for first, second in self._iter_edges():
            start = self._to_screen(self._points[first], transform)
            end = self._to_screen(self._points[second], transform)
            if first in selected_indices and second in selected_indices:
                selected_edges.add(_edge_key(first, second))
            elif _segment_intersects_rect(start, end, rect):
                selected_edges.add(_edge_key(first, second))

        return selected_indices, selected_edges

    def _drag_positions_from_screen(self, event: QMouseEvent) -> dict[int, ProjectedPoint]:
        transform = self._drag_transform or self._make_transform()
        scale = max(0.000001, transform[1])
        delta = event.position() - self._drag_start_screen
        precision = 0.25 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        delta_x = (delta.x() / scale) * precision
        delta_y = -(delta.y() / scale) * precision
        moved: dict[int, ProjectedPoint] = {}
        for index, start_point in self._drag_start_points.items():
            x = start_point[0] + delta_x
            y = start_point[1] + delta_y
            if self._fixed_byte_space:
                moved[index] = (_clamp_byte(round(x)), _clamp_byte(round(y)))
            else:
                moved[index] = (x, y)
        return moved

    def _draw_grid(self, painter: QPainter, transform: tuple[QRectF, float, float, float, float, float]) -> None:
        rect, scale, min_x, min_y, offset_x, offset_y = transform
        viewport = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        top_left_world = self._from_screen(viewport.topLeft())
        bottom_right_world = self._from_screen(viewport.bottomRight())
        min_world_x = min(top_left_world[0], bottom_right_world[0])
        max_world_x = max(top_left_world[0], bottom_right_world[0])
        min_world_y = min(top_left_world[1], bottom_right_world[1])
        max_world_y = max(top_left_world[1], bottom_right_world[1])
        world_span = max(max_world_x - min_world_x, max_world_y - min_world_y, 1.0)
        major_step = _nice_grid_step(world_span / 10.0)
        if major_step <= 0.0:
            return

        minor_step = major_step / 5.0
        axis_pen = QPen(QColor(255, 204, 70, 95), 1.2)

        def draw_lines(step: float, pen: QPen) -> None:
            painter.setPen(pen)
            for index in range(
                math.floor(min_world_x / step),
                math.ceil(max_world_x / step) + 1,
            ):
                value = index * step
                if abs(value) <= step * 0.001:
                    continue
                painter.drawLine(
                    self._to_screen((value, min_world_y), transform),
                    self._to_screen((value, max_world_y), transform),
                )
            for index in range(
                math.floor(min_world_y / step),
                math.ceil(max_world_y / step) + 1,
            ):
                value = index * step
                if abs(value) <= step * 0.001:
                    continue
                painter.drawLine(
                    self._to_screen((min_world_x, value), transform),
                    self._to_screen((max_world_x, value), transform),
                )

        draw_lines(minor_step, QPen(QColor(255, 255, 255, 20), 1.0))
        draw_lines(major_step, QPen(QColor(255, 255, 255, 45), 1.1))

        painter.setPen(axis_pen)
        zero_x_top = self._to_screen((0.0, min_world_y), transform)
        zero_x_bottom = self._to_screen((0.0, max_world_y), transform)
        zero_y_left = self._to_screen((min_world_x, 0.0), transform)
        zero_y_right = self._to_screen((max_world_x, 0.0), transform)
        painter.drawLine(zero_x_top, zero_x_bottom)
        painter.drawLine(zero_y_left, zero_y_right)

        center = self._to_screen((0.0, 0.0), transform)
        painter.setBrush(QColor(255, 204, 70, 150))
        painter.setPen(QPen(QColor(255, 204, 70, 180), 1.0))
        painter.drawLine(center + QPointF(-7.0, 0.0), center + QPointF(7.0, 0.0))
        painter.drawLine(center + QPointF(0.0, -7.0), center + QPointF(0.0, 7.0))
        painter.drawEllipse(center, 3.0, 3.0)

    def _update_snap_preview(self, moved_positions: dict[int, ProjectedPoint]) -> None:
        self._snap_preview_pairs.clear()
        if not self._auto_align_enabled or not moved_positions or not self._points:
            self.update()
            return
        moved_indices = set(moved_positions)
        stable_indices = [index for index in range(len(self._points)) if index not in moved_indices]
        if not stable_indices:
            self.update()
            return
        transform = self._make_transform()
        scale = max(abs(transform[1]), 1.0e-9)
        # Screen-space sensitivity stays stable when model extents change.
        line_threshold = 10.0 / scale
        edges = self._iter_edges()
        preview_pairs: set[tuple[ProjectedPoint, ProjectedPoint]] = set()
        for index, point in moved_positions.items():
            if not (0 <= index < len(self._points)):
                continue

            x_candidates, z_candidates = _axis_aligned_line_snap_candidates(
                self._points, edges, moved_indices, point, line_threshold
            )
            if x_candidates:
                preview_pairs.add((point, min(x_candidates, key=lambda item: item[0])[2]))
            if z_candidates:
                preview_pairs.add((point, min(z_candidates, key=lambda item: item[0])[2]))

        self._snap_preview_pairs.extend(sorted(preview_pairs))
        self.update()

    @staticmethod
    def _calculate_view_bounds(
        points: list[ProjectedPoint], fixed_byte_space: bool
    ) -> tuple[float, float, float, float] | None:
        if not points:
            return None
        if fixed_byte_space:
            return 0.0, 255.0, 0.0, 255.0

        min_x = min(point[0] for point in points)
        max_x = max(point[0] for point in points)
        min_y = min(point[1] for point in points)
        max_y = max(point[1] for point in points)
        span = max(max_x - min_x, max_y - min_y, 1.0)
        padding = max(span * 0.12, 1.0)
        return (
            min_x - padding,
            max_x + padding,
            min_y - padding,
            max_y + padding,
        )

    def _make_transform(self) -> tuple[QRectF, float, float, float, float, float]:
        margin = 24.0
        rect = QRectF(
            margin,
            margin,
            max(1.0, self.width() - margin * 2.0),
            max(1.0, self.height() - margin * 2.0),
        )

        if (
            self._view_bounds is None
            or self._view_bounds_fixed != self._fixed_byte_space
        ):
            self._view_bounds = self._calculate_view_bounds(
                self._points, self._fixed_byte_space)
            self._view_bounds_fixed = self._fixed_byte_space
        if self._view_bounds is None:
            if self._fixed_byte_space:
                min_x, max_x, min_y, max_y = 0.0, 255.0, 0.0, 255.0
            else:
                # Neutral empty-wireframe workspace with the origin centered.
                min_x, max_x, min_y, max_y = -100.0, 100.0, -100.0, 100.0
        else:
            min_x, max_x, min_y, max_y = self._view_bounds

        scale = min(rect.width() / (max_x - min_x), rect.height() / (max_y - min_y))
        scale *= self._zoom
        content_width = (max_x - min_x) * scale
        content_height = (max_y - min_y) * scale
        offset_x = max(0.0, (rect.width() - content_width) * 0.5)
        offset_y = max(0.0, (rect.height() - content_height) * 0.5)
        return rect, scale, min_x, min_y, offset_x, offset_y

    def _to_screen(
        self, point: ProjectedPoint, transform: tuple[QRectF, float, float, float, float, float]
    ) -> QPointF:
        rect, scale, min_x, min_y, offset_x, offset_y = transform
        x, y = point
        return QPointF(
            rect.left() + offset_x + (x - min_x) * scale + self._pan.x(),
            rect.bottom() - offset_y - (y - min_y) * scale + self._pan.y(),
        )

    def _from_screen(self, screen_pos: QPointF) -> ProjectedPoint:
        rect, scale, min_x, min_y, offset_x, offset_y = self._make_transform()
        x = min_x + (screen_pos.x() - self._pan.x() - rect.left() - offset_x) / scale
        y = min_y + (rect.bottom() - offset_y + self._pan.y() - screen_pos.y()) / scale
        if self._fixed_byte_space:
            return (_clamp_byte(round(x)), _clamp_byte(round(y)))
        return (x, y)


class OutlineEditor(QWidget):
    dirtyChanged = Signal(bool)
    selectionChanged = Signal(int)
    geometryChanged = Signal()
    undoRedoChanged = Signal()
    resetApplied = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model: SkltModel | None = None
        self._mode = "none"
        self._points_2d: list[Point2D] = []
        self._points_3d: list[EditablePoint] = []
        self._groups: list[OutlineGroup] = []
        self._selected_index = -1
        self._selected_indices: set[int] = set()
        self._selected_link: tuple[int, int] | None = None
        self._selected_edges: set[tuple[int, int]] = set()
        self._clipboard: ClipboardPayload | None = None
        self._dirty = False
        self._clean_signature: tuple = ()
        self._loaded_state: EditorState | None = None
        self._undo_stack: list[HistoryEdit] = []
        self._redo_stack: list[HistoryEdit] = []
        self._drag_start_state: EditorState | None = None
        self._link_start_index = -1
        self._last_auto_align_message = ""
        self._syncing_controls = False

        self.canvas = OutlineCanvas()
        self.canvas.pointSelectionRequested.connect(self.select_point)
        self.canvas.pointsMoved.connect(self.move_projected_points)
        self.canvas.lineSelectionRequested.connect(self.select_link)
        self.canvas.emptySelectionRequested.connect(self.clear_selection)
        self.canvas.boxSelectionRequested.connect(self.select_box)
        self.canvas.pointContextMenuRequested.connect(self._show_point_context_menu)
        self.canvas.lineContextMenuRequested.connect(self._show_link_context_menu)
        self.canvas.emptyContextMenuRequested.connect(self._show_empty_context_menu)
        self.canvas.selectionContextMenuRequested.connect(self._show_selection_context_menu)
        self.canvas.selectionDragStarted.connect(self.prepare_selection_drag)

        self.selected_label = QLabel("None")
        self.x_spin = self._make_float_spinbox()
        self.y_spin = self._make_float_spinbox()
        self.z_spin = self._make_float_spinbox()
        self.undo_button = QPushButton("< Undo")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Redo >")
        self.redo_button.clicked.connect(self.redo)
        for button in (self.undo_button, self.redo_button):
            button.setStyleSheet("QPushButton { text-align: center; }")
        self.link_button = QPushButton("Link")
        self.link_button.clicked.connect(self.start_link)
        self.add_point_button = QPushButton("Add Vertex")
        self.add_point_button.clicked.connect(self.add_point)
        self.delete_point_button = QPushButton("Delete Vertex")
        self.delete_point_button.clicked.connect(self.delete_selected_point)
        self.show_indices_check = QCheckBox("Vertex IDs")
        self.show_indices_check.toggled.connect(self.canvas.set_show_vertex_indices)
        self.auto_align_check = QCheckBox("Auto Align")
        self.auto_align_check.setChecked(True)
        self.auto_align_check.toggled.connect(self.canvas.set_auto_align_enabled)
        self.auto_align_check.setToolTip(
            "Snaps a dragged vertex to connected vertices when it is close to a straight horizontal or vertical line."
        )
        self.status_label = QLabel("")
        self.reset_button = QPushButton("Reset")
        self.reset_button.clicked.connect(self.reset_to_loaded)

        self._build_ui()
        self._update_controls()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape and self._link_start_index >= 0:
            self.cancel_link()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and (self._selected_indices or self._selected_edges):
            self.clear_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    @property
    def save_mode(self) -> str:
        if self._mode in ("otl2", "poo2"):
            return self._mode
        return "none"

    @property
    def outline_points(self) -> list[Point2D]:
        return list(self._points_2d)

    @property
    def projected_points(self) -> list[Point3D]:
        return [(point[0], point[1], point[2]) for point in self._points_3d]

    @property
    def polygons(self) -> list[Polygon]:
        return [list(group) for group in self._groups]

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def can_save(self) -> bool:
        return bool(
            (self._mode == "otl2" and self._model and self._model.otl2_payload_offset is not None)
            or (
                self._mode == "poo2"
                and self._model
                and self._model.poo2_payload_offset is not None
                and self._model.pol2_payload_offset is not None
            )
        )

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @property
    def can_reset(self) -> bool:
        if self._loaded_state is None:
            return False
        loaded_signature = (
            self._loaded_state.mode,
            self._loaded_state.points_2d,
            self._loaded_state.points_3d,
            self._loaded_state.groups,
        )
        return self._current_signature() != loaded_signature

    @property
    def selected_index(self) -> int:
        return self._selected_index

    @property
    def has_selected_link(self) -> bool:
        return bool(self._selected_edges)

    @property
    def has_vertex_selection(self) -> bool:
        return bool(self._selected_indices)

    @property
    def selected_vertex_count(self) -> int:
        return len(self._selected_indices)

    @property
    def selected_edge_count(self) -> int:
        return len(self._selected_edges)

    @property
    def selected_vertex_indices(self) -> tuple[int, ...]:
        """Selected vertex IDs in stable display order."""
        return tuple(sorted(self._selected_indices))

    @property
    def selected_edges(self) -> tuple[tuple[int, int], ...]:
        """Selected connection endpoint IDs in stable display order."""
        return tuple(sorted(self._selected_edges))

    @property
    def has_clipboard(self) -> bool:
        return self._clipboard is not None and bool(self._clipboard.points)

    @property
    def has_any_selection(self) -> bool:
        return bool(self._selected_indices or self._selected_edges)

    @property
    def uses_projected_hud_mode(self) -> bool:
        return self._mode == "poo2"

    @property
    def has_structural_changes(self) -> bool:
        if self._mode != "poo2" or not self._model:
            return False
        return (
            len(self._points_3d) != len(self._model.points)
            or [list(group) for group in self._groups] != self._model.polygons
        )

    @property
    def edit_mode_text(self) -> str:
        if self._mode == "otl2":
            return "OTL2/OLPL Vertex Mode"
        if self._mode == "poo2":
            return "HUD / Wireframe Edit Mode"
        return "No editable 2D mode"

    @property
    def editable_point_count(self) -> int:
        return self._point_count()

    @property
    def editable_polygon_count(self) -> int:
        return len(self._groups)

    @property
    def editable_line_count(self) -> int:
        return _outline_line_count(self._groups)

    def set_model(self, model: SkltModel) -> None:
        self._model = model
        self._points_2d = list(model.outline_points)
        self._points_3d = [(point[0], point[1], point[2]) for point in model.points]

        if model.has_outline_data:
            self._mode = "otl2"
            self._groups = [list(group) for group in model.outline_groups]
            self._selected_index = 0 if self._points_2d else -1
            self._selected_indices = {self._selected_index} if self._selected_index >= 0 else set()
        elif model.poo2_payload_offset is not None and model.pol2_payload_offset is not None:
            # Empty POO2/POL2 is a valid editable New document.  Do not require
            # a synthetic origin vertex just to enter wireframe edit mode.
            self._mode = "poo2"
            self._groups = [list(group) for group in model.polygons]
            self._selected_index = 0 if self._points_3d else -1
            self._selected_indices = {self._selected_index} if self._selected_index >= 0 else set()
        else:
            self._mode = "none"
            self._groups = []
            self._selected_index = -1
            self._selected_indices.clear()

        self._undo_stack.clear()
        self._redo_stack.clear()
        self._drag_start_state = None
        self._link_start_index = -1
        self._selected_link = None
        self._selected_edges.clear()
        self._last_auto_align_message = ""
        self._loaded_state = self._snapshot()
        self.mark_clean()
        self.canvas.reset_view(fit_content=False)
        self._refresh_canvas()
        if self._mode == "poo2" and not self._points_3d:
            self.canvas.set_empty_workspace_view()
        self._update_controls()

    def prepare_selection_drag(self) -> None:
        # Dragging is intentionally simple again: selected vertices/link endpoints
        # move as-is. Do not detach/clone connected geometry during mouse drag.
        return

    def select_point(self, index: int, toggle: bool = False) -> None:
        point_count = self._point_count()
        if not (0 <= index < point_count):
            self.clear_selection()
            return
        if self._link_start_index >= 0 and index >= 0:
            self._finish_link(index)
            return

        if toggle:
            if index in self._selected_indices:
                self._selected_indices.remove(index)
            else:
                self._selected_indices.add(index)
            self._selected_index = index if index in self._selected_indices else -1
        else:
            self._selected_indices = {index}
            self._selected_edges.clear()
            self._selected_index = index

        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        self._sync_canvas_selection()
        self._update_controls()
        self.selectionChanged.emit(self._selected_index)

    def select_link(self, first: int, second: int, toggle: bool = False) -> None:
        if self._mode != "poo2" or not self._has_edge(first, second):
            return
        self._link_start_index = -1
        edge = _edge_key(first, second)
        if toggle:
            if edge in self._selected_edges:
                self._selected_edges.remove(edge)
            else:
                self._selected_edges.add(edge)
        else:
            self._selected_indices.clear()
            self._selected_index = -1
            self._selected_edges = {edge}

        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        if self._selected_indices and self._selected_index not in self._selected_indices:
            self._selected_index = sorted(self._selected_indices)[0]
        if not self._selected_indices:
            self._selected_index = -1
        self._sync_canvas_selection()
        self._update_controls()
        self.selectionChanged.emit(self._selected_index)
        self.undoRedoChanged.emit()

    def select_box(self, indices_object: object, edges_object: object, additive: bool = False) -> None:
        point_count = self._point_count()
        indices = {
            index
            for index in indices_object
            if isinstance(index, int) and 0 <= index < point_count
        } if isinstance(indices_object, (set, list, tuple)) else set()
        edges = {
            _edge_key(first, second)
            for first, second in edges_object
            if isinstance(first, int)
            and isinstance(second, int)
            and 0 <= first < point_count
            and 0 <= second < point_count
            and self._has_edge(first, second)
        } if isinstance(edges_object, (set, list, tuple)) else set()

        self._link_start_index = -1
        if additive:
            self._selected_indices |= indices
            self._selected_edges |= edges
        else:
            self._selected_indices = indices
            self._selected_edges = edges
        self._selected_index = sorted(self._selected_indices)[0] if self._selected_indices else -1
        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        self._last_auto_align_message = (
            f"Box selected {len(indices)} vertex/vertices"
            + (f" and {len(edges)} link(s)" if edges else "")
        ) if (indices or edges) else "Box selection empty"
        self._sync_canvas_selection()
        self._update_controls()
        self.selectionChanged.emit(self._selected_index)
        self.undoRedoChanged.emit()

    def clear_selection(self) -> None:
        self._link_start_index = -1
        self._selected_index = -1
        self._selected_indices.clear()
        self._selected_link = None
        self._selected_edges.clear()
        self._last_auto_align_message = ""
        self._sync_canvas_selection()
        self._update_controls()
        self.selectionChanged.emit(-1)

    def _sync_canvas_selection(self) -> None:
        self.canvas.set_selection(
            set(self._selected_indices), set(self._selected_edges), self._selected_index
        )
        self.canvas.set_link_mode_active(self._link_start_index >= 0)

    def _iter_edges(self) -> list[tuple[int, int]]:
        edges: list[tuple[int, int]] = []
        point_count = len(self._points_3d) if self._mode == "poo2" else len(self._points_2d)
        for group in self._groups:
            valid_group = [index for index in group if 0 <= index < point_count]
            pairs = list(zip(valid_group, valid_group[1:]))
            if len(valid_group) > 2:
                pairs.append((valid_group[-1], valid_group[0]))
            edges.extend((_edge_key(left, right) for left, right in pairs if left != right))
        return edges

    def _detach_selected_edges_from_unselected_edges(self) -> bool:
        if self._mode != "poo2" or not self._selected_edges:
            return False

        selected_edges = {_edge_key(first, second) for first, second in self._selected_edges}
        selected_vertices: set[int] = set(self._selected_indices)
        for first, second in selected_edges:
            selected_vertices.add(first)
            selected_vertices.add(second)

        outside_incident: set[int] = set()
        for first, second in self._iter_edges():
            edge = _edge_key(first, second)
            if edge in selected_edges:
                continue
            if first in selected_vertices:
                outside_incident.add(first)
            if second in selected_vertices:
                outside_incident.add(second)

        if not outside_incident:
            return False

        clone_map: dict[int, int] = {}
        for old_index in sorted(outside_incident):
            if 0 <= old_index < len(self._points_3d):
                clone_map[old_index] = len(self._points_3d)
                self._points_3d.append(self._points_3d[old_index])

        if not clone_map:
            return False

        new_groups: list[OutlineGroup] = []
        for group in self._groups:
            point_count = len(self._points_3d)
            valid_group = [index for index in group if 0 <= index < point_count]
            if len(valid_group) < 2:
                continue
            pairs = list(zip(valid_group, valid_group[1:]))
            if len(valid_group) > 2:
                pairs.append((valid_group[-1], valid_group[0]))
            for first, second in pairs:
                if first == second:
                    continue
                edge = _edge_key(first, second)
                if edge in selected_edges:
                    new_groups.append([clone_map.get(first, first), clone_map.get(second, second)])
                else:
                    new_groups.append([first, second])

        self._groups = new_groups
        self._selected_edges = {
            _edge_key(clone_map.get(first, first), clone_map.get(second, second))
            for first, second in selected_edges
        }
        self._selected_indices = {clone_map.get(index, index) for index in selected_vertices}
        self._selected_index = sorted(self._selected_indices)[0] if self._selected_indices else -1
        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        return True

    def move_projected_point(
        self, index: int, first_axis_value: float, second_axis_value: float, commit_undo: bool = True
    ) -> None:
        self.move_projected_points({index: (first_axis_value, second_axis_value)}, commit_undo)

    def move_projected_points(
        self, point_positions: object, commit_undo: bool = True
    ) -> None:
        if self._mode not in ("otl2", "poo2") or not isinstance(point_positions, dict):
            return

        valid_positions: dict[int, ProjectedPoint] = {}
        for index, value in point_positions.items():
            if not isinstance(index, int):
                continue
            if not isinstance(value, tuple) and not isinstance(value, list):
                continue
            if len(value) != 2:
                continue
            if 0 <= index < self._point_count():
                valid_positions[index] = (float(value[0]), float(value[1]))
        if not valid_positions:
            return

        before = self._snapshot() if commit_undo and self._drag_start_state is None else None
        if not commit_undo and self._drag_start_state is None:
            self._drag_start_state = self._snapshot()

        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        if self._mode == "poo2":
            valid_positions = self._snap_moved_group_center_to_origin(valid_positions)

        use_single_vertex_snap = len(valid_positions) == 1 and self._mode == "poo2"
        for index, (first_axis_value, second_axis_value) in valid_positions.items():
            if self._mode == "otl2":
                new_point = (_clamp_byte(round(first_axis_value)), _clamp_byte(round(second_axis_value)))
                if self._points_2d[index] != new_point:
                    self._points_2d[index] = new_point
            elif self._mode == "poo2":
                if use_single_vertex_snap:
                    first_axis_value, second_axis_value = self._auto_align_projected_vertex(
                        index, first_axis_value, second_axis_value
                    )
                else:
                    self._last_auto_align_message = ""
                coords = list(self._points_3d[index])
                coords[HUD_PROJECTION_AXES[0]] = float(first_axis_value)
                coords[HUD_PROJECTION_AXES[1]] = float(second_axis_value)
                new_point = (coords[0], coords[1], coords[2])
                if self._points_3d[index] != new_point:
                    self._points_3d[index] = new_point

        if self._selected_indices:
            self._selected_index = sorted(self._selected_indices)[0]
        elif len(valid_positions) == 1:
            self._selected_index = next(iter(valid_positions))
            self._selected_indices = {self._selected_index}
        self._after_edit()

        if commit_undo:
            before = self._drag_start_state or before or self._snapshot()
            self._drag_start_state = None
            self._push_history(before, self._snapshot())

    def _snap_moved_group_center_to_origin(
        self, positions: dict[int, ProjectedPoint]
    ) -> dict[int, ProjectedPoint]:
        if (
            self._mode != "poo2"
            or not self.auto_align_check.isChecked()
            or len(positions) < 2
        ):
            return positions
        center_x = sum(point[0] for point in positions.values()) / len(positions)
        center_z = sum(point[1] for point in positions.values()) / len(positions)
        transform = self.canvas._make_transform()
        scale = max(abs(transform[1]), 1.0e-9)
        threshold = 12.0 / scale
        if math.hypot(center_x, center_z) > threshold:
            return positions
        self._last_auto_align_message = "Auto Align: centered selection on origin"
        return {
            index: (point[0] - center_x, point[1] - center_z)
            for index, point in positions.items()
        }

    def _merge_overlapping_vertices(self, tolerance: float = 0.0001) -> None:
        """Collapse truly overlapping projected POO2 vertices into one vertex.

        This runs only after a committed edit.  It prevents two vertices from
        sitting on top of each other after manual drag or Auto Align snap.
        The wireframe links are remapped to the surviving vertex and self-links
        are discarded.
        """
        if self._mode != "poo2" or len(self._points_3d) < 2:
            return

        projected = self._projected_points()
        canonical_for: dict[int, int] = {}
        for index, point in enumerate(projected):
            for other in range(index):
                other_point = projected[other]
                if (
                    abs(point[0] - other_point[0]) <= tolerance
                    and abs(point[1] - other_point[1]) <= tolerance
                ):
                    canonical_for[index] = canonical_for.get(other, other)
                    break
        if not canonical_for:
            return

        old_to_new: dict[int, int] = {}
        new_points: list[EditablePoint] = []
        for old_index, point in enumerate(self._points_3d):
            if old_index in canonical_for:
                continue
            old_to_new[old_index] = len(new_points)
            new_points.append(point)
        for old_index, canonical in canonical_for.items():
            old_to_new[old_index] = old_to_new[canonical]

        new_edges: set[tuple[int, int]] = set()
        for first, second in self._iter_edges():
            if first not in old_to_new or second not in old_to_new:
                continue
            new_first = old_to_new[first]
            new_second = old_to_new[second]
            if new_first != new_second:
                new_edges.add(_edge_key(new_first, new_second))

        self._points_3d = new_points
        self._groups = [[first, second] for first, second in sorted(new_edges)]
        self._selected_indices = {
            old_to_new[index]
            for index in self._selected_indices
            if index in old_to_new
        }
        self._selected_edges = {
            _edge_key(old_to_new[first], old_to_new[second])
            for first, second in self._selected_edges
            if first in old_to_new and second in old_to_new and old_to_new[first] != old_to_new[second]
        }
        self._selected_index = sorted(self._selected_indices)[0] if self._selected_indices else -1
        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        self._last_auto_align_message = "Merged overlapping vertex/vertices"

    def undo(self) -> None:
        if not self._undo_stack:
            return

        edit = self._undo_stack.pop()
        self._restore_state(edit.before)
        self._redo_stack.append(edit)
        self._after_edit()

    def redo(self) -> None:
        if not self._redo_stack:
            return

        edit = self._redo_stack.pop()
        self._restore_state(edit.after)
        self._undo_stack.append(edit)
        self._after_edit()

    def add_point(self) -> None:
        if self._mode != "poo2":
            return

        self._link_start_index = -1
        self._selected_link = None
        if 0 <= self._selected_index < len(self._points_3d):
            self.add_point_near_index(self._selected_index)
        else:
            x, z = self.canvas.view_center_world()
            self.add_point_at_projected(x, z)

    def add_point_near_index(self, index: int) -> None:
        if self._mode != "poo2" or not (0 <= index < len(self._points_3d)):
            return
        x, y, z = self._points_3d[index]
        self._add_poo2_point((x + 10.0, y, z + 10.0))

    def add_point_at_projected(self, x_value: float, z_value: float) -> None:
        if self._mode != "poo2":
            return
        self._add_poo2_point((float(x_value), _average_y(self._points_3d), float(z_value)))

    def add_shape(self, shape: str) -> None:
        if self._mode != "poo2":
            return
        # Top-bar Add creates clean construction primitives at the editor origin.
        # Context-menu Add creates them exactly where the user opened the menu.
        self.add_shape_at_projected(shape, 0.0, 0.0)

    def add_shape_at_projected(self, shape: str, center_x: float, center_z: float) -> None:
        if self._mode != "poo2":
            return
        center_x, center_z = float(center_x), float(center_z)
        shape = shape.lower()
        radius = 80.0
        if shape == "triangle":
            count = 3
        elif shape == "square":
            count = 4
        elif shape == "pentagon":
            count = 5
        elif shape in {"circle", "round"}:
            count = 16
        else:
            return

        before = self._snapshot()
        y_value = _average_y(self._points_3d)
        first_index = len(self._points_3d)
        new_indices: list[int] = []
        start_angle = -math.pi / 2.0
        for step in range(count):
            angle = start_angle + (math.tau * step / count)
            x_value = center_x + math.cos(angle) * radius
            z_value = center_z + math.sin(angle) * radius
            self._points_3d.append((float(x_value), y_value, float(z_value)))
            new_indices.append(first_index + step)
        self._groups.append(new_indices)
        self._selected_indices = set(new_indices)
        self._selected_index = new_indices[0] if new_indices else -1
        self._selected_edges = {
            _edge_key(new_indices[i], new_indices[(i + 1) % len(new_indices)])
            for i in range(len(new_indices))
        } if len(new_indices) > 2 else set()
        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None
        self._link_start_index = -1
        self._last_auto_align_message = f"Added {shape}"
        self._push_history(before, self._snapshot())
        self._after_edit()
        self.canvas.ensure_points_visible(new_indices)
        self.selectionChanged.emit(self._selected_index)

    def _add_poo2_point(self, point: EditablePoint) -> None:
        before = self._snapshot()
        self._link_start_index = -1
        self._selected_link = None
        self._selected_edges.clear()
        self._points_3d.append(point)
        self._selected_index = len(self._points_3d) - 1
        self._selected_indices = {self._selected_index}
        self._push_history(before, self._snapshot())
        self._after_edit()
        # Adding a single vertex is a local edit, never a camera command.
        # In particular, the first "Vertex Here" in a New document must keep
        # the empty grid framing exactly as the user saw it before the click.

    def delete_selected_point(self) -> None:
        self.delete_selection(confirm=False)

    def delete_selection(self, confirm: bool = False) -> None:
        if self._mode != "poo2":
            return
        edges = set(self._selected_edges)
        # If the user selected only links, Delete must remove only those links.
        # Vertex deletion happens only when vertices are explicitly selected.
        vertices = set(self._selected_indices)
        if not vertices and not edges:
            return

        # No safety dialog here: Undo is available and faster for editing workflows.

        before = self._snapshot()
        self._link_start_index = -1
        if vertices:
            self._delete_vertices(vertices)
            self._last_auto_align_message = "Deleted selection"
        elif edges:
            removed_links, removed_vertices = self._delete_edges(edges)
            self._last_auto_align_message = _deleted_links_message(
                removed_links, removed_vertices
            )
        self._selected_indices.clear()
        self._selected_index = -1
        self._selected_edges.clear()
        self._selected_link = None
        self._push_history(before, self._snapshot())
        self._after_edit()
        self.selectionChanged.emit(-1)

    def _delete_vertices(self, deleted_indices: set[int]) -> None:
        deleted = {index for index in deleted_indices if 0 <= index < len(self._points_3d)}
        if not deleted:
            return
        index_map: dict[int, int] = {}
        new_points: list[EditablePoint] = []
        for old_index, point in enumerate(self._points_3d):
            if old_index in deleted:
                continue
            index_map[old_index] = len(new_points)
            new_points.append(point)

        remapped_groups: list[OutlineGroup] = []
        for group in self._groups:
            if any(index in deleted for index in group):
                continue
            remapped = [index_map[index] for index in group if index in index_map]
            if len(remapped) >= 2:
                remapped_groups.append(remapped)
        self._points_3d = new_points
        self._groups = remapped_groups

    def _delete_edges(self, edges: set[tuple[int, int]]) -> tuple[int, int]:
        removed_count = 0
        for first, second in sorted(edges):
            if self._has_edge(first, second):
                self._remove_edge_from_groups(first, second)
                removed_count += 1

        # Deleting a connection must never delete its vertices implicitly.
        # Standalone vertices are valid editable data and may be linked again
        # later; removing them here made link editing look destructive/random.
        return removed_count, 0

    def start_link(self) -> None:
        if (
            self._mode != "poo2"
            or len(self._selected_indices) != 1
            or not (0 <= self._selected_index < len(self._points_3d))
        ):
            return

        self._selected_link = None
        self._selected_edges.clear()
        self._sync_canvas_selection()
        self._link_start_index = self._selected_index
        self.canvas.set_link_mode_active(True)
        self._update_controls()

    def cancel_link(self) -> None:
        if self._link_start_index < 0:
            return
        self._link_start_index = -1
        self.canvas.set_link_mode_active(False)
        self._update_controls()

    def _finish_link(self, target_index: int) -> None:
        start_index = self._link_start_index
        self._link_start_index = -1
        self.canvas.set_link_mode_active(False)
        if start_index == target_index:
            self.select_point(target_index)
            return
        if not (
            0 <= start_index < len(self._points_3d)
            and 0 <= target_index < len(self._points_3d)
        ):
            self.select_point(target_index)
            return
        if self._has_edge(start_index, target_index):
            self.select_point(target_index)
            return

        before = self._snapshot()
        self._selected_link = None
        self._groups.append([start_index, target_index])
        self._selected_index = target_index
        self._selected_indices = {target_index}
        self._selected_edges.clear()
        self._push_history(before, self._snapshot())
        self._after_edit()
        self.selectionChanged.emit(target_index)

    def align_selected_horizontal(self) -> None:
        """Align the selected vertex horizontally by copying Z from the nearest linked vertex."""
        self._align_selected_projected_axis(axis=1)

    def align_selected_vertical(self) -> None:
        """Align the selected vertex vertically by copying X from the nearest linked vertex."""
        self._align_selected_projected_axis(axis=0)

    def _align_selected_projected_axis(self, axis: int) -> None:
        if self._mode != "poo2" or not (0 <= self._selected_index < len(self._points_3d)):
            return

        target = self._nearest_alignment_vertex(self._selected_index, axis)
        if target is None:
            return

        before = self._snapshot()
        self._selected_link = None
        coords = list(self._points_3d[self._selected_index])
        projected = self._projected_points()[target]
        coords[HUD_PROJECTION_AXES[axis]] = projected[axis]
        self._points_3d[self._selected_index] = (coords[0], coords[1], coords[2])
        self._link_start_index = -1
        self._push_history(before, self._snapshot())
        self._last_auto_align_message = (
            f"Aligned vertex {self._selected_index} to vertex {target}"
        )
        self._after_edit()
        self.selectionChanged.emit(self._selected_index)

    def _nearest_alignment_vertex(self, index: int, axis: int) -> int | None:
        projected = self._projected_points()
        if not (0 <= index < len(projected)):
            return None

        candidates = self._connected_vertices(index)
        if not candidates:
            candidates = [candidate for candidate in range(len(projected)) if candidate != index]
        if not candidates:
            return None

        moving = projected[index]
        other_axis = 1 - axis
        return min(
            candidates,
            key=lambda candidate: (
                abs(projected[candidate][axis] - moving[axis]),
                abs(projected[candidate][other_axis] - moving[other_axis]),
            ),
        )

    def _connected_vertices(self, index: int) -> list[int]:
        connected: set[int] = set()
        for group in self._groups:
            pairs = list(zip(group, group[1:]))
            if len(group) > 2:
                pairs.append((group[-1], group[0]))
            for left, right in pairs:
                if left == index:
                    connected.add(right)
                elif right == index:
                    connected.add(left)
        return sorted(connected)

    def _auto_align_projected_vertex(
        self, index: int, first_axis_value: float, second_axis_value: float
    ) -> tuple[float, float]:
        """Magnetically snap a vertex without ever rewriting topology.

        Point targets are strongest: when the cursor enters the screen-space
        magnet radius of another vertex (or the grid origin), Auto Align tries
        to snap both projected axes together.  Outside that radius each axis
        may still snap independently to any stable vertex or aligned edge.

        A connected endpoint is the one safety exception: Auto Align may align
        both axes toward it, but the final zero-length guard releases one axis
        if the result would collapse a live connection.  IDs and links are
        never merged, deleted or remapped by snapping.
        """
        if (
            self._mode != "poo2"
            or not self.auto_align_check.isChecked()
            or not (0 <= index < len(self._points_3d))
        ):
            self._last_auto_align_message = ""
            return first_axis_value, second_axis_value

        projected = self._projected_points()
        transform = self.canvas._make_transform()
        scale = max(abs(transform[1]), 1.0e-9)

        # Keep the magnetic feel stable in screen space, independent of model
        # dimensions or current zoom.  Point snapping is intentionally a touch
        # stronger than line snapping.
        point_snap_threshold = 14.0 / scale
        axis_snap_threshold = 12.0 / scale
        line_snap_threshold = 10.0 / scale

        connected_vertices = set(self._connected_vertices(index))
        point_candidates: list[tuple[float, float, float, str]] = []
        x_candidates: list[tuple[float, float, str]] = []
        z_candidates: list[tuple[float, float, str]] = []

        # The visible grid origin is a real magnetic point, not a fake vertex.
        origin_distance = math.hypot(first_axis_value, second_axis_value)
        if origin_distance <= point_snap_threshold:
            point_candidates.append((origin_distance, 0.0, 0.0, "origin"))
        else:
            if abs(first_axis_value) <= axis_snap_threshold:
                x_candidates.append((abs(first_axis_value), 0.0, "origin"))
            if abs(second_axis_value) <= axis_snap_threshold:
                z_candidates.append((abs(second_axis_value), 0.0, "origin"))

        # Every stable vertex can act as a magnet, not only already-connected
        # endpoints.  Close 2D targets pull on both X and Z together; otherwise
        # either axis can align independently.
        for candidate, (x_value, z_value) in enumerate(projected):
            if candidate == index:
                continue
            x_distance = abs(x_value - first_axis_value)
            z_distance = abs(z_value - second_axis_value)
            point_distance = math.hypot(x_distance, z_distance)
            if point_distance <= point_snap_threshold:
                point_candidates.append(
                    (point_distance, x_value, z_value, f"v{candidate}")
                )
            else:
                if x_distance <= axis_snap_threshold:
                    x_candidates.append((x_distance, x_value, f"v{candidate}"))
                if z_distance <= axis_snap_threshold:
                    z_candidates.append((z_distance, z_value, f"v{candidate}"))

        line_x_candidates, line_z_candidates = _axis_aligned_line_snap_candidates(
            projected,
            self._iter_edges(),
            {index},
            (first_axis_value, second_axis_value),
            line_snap_threshold,
        )
        x_candidates.extend(
            (distance, value, f"line v{edge[0]}-v{edge[1]}")
            for distance, value, _target, edge in line_x_candidates
            if distance <= line_snap_threshold
        )
        z_candidates.extend(
            (distance, value, f"line v{edge[0]}-v{edge[1]}")
            for distance, value, _target, edge in line_z_candidates
            if distance <= line_snap_threshold
        )

        snapped_x = first_axis_value
        snapped_z = second_axis_value
        message_parts: list[str] = []

        if point_candidates:
            _distance, target_x, target_z, label = min(
                point_candidates, key=lambda item: item[0]
            )
            snapped_x = target_x
            snapped_z = target_z
            message_parts.extend((f"X→{label}", f"Z→{label}"))
        else:
            if x_candidates:
                _distance, value, label = min(x_candidates, key=lambda item: item[0])
                snapped_x = value
                message_parts.append(f"X→{label}")
            if z_candidates:
                _distance, value, label = min(z_candidates, key=lambda item: item[0])
                snapped_z = value
                message_parts.append(f"Z→{label}")

        # Auto Align must never collapse a live connection to zero length.
        # Preserve the strongest axis and release the other one when a magnetic
        # two-axis snap lands exactly on a connected endpoint.
        for candidate in connected_vertices:
            target_x, target_z = projected[candidate]
            if abs(snapped_x - target_x) > 1.0e-6 or abs(snapped_z - target_z) > 1.0e-6:
                continue
            x_was_snapped = abs(snapped_x - first_axis_value) > 1.0e-6
            z_was_snapped = abs(snapped_z - second_axis_value) > 1.0e-6
            if x_was_snapped and z_was_snapped:
                # Keep the axis requiring the smaller correction: it feels like
                # the stronger magnetic alignment while keeping the link visible.
                x_delta = abs(target_x - first_axis_value)
                z_delta = abs(target_z - second_axis_value)
                if x_delta <= z_delta:
                    snapped_z = second_axis_value
                    message_parts = [
                        part for part in message_parts if not part.startswith("Z→")
                    ]
                else:
                    snapped_x = first_axis_value
                    message_parts = [
                        part for part in message_parts if not part.startswith("X→")
                    ]
            elif x_was_snapped:
                snapped_x = first_axis_value
                message_parts = [part for part in message_parts if not part.startswith("X→")]
            elif z_was_snapped:
                snapped_z = second_axis_value
                message_parts = [part for part in message_parts if not part.startswith("Z→")]
            break

        self._last_auto_align_message = (
            "Auto Align: " + ", ".join(message_parts) if message_parts else ""
        )
        return snapped_x, snapped_z

    def delete_selected_link(self) -> None:
        if self._mode != "poo2" or not self._selected_edges:
            return
        before = self._snapshot()
        self._link_start_index = -1
        removed_count, removed_vertices = self._delete_edges(set(self._selected_edges))
        self._last_auto_align_message = _deleted_links_message(
            removed_count, removed_vertices
        )
        self._selected_link = None
        self._selected_edges.clear()
        self._push_history(before, self._snapshot())
        self._after_edit()
        self.selectionChanged.emit(-1)

    def _remove_edge_from_groups(self, first: int, second: int) -> None:
        target = _edge_key(first, second)
        new_groups: list[OutlineGroup] = []
        for group in self._groups:
            pairs = list(zip(group, group[1:]))
            if len(group) > 2:
                pairs.append((group[-1], group[0]))

            if not any(_edge_key(left, right) == target for left, right in pairs):
                new_groups.append(list(group))
                continue

            for left, right in pairs:
                if _edge_key(left, right) != target and left != right:
                    new_groups.append([left, right])
        self._groups = new_groups

    def copy_selection(self) -> None:
        if self._mode != "poo2":
            return
        vertex_indices = self._selected_vertices_for_clipboard()
        if not vertex_indices:
            return

        index_map = {old_index: new_index for new_index, old_index in enumerate(vertex_indices)}
        points = tuple(self._points_3d[index] for index in vertex_indices)
        copied_groups: list[tuple[int, ...]] = []
        for first, second in sorted(self._selected_edges):
            if first in index_map and second in index_map:
                copied_groups.append((index_map[first], index_map[second]))

        self._clipboard = ClipboardPayload(points=points, groups=tuple(copied_groups))
        self._last_auto_align_message = (
            f"Copied {len(points)} vertex/vertices"
            + (f" and {len(copied_groups)} link(s)" if copied_groups else "")
        )
        self._update_controls()
        self.undoRedoChanged.emit()

    def cut_selection(self) -> None:
        if self._mode != "poo2":
            return
        if not self._selected_indices and not self._selected_edges:
            return
        self.copy_selection()
        self.delete_selection(confirm=False)

    def paste_clipboard(self) -> None:
        target_x, target_z = self.canvas.view_center_world()
        self.paste_clipboard_at_projected(target_x, target_z)

    def paste_clipboard_at_projected(self, target_x: float, target_z: float) -> None:
        if self._mode != "poo2" or self._clipboard is None or not self._clipboard.points:
            return

        before = self._snapshot()
        points = list(self._clipboard.points)
        projected = [(point[HUD_PROJECTION_AXES[0]], point[HUD_PROJECTION_AXES[1]]) for point in points]
        center_x = sum(point[0] for point in projected) / len(projected)
        center_z = sum(point[1] for point in projected) / len(projected)
        delta_x = target_x - center_x
        delta_z = target_z - center_z

        first_new_index = len(self._points_3d)
        new_indices: list[int] = []
        for point in points:
            coords = list(point)
            coords[HUD_PROJECTION_AXES[0]] = float(coords[HUD_PROJECTION_AXES[0]] + delta_x)
            coords[HUD_PROJECTION_AXES[1]] = float(coords[HUD_PROJECTION_AXES[1]] + delta_z)
            self._points_3d.append((coords[0], coords[1], coords[2]))
            new_indices.append(len(self._points_3d) - 1)

        new_edges: set[tuple[int, int]] = set()
        for group in self._clipboard.groups:
            remapped = [first_new_index + index for index in group if 0 <= index < len(points)]
            if len(remapped) >= 2:
                self._groups.append(remapped)
                if len(remapped) == 2:
                    new_edges.add(_edge_key(remapped[0], remapped[1]))

        self._selected_indices = set(new_indices)
        self._selected_index = new_indices[0] if new_indices else -1
        self._selected_edges = new_edges
        self._selected_link = sorted(new_edges)[0] if new_edges else None
        self._link_start_index = -1
        self._last_auto_align_message = f"Pasted {len(new_indices)} vertex/vertices"
        self._push_history(before, self._snapshot())
        self._after_edit()
        self.canvas.ensure_points_visible(new_indices)
        self.selectionChanged.emit(self._selected_index)

    def _selected_vertices_explicit_only(self) -> set[int]:
        return {index for index in self._selected_indices if 0 <= index < len(self._points_3d)}

    def _selected_vertices_for_clipboard(self) -> list[int]:
        vertices = self._selected_vertices_explicit_only()
        for first, second in self._selected_edges:
            if 0 <= first < len(self._points_3d):
                vertices.add(first)
            if 0 <= second < len(self._points_3d):
                vertices.add(second)
        return sorted(vertices)

    def _add_add_actions_to_menu(self, menu: QMenu) -> tuple[object, object, object, object, object]:
        add_menu = menu.addMenu("Add")
        vertex_action = add_menu.addAction("Vertex Here")
        triangle_action = add_menu.addAction("Triangle")
        square_action = add_menu.addAction("Square")
        pentagon_action = add_menu.addAction("Pentagon")
        circle_action = add_menu.addAction("Circle")
        return vertex_action, triangle_action, square_action, pentagon_action, circle_action

    def _handle_add_menu_action(
        self,
        chosen: object,
        actions: tuple[object, object, object, object, object],
        x_value: float,
        z_value: float,
    ) -> bool:
        vertex_action, triangle_action, square_action, pentagon_action, circle_action = actions
        if chosen == vertex_action:
            self.add_point_at_projected(x_value, z_value)
            return True
        if chosen == triangle_action:
            self.add_shape_at_projected("triangle", x_value, z_value)
            return True
        if chosen == square_action:
            self.add_shape_at_projected("square", x_value, z_value)
            return True
        if chosen == pentagon_action:
            self.add_shape_at_projected("pentagon", x_value, z_value)
            return True
        if chosen == circle_action:
            self.add_shape_at_projected("circle", x_value, z_value)
            return True
        return False

    def _show_selection_context_menu(self, x_value: float, z_value: float, global_pos: QPoint) -> None:
        if self._mode != "poo2":
            return

        menu = QMenu(self)
        undo_action = menu.addAction("< Undo")
        undo_action.setEnabled(self.can_undo)
        redo_action = menu.addAction("Redo >")
        redo_action.setEnabled(self.can_redo)
        menu.addSeparator()
        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(self.has_any_selection)
        cut_action = menu.addAction("Cut")
        cut_action.setEnabled(self.has_any_selection)
        paste_action = menu.addAction("Paste")
        paste_action.setEnabled(self.has_clipboard)
        menu.addSeparator()
        link_action = menu.addAction("Link")
        link_action.setEnabled(len(self._selected_indices) == 1)
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        delete_action.setEnabled(self.has_any_selection)
        add_actions = self._add_add_actions_to_menu(menu)
        menu.addSeparator()
        cancel_action = menu.addAction("Cancel")

        chosen = menu.exec(global_pos)
        if chosen == undo_action:
            self.undo()
        elif chosen == redo_action:
            self.redo()
        elif chosen == copy_action:
            self.copy_selection()
        elif chosen == cut_action:
            self.cut_selection()
        elif chosen == paste_action:
            self.paste_clipboard_at_projected(x_value, z_value)
        elif chosen == link_action:
            self.start_link()
        elif chosen == delete_action:
            self.delete_selection()
        elif self._handle_add_menu_action(chosen, add_actions, x_value, z_value):
            return
        elif chosen == cancel_action:
            self.clear_selection()

    def _show_link_context_menu(self, first: int, second: int, x_value: float, z_value: float, global_pos: QPoint) -> None:
        if self._mode != "poo2" or not self._has_edge(first, second):
            return

        edge = _edge_key(first, second)
        if edge not in self._selected_edges:
            self.select_link(first, second, False)

        menu = QMenu(self)
        undo_action = menu.addAction("< Undo")
        undo_action.setEnabled(self.can_undo)
        redo_action = menu.addAction("Redo >")
        redo_action.setEnabled(self.can_redo)
        menu.addSeparator()
        copy_action = menu.addAction("Copy")
        cut_action = menu.addAction("Cut")
        paste_action = menu.addAction("Paste")
        paste_action.setEnabled(self.has_clipboard)
        menu.addSeparator()
        delete_link_action = menu.addAction("Delete")
        menu.addSeparator()
        cancel_action = menu.addAction("Cancel")

        chosen = menu.exec(global_pos)
        if chosen == undo_action:
            self.undo()
        elif chosen == redo_action:
            self.redo()
        elif chosen == copy_action:
            self.copy_selection()
        elif chosen == cut_action:
            self.cut_selection()
        elif chosen == paste_action:
            self.paste_clipboard_at_projected(x_value, z_value)
        elif chosen == delete_link_action:
            self.delete_selected_link()
        elif chosen == cancel_action:
            self.clear_selection()

    def _show_point_context_menu(self, index: int, x_value: float, z_value: float, global_pos: QPoint) -> None:
        if self._mode != "poo2" or not (0 <= index < len(self._points_3d)):
            return

        self._link_start_index = -1
        if index not in self._selected_indices:
            self.select_point(index, False)

        menu = QMenu(self)
        undo_action = menu.addAction("< Undo")
        undo_action.setEnabled(self.can_undo)
        redo_action = menu.addAction("Redo >")
        redo_action.setEnabled(self.can_redo)
        menu.addSeparator()
        copy_action = menu.addAction("Copy")
        cut_action = menu.addAction("Cut")
        paste_action = menu.addAction("Paste")
        paste_action.setEnabled(self.has_clipboard)
        menu.addSeparator()
        link_action = menu.addAction("Link")
        link_action.setEnabled(len(self._selected_indices) == 1)
        delete_action = menu.addAction("Delete")
        add_actions = self._add_add_actions_to_menu(menu)
        menu.addSeparator()
        align_h_action = menu.addAction("Align Horizontally")
        align_v_action = menu.addAction("Align Vertically")
        align_h_action.setEnabled(len(self._selected_indices) == 1)
        align_v_action.setEnabled(len(self._selected_indices) == 1)
        menu.addSeparator()
        cancel_action = menu.addAction("Cancel")

        chosen = menu.exec(global_pos)
        if chosen == undo_action:
            self.undo()
        elif chosen == redo_action:
            self.redo()
        elif chosen == copy_action:
            self.copy_selection()
        elif chosen == cut_action:
            self.cut_selection()
        elif chosen == paste_action:
            self.paste_clipboard_at_projected(x_value, z_value)
        elif chosen == link_action:
            self.start_link()
        elif chosen == delete_action:
            self.delete_selected_point()
        elif self._handle_add_menu_action(chosen, add_actions, x_value, z_value):
            return
        elif chosen == align_h_action:
            self.align_selected_horizontal()
        elif chosen == align_v_action:
            self.align_selected_vertical()
        elif chosen == cancel_action:
            self.clear_selection()

    def _show_empty_context_menu(self, x_value: float, z_value: float, global_pos: QPoint) -> None:
        if self._mode != "poo2":
            return

        menu = QMenu(self)
        undo_action = menu.addAction("< Undo")
        undo_action.setEnabled(self.can_undo)
        redo_action = menu.addAction("Redo >")
        redo_action.setEnabled(self.can_redo)
        menu.addSeparator()
        paste_action = menu.addAction("Paste")
        paste_action.setEnabled(self.has_clipboard)
        menu.addSeparator()
        add_actions = self._add_add_actions_to_menu(menu)
        reset_action = menu.addAction("Reset")
        reset_action.setEnabled(self.can_reset)
        menu.addSeparator()
        cancel_action = menu.addAction("Cancel")

        chosen = menu.exec(global_pos)
        if chosen == undo_action:
            self.undo()
        elif chosen == redo_action:
            self.redo()
        elif chosen == paste_action:
            self.paste_clipboard_at_projected(x_value, z_value)
        elif self._handle_add_menu_action(chosen, add_actions, x_value, z_value):
            return
        elif chosen == reset_action:
            self.reset_to_loaded()
        elif chosen == cancel_action:
            self.clear_selection()

    def reset_to_loaded(self) -> None:
        if self._loaded_state is None:
            self.canvas.reset_view()
            self.resetApplied.emit()
            return

        before = self._snapshot()
        if before != self._loaded_state:
            self._link_start_index = -1
            self._selected_link = None
            self._selected_edges.clear()
            self._selected_indices.clear()
            self._selected_index = -1
            self._last_auto_align_message = ""
            self._push_history(before, self._loaded_state)
            self._restore_state(self._loaded_state)
            self._after_edit()
        self.canvas.reset_view()
        if self._mode == "poo2" and not self._points_3d:
            self.canvas.set_empty_workspace_view()
        self.resetApplied.emit()

    def mark_clean(self) -> None:
        self._clean_signature = self._current_signature()
        self._update_dirty_from_signature()
        self._update_controls()
        self.undoRedoChanged.emit()

    def mark_dirty(self) -> None:
        # Used for File -> New: the generated model is valid but unsaved.
        self._clean_signature = ()
        self._update_dirty_from_signature()
        self._update_controls()
        self.undoRedoChanged.emit()

    def _move_otl2_point(self, index: int, x_value: float, y_value: float) -> None:
        if not (0 <= index < len(self._points_2d)):
            return
        new_point = (_clamp_byte(round(x_value)), _clamp_byte(round(y_value)))
        if self._points_2d[index] == new_point:
            return
        self._points_2d[index] = new_point
        self._selected_index = index
        self._after_edit()

    def _move_poo2_projected_vertex(
        self, index: int, first_axis_value: float, second_axis_value: float
    ) -> None:
        if not (0 <= index < len(self._points_3d)):
            return

        coords = list(self._points_3d[index])
        coords[HUD_PROJECTION_AXES[0]] = float(first_axis_value)
        coords[HUD_PROJECTION_AXES[1]] = float(second_axis_value)
        new_point = (coords[0], coords[1], coords[2])
        if self._points_3d[index] == new_point:
            return
        self._points_3d[index] = new_point
        self._selected_index = index
        self._after_edit()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)

    def _make_float_spinbox(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1000000.0, 1000000.0)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self._spinbox_changed)
        return spin

    def _spinbox_changed(self) -> None:
        if self._syncing_controls or self._selected_index < 0:
            return

        before = self._snapshot()
        if self._mode == "otl2":
            self._move_otl2_point(
                self._selected_index, self.x_spin.value(), self.y_spin.value()
            )
        elif self._mode == "poo2":
            self._move_poo2_manual_vertex(
                self._selected_index,
                (self.x_spin.value(), self.y_spin.value(), self.z_spin.value()),
            )
        self._push_history(before, self._snapshot())

    def _move_poo2_manual_vertex(self, index: int, new_point: EditablePoint) -> None:
        if not (0 <= index < len(self._points_3d)):
            return
        if self._points_3d[index] == new_point:
            return
        self._points_3d[index] = new_point
        self._selected_index = index
        self._after_edit()

    def _refresh_canvas(self) -> None:
        if self._mode == "otl2":
            self.canvas.set_view(
                [(float(x), float(y)) for x, y in self._points_2d],
                self._groups,
                self._selected_index,
                "OTL2/OLPL vertex mode - 0..255 byte coordinate space",
                True,
                None,
                set(self._selected_indices),
                set(self._selected_edges),
            )
        elif self._mode == "poo2":
            self.canvas.set_view(
                self._projected_points(),
                self._groups,
                self._selected_index,
                "",
                False,
                self._selected_link,
                set(self._selected_indices),
                set(self._selected_edges),
            )
        else:
            self.canvas.set_view(
                [],
                [],
                -1,
                "",
                False,
                None,
                set(),
                set(),
            )

    def _update_controls(self) -> None:
        self._syncing_controls = True
        point_count = self._point_count()
        has_single_vertex_selection = (
            len(self._selected_indices) == 1 and 0 <= self._selected_index < point_count
        )
        has_any_selection = bool(self._selected_indices or self._selected_edges)

        if self._mode == "otl2":
            self.x_spin.setDecimals(0)
            self.y_spin.setDecimals(0)
            self.z_spin.setDecimals(0)
            self.x_spin.setRange(0, 255)
            self.y_spin.setRange(0, 255)
            self.z_spin.setRange(0, 0)
        elif self._mode == "poo2":
            for spin in (self.x_spin, self.y_spin, self.z_spin):
                spin.setDecimals(3)
                spin.setRange(-1000000.0, 1000000.0)

        self.x_spin.setEnabled(has_single_vertex_selection)
        self.y_spin.setEnabled(has_single_vertex_selection)
        self.z_spin.setEnabled(has_single_vertex_selection and self._mode == "poo2")

        if has_single_vertex_selection:
            self.selected_label.setText(str(self._selected_index))
            if self._mode == "otl2":
                x, y = self._points_2d[self._selected_index]
                self.x_spin.setValue(x)
                self.y_spin.setValue(y)
                self.z_spin.setValue(0)
            else:
                x, y, z = self._points_3d[self._selected_index]
                self.x_spin.setValue(x)
                self.y_spin.setValue(y)
                self.z_spin.setValue(z)
        else:
            if self._selected_indices or self._selected_edges:
                parts: list[str] = []
                if self._selected_indices:
                    parts.append(f"{len(self._selected_indices)} vertices")
                if self._selected_edges:
                    parts.append(f"{len(self._selected_edges)} links")
                self.selected_label.setText(" + ".join(parts))
            else:
                self.selected_label.setText("none")
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
            self.z_spin.setValue(0)

        can_edit_structure = self._mode == "poo2"
        self.link_button.setEnabled(can_edit_structure and has_single_vertex_selection)
        self.link_button.setText(
            "Link: pick target vertex" if self._link_start_index >= 0 else "Link"
        )
        if self._link_start_index >= 0:
            self.status_label.setText("Link mode: click another vertex")
        elif self._selected_edges:
            if len(self._selected_edges) == 1 and self._selected_link is not None:
                self.status_label.setText(
                    f"Selected link v{self._selected_link[0]}-v{self._selected_link[1]}"
                )
            else:
                self.status_label.setText(f"Selected {len(self._selected_edges)} links")
        elif self._selected_indices:
            self.status_label.setText(
                self._last_auto_align_message or f"Selected {len(self._selected_indices)} vertices"
            )
        else:
            self.status_label.setText(self._last_auto_align_message)
        self.add_point_button.setEnabled(can_edit_structure)
        self.delete_point_button.setEnabled(can_edit_structure and has_any_selection)
        self.undo_button.setEnabled(self.can_undo)
        self.redo_button.setEnabled(self.can_redo)
        self.reset_button.setEnabled(self.can_reset)
        self._syncing_controls = False

    def _has_edge(self, first: int, second: int) -> bool:
        edge_key = {first, second}
        for group in self._groups:
            pairs = list(zip(group, group[1:]))
            if len(group) > 2:
                pairs.append((group[-1], group[0]))
            for left, right in pairs:
                if {left, right} == edge_key:
                    return True
        return False

    def _projected_points(self) -> list[ProjectedPoint]:
        return [
            (point[HUD_PROJECTION_AXES[0]], point[HUD_PROJECTION_AXES[1]])
            for point in self._points_3d
        ]

    def _point_count(self) -> int:
        if self._mode == "otl2":
            return len(self._points_2d)
        if self._mode == "poo2":
            return len(self._points_3d)
        return 0

    def _push_history(self, before: EditorState, after: EditorState) -> None:
        if before == after:
            return
        self._undo_stack.append(HistoryEdit(before, after))
        self._redo_stack.clear()
        self.undoRedoChanged.emit()

    def _restore_state(self, state: EditorState) -> None:
        self._mode = state.mode
        self._points_2d = [tuple(point) for point in state.points_2d]
        self._points_3d = [tuple(point) for point in state.points_3d]
        self._groups = [list(group) for group in state.groups]
        self._selected_indices = {
            index for index in state.selected_indices if 0 <= index < self._point_count()
        }
        self._selected_index = (
            state.selected_index if state.selected_index in self._selected_indices else -1
        )
        if self._selected_indices and self._selected_index < 0:
            self._selected_index = sorted(self._selected_indices)[0]
        self._selected_edges = {
            edge for edge in (_edge_key(*edge) for edge in state.selected_edges) if self._has_edge(*edge)
        }
        self._selected_link = sorted(self._selected_edges)[0] if self._selected_edges else None

    def _snapshot(self) -> EditorState:
        return EditorState(
            mode=self._mode,
            points_2d=tuple((int(x), int(y)) for x, y in self._points_2d),
            points_3d=tuple((x, y, z) for x, y, z in self._points_3d),
            groups=tuple(tuple(group) for group in self._groups),
            selected_index=self._selected_index,
            selected_indices=tuple(sorted(self._selected_indices)),
            selected_edges=tuple(sorted(self._selected_edges)),
        )

    def _after_edit(self) -> None:
        self._update_dirty_from_signature()
        self._refresh_canvas()
        self._update_controls()
        if self._mode == "poo2":
            self.geometryChanged.emit()
        self.undoRedoChanged.emit()

    def _current_signature(self) -> tuple:
        return (
            self._mode,
            tuple((int(x), int(y)) for x, y in self._points_2d),
            tuple((x, y, z) for x, y, z in self._points_3d),
            tuple(tuple(group) for group in self._groups),
        )

    def _update_dirty_from_signature(self) -> None:
        dirty = self._current_signature() != self._clean_signature
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self.dirtyChanged.emit(dirty)


def _axis_aligned_line_snap_candidates(
    points: list[ProjectedPoint],
    edges: list[tuple[int, int]],
    moved_indices: set[int],
    moving_point: ProjectedPoint,
    threshold: float,
) -> tuple[
    list[tuple[float, float, ProjectedPoint, tuple[int, int]]],
    list[tuple[float, float, ProjectedPoint, tuple[int, int]]],
]:
    """Return X/Z snap targets from stable near-vertical/horizontal edges."""
    x_candidates: list[tuple[float, float, ProjectedPoint, tuple[int, int]]] = []
    z_candidates: list[tuple[float, float, ProjectedPoint, tuple[int, int]]] = []
    moving_x, moving_z = moving_point
    orientation_tolerance = max(0.001, threshold * 0.25)

    for first, second in edges:
        if first in moved_indices or second in moved_indices:
            continue
        if not (0 <= first < len(points) and 0 <= second < len(points)):
            continue
        first_point = points[first]
        second_point = points[second]
        delta_x = abs(second_point[0] - first_point[0])
        delta_z = abs(second_point[1] - first_point[1])
        edge = _edge_key(first, second)

        vertical_tolerance = min(orientation_tolerance, max(delta_z, 0.001) * 0.05)
        if delta_z > 0.001 and delta_x <= vertical_tolerance:
            line_x = (first_point[0] + second_point[0]) * 0.5
            min_z, max_z = sorted((first_point[1], second_point[1]))
            if min_z - threshold <= moving_z <= max_z + threshold:
                target = (line_x, min(max(moving_z, min_z), max_z))
                x_candidates.append((abs(line_x - moving_x), line_x, target, edge))

        horizontal_tolerance = min(orientation_tolerance, max(delta_x, 0.001) * 0.05)
        if delta_x > 0.001 and delta_z <= horizontal_tolerance:
            line_z = (first_point[1] + second_point[1]) * 0.5
            min_x, max_x = sorted((first_point[0], second_point[0]))
            if min_x - threshold <= moving_x <= max_x + threshold:
                target = (min(max(moving_x, min_x), max_x), line_z)
                z_candidates.append((abs(line_z - moving_z), line_z, target, edge))

    return x_candidates, z_candidates


def _deleted_links_message(removed_links: int, removed_vertices: int) -> str:
    message = f"Deleted {removed_links} link(s)"
    if removed_vertices:
        message += f" and {removed_vertices} vertex/vertices"
    return message


def _points_center(points: list[ProjectedPoint]) -> ProjectedPoint:
    if not points:
        return (0.0, 0.0)
    return (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))


def _edge_key(first: int, second: int) -> tuple[int, int]:
    return (first, second) if first <= second else (second, first)


def _segment_intersects_rect(start: QPointF, end: QPointF, rect: QRectF) -> bool:
    if rect.contains(start) or rect.contains(end):
        return True

    top_left = rect.topLeft()
    top_right = rect.topRight()
    bottom_right = rect.bottomRight()
    bottom_left = rect.bottomLeft()
    return (
        _segments_intersect(start, end, top_left, top_right)
        or _segments_intersect(start, end, top_right, bottom_right)
        or _segments_intersect(start, end, bottom_right, bottom_left)
        or _segments_intersect(start, end, bottom_left, top_left)
    )


def _segments_intersect(a: QPointF, b: QPointF, c: QPointF, d: QPointF) -> bool:
    def orientation(p: QPointF, q: QPointF, r: QPointF) -> float:
        return (q.y() - p.y()) * (r.x() - q.x()) - (q.x() - p.x()) * (r.y() - q.y())

    def on_segment(p: QPointF, q: QPointF, r: QPointF) -> bool:
        return (
            min(p.x(), r.x()) - 0.000001 <= q.x() <= max(p.x(), r.x()) + 0.000001
            and min(p.y(), r.y()) - 0.000001 <= q.y() <= max(p.y(), r.y()) + 0.000001
        )

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)

    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if abs(o1) <= 0.000001 and on_segment(a, c, b):
        return True
    if abs(o2) <= 0.000001 and on_segment(a, d, b):
        return True
    if abs(o3) <= 0.000001 and on_segment(c, a, d):
        return True
    if abs(o4) <= 0.000001 and on_segment(c, b, d):
        return True
    return False


def _distance_sq_to_segment(point: QPointF, start: QPointF, end: QPointF) -> float:
    vx = end.x() - start.x()
    vy = end.y() - start.y()
    wx = point.x() - start.x()
    wy = point.y() - start.y()
    length_sq = vx * vx + vy * vy
    if length_sq <= 0.000001:
        dx = point.x() - start.x()
        dy = point.y() - start.y()
        return dx * dx + dy * dy
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    closest_x = start.x() + t * vx
    closest_y = start.y() + t * vy
    dx = point.x() - closest_x
    dy = point.y() - closest_y
    return dx * dx + dy * dy


def _nice_grid_step(raw_step: float) -> float:
    if raw_step <= 0.0 or not math.isfinite(raw_step):
        return 10.0
    exponent = math.floor(math.log10(raw_step))
    base = 10.0 ** exponent
    fraction = raw_step / base
    if fraction <= 1.0:
        nice = 1.0
    elif fraction <= 2.0:
        nice = 2.0
    elif fraction <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * base


def _average_y(points: list[EditablePoint]) -> float:
    if not points:
        return 0.0
    return sum(point[1] for point in points) / len(points)


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def _outline_line_count(groups: list[OutlineGroup]) -> int:
    line_count = 0
    for group in groups:
        if len(group) == 2:
            line_count += 1
        elif len(group) > 2:
            line_count += len(group)
    return line_count
