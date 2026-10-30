"""Data types and helpers of the asset viewport: view materials/faces,
pick shapes, paste/precision state and small geometry helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPolygonF,
)



MATERIAL_COLORS = [
    QColor(96, 170, 255), QColor(255, 170, 80), QColor(120, 220, 120),
    QColor(235, 110, 200), QColor(255, 230, 90), QColor(120, 220, 220),
    QColor(200, 130, 255), QColor(255, 120, 120), QColor(160, 200, 90),
    QColor(140, 150, 255),
]


VIEW_MODES = ("wireframe", "solid", "materials", "textured")


VIEW_PRESET_ANGLES = {
    "Front": (0.0, 0.0),
    "Back": (180.0, 0.0),
    "Left": (90.0, 0.0),
    "Right": (-90.0, 0.0),
    "Top": (0.0, 90.0),
    "Bottom": (0.0, -90.0),
    "Isometric Front Right": (-45.0, 35.264),
    "Isometric Front Left": (45.0, 35.264),
    "Isometric Back Right": (-135.0, 35.264),
    "Isometric Back Left": (135.0, 35.264),
}


VIEW_PRESETS = ("Current View", *VIEW_PRESET_ANGLES)


# One scale for every preview, so a tank, a missile and an explosion keep their
# real proportions when you switch VP. 240 model units fill the view; larger
# objects are reached by zooming out, smaller ones simply stay small.
PREVIEW_REFERENCE_EXTENT = 240.0
PREVIEW_SCALE = 2.0 / PREVIEW_REFERENCE_EXTENT

_PICK_CAMERA_DISTANCE = 4.0


_PICK_NEAR_DISTANCE = 0.2


_PICK_DEPTH_EPSILON = 1e-5


PASTE_GHOST_OPACITY = 0.18


# ----------------------------------------------------------------------------
# Reusable Qt widgets and image helpers for the OpenNeoUAStudio workbench.
# (formerly editor_widgets.py)
# ----------------------------------------------------------------------------

# Canonical red used for selected editable vertices in the Model Editor.
MODEL_EDIT_SELECTION_RED = QColor(255, 32, 48)



@dataclass
class ViewMaterial:
    label: str
    kind: str = ""                  # "ilbm" | "bmpanim" | other
    color: QColor = field(default_factory=lambda: QColor(150, 150, 150))
    image: QImage | None = None     # static texture (ARGB32, chroma applied)
    anim_images: list[QImage] = field(default_factory=list)
    anim_bitmap_names: list[str] = field(default_factory=list)
    anim_uv_groups: list[list[tuple[int, int]]] = field(default_factory=list)
    anim_frames: list[tuple[int, int, int]] = field(default_factory=list)
    # (duration_ticks, image_index, uv_group_index)
    anim_type: int = 0              # 0 loop, 1 ping-pong (from BANI STRC)
    tracy_mode: str = "none"        # none | clear | flat | mapped
    tracy_light: bool = False

    @property
    def additive(self) -> bool:
        # Flat tracy renders additively in the modern GL path (CONFIRMED,
        # gfx.cpp RFLAGS_LUMTRACY with can_destblend: GL_ONE/GL_ONE).
        return self.tracy_mode == "flat"


@dataclass
class ViewFace:
    vertices: list[tuple[float, float, float]]
    uvs: list[tuple[int, int]]      # texture-space bytes (0..255)
    material: int
    shade: int = 0
    poly_id: int = -1
    animated: bool = False
    mapped: bool = True             # False: POL2 polygon with no ATTS entry
    primary: bool = True            # belongs to the first skeleton object
                                    # (pickable / selectable scope)
    owner: str = "root"             # FamilyObject.owner_path for object
                                    # selection / isolation
    block_index: int = -1           # structural ADES material identity
    billboard_center: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class _RenderPayload:
    face: ViewFace
    front_facing: bool
    uv_mapping_valid: bool = True
    source_order: int = 0
    sort_depth: float = 0.0


@dataclass
class PickShape:
    polygon: QPolygonF
    face: ViewFace
    camera_vertices: tuple[tuple[float, float, float], ...]
    draw_order: int
    front_facing: bool = True


@dataclass
class PastePreviewState:
    clipboard: object
    operation: str
    delta_model: tuple[float, float, float]
    raw_delta_model: tuple[float, float, float]
    source_pivot_world: tuple[float, float, float]
    mouse_anchor: QPoint
    anchor_delta_model: tuple[float, float, float]
    anchor_raw_delta_model: tuple[float, float, float]
    last_mouse: QPoint
    material_ids: tuple[int, ...]
    previous_mouse_tracking: bool
    axis: str | None = None
    numeric: str = ""
    camera_inspect: bool = False
    camera_inspect_anchor_screen: QPointF | None = None


@dataclass(frozen=True)
class AlignmentTarget:
    value: float
    label: str


@dataclass(frozen=True)
class PrecisionGuide:
    axis: int
    value: float
    label: str


@dataclass
class VertexPressState:
    vertex: int
    position: QPoint
    selection: set[int]
    was_selected: bool


def _triangle_barycentric(point: QPointF, triangle) -> tuple[float, float, float] | None:
    """Screen-space barycentrics, including points on triangle edges."""

    a, b, c = triangle
    denominator = ((b.y() - c.y()) * (a.x() - c.x())
                   + (c.x() - b.x()) * (a.y() - c.y()))
    if abs(denominator) < 1e-9:
        return None
    w0 = ((b.y() - c.y()) * (point.x() - c.x())
          + (c.x() - b.x()) * (point.y() - c.y())) / denominator
    w1 = ((c.y() - a.y()) * (point.x() - c.x())
          + (a.x() - c.x()) * (point.y() - c.y())) / denominator
    w2 = 1.0 - w0 - w1
    if min(w0, w1, w2) < -1e-7:
        return None
    return w0, w1, w2


def _pick_shape_depth(shape: PickShape, point: QPointF) -> float | None:
    """Perspective-correct camera distance at ``point`` for one rendered face."""

    screen = list(shape.polygon)
    camera = shape.camera_vertices
    if len(screen) != len(camera) or len(screen) < 3:
        return None
    best = None
    # Same fan used by the Retail-indexed rasterizer: (0, j, j - 1).
    for j in range(2, len(screen)):
        indices = (0, j, j - 1)
        weights = _triangle_barycentric(
            point, tuple(screen[index] for index in indices))
        if weights is None:
            continue
        inverse_distance = 0.0
        for weight, index in zip(weights, indices):
            z = camera[index][2]
            distance = max(
                _PICK_NEAR_DISTANCE, _PICK_CAMERA_DISTANCE - z)
            inverse_distance += weight / distance
        if inverse_distance <= 1e-12:
            continue
        local_distance = 1.0 / inverse_distance
        if best is None or local_distance < best:
            best = local_distance
    return best


def resolve_pick_shape(shapes, point: QPointF) -> PickShape | None:
    """Return the geometrically nearest rendered face below ``point``."""

    best_shape = None
    best_depth = None
    for shape in shapes:
        if not shape.polygon.containsPoint(point, Qt.FillRule.OddEvenFill):
            continue
        depth = _pick_shape_depth(shape, point)
        if depth is None:
            continue
        if best_depth is None:
            best_shape, best_depth = shape, depth
            continue
        epsilon = _PICK_DEPTH_EPSILON * max(1.0, best_depth, depth)
        if depth < best_depth - epsilon \
                or (abs(depth - best_depth) <= epsilon
                    and shape.draw_order > best_shape.draw_order):
            best_shape, best_depth = shape, depth
    return best_shape


def resolve_visible_edit_vertices(
        shapes, screen_points, polygons, owner: str,
        selected=(), target: QRectF | None = None) -> set[int]:
    """Return handles that are front-facing and nearest at their screen point.

    The function consumes the BSP-generated pick pieces, so edit visibility
    follows the same camera/depth result as polygon rendering without a second
    geometry pass. Explicitly selected handles remain visible by design.
    """

    points = tuple(screen_points)
    selected_indices = {
        int(index) for index in selected
        if isinstance(index, int) and 0 <= index < len(points)
    }
    visible = set(selected_indices)
    if not shapes or not points:
        return visible

    if target is None:
        xs = [point.x() for point in points]
        ys = [point.y() for point in points]
        target = QRectF(
            min(xs), min(ys),
            max(1.0, max(xs) - min(xs)),
            max(1.0, max(ys) - min(ys)),
        )
    target = QRectF(target).normalized()
    if target.isEmpty():
        return visible
    expanded_target = target.adjusted(-1.0, -1.0, 1.0, 1.0)

    # A small deterministic screen grid keeps the per-frame visibility pass
    # local even for models with many BSP pieces.
    cell_size = 64.0
    columns = max(1, math.ceil(target.width() / cell_size))
    rows = max(1, math.ceil(target.height() / cell_size))
    grid: dict[tuple[int, int], list[PickShape]] = {}

    def cell(value: float, origin: float, count: int) -> int:
        return max(
            0, min(count - 1, math.floor((value - origin) / cell_size)))

    for shape in shapes:
        bounds = shape.polygon.boundingRect().intersected(target)
        if bounds.isEmpty():
            continue
        left = cell(bounds.left(), target.left(), columns)
        right = cell(bounds.right(), target.left(), columns)
        top = cell(bounds.top(), target.top(), rows)
        bottom = cell(bounds.bottom(), target.top(), rows)
        for grid_y in range(top, bottom + 1):
            for grid_x in range(left, right + 1):
                grid.setdefault((grid_x, grid_y), []).append(shape)

    polygon_sets: dict[int, frozenset[int]] = {}
    for index, point in enumerate(points):
        if index in selected_indices \
                or not expanded_target.contains(point):
            continue
        grid_x = cell(point.x(), target.left(), columns)
        grid_y = cell(point.y(), target.top(), rows)
        nearest_depth = None
        incident_front_depth = None
        for shape in grid.get((grid_x, grid_y), ()):
            depth = _pick_shape_depth(shape, point)
            if depth is None:
                continue
            if nearest_depth is None or depth < nearest_depth:
                nearest_depth = depth
            face = shape.face
            if not shape.front_facing or face.owner != owner \
                    or not (0 <= face.poly_id < len(polygons)):
                continue
            winding = polygon_sets.setdefault(
                face.poly_id, frozenset(polygons[face.poly_id]))
            if index not in winding:
                continue
            if incident_front_depth is None or depth < incident_front_depth:
                incident_front_depth = depth
        if nearest_depth is None or incident_front_depth is None:
            continue
        epsilon = _PICK_DEPTH_EPSILON * max(
            1.0, nearest_depth, incident_front_depth)
        if incident_front_depth <= nearest_depth + epsilon:
            visible.add(index)
    return visible


def _rotation_matrix(euler: tuple[int, int, int],
                     scale: tuple[float, float, float]) -> list[list[float]]:
    """Exact port of TForm3D::scale_rot_7 (UA angles are degrees, CONFIRMED)."""

    ax, ay, az = (math.radians(a % 360) for a in euler)
    sx, cx = math.sin(ax), math.cos(ax)
    sy, cy = math.sin(ay), math.cos(ay)
    sz, cz = math.sin(az), math.cos(az)
    kx, ky, kz = scale
    return [
        [(cz * cy - sz * sx * sy) * kx, -sz * cx * kx, (cz * sy + sz * sx * cy) * kx],
        [(sz * cy + cz * sx * sy) * ky, cz * cx * ky, (sz * sy - cz * sx * cy) * ky],
        [-cx * sy * kz, sx * kz, cx * cy * kz],
    ]


def _apply(matrix: list[list[float]], point, pos) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + pos[0],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + pos[1],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + pos[2],
    )


def _image_from_ilbm(img, palette_override=None,
                     alpha_mode: str = "chroma") -> QImage | None:
    """Preview QImage in ARGB32.  ``alpha_mode`` follows IlbmImage.to_rgba_bytes:
    "chroma" = engine ConvAlphaPalette default (yellow RGB(255,255,0) becomes
    transparent), "luma" = source-blend path, "opaque" = no alpha."""

    rgba = (img.to_rgba_bytes(palette_override, alpha_mode)
            if img is not None else None)
    if rgba is None:
        return None
    qimage = QImage(rgba, img.width, img.height, img.width * 4,
                    QImage.Format.Format_RGBA8888)
    return qimage.convertToFormat(QImage.Format.Format_ARGB32)


def _image_from_effect_png(path) -> QImage | None:
    """Load an OpenNeoUA HI/ALPHA effect PNG with engine chroma handling."""

    if path is None:
        return None
    image = QImage(str(path))
    if image.isNull():
        return None
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = image.bits()
    for offset in range(0, image.sizeInBytes(), 4):
        r, g, b, a = pixels[offset:offset + 4]
        if a == 0 or (r == 255 and g == 255 and b == 0):
            pixels[offset:offset + 4] = b"\x00\x00\x00\x00"
    return image.convertToFormat(QImage.Format.Format_ARGB32)
