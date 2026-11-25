"""Interactive multi-loop OLPL UV editor and its atomic topology edits.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Hashable, Iterable

from PySide6.QtCore import QPoint, QPointF, QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPen, QPolygonF

from PySide6.QtWidgets import QApplication

from studio.rendering.gpu import AcceleratedWidget

# ----------------------------------------------------------------------------
# Atomic POL2/POO2/OLPL vertex edits for the model UV editor.
# (formerly uv_topology_editor.py)
# ----------------------------------------------------------------------------

# Same look as the 3D view: selected static handles are red squares (or blue squares
# when UVs are unified), and polygon lines use the cyan selection-line color of the 3D view.
UV_SELECTED_COLOR = QColor(255, 32, 48)
UV_UNIFIED_COLOR = QColor(56, 140, 255)
UV_SELECTED_RIM_COLOR = QColor(255, 214, 40)
UV_SELECTED_LINE_COLOR = QColor(90, 230, 255)
UV_EMITTER_LINE_COLOR = QColor(65, 225, 115)
UV_EMITTER_SELECTED_COLOR = QColor(80, 255, 130)

class UVTopologyError(ValueError):
    pass


@dataclass(frozen=True)
class UVVertexInsertion:
    poly_id: int
    atts_index: int
    edge_start: int
    insert_at: int
    new_point_index: int
    edge_point_indices: tuple[int, int]
    edge_uvs: tuple[tuple[int, int], tuple[int, int]]
    point: tuple[float, float, float]
    uv: tuple[int, int]


@dataclass(frozen=True)
class UVVertexDeletion:
    poly_id: int
    atts_index: int
    vertex_index: int
    point_index: int
    uv: tuple[int, int]


def _editable_uv_group(model, block, poly_id: int, atts_index: int):
    if (block.class_id or "").lower() != "amesh.class":
        raise UVTopologyError("only amesh.class OLPL mappings are editable")
    if not (0 <= poly_id < len(model.polygons)):
        raise UVTopologyError(f"polygon #{poly_id} is out of range")
    if not (0 <= atts_index < len(block.atts)):
        raise UVTopologyError("the ATTS mapping entry is unavailable")
    if block.atts[atts_index].poly_id != poly_id:
        raise UVTopologyError("the selected ATTS entry maps another polygon")
    if not (0 <= atts_index < len(block.olpl)):
        raise UVTopologyError("the matching OLPL group is unavailable")
    polygon = model.polygons[poly_id]
    uvs = block.olpl[atts_index]
    if len(polygon) < 3:
        raise UVTopologyError("the selected POL2 record is not a face")
    if len(uvs) != len(polygon):
        raise UVTopologyError(
            "POL2 winding and OLPL coordinate counts do not match")
    return polygon, uvs


def plan_uv_vertex_insertion(
        model, block, poly_id: int, atts_index: int,
        edge_start: int,
        target_uv: tuple[int, int] | None = None) -> UVVertexInsertion:
    """Plan a vertex inserted after one selected UV/winding vertex.

    Without ``target_uv`` it is the edge midpoint. With ``target_uv`` the new
    UV sits exactly there and the 3D point lies on the edge at the closest
    position, so the face shape does not change.
    """

    polygon, uvs = _editable_uv_group(
        model, block, poly_id, atts_index)
    if not (0 <= edge_start < len(polygon)):
        raise UVTopologyError("select exactly one UV handle first")
    if len(model.points) >= 0x10000:
        raise UVTopologyError("POO2 already uses every unsigned 16-bit index")
    if len(polygon) >= 0x7FFF:
        raise UVTopologyError("the OLPL signed 16-bit vertex limit was reached")

    edge_end = (edge_start + 1) % len(polygon)
    point_indices = (polygon[edge_start], polygon[edge_end])
    if any(index < 0 or index >= len(model.points)
           for index in point_indices):
        raise UVTopologyError("the selected POL2 edge references invalid POO2")
    points = (model.points[point_indices[0]],
              model.points[point_indices[1]])
    if not all(math.isfinite(value) for point in points for value in point):
        raise UVTopologyError("the selected POO2 edge is not finite")
    edge_uvs = (tuple(uvs[edge_start]), tuple(uvs[edge_end]))
    if any(
            len(uv) != 2
            or any(value < 0 or value > 255 for value in uv)
            for uv in edge_uvs):
        raise UVTopologyError("the selected OLPL edge is outside byte range")

    t = 0.5
    uv = tuple(
        round((edge_uvs[0][axis] + edge_uvs[1][axis]) / 2.0)
        for axis in range(2))
    if target_uv is not None:
        uv = (max(0, min(255, int(target_uv[0]))),
              max(0, min(255, int(target_uv[1]))))
        du = edge_uvs[1][0] - edge_uvs[0][0]
        dv = edge_uvs[1][1] - edge_uvs[0][1]
        length_sq = du * du + dv * dv
        if length_sq:
            t = ((uv[0] - edge_uvs[0][0]) * du
                 + (uv[1] - edge_uvs[0][1]) * dv) / length_sq
            t = max(0.0, min(1.0, t))
    if target_uv is None:
        point = tuple(
            (points[0][axis] + points[1][axis]) / 2.0
            for axis in range(3))
    else:
        point = tuple(
            points[0][axis] + (points[1][axis] - points[0][axis]) * t
            for axis in range(3))
    return UVVertexInsertion(
        poly_id=poly_id,
        atts_index=atts_index,
        edge_start=edge_start,
        insert_at=edge_start + 1,
        new_point_index=len(model.points),
        edge_point_indices=point_indices,
        edge_uvs=edge_uvs,
        point=point,
        uv=uv,
    )


def apply_uv_vertex_insertion(model, block,
                              plan: UVVertexInsertion) -> None:
    """Apply a still-current plan; callers provide transaction rollback."""

    if len(model.points) != plan.new_point_index:
        raise UVTopologyError("POO2 changed after the insertion was planned")
    polygon = model.polygons[plan.poly_id]
    uvs = block.olpl[plan.atts_index]
    edge_end = plan.insert_at % len(polygon)
    current_points = (
        polygon[plan.edge_start], polygon[edge_end])
    current_uvs = (
        tuple(uvs[plan.edge_start]), tuple(uvs[edge_end]))
    if current_points != plan.edge_point_indices \
            or current_uvs != plan.edge_uvs:
        raise UVTopologyError(
            "the selected POL2/OLPL edge changed before insertion")
    model.points.append(plan.point)
    polygon.insert(plan.insert_at, plan.new_point_index)
    uvs.insert(plan.insert_at, plan.uv)


def plan_uv_vertex_deletion(
        model, block, poly_id: int, atts_index: int,
        vertex_index: int) -> UVVertexDeletion:
    """Plan removal of one winding/OLPL element without compacting POO2."""

    polygon, uvs = _editable_uv_group(
        model, block, poly_id, atts_index)
    if len(polygon) <= 3:
        raise UVTopologyError(
            "deleting this vertex would leave fewer than three vertices")
    if not (0 <= vertex_index < len(polygon)):
        raise UVTopologyError("select exactly one UV handle first")
    point_index = polygon[vertex_index]
    if not (0 <= point_index < len(model.points)):
        raise UVTopologyError("the selected POL2 vertex references invalid POO2")
    uv = tuple(uvs[vertex_index])
    if len(uv) != 2 or any(value < 0 or value > 255 for value in uv):
        raise UVTopologyError("the selected OLPL vertex is outside byte range")
    return UVVertexDeletion(
        poly_id=poly_id,
        atts_index=atts_index,
        vertex_index=vertex_index,
        point_index=point_index,
        uv=uv,
    )


def apply_uv_vertex_deletion(model, block,
                             plan: UVVertexDeletion) -> None:
    """Apply a still-current plan; callers provide transaction rollback."""

    polygon, uvs = _editable_uv_group(
        model, block, plan.poly_id, plan.atts_index)
    if len(polygon) <= 3:
        raise UVTopologyError(
            "deleting this vertex would leave fewer than three vertices")
    if not (0 <= plan.vertex_index < len(polygon)):
        raise UVTopologyError("the selected vertex changed before deletion")
    if polygon[plan.vertex_index] != plan.point_index \
            or tuple(uvs[plan.vertex_index]) != plan.uv:
        raise UVTopologyError(
            "the selected POL2/OLPL vertex changed before deletion")
    polygon.pop(plan.vertex_index)
    uvs.pop(plan.vertex_index)


def _uv_signed_area(points: list[tuple[int, int]]) -> int:
    return sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )


def _convex_hull_2d(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[int, int]] = []
    for p in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[int, int]] = []
    for p in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _extract_shared_boundary_cycle(
        loop_uvs: dict[Hashable, list[tuple[int, int]]]) -> list[tuple[int, int]] | None:
    """Return the single outer boundary cycle if loops tile cleanly without overlap."""
    directed: dict[tuple[tuple[int, int], tuple[int, int]], int] = {}
    for uvs in loop_uvs.values():
        pts = [(int(u), int(v)) for u, v in uvs]
        area = _uv_signed_area(pts)
        if area == 0:
            continue
        ordered = pts if area > 0 else list(reversed(pts))
        for i in range(len(ordered)):
            a = ordered[i]
            b = ordered[(i + 1) % len(ordered)]
            if a == b:
                continue
            rev = (b, a)
            if directed.get(rev, 0) > 0:
                directed[rev] -= 1
                if directed[rev] == 0:
                    del directed[rev]
            else:
                directed[(a, b)] = directed.get((a, b), 0) + 1

    if not directed or not all(count == 1 for count in directed.values()):
        return None
    out_map: dict[tuple[int, int], list[tuple[int, int]]] = {}
    in_count: dict[tuple[int, int], int] = {}
    for a, b in directed:
        out_map.setdefault(a, []).append(b)
        in_count[b] = in_count.get(b, 0) + 1
    if not (all(len(v) == 1 for v in out_map.values())
            and all(in_count.get(k, 0) == 1 for k in out_map)):
        return None
    start = next(iter(out_map))
    cycle = [start]
    curr = out_map[start][0]
    while curr != start and len(cycle) <= len(directed):
        cycle.append(curr)
        curr = out_map[curr][0]
    if curr == start and len(cycle) == len(directed) and len(cycle) >= 3:
        return cycle
    return None


def uv_loops_are_seamless_patch(
        loop_uvs: dict[Hashable, list[tuple[int, int]]]) -> bool:
    """True when the loops already form one non-overlapping continuous UV island."""
    if len(loop_uvs) < 2:
        return True
    cycle = _extract_shared_boundary_cycle(loop_uvs)
    if cycle is None:
        return False
    outer_area = abs(_uv_signed_area(cycle))
    if outer_area <= 0:
        return False
    total_area = sum(
        abs(_uv_signed_area([(int(u), int(v)) for u, v in uvs]))
        for uvs in loop_uvs.values()
    )
    return abs(total_area - outer_area) <= 0.15 * outer_area


def spread_unified_uvs(
        points_by_key: dict[Hashable, list[tuple[float, float, float]]],
        current_uvs_by_key: dict[Hashable, list[tuple[int, int]]],
        *,
        camera_project,
        to_view=None,
        from_view=None,
) -> dict[Hashable, list[tuple[int, int]]]:
    """Spread overlapping/disconnected polygons into one continuous UV patch.

    Projects all 3D corners from the current 3D camera view into the current UV
    bounding rectangle (in preview-view space, respecting any 90° preview
    rotation), so shared 3D vertices get identical (u, v) coordinates and the
    texture stretches once cleanly across the whole selected surface.
    """
    if not points_by_key:
        raise UVTopologyError("select at least one mapped polygon first")
    if camera_project is None:
        raise UVTopologyError("camera projection is unavailable")
    to_view = to_view or (lambda u, v: (float(u), float(v)))
    from_view = from_view or (lambda ru, rv: (float(ru), float(rv)))

    projected: dict[Hashable, list[tuple[float, float]]] = {}
    for key, pts in points_by_key.items():
        if len(pts) < 3:
            raise UVTopologyError(f"polygon {key!r} has fewer than 3 vertices")
        projected[key] = [tuple(camera_project(p)) for p in pts]

    all_xy = [xy for coords in projected.values() for xy in coords]
    min_x = min(xy[0] for xy in all_xy)
    max_x = max(xy[0] for xy in all_xy)
    min_y = min(xy[1] for xy in all_xy)
    max_y = max(xy[1] for xy in all_xy)
    span_x = max_x - min_x
    span_y = max_y - min_y
    if span_x < 1e-6 and span_y < 1e-6:
        raise UVTopologyError("the selected polygons are degenerate in this view")

    view_uvs = [
        to_view(uv[0], uv[1])
        for uvs in current_uvs_by_key.values()
        for uv in uvs
    ]
    if view_uvs:
        ru0 = min(p[0] for p in view_uvs)
        rv0 = min(p[1] for p in view_uvs)
        ru1 = max(p[0] for p in view_uvs)
        rv1 = max(p[1] for p in view_uvs)
        if (ru1 - ru0) < 8.0 or (rv1 - rv0) < 8.0:
            ru0, rv0, ru1, rv1 = 16.0, 16.0, 240.0, 240.0
    else:
        ru0, rv0, ru1, rv1 = 16.0, 16.0, 240.0, 240.0

    result: dict[Hashable, list[tuple[int, int]]] = {}
    for key, coords in projected.items():
        mapped: list[tuple[int, int]] = []
        for px, py in coords:
            tx = (px - min_x) / span_x if span_x >= 1e-6 else 0.5
            ty = (py - min_y) / span_y if span_y >= 1e-6 else 0.5
            ru = ru0 + tx * (ru1 - ru0)
            rv = rv0 + ty * (rv1 - rv0)
            u, v = from_view(ru, rv)
            mapped.append((max(0, min(255, round(u))), max(0, min(255, round(v)))))
        result[key] = mapped
    return result


def _extract_outer_contour(
        loop_uvs: dict[Hashable, list[tuple[int, int]]]) -> list[tuple[int, int]]:
    """Extract a simplified outer boundary polygon from the current UV loops."""
    all_points: list[tuple[int, int]] = [
        (int(u), int(v)) for uvs in loop_uvs.values() for u, v in uvs
    ]
    contour = _extract_shared_boundary_cycle(loop_uvs)

    if contour is None:
        hull = _convex_hull_2d(all_points)
        if len(hull) >= 3:
            contour = hull
        else:
            us = [p[0] for p in all_points]
            vs = [p[1] for p in all_points]
            u0, u1 = min(us), max(us)
            v0, v1 = min(vs), max(vs)
            if u0 == u1:
                u1 = min(255, u0 + 1)
            if v0 == v1:
                v1 = min(255, v0 + 1)
            contour = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]

    # Simplify collinear vertices along straight outer edges (e.g. two triangles
    # sharing a midpoint on one side collapse into a single 3-vertex triangle).
    changed = True
    while changed and len(contour) > 3:
        changed = False
        for i in range(len(contour)):
            prev_pt = contour[(i - 1) % len(contour)]
            curr_pt = contour[i]
            next_pt = contour[(i + 1) % len(contour)]
            dx = next_pt[0] - prev_pt[0]
            dy = next_pt[1] - prev_pt[1]
            seg_len = math.hypot(dx, dy)
            if seg_len < 1e-6:
                contour.pop(i)
                changed = True
                break
            cross = abs((curr_pt[0] - prev_pt[0]) * dy - (curr_pt[1] - prev_pt[1]) * dx)
            dist = cross / seg_len
            dot1 = (curr_pt[0] - prev_pt[0]) * dx + (curr_pt[1] - prev_pt[1]) * dy
            dot2 = (next_pt[0] - curr_pt[0]) * dx + (next_pt[1] - curr_pt[1]) * dy
            if dist <= 1.5 and dot1 >= 0 and dot2 >= 0:
                contour.pop(i)
                changed = True
                break
    return contour


def _mean_value_weights(
        point: tuple[int, int], contour: list[tuple[int, int]]) -> list[float]:
    """Compute generalized barycentric (Mean Value) weights of point w.r.t. contour."""
    n = len(contour)
    px, py = float(point[0]), float(point[1])
    for i, (vx, vy) in enumerate(contour):
        if math.hypot(px - vx, py - vy) < 1e-6:
            w = [0.0] * n
            w[i] = 1.0
            return w

    for i in range(n):
        ax, ay = contour[i]
        bx, by = contour[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq < 1e-9:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
        if 0.0 <= t <= 1.0:
            proj_x = ax + t * dx
            proj_y = ay + t * dy
            if math.hypot(px - proj_x, py - proj_y) <= 1e-4:
                w = [0.0] * n
                w[i] = 1.0 - t
                w[(i + 1) % n] = t
                return w

    vecs = [(vx - px, vy - py) for vx, vy in contour]
    radii = [math.hypot(vx, vy) for vx, vy in vecs]
    tan_half = [0.0] * n
    for i in range(n):
        j = (i + 1) % n
        sx1, sy1 = vecs[i]
        sx2, sy2 = vecs[j]
        dot = sx1 * sx2 + sy1 * sy2
        cross = sx1 * sy2 - sy1 * sx2
        denom = radii[i] * radii[j] + dot
        if abs(denom) < 1e-9:
            tan_half[i] = 0.0
        else:
            tan_half[i] = cross / denom

    raw = [
        (tan_half[(i - 1) % n] + tan_half[i]) / max(radii[i], 1e-9)
        for i in range(n)
    ]
    total = sum(raw)
    if abs(total) < 1e-9:
        inv = [1.0 / max(r, 1e-9) for r in radii]
        inv_sum = sum(inv)
        return [val / inv_sum for val in inv]
    return [val / total for val in raw]


def build_unified_uv_proxy(
        loop_uvs: dict[Hashable, list[tuple[int, int]]],
) -> tuple[list[tuple[int, int]], dict[Hashable, list[list[float]]]]:
    """Unify multiple UV loops into a single simplified outer contour and weights."""
    if len(loop_uvs) < 2:
        raise UVTopologyError("select at least two mapped polygons to unify")
    for key, uvs in loop_uvs.items():
        if len(uvs) < 3:
            raise UVTopologyError(f"polygon {key!r} has fewer than 3 UV vertices")
    contour = _extract_outer_contour(loop_uvs)
    weights = {
        key: [_mean_value_weights(uv, contour) for uv in uvs]
        for key, uvs in loop_uvs.items()
    }
    return contour, weights


def evaluate_unified_uv_proxy(
        proxy_uvs: list[tuple[int, int]],
        weights_by_key: dict[Hashable, list[list[float]]],
) -> dict[Hashable, list[tuple[int, int]]]:
    """Evaluate underlying polygon UVs from the unified contour handles."""
    result: dict[Hashable, list[tuple[int, int]]] = {}
    for key, vertex_weights in weights_by_key.items():
        result[key] = [
            (
                max(0, min(255, round(sum(wk * proxy_uvs[k][0] for k, wk in enumerate(w))))),
                max(0, min(255, round(sum(wk * proxy_uvs[k][1] for k, wk in enumerate(w))))),
            )
            for w in vertex_weights
        ]
    return result


# ----------------------------------------------------------------------------
# Interactive multi-loop OLPL UV editor over a texture preview.
# (formerly uv_editor_widget.py)
# ----------------------------------------------------------------------------

HANDLE_RADIUS = 6.0


SNAP_DISTANCE_PX = 8.0


@dataclass
class UVLoop:
    """One visible polygon loop and its stable mapping identity."""

    key: Hashable
    poly_id: int
    uvs: list[tuple[int, int]]
    editable: bool = True


class UVEditorWidget(AcceleratedWidget):
    _timeline_visible = None
    _timeline_active = None
    _timeline_is_emitter = False
    _timeline_emitter_anchor_animated = False
    # Legacy single-loop signal/API remains available to existing callers.
    uvChanged = Signal(list)
    loopsChanged = Signal(object)
    editFinished = Signal()
    pointSelected = Signal(int)
    selectionChanged = Signal()
    handleContextMenuRequested = Signal(object, int, object)
    # Right click on the bare texture (no handle): global position + (u, v).
    backgroundContextMenuRequested = Signal(object, object)
    # Grey placement preview: LMB/Enter places it (final UVs), RMB/Esc cancels it.
    placementFinished = Signal(list)
    placementCancelled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(280, 280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._image: QImage | None = None
        self._loops: list[UVLoop] = []
        self._loop_indices: dict[Hashable, int] = {}
        # None renders every UV phase; otherwise only the matching loop is
        # shown and hit-testable. The underlying UV data always stays loaded.
        self._visible_loop_key: Hashable | None = None
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
        self._view_rotation = 0
        self._corner_widget = None
        self._scrollbars = ()
        self._pan_last: QPoint | None = None
        # Right press on the bare texture: pans if dragged, opens the menu if not.
        self._rclick_pos: QPointF | None = None
        self._auto_align_enabled = True
        self._snap_guides: tuple[tuple[str, int, str], ...] = ()
        # Placement preview: the shape (UVs) that follows the pointer, or None.
        self._place_base: list[tuple[int, int]] | None = None
        self._place_uvs: list[tuple[int, int]] = []

    def set_data(self, image: QImage | None, uvs: list[tuple[int, int]],
                 editable: bool, message: str = "") -> None:
        """Compatibility entry point for a single polygon loop."""

        loops = [UVLoop(0, 0, list(uvs), editable)] if uvs else []
        self.set_loops(image, loops, message)

    def set_loops(self, image: QImage | None, loops: list[UVLoop],
                  message: str = "") -> None:
        old_lengths = {loop.key: len(loop.uvs) for loop in self._loops}
        # A refresh with the same loops (playback) keeps the preview; other loops mean another target.
        if self._place_base is not None and set(old_lengths) != {loop.key for loop in loops}:
            self.cancel_placement()
        previous_selection = set(self._selected_handles)
        previous_active = self._active_handle
        self._image = image
        self._timeline_visible = None
        self._timeline_active = None
        self._timeline_is_emitter = False
        self._timeline_emitter_anchor_animated = False
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
        if (self._visible_loop_key is not None
                and self._visible_loop_key not in self._loop_indices):
            self._visible_loop_key = None
        self._sync_primary_uvs()
        self._editable = any(loop.editable and loop.uvs
                             for loop in self._loops)
        self.setToolTip(message)
        self._selected_handles = {
            handle for handle in previous_selection
            if self._valid_handle(handle)
            and old_lengths.get(handle[0]) == len(self._loop(handle[0]).uvs)}
        self._active_handle = (
            previous_active if previous_active in self._selected_handles
            else self._first_handle(self._selected_handles))
        self._sync_legacy_selection()
        self._dragging = False
        self._box_start = None
        self._box_rect = None
        self._press_hit = None
        self._press_pos = None
        self._press_selection.clear()
        self._snap_guides = ()
        self._timeline_is_emitter = False
        self.update()

    # ---- placement preview (Add UV Phase / Copy / Paste UV Position) -------------------------
    @property
    def placement_active(self) -> bool:
        return self._place_base is not None

    def placement_uvs(self) -> list[tuple[int, int]]:
        return list(self._place_uvs)

    def begin_placement(self, uvs) -> None:
        """Show ``uvs`` as a grey transparent preview at its own position; it then follows the pointer."""
        self._place_base = [tuple(uv) for uv in uvs]
        self._place_uvs = list(self._place_base)
        self.setMouseTracking(True)
        self.setFocus()
        self.update()

    def cancel_placement(self) -> bool:
        if self._place_base is None:
            return False
        self._end_placement()
        self.placementCancelled.emit()
        return True

    def finish_placement(self) -> bool:
        if self._place_base is None:
            return False
        uvs = list(self._place_uvs)
        self._end_placement()
        self.placementFinished.emit(uvs)
        return True

    def _end_placement(self) -> None:
        self._place_base = None
        self._place_uvs = []
        self.setMouseTracking(False)
        self.update()

    def _move_placement(self, pos: QPointF) -> None:
        """Center the preview on the pointer; it never leaves the 0..255 texture."""
        base = self._place_base
        if not base:
            return
        us, vs = [uv[0] for uv in base], [uv[1] for uv in base]
        u, v = self._screen_to_uv(pos)
        du = max(-min(us), min(255 - max(us), u - (min(us) + max(us)) // 2))
        dv = max(-min(vs), min(255 - max(vs), v - (min(vs) + max(vs)) // 2))
        placed = [(x + du, y + dv) for x, y in base]
        if placed != self._place_uvs:
            self._place_uvs = placed
            self.update()

    def set_preview_image(self, image: QImage) -> None:
        """Change the playback frame without interrupting a UV gesture."""
        self._image = image
        self.update()

    def set_timeline_overlay(self, visible, active):
        self._timeline_visible = visible
        self._timeline_active = active
        self.update()


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

    def set_visible_loop_key(self, key: Hashable | None) -> None:
        """Show all UV phases, or only one phase, without changing UV data."""

        self._visible_loop_key = (
            key if key is None or key in self._loop_indices else None)
        self.update()

    def _visible_loops(self) -> list[UVLoop]:
        if self._visible_loop_key is None:
            loops = self._loops
        else:
            loop = self._loop(self._visible_loop_key)
            loops = [loop] if loop is not None else []
        if self._timeline_visible is None:
            return loops
        return [loop for loop in loops if loop.key in self._timeline_visible]

    def _shows_uv_handles(self, key: Hashable) -> bool:
        return (
            self._timeline_visible is None
            or key == self._timeline_active
            or key == self._visible_loop_key
            or (self._active_handle is not None and key == self._active_handle[0])
            or (self._press_hit is not None and key == self._press_hit[0])
        )

    def loop_keys(self) -> tuple[Hashable, ...]:
        return tuple(loop.key for loop in self._loops)

    def loop_uvs(self) -> dict[Hashable, list[tuple[int, int]]]:
        return {loop.key: list(loop.uvs) for loop in self._loops}

    def loop_editable(self, key: Hashable) -> bool:
        loop = self._loop(key)
        return bool(loop is not None and loop.editable)

    def loop_color(self, key: Hashable) -> QColor:
        if self._timeline_visible is not None:
            return QColor(255, 220, 0)
        index = self._loop_indices.get(key, 0)
        return QColor(255, 220, 0) if index % 2 == 0 else QColor(0, 220, 255)

    @staticmethod
    def _opposite_loop_color(color: QColor) -> QColor:
        return QColor(0, 220, 255) if color == QColor(255, 220, 0) \
            else QColor(255, 220, 0)

    def uvs(self) -> list[tuple[int, int]]:
        return list(self._uvs)



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

    def replace_loop_uvs(self, updates: dict) -> None:
        """Give whole loops new coordinates, as one undoable edit."""

        before = self.loop_uvs()
        for key, uvs in updates.items():
            loop = self._loop(key)
            if loop is not None and loop.editable and len(uvs) == len(loop.uvs):
                loop.uvs[:] = [tuple(uv) for uv in uvs]
        self.update()
        self._finish_programmatic_edit(before)

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

    def _selected_editable_handles(self):
        return sorted(
            self._editable_selected_handles(), key=self._handle_sort_key)

    def _transform_selected(self, transform) -> bool:
        handles = self._selected_editable_handles()
        if not handles:
            return False
        before = self.loop_uvs()
        points = [self._handle_uv(handle) for handle in handles]
        pivot = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        import math
        moved = {}
        for handle in handles:
            u, v = transform(self._handle_uv(handle), pivot)
            if not (isinstance(u, (int, float)) and isinstance(v, (int, float))):
                return False
            if not math.isfinite(u) or not math.isfinite(v):
                return False
            target = (round(u), round(v))
            if not all(0 <= value <= 255 for value in target):
                return False
            moved[handle] = target
        for key in {handle[0] for handle in handles}:
            loop_values = list(self._loop(key).uvs)
            for handle, target in moved.items():
                if handle[0] == key:
                    loop_values[handle[1]] = target
            if len(set(loop_values)) != len(loop_values):
                return False
        if all(self._handle_uv(handle) == target
               for handle, target in moved.items()):
            return False
        for handle, target in moved.items():
            self._set_handle_uv(handle, target)
        self.update()
        self._finish_programmatic_edit(before)
        return True

    def rotate_selected(self, degrees: float) -> bool:
        """Rotate selected editable handles around their arithmetic centroid."""
        import math
        if not isinstance(degrees, (int, float)) or not math.isfinite(degrees):
            return False
        angle = math.radians(float(degrees))
        cosine, sine = math.cos(angle), math.sin(angle)

        def transform(point, pivot):
            dx, dy = point[0] - pivot[0], point[1] - pivot[1]
            return (pivot[0] + dx * cosine - dy * sine,
                    pivot[1] + dx * sine + dy * cosine)

        return self._transform_selected(transform)

    def scale_selected(self, percent: float) -> bool:
        """Uniformly scale selected handles by a percentage around the centroid."""
        import math
        if not isinstance(percent, (int, float)) or not math.isfinite(percent):
            return False
        factor = float(percent) / 100.0
        if factor <= 0.0 or factor > 1000.0:
            return False
        return self._transform_selected(
            lambda point, pivot: (
                pivot[0] + (point[0] - pivot[0]) * factor,
                pivot[1] + (point[1] - pivot[1]) * factor))

    def flip_selected(self, horizontal: bool = True) -> bool:
        """Reflect selected handles around the centroid on one UV axis."""
        return self._transform_selected(
            lambda point, pivot: (
                2.0 * pivot[0] - point[0] if horizontal else point[0],
                point[1] if horizontal else 2.0 * pivot[1] - point[1]))



    def set_scrollbars(self, horizontal, vertical) -> None:
        self._scrollbars = (horizontal, vertical)
        horizontal.valueChanged.connect(lambda value: self._scroll_uv_axis(0, value))
        vertical.valueChanged.connect(lambda value: self._scroll_uv_axis(1, value))
        self._sync_scrollbars()

    def _scroll_uv_axis(self, axis, value) -> None:
        if axis == 0:
            self._pan.setX(-value)
        else:
            self._pan.setY(-value)
        self.update()

    def _sync_scrollbars(self) -> None:
        if not self._scrollbars:
            return
        _, _, size = self._canvas_rect()
        for bar, extent, pan in zip(
                self._scrollbars, (self.width(), self.height()),
                (self._pan.x(), self._pan.y())):
            value = round(-pan)
            limit = max(0, math.ceil((size - extent) / 2), abs(value))
            with QSignalBlocker(bar):
                bar.setRange(-limit, limit)
                bar.setPageStep(extent)
                bar.setValue(value)

    def set_corner_widget(self, widget) -> None:
        """Pin a small overlay button to the bottom-right corner of the UV preview."""
        self._corner_widget = widget
        if widget is not None:
            widget.setParent(self)
            widget.show()
            widget.raise_()
            self._position_corner_widget()

    def _position_corner_widget(self) -> None:
        if self._corner_widget is None:
            return
        margin = 8
        x = max(0, self.width() - self._corner_widget.width() - margin)
        y = max(0, self.height() - self._corner_widget.height() - margin)
        self._corner_widget.move(x, y)
        self._corner_widget.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_scrollbars()
        self._position_corner_widget()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._sync_scrollbars()
        self.update()

    def view_rotation_degrees(self) -> int:
        return self._view_rotation * 90

    def rotate_view_90(self) -> int:
        """Rotate the preview (texture + UV overlay) 90° clockwise visually only."""
        self._view_rotation = (self._view_rotation + 1) % 4
        self.update()
        return self.view_rotation_degrees()

    def reset_view_rotation(self) -> None:
        self._view_rotation = 0
        self.update()

    def set_timeline_emitter(self, is_emitter: bool,
                             anchor_animated: bool = False) -> None:
        self._timeline_is_emitter = bool(is_emitter)
        self._timeline_emitter_anchor_animated = bool(anchor_animated)
        self.update()

    def current_image(self) -> QImage | None:
        return self._image

    def uv_to_view_space(self, u: float, v: float) -> tuple[float, float]:
        return self._rotate_uv_for_view(u, v)

    def view_space_to_uv(self, ru: float, rv: float) -> tuple[float, float]:
        return self._unrotate_uv_from_view(ru, rv)

    def _rotate_uv_for_view(self, u: float, v: float) -> tuple[float, float]:
        rot = self._view_rotation
        if rot == 1:
            return 256.0 - v, u
        if rot == 2:
            return 256.0 - u, 256.0 - v
        if rot == 3:
            return v, 256.0 - u
        return u, v

    def _unrotate_uv_from_view(self, ru: float, rv: float) -> tuple[float, float]:
        rot = self._view_rotation
        if rot == 1:
            return rv, 256.0 - ru
        if rot == 2:
            return 256.0 - ru, 256.0 - rv
        if rot == 3:
            return 256.0 - rv, ru
        return ru, rv

    def fit_selection(self) -> None:
        """Fit selected handles, or all UV content when nothing is selected."""
        visible = self._visible_loops()
        visible_keys = {loop.key for loop in visible}
        handles = {handle for handle in self._selected_editable_handles()
                   if handle[0] in visible_keys} or {
            (loop.key, index)
            for loop in visible
            for index in range(len(loop.uvs))
        }
        points = [self._handle_uv(handle) for handle in handles]
        if not points:
            self.reset_view()
            return
        rot_points = [self._rotate_uv_for_view(p[0], p[1]) for p in points]
        min_u = min(point[0] for point in rot_points)
        max_u = max(point[0] for point in rot_points)
        min_v = min(point[1] for point in rot_points)
        max_v = max(point[1] for point in rot_points)
        span = max(max_u - min_u, max_v - min_v, 16.0)
        self._zoom = max(1.0, min(6.0, 256.0 * 0.82 / span))
        self._pan = QPointF(0.0, 0.0)
        _ox, _oy, size = self._canvas_rect()
        center = QPointF(
            (min_u + max_u) / 512.0 * size,
            (min_v + max_v) / 512.0 * size)
        self._pan = QPointF(
            self.width() * 0.5 - ((self.width() - size) * 0.5 + center.x()),
            self.height() * 0.5 - ((self.height() - size) * 0.5 + center.y()))
        self._sync_scrollbars()
        self.update()

    def _canvas_rect(self) -> tuple[float, float, float]:
        size = min(self.width(), self.height()) - 8
        size = max(64, size) * self._zoom
        ox = (self.width() - size) / 2 + self._pan.x()
        oy = (self.height() - size) / 2 + self._pan.y()
        return ox, oy, size

    def _uv_to_screen(self, uv: tuple[int, int]) -> QPointF:
        ox, oy, size = self._canvas_rect()
        ru, rv = self._rotate_uv_for_view(uv[0], uv[1])
        return QPointF(ox + ru / 256.0 * size,
                       oy + rv / 256.0 * size)

    def _screen_to_uv(self, point: QPointF) -> tuple[int, int]:
        ox, oy, size = self._canvas_rect()
        ru = (point.x() - ox) / size * 256.0
        rv = (point.y() - oy) / size * 256.0
        u, v = self._unrotate_uv_from_view(ru, rv)
        return max(0, min(255, round(u))), max(0, min(255, round(v)))

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

    def _paint_viewport(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QApplication.instance().palette().color(QPalette.ColorRole.Base))
        ox, oy, size = self._canvas_rect()
        cell = size / 16
        for row in range(16):
            for col in range(16):
                light = (row + col) % 2 == 0
                painter.fillRect(
                    int(ox + col * cell), int(oy + row * cell),
                    int(cell) + 1, int(cell) + 1,
                    QApplication.instance().palette().color(QPalette.ColorRole.Base if light
                                         else QPalette.ColorRole.AlternateBase),
                )
        if self._image is not None and not self._image.isNull():
            if self._view_rotation:
                painter.save()
                cx, cy = ox + size * 0.5, oy + size * 0.5
                painter.translate(cx, cy)
                painter.rotate(self._view_rotation * 90)
                painter.drawImage(QRectF(-size * 0.5, -size * 0.5, size, size), self._image)
                painter.restore()
            else:
                painter.drawImage(QRectF(ox, oy, size, size), self._image)
        if self._loops:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            visible = self._visible_loops()
            if self._timeline_visible is not None:
                selected_keys = {key for key, _ in self._selected_handles}
                visible = sorted(visible, key=lambda loop: (
                    loop.key in selected_keys, loop.key == self._timeline_active))
            for loop in visible:
                color = self.loop_color(loop.key)
                points = [self._uv_to_screen(uv) for uv in loop.uvs]
                playing = loop.key == self._timeline_active
                selected_loop = any((loop.key, index) in self._selected_handles
                                    for index in range(len(points)))
                dimmed = not self._shows_uv_handles(loop.key)
                is_timeline = self._timeline_visible is not None
                is_emitter = is_timeline and self._timeline_is_emitter
                is_unified = (
                    isinstance(loop.key, tuple) and bool(loop.key)
                    and loop.key[0] == "@UNIFIED"
                )
                poly = QPolygonF(points)
                if is_emitter:
                    if selected_loop:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(
                            QColor(110, 255, 160, 110) if playing
                            else QColor(65, 225, 115, 65))
                        painter.drawPolygon(poly)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(QPen(
                            UV_SELECTED_RIM_COLOR, 7.2 if playing else 5.8))
                        painter.drawPolygon(poly)
                        painter.setPen(QPen(
                            QColor(135, 255, 170) if playing else UV_EMITTER_SELECTED_COLOR,
                            3.2 if playing else 2.8))
                        painter.drawPolygon(poly)
                    else:
                        line_color = QColor(UV_EMITTER_LINE_COLOR)
                        line_color.setAlpha(150 if dimmed else (240 if playing else 185))
                        painter.setPen(QPen(line_color, 2.2 if playing else 1.5))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawPolygon(poly)
                elif is_timeline:
                    if selected_loop:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(
                            QColor(120, 245, 255, 105) if playing
                            else QColor(90, 230, 255, 60))
                        painter.drawPolygon(poly)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(QPen(
                            UV_SELECTED_RIM_COLOR, 7.2 if playing else 5.8))
                        painter.drawPolygon(poly)
                        painter.setPen(QPen(
                            QColor(180, 250, 255) if playing else UV_SELECTED_LINE_COLOR,
                            3.0))
                        painter.drawPolygon(poly)
                    else:
                        line_color = QColor(UV_SELECTED_LINE_COLOR)
                        line_color.setAlpha(140 if dimmed else (240 if playing else 180))
                        painter.setPen(QPen(line_color, 2.2 if playing else 1.5))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawPolygon(poly)
                else:
                    if selected_loop:
                        fill = QColor(56, 140, 255, 65) if is_unified else QColor(90, 230, 255, 60)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(fill)
                        painter.drawPolygon(poly)
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(QPen(UV_SELECTED_RIM_COLOR, 6.0))
                        painter.drawPolygon(poly)
                        painter.setPen(QPen(UV_SELECTED_LINE_COLOR, 3.0))
                        painter.drawPolygon(poly)
                    else:
                        fill = QColor(56, 140, 255, 24) if is_unified else QColor(90, 230, 255, 20)
                        painter.setPen(QPen(UV_SELECTED_LINE_COLOR, 1.6))
                        painter.setBrush(fill)
                        painter.drawPolygon(poly)
                # The frame the animation sits on shows only its lines until it is selected.
                if dimmed or (playing and not selected_loop):
                    continue
                for index, point in enumerate(points):
                    handle = (loop.key, index)
                    active = handle == self._active_handle
                    selected = handle in self._selected_handles
                    if is_emitter:
                        ring_radius = 8.5 if (selected or active) else 6.5
                        ring_color = (
                            UV_EMITTER_SELECTED_COLOR if selected
                            else UV_EMITTER_LINE_COLOR
                        )
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.setPen(QPen(ring_color, 2.2 if selected else 1.6))
                        painter.drawEllipse(point, ring_radius, ring_radius)
                        if self._timeline_emitter_anchor_animated:
                            radius = 4.6 if (selected or active) else 3.5
                            fill_color = (
                                UV_SELECTED_COLOR if selected
                                else UV_EMITTER_LINE_COLOR
                            )
                            painter.setBrush(fill_color)
                            painter.setPen(QPen(
                                UV_SELECTED_RIM_COLOR, 2.0 if selected else 1.5))
                            painter.drawEllipse(point, radius, radius)
                        else:
                            sq = 4.4 if (selected or active) else 3.2
                            fill_color = (
                                UV_SELECTED_COLOR if selected
                                else UV_EMITTER_LINE_COLOR
                            )
                            rim_color = (
                                UV_SELECTED_COLOR.lighter(165) if selected
                                else UV_EMITTER_SELECTED_COLOR
                            )
                            painter.setBrush(fill_color)
                            painter.setPen(QPen(rim_color, 1.8 if selected else 1.4))
                            painter.drawRect(QRectF(
                                point.x() - sq, point.y() - sq, sq * 2.0, sq * 2.0))
                    elif is_timeline:
                        radius = 6.0 if (selected or active) else 4.5
                        painter.setBrush(
                            UV_SELECTED_COLOR if selected else UV_SELECTED_LINE_COLOR)
                        painter.setPen(QPen(
                            UV_SELECTED_RIM_COLOR, 2.4 if selected else 1.8))
                        painter.drawEllipse(point, radius, radius)
                    else:
                        base_color = UV_UNIFIED_COLOR if is_unified else UV_SELECTED_COLOR
                        radius = 6.0 if (active or selected) else 4.5
                        rim = QColor(255, 255, 255) if selected else base_color.lighter(165)
                        painter.setBrush(base_color)
                        painter.setPen(QPen(rim, 2.2 if selected else 1.6))
                        painter.drawRect(QRectF(
                            point.x() - radius, point.y() - radius,
                            radius * 2.0, radius * 2.0))
        if self._place_base is not None:
            ghost = QPolygonF([self._uv_to_screen(uv) for uv in self._place_uvs])
            painter.setPen(QPen(QColor(235, 235, 235, 230), 1.4, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(170, 170, 170, 110))
            painter.drawPolygon(ghost)
        if self._snap_guides:
            painter.setPen(QPen(
                QColor(120, 230, 255, 180), 1.0, Qt.PenStyle.DashLine))
            for axis, value, _label in self._snap_guides:
                if axis == "u":
                    p0 = self._uv_to_screen((value, 0))
                    p1 = self._uv_to_screen((value, 256))
                else:
                    p0 = self._uv_to_screen((0, value))
                    p1 = self._uv_to_screen((256, value))
                painter.drawLine(p0, p1)
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
    def _hit_point(self, pos: QPointF):
        hits = []
        visible = self._visible_loops()
        for loop_index, loop in enumerate(visible):
            if not self._shows_uv_handles(loop.key):
                continue
            for point_index, uv in enumerate(loop.uvs):
                point = self._uv_to_screen(uv)
                distance = (point - pos).manhattanLength()
                radius = HANDLE_RADIUS * (2 if self._timeline_visible is None else 1.25)
                if distance <= radius:
                    # Later loops are painted on top and win equal-distance hits.
                    hits.append(
                        (distance, -loop_index, (loop.key, point_index)))
        return min(hits, default=(0, 0, None))[2]

    def _hit_timeline_loop(self, pos: QPointF) -> UVLoop | None:
        edges, interiors = [], []
        for index, loop in enumerate(self._visible_loops()):
            points = [self._uv_to_screen(uv) for uv in loop.uvs]
            if len(points) < 3:
                continue
            polygon = QPolygonF(points)
            for start, end in zip(points, points[1:] + points[:1]):
                segment = end - start
                length_squared = QPointF.dotProduct(segment, segment)
                offset = pos - start
                t = (max(0.0, min(1.0, QPointF.dotProduct(offset, segment) / length_squared))
                     if length_squared else 0.0)
                distance = (offset - segment * t).manhattanLength()
                if distance <= 6:
                    edges.append((distance, -index, loop))
            if polygon.containsPoint(pos, Qt.FillRule.OddEvenFill):
                bounds = polygon.boundingRect()
                interiors.append((bounds.width() * bounds.height(), -index, loop))
        candidates = edges or interiors
        return min(candidates, key=lambda hit: hit[:2])[2] if candidates else None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._place_base is not None \
                and event.button() != Qt.MouseButton.MiddleButton:
            self._move_placement(pos)
            if event.button() == Qt.MouseButton.LeftButton:
                self.finish_placement()
            elif event.button() == Qt.MouseButton.RightButton:
                self.cancel_placement()
            event.accept()
            return
        hit = self._hit_point(pos)
        if event.button() == Qt.MouseButton.LeftButton:
            additive = bool(event.modifiers()
                            & Qt.KeyboardModifier.ControlModifier)
            whole_loop = None
            if not additive:
                if hit is None:
                    whole_loop = self._hit_timeline_loop(pos)
                    if whole_loop is not None:
                        hit = (whole_loop.key, 0)
                elif (self._timeline_visible is not None
                      and (self._active_handle is None
                           or self._active_handle[0] != hit[0])):
                    whole_loop = self._loop(hit[0])
            self._press_hit = hit
            self._press_pos = QPointF(pos)
            self._press_additive = additive
            self._press_selection = set(self._selected_handles)
            if hit is not None:
                if whole_loop is not None:
                    self._selected_handles = {
                        (whole_loop.key, index) for index in range(len(whole_loop.uvs))
                    }
                elif self._timeline_visible is not None and not additive:
                    self._selected_handles = {hit}
                elif additive:
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
            if event.button() == Qt.MouseButton.RightButton:
                self._rclick_pos = QPointF(pos)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._place_base is not None:
            self._move_placement(pos)
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
                self._drag_anchor_uv = self._screen_to_uv(self._press_pos)
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
            if self._rclick_pos is not None \
                    and (pos - self._rclick_pos).manhattanLength() >= 5.0:
                self._rclick_pos = None  # it is a pan, not a click
            delta = pos.toPoint() - self._pan_last
            self._pan_last = pos.toPoint()
            self._pan += QPointF(delta.x(), delta.y())
            self._sync_scrollbars()
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
                    for loop in self._visible_loops()
                    if self._shows_uv_handles(loop.key)
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
        if event.button() == Qt.MouseButton.RightButton \
                and self._rclick_pos is not None:
            click = self._rclick_pos
            self._rclick_pos = None
            self.backgroundContextMenuRequested.emit(
                self.mapToGlobal(click.toPoint()),
                self._screen_to_uv(click))
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._place_base is not None:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.finish_placement()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Escape:
                self.cancel_placement()
                event.accept()
                return
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
        cursor = event.position()
        world_under_cursor = self._screen_to_uv(cursor)
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_zoom = max(1.0, min(6.0, self._zoom * factor))
        if new_zoom == self._zoom:
            event.accept()
            return
        self._zoom = new_zoom
        after = self._uv_to_screen(world_under_cursor)
        self._pan += cursor - after
        if self._zoom == 1.0:
            self._pan = QPointF(0.0, 0.0)
        self._sync_scrollbars()
        self.update()
        event.accept()
