"""Interactive multi-loop OLPL UV editor over a texture preview.

Coordinates remain native amesh bytes (0..255).  The widget only changes
in-memory data through signals and never writes asset files itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

HANDLE_RADIUS = 6.0
SNAP_DISTANCE_PX = 8.0


@dataclass
class UVLoop:
    """One visible polygon loop and its stable mapping identity."""

    key: Hashable
    poly_id: int
    uvs: list[tuple[int, int]]
    editable: bool = True


class UVEditorWidget(QWidget):
    # Legacy single-loop signal/API remains available to existing callers.
    uvChanged = Signal(list)
    loopsChanged = Signal(object)
    editFinished = Signal()
    pointSelected = Signal(int)
    selectionChanged = Signal()
    handleContextMenuRequested = Signal(object, int, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(280, 280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._image: QImage | None = None
        self._loops: list[UVLoop] = []
        self._loop_indices: dict[Hashable, int] = {}
        # Compatibility mirrors for the primary loop.
        self._uvs: list[tuple[int, int]] = []
        self._editable = False
        self._active_point = -1
        self._selected_points: set[int] = set()
        self._active_handle: tuple[Hashable, int] | None = None
        self._selected_handles: set[tuple[Hashable, int]] = set()
        self._dragging = False
        self._drag_axis: str | None = None
        self._drag_start_uvs: dict[
            tuple[Hashable, int], tuple[int, int]] = {}
        self._drag_anchor_uv: tuple[int, int] | None = None
        self._drag_changed = False
        self._box_start: QPointF | None = None
        self._box_rect: QRectF | None = None
        self._press_hit: tuple[Hashable, int] | None = None
        self._press_pos: QPointF | None = None
        self._press_additive = False
        self._press_selection: set[tuple[Hashable, int]] = set()
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._pan_last: QPoint | None = None
        self._auto_align_enabled = True
        self._snap_guides: tuple[tuple[str, int, str], ...] = ()

    def set_data(self, image: QImage | None, uvs: list[tuple[int, int]],
                 editable: bool, message: str = "") -> None:
        """Compatibility entry point for a single polygon loop."""

        loops = [UVLoop(0, 0, list(uvs), editable)] if uvs else []
        self.set_loops(image, loops, message)

    def set_loops(self, image: QImage | None, loops: list[UVLoop],
                  message: str = "") -> None:
        self._image = image
        self._loops = []
        self._loop_indices = {}
        for loop in loops:
            if loop.key in self._loop_indices:
                raise ValueError("UV loop keys must be unique")
            self._loop_indices[loop.key] = len(self._loops)
            self._loops.append(UVLoop(
                loop.key,
                int(loop.poly_id),
                [tuple(uv) for uv in loop.uvs],
                bool(loop.editable),
            ))
        self._sync_primary_uvs()
        self._editable = any(loop.editable and loop.uvs
                             for loop in self._loops)
        self.setToolTip(message)
        self._active_handle = None
        self._selected_handles.clear()
        self._sync_legacy_selection()
        self._dragging = False
        self._box_start = None
        self._box_rect = None
        self._press_hit = None
        self._press_pos = None
        self._press_selection.clear()
        self._snap_guides = ()
        self.update()

    @property
    def auto_align_enabled(self) -> bool:
        return self._auto_align_enabled

    @property
    def snap_guides(self) -> tuple[tuple[str, int, str], ...]:
        return self._snap_guides

    def set_auto_align_enabled(self, enabled: bool) -> None:
        self._auto_align_enabled = bool(enabled)
        if not enabled:
            self._snap_guides = ()
        self.update()

    def _sync_primary_uvs(self) -> None:
        self._uvs = self._loops[0].uvs if self._loops else []

    def _loop(self, key: Hashable) -> UVLoop | None:
        index = self._loop_indices.get(key)
        return self._loops[index] if index is not None else None

    def loop_count(self) -> int:
        return len(self._loops)

    def loop_keys(self) -> tuple[Hashable, ...]:
        return tuple(loop.key for loop in self._loops)

    def loop_uvs(self) -> dict[Hashable, list[tuple[int, int]]]:
        return {loop.key: list(loop.uvs) for loop in self._loops}

    def loop_editable(self, key: Hashable) -> bool:
        loop = self._loop(key)
        return bool(loop is not None and loop.editable)

    def loop_color(self, key: Hashable) -> QColor:
        index = self._loop_indices.get(key, 0)
        # Golden-angle spacing remains distinct and deterministic.
        return QColor.fromHsv((48 + index * 137) % 360, 190, 255)

    def uvs(self) -> list[tuple[int, int]]:
        return list(self._uvs)

    def active_point(self) -> int:
        return self._active_point

    def active_handle(self) -> tuple[Hashable, int] | None:
        return self._active_handle

    def selected_points(self) -> set[int]:
        """Return selected indices in the active loop (legacy API)."""

        return set(self._selected_points)

    def selected_handles(self) -> set[tuple[Hashable, int]]:
        return set(self._selected_handles)

    def _handle_sort_key(self, handle: tuple[Hashable, int]) -> tuple[int, int]:
        return self._loop_indices.get(handle[0], len(self._loops)), handle[1]

    def _first_handle(self, handles: Iterable[tuple[Hashable, int]]):
        return min(handles, key=self._handle_sort_key, default=None)

    def _valid_handle(self, handle: tuple[Hashable, int]) -> bool:
        loop = self._loop(handle[0])
        return loop is not None and 0 <= handle[1] < len(loop.uvs)

    def _sync_legacy_selection(self) -> None:
        active_key = (
            self._active_handle[0] if self._active_handle is not None
            else self._loops[0].key if self._loops else None)
        self._selected_points = {
            index for key, index in self._selected_handles
            if key == active_key
        }
        self._active_point = (
            self._active_handle[1]
            if self._active_handle is not None else -1)

    def _emit_selection_changed(self) -> None:
        self._sync_legacy_selection()
        self.pointSelected.emit(self._active_point)
        self.selectionChanged.emit()

    def select_all(self) -> None:
        self._selected_handles = {
            (loop.key, index)
            for loop in self._loops
            for index in range(len(loop.uvs))
        }
        self._active_handle = self._first_handle(self._selected_handles)
        self._emit_selection_changed()
        self.update()

    def select_none(self) -> None:
        self._selected_handles.clear()
        self._active_handle = None
        self._emit_selection_changed()
        self.update()

    def select_point(self, index: int) -> None:
        if not self._loops:
            self.select_none()
            return
        self.select_handle(self._loops[0].key, index)

    def select_handle(self, key: Hashable, index: int,
                      additive: bool = False) -> None:
        handle = (key, index)
        if not self._valid_handle(handle):
            if not additive:
                self._selected_handles.clear()
                self._active_handle = None
        elif additive:
            if handle in self._selected_handles:
                self._selected_handles.remove(handle)
            else:
                self._selected_handles.add(handle)
            self._active_handle = (
                handle if handle in self._selected_handles
                else self._first_handle(self._selected_handles))
        else:
            self._selected_handles = {handle}
            self._active_handle = handle
        self._emit_selection_changed()
        self.update()

    def select_handles(
            self, handles: Iterable[tuple[Hashable, int]],
            active: tuple[Hashable, int] | None = None) -> None:
        self._selected_handles = {
            handle for handle in handles if self._valid_handle(handle)
        }
        self._active_handle = (
            active if active in self._selected_handles
            else self._first_handle(self._selected_handles))
        self._emit_selection_changed()
        self.update()

    def _emit_uv_changed(self) -> None:
        self.loopsChanged.emit(self.loop_uvs())
        if self._loops:
            self.uvChanged.emit(self.uvs())

    def _finish_programmatic_edit(self, before: dict) -> None:
        if before == self.loop_uvs():
            return
        self._emit_uv_changed()
        self.editFinished.emit()

    def _editable_selected_handles(self):
        return {
            handle for handle in self._selected_handles
            if self.loop_editable(handle[0]) and self._valid_handle(handle)
        }

    def _handle_uv(self, handle: tuple[Hashable, int]) -> tuple[int, int]:
        loop = self._loop(handle[0])
        if loop is None:
            raise IndexError(handle)
        return loop.uvs[handle[1]]

    def _set_handle_uv(self, handle: tuple[Hashable, int],
                       uv: tuple[int, int]) -> None:
        loop = self._loop(handle[0])
        if loop is None:
            raise IndexError(handle)
        loop.uvs[handle[1]] = (
            max(0, min(255, int(uv[0]))),
            max(0, min(255, int(uv[1]))),
        )

    @staticmethod
    def _shared_delta(
            starts: Iterable[tuple[int, int]], du: int,
            dv: int) -> tuple[int, int]:
        """Keep a common delta whenever the byte range can represent it."""

        points = list(starts)
        if not points:
            return 0, 0
        bounded_u = max(-min(u for u, _v in points),
                        min(int(du), 255 - max(u for u, _v in points)))
        bounded_v = max(-min(v for _u, v in points),
                        min(int(dv), 255 - max(v for _u, v in points)))
        return bounded_u, bounded_v

    def nudge_selected(self, du: int, dv: int) -> None:
        handles = self._editable_selected_handles()
        if not handles:
            return
        before = self.loop_uvs()
        du, dv = self._shared_delta(
            (self._handle_uv(handle) for handle in handles), du, dv)
        for handle in handles:
            u, v = self._handle_uv(handle)
            self._set_handle_uv(handle, (u + du, v + dv))
        self.update()
        self._finish_programmatic_edit(before)

    def set_point(self, index: int, u: int, v: int,
                  notify: bool = True) -> None:
        if not self._loops:
            return
        self.set_handle(self._loops[0].key, index, u, v, notify)

    def set_handle(self, key: Hashable, index: int, u: int, v: int,
                   notify: bool = True) -> None:
        handle = (key, index)
        if not self._valid_handle(handle):
            return
        self._set_handle_uv(
            handle,
            (max(0, min(255, int(u))), max(0, min(255, int(v)))))
        self.update()
        if notify:
            self._emit_uv_changed()

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

    @staticmethod
    def _signed_area(points: list[tuple[int, int]]) -> int:
        return sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points)))

    @classmethod
    def _near_quad_target(
            cls, loop: UVLoop, handle_index: int,
            proposed: tuple[int, int],
            threshold_uv: float) -> tuple[tuple[int, int], str] | None:
        """Complete a near rectangle/square from three ordered vertices."""

        if len(loop.uvs) != 4 or not (0 <= handle_index < 4):
            return None
        previous = loop.uvs[(handle_index - 1) % 4]
        opposite = loop.uvs[(handle_index + 2) % 4]
        following = loop.uvs[(handle_index + 1) % 4]
        first = (previous[0] - opposite[0], previous[1] - opposite[1])
        second = (following[0] - opposite[0], following[1] - opposite[1])
        first_len = (first[0] ** 2 + first[1] ** 2) ** 0.5
        second_len = (second[0] ** 2 + second[1] ** 2) ** 0.5
        if first_len < 2.0 or second_len < 2.0:
            return None
        cosine = abs(
            first[0] * second[0] + first[1] * second[1]
        ) / (first_len * second_len)
        ratio = max(first_len, second_len) / min(first_len, second_len)
        if cosine > 0.15:
            return None
        target = (
            previous[0] + following[0] - opposite[0],
            previous[1] + following[1] - opposite[1],
        )
        if not (0 <= target[0] <= 255 and 0 <= target[1] <= 255):
            return None
        if ((target[0] - proposed[0]) ** 2
                + (target[1] - proposed[1]) ** 2) ** 0.5 > threshold_uv:
            return None
        candidate = list(loop.uvs)
        candidate[handle_index] = target
        if cls._signed_area(candidate) * cls._signed_area(loop.uvs) <= 0:
            return None
        if len(set(candidate)) != 4:
            return None
        return target, "square" if ratio <= 1.15 else "rectangle"

    @classmethod
    def _near_square_target(
            cls, loop: UVLoop, handle_index: int,
            proposed: tuple[int, int],
            threshold_uv: float) -> tuple[int, int] | None:
        candidate = cls._near_quad_target(
            loop, handle_index, proposed, threshold_uv)
        if candidate is None or candidate[1] != "square":
            return None
        return candidate[0]

    def _snap_drag_delta(
            self, handles: set[tuple[Hashable, int]],
            du: int, dv: int, *, allow_u: bool = True,
            allow_v: bool = True) -> tuple[int, int]:
        """Return one common screen-distance-based delta for all handles."""

        self._snap_guides = ()
        if not self._auto_align_enabled or not handles:
            return du, dv
        _ox, _oy, size = self._canvas_rect()
        threshold_uv = SNAP_DISTANCE_PX * 256.0 / max(size, 1.0)
        guides: list[tuple[str, int, str]] = []

        if len(handles) == 1 and allow_u and allow_v:
            handle = next(iter(handles))
            loop = self._loop(handle[0])
            if loop is not None and len(loop.uvs) == 4:
                start = self._drag_start_uvs[handle]
                quad_target = self._near_quad_target(
                    loop, handle[1],
                    (start[0] + du, start[1] + dv), threshold_uv)
                if quad_target is not None:
                    target, shape = quad_target
                    self._snap_guides = (
                        ("u", target[0], shape),
                        ("v", target[1], shape),
                    )
                    return target[0] - start[0], target[1] - start[1]

        starts = list(self._drag_start_uvs.values())
        moved_u = [u + du for u, _v in starts]
        moved_v = [v + dv for _u, v in starts]
        other_uvs = [
            uv
            for loop in self._loops
            for index, uv in enumerate(loop.uvs)
            if (loop.key, index) not in handles
        ]

        def correction(axis: str, moved: list[int]) -> tuple[int, int, str] | None:
            other = [uv[0 if axis == "u" else 1] for uv in other_uvs]
            targets = [(0, "boundary"), (255, "boundary")]
            targets.extend((value, "coordinate") for value in other)
            if other:
                targets.extend((
                    (round((min(other) + max(other)) / 2), "center"),
                    (min(other), "minimum"),
                    (max(other), "maximum"),
                ))
                ordered = sorted(set(other))
                spacing_targets = set()
                for left_index, left in enumerate(ordered):
                    for right in ordered[left_index + 1:]:
                        spacing_targets.add(round((left + right) / 2))
                        spacing_targets.add(2 * right - left)
                        spacing_targets.add(2 * left - right)
                targets.extend(
                    (value, "equal spacing")
                    for value in sorted(spacing_targets)
                    if 0 <= value <= 255)
            features = (
                (min(moved), "minimum"),
                (round((min(moved) + max(moved)) / 2), "center"),
                (max(moved), "maximum"),
            )
            best = None
            for source, source_label in features:
                for target, target_label in targets:
                    distance = abs(target - source)
                    if distance > threshold_uv:
                        continue
                    candidate = (
                        distance, abs(target), target - source, target,
                        f"{source_label} to {target_label}")
                    if best is None or candidate < best:
                        best = candidate
            if best is None:
                return None
            return best[2], best[3], best[4]

        if allow_u:
            snap = correction("u", moved_u)
            if snap is not None:
                du += snap[0]
                guides.append(("u", snap[1], snap[2]))
        if allow_v:
            snap = correction("v", moved_v)
            if snap is not None:
                dv += snap[0]
                guides.append(("v", snap[1], snap[2]))
        self._snap_guides = tuple(guides)
        return du, dv

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
        if self._loops:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            multi_loop = len(self._loops) > 1
            for loop in self._loops:
                color = self.loop_color(loop.key)
                points = [self._uv_to_screen(uv) for uv in loop.uvs]
                painter.setPen(QPen(color, 1.8))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolygon(QPolygonF(points))
                for index, point in enumerate(points):
                    handle = (loop.key, index)
                    active = handle == self._active_handle
                    selected = handle in self._selected_handles
                    painter.setBrush(
                        QColor(90, 230, 255) if active else
                        QColor(255, 145, 55) if selected else color)
                    painter.setPen(QPen(
                        QColor(255, 255, 255) if selected
                        else QColor(20, 20, 20), 1.4))
                    painter.drawEllipse(point, HANDLE_RADIUS, HANDLE_RADIUS)
                    painter.setPen(QColor(255, 255, 255))
                    prefix = f"P{loop.poly_id}:" if multi_loop else ""
                    painter.drawText(
                        point + QPointF(8, -6),
                        f"{prefix}{index} ({loop.uvs[index][0]},"
                        f"{loop.uvs[index][1]})")
        if self._snap_guides:
            painter.setPen(QPen(
                QColor(120, 230, 255, 180), 1.0, Qt.PenStyle.DashLine))
            for axis, value, _label in self._snap_guides:
                if axis == "u":
                    point = self._uv_to_screen((value, 0))
                    painter.drawLine(
                        QPointF(point.x(), oy),
                        QPointF(point.x(), oy + size))
                else:
                    point = self._uv_to_screen((0, value))
                    painter.drawLine(
                        QPointF(ox, point.y()),
                        QPointF(ox + size, point.y()))
        if self._box_rect is not None:
            painter.setPen(QPen(QColor(90, 230, 255), 1.0,
                                Qt.PenStyle.DashLine))
            painter.setBrush(QColor(90, 230, 255, 40))
            painter.drawRect(self._box_rect)
        if not self._editable and self._loops:
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(self.rect().adjusted(6, 4, -6, -4),
                             Qt.AlignmentFlag.AlignTop
                             | Qt.AlignmentFlag.AlignRight, "[READ-ONLY]")
        painter.end()

    def _hit_point(self, pos: QPointF):
        hits = []
        for loop_index, loop in enumerate(self._loops):
            for point_index, uv in enumerate(loop.uvs):
                point = self._uv_to_screen(uv)
                distance = (point - pos).manhattanLength()
                if distance <= HANDLE_RADIUS * 2:
                    # Later loops are painted on top and win equal-distance hits.
                    hits.append(
                        (distance, -loop_index, (loop.key, point_index)))
        return min(hits, default=(0, 0, None))[2]

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        hit = self._hit_point(pos)
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
            self._press_hit = hit
            self._press_pos = QPointF(pos)
            self._press_additive = additive
            self._press_selection = set(self._selected_handles)
            if hit is not None:
                if additive:
                    if hit in self._selected_handles:
                        self._selected_handles.remove(hit)
                    else:
                        self._selected_handles.add(hit)
                elif hit not in self._selected_handles:
                    self._selected_handles = {hit}
                self._active_handle = (
                    hit if hit in self._selected_handles
                    else self._first_handle(self._selected_handles))
                self._emit_selection_changed()
            else:
                self._box_start = pos
                self._box_rect = None
            self.update()
        elif event.button() == Qt.MouseButton.RightButton \
                and hit is not None:
            self.handleContextMenuRequested.emit(
                hit[0], hit[1], self.mapToGlobal(pos.toPoint()))
        elif event.button() in (Qt.MouseButton.MiddleButton,
                                Qt.MouseButton.RightButton):
            self._pan_last = pos.toPoint()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if event.buttons() & Qt.MouseButton.LeftButton \
                and self._press_hit is not None and not self._dragging \
                and self.loop_editable(self._press_hit[0]) \
                and self._press_hit in self._selected_handles:
            if self._press_pos is not None \
                    and (pos - self._press_pos).manhattanLength() >= 5.0:
                self._dragging = True
                self._drag_axis = None
                handles = self._editable_selected_handles()
                self._drag_start_uvs = {
                    handle: self._handle_uv(handle) for handle in handles
                }
                self._drag_anchor_uv = self._handle_uv(self._press_hit)
                self._drag_changed = False
        if self._dragging and self._drag_anchor_uv is not None:
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
            bypass_snap = bool(
                event.modifiers() & Qt.KeyboardModifier.AltModifier)
            if bypass_snap:
                self._snap_guides = ()
            else:
                du, dv = self._snap_drag_delta(
                    set(self._drag_start_uvs),
                    du, dv,
                    allow_u=self._drag_axis != "v",
                    allow_v=self._drag_axis != "u")
            du, dv = self._shared_delta(
                self._drag_start_uvs.values(), du, dv)
            changed_now = False
            for handle, (start_u, start_v) in self._drag_start_uvs.items():
                target = (start_u + du, start_v + dv)
                if self._handle_uv(handle) != target:
                    self._set_handle_uv(handle, target)
                    changed_now = True
            self._drag_changed = self._drag_changed or changed_now
            if changed_now:
                self._emit_uv_changed()
                self.update()
        elif self._box_start is not None:
            if (pos - self._box_start).manhattanLength() >= 5.0:
                self._box_rect = QRectF(self._box_start, pos).normalized()
                self.update()
        elif self._pan_last is not None:
            delta = pos.toPoint() - self._pan_last
            self._pan_last = pos.toPoint()
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
            self._snap_guides = ()
        if self._box_start is not None:
            if self._box_rect is None:
                if not self._press_additive:
                    self._selected_handles.clear()
                    self._active_handle = None
            else:
                hits = {
                    (loop.key, index)
                    for loop in self._loops
                    for index, uv in enumerate(loop.uvs)
                    if self._box_rect.contains(self._uv_to_screen(uv))
                }
                self._selected_handles = (
                    self._press_selection | hits
                    if self._press_additive else hits)
                self._active_handle = (
                    self._first_handle(hits)
                    or self._first_handle(self._selected_handles))
            self._emit_selection_changed()
            self._box_start = None
            self._box_rect = None
            self.update()
        self._press_hit = None
        self._press_pos = None
        self._press_selection.clear()
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
