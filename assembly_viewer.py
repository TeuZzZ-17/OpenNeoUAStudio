"""Software-rendered 3D preview widget for assembled Urban Assault assets.

Renders the polygons of an :class:`asset_family.AssetFamily` with QPainter and
the shared Retail geometry pipeline.  All view modes use the same source-face
visibility test, runtime fan triangulation, near-plane clipping and exact
camera-space BSP ordering.  Textured mode then adds the Retail-indexed
SHADERMP/TRACYRMP framebuffer pass.

View modes:
  - wireframe        SKLT-style outline using Retail-visible polygons
  - solid            flat shading from the ATTS shade value (CONFIRMED
                     formula: brightness = 1 - shade/256, amesh.cpp)
  - materials        BASE/material blocks with distinct colors
  - textured         Retail-indexed rendering using OLPL UVs, SHADERMP/TRACYRMP

Extras: SEN2 bounding/culling volume overlay (read-only), axes, grid,
VANM texture animation playback (play/pause, loop or ping-pong).

UA model space uses negative Y as "up"; the widget flips Y for display only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bisect
import math

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget
from PySide6.QtWidgets import QApplication

from asset_family import AssetFamily, FamilyObject
from depth_renderer import (
    CameraPolygon,
    clip_camera_polygon_near,
    order_camera_polygons,
)
from geometry_editor import (
    AXIS_VECTORS,
    GeometryEditSession,
    mat_apply,
)
from indexed_family_adapter import (
    IndexedFamilyAdapter,
    UnsupportedIndexedMaterialError,
    VANILLA_FADE_LENGTH,
    VANILLA_GAMEPLAY_FADE_START,
    VANILLA_GAMEPLAY_VIS_LIMIT,
)
from indexed_renderer import (
    IndexedPiece,
    IndexedRasterizer,
    retail_source_face_front_facing,
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
_PICK_CAMERA_DISTANCE = 4.0
_PICK_NEAR_DISTANCE = 0.2
_PICK_DEPTH_EPSILON = 1e-5
PASTE_GHOST_OPACITY = 0.18

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


class AssetViewport(QWidget):
    """3D preview of an asset family with polygon picking."""

    RESET_YAW = -35.0
    RESET_PITCH = 20.0
    RESET_ZOOM = 1.0
    CAMERA_EPSILON = 1e-6

    statusMessage = Signal(str)
    polygonPicked = Signal(int)     # compatibility signal
    polygonPickedDetailed = Signal(int, bool)  # poly_id, Ctrl additive select
    polygonStructurePicked = Signal(str, int, int)  # owner, poly_id, ADES block
    polygonDeselected = Signal(int)
    selectionCleared = Signal()
    objectPicked = Signal(str)      # owner_path of the clicked object
    editModeChanged = Signal(bool)  # geometry Edit Mode entered / left
    editSelectionChanged = Signal(int)
    editSelectionDetailsChanged = Signal(object, str)
    geometryEdited = Signal(str)    # owner_path whose vertices changed
    geometryCommandCommitted = Signal(str, object, object)
    undoRequested = Signal()
    redoRequested = Signal()
    editHint = Signal(str)          # live hint text for the active tool
    animationFrameChanged = Signal(str)
    manualCameraChanged = Signal()  # orbit, pan or zoom changed by the user
    pastePreviewConfirmRequested = Signal()
    pastePreviewActiveChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Keep the viewport comfortably usable, but do not force the whole
        # application beyond the available desktop height when optional
        # panels (for example Diagnostics) are opened.
        self.setMinimumSize(420, 260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._faces: list[ViewFace] = []
        self._materials: list[ViewMaterial] = []
        self._block_material_ids: dict[tuple[str, int], int] = {}
        self._sen_boxes: list[list[tuple[float, float, float]]] = []
        self._diagnostics: list[str] = []
        self._center = (0.0, 0.0, 0.0)
        self._scale = 1.0

        # Polygon Mapping Workbench state
        self._selected_poly: int | None = None
        self._mapping_diagnostics = False
        self._highlight_polys: set[int] = set()
        self._duplicate_polys: set[int] = set()
        self._pick_shapes: list[PickShape] = []
        self._press_pos: QPoint | None = None
        self._press_additive = False

        # Object selection / isolation (Blender-style)
        self._family_ref = None
        self._visible_owners: set[str] | None = None   # None = all visible
        self._selected_owner: str | None = None
        self._primary_owner: str | None = None
        self._owner_bounds: dict[str, tuple] = {}
        self._texture_image_cache: dict[tuple, QImage] = {}
        self._show_diag_overlay = True
        # Every viewport mode shares the Retail geometry path.  Textured mode
        # additionally uses the Retail-indexed palette/remap framebuffer.
        # Missing or unsupported SET resources fail visibly instead of
        # selecting a second approximation behind the user's back.
        self._indexed_adapter: IndexedFamilyAdapter | None = None
        self._indexed_unavailable_reason = "no AssetFamily is loaded"
        self._indexed_runtime_error = ""
        self._last_indexed_stats: dict = {}
        self._last_effective_renderer = "not_rendered"
        self._last_render_error = ""
        self._indexed_view_cache_key: tuple | None = None
        self._indexed_view_cache_image = QImage()
        self._indexed_render_revision = 0
        # Recovered fork behavior: retail AREA distance fade using the normal
        # Urban Assault gameplay profile (1400/600, start 800).  Keep it
        # optional and disabled by default; the fixed profile can only be toggled.
        self._retail_distance_fade_enabled = False

        # Geometry Edit Mode (Blender-style vertex editing)
        self._edit_session: GeometryEditSession | None = None
        self._edit_owner: str | None = None
        self._edit_faces: list[tuple[ViewFace, list[int]]] = []
        self._modal_op: str | None = None       # "grab" | "rotate" | "scale"
        self._modal_axis: str | None = None
        self._modal_numeric = ""
        self._modal_start = QPoint()
        self._modal_last_mouse = QPoint()
        self._modal_center_screen = QPointF()
        self._modal_denom = 4.0
        self._modal_pivot_world = (0.0, 0.0, 0.0)
        self._paste_preview: PastePreviewState | None = None
        self._auto_align_enabled = True
        self._alignment_targets: tuple[tuple[AlignmentTarget, ...], ...] = (
            (), (), ())
        self._alignment_values: tuple[tuple[float, ...], ...] = ((), (), ())
        self._alignment_selection_points: tuple[
            tuple[float, float, float], ...] = ()
        self._precision_guides: tuple[PrecisionGuide, ...] = ()
        self._precision_bbox_points: tuple[
            tuple[float, float, float], ...] = ()
        self._auto_axis_lock: int | None = None
        self._model_nudge_before = None
        self._model_nudge_delta = (0.0, 0.0, 0.0)
        self._model_transform_mode: str | None = None
        self._model_transform_before = None
        self._model_transform_direction = (0, 0, 0)
        self._model_transform_angle = 0.0
        self._model_transform_factors = (1.0, 1.0, 1.0)
        self._suppress_next_context_menu = False
        self._direct_grab_active = False
        self._direct_transform_mode = "move"
        self._direct_transform_intensity = 0.5
        self._active_edit_vertex: int | None = None
        self._direct_grab_selected_only = False
        self._edit_pick_polygons = False
        self._edit_read_only_vertices: set[int] = set()
        self._edit_visible_vertices: set[int] = set()
        self._edit_visibility_ready = False
        self._edit_visibility_view_key: tuple | None = None
        self._box_start: QPoint | None = None
        self._box_rect: QRect | None = None
        self._marquee_candidate = False
        self._vertex_press: VertexPressState | None = None
        self._mode_label_rect: QRect | None = None

        self._mode = "textured"
        self._show_sen = False
        self._show_axes = True
        self._show_grid = True
        self._show_wire_overlay = False
        self._backface_cull = True

        self._yaw = self.RESET_YAW
        self._pitch = self.RESET_PITCH
        self._zoom = self.RESET_ZOOM
        self._pan = QPointF(0.0, 0.0)
        self._last_mouse = QPoint()
        self._camera_interacting = False
        self._snapshot_active = False
        self._snapshot_show_guides = False
        self._snapshot_background: QColor | None = None
        self._snapshot_saved_state: dict | None = None
        self._snapshot_current_camera: dict | None = None

        # VANM playback state (per material with anim_frames)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._advance_animation)
        self._anim_playing = False
        self._anim_speed = 1.0
        self._anim_clock_ms = 0.0
        self._anim_states: dict[int, tuple[int, int]] = {}  # mat -> (frame, dir)
        self._anim_left_ms: dict[int, float] = {}

    # -- scene construction ---------------------------------------------------

    def clear(self) -> None:
        self.cancel_paste_preview()
        if self._edit_session is not None:
            self._edit_session.cancel_modal()
            self._edit_session = None
            self._edit_owner = None
            self.editModeChanged.emit(False)
        self._edit_faces = []
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._clear_precision_guides()
        self._model_nudge_before = None
        self._model_nudge_delta = (0.0, 0.0, 0.0)
        self._reset_model_transform()
        self._direct_grab_active = False
        self._direct_grab_selected_only = False
        self._edit_pick_polygons = False
        self._edit_read_only_vertices.clear()
        self._edit_visible_vertices.clear()
        self._edit_visibility_ready = False
        self._edit_visibility_view_key = None
        self._box_start = None
        self._box_rect = None
        self._marquee_candidate = False
        self._vertex_press = None
        self._faces = []
        self._materials = []
        self._block_material_ids = {}
        self._sen_boxes = []
        self._diagnostics = []
        self._selected_poly = None
        self._highlight_polys = set()
        self._duplicate_polys = set()
        self._pick_shapes = []
        self._owner_bounds = {}
        self._selected_owner = None
        self.stop_animation()
        self._invalidate_indexed_view_cache()
        self.update()

    # -- Polygon Mapping Workbench controls --------------------------------------

    def set_mapping_diagnostics(self, enabled: bool) -> None:
        self._mapping_diagnostics = enabled
        self.update()

    def set_selected_polygon(self, poly_id: int | None) -> None:
        self._selected_poly = poly_id
        self.update()

    def set_highlight_polys(self, poly_ids: set[int]) -> None:
        self._highlight_polys = set(poly_ids)
        self.update()

    def unmapped_polys(self) -> list[int]:
        return sorted({f.poly_id for f in self._faces
                       if f.primary and not f.mapped})

    def set_diagnostics(self, diagnostics: list[str]) -> None:
        self._diagnostics = list(diagnostics)
        self.update()

    def load_family(self, family: AssetFamily,
                    visible_owners: set[str] | None = None,
                    keep_camera: bool = False,
                    primary_owner: str | None = None,
                    reuse_indexed_adapter: bool = False) -> None:
        same_family = family is self._family_ref
        cached_indexed_adapter = self._indexed_adapter
        cached_indexed_reason = self._indexed_unavailable_reason
        previous_bounds = (
            dict(self._owner_bounds) if keep_camera and same_family else {})
        if not same_family:
            self._texture_image_cache.clear()
        selected = self._selected_owner
        self.clear()
        self._family_ref = family
        if same_family and reuse_indexed_adapter:
            self._indexed_adapter = cached_indexed_adapter
            self._indexed_unavailable_reason = cached_indexed_reason
        else:
            self._indexed_adapter, self._indexed_unavailable_reason = (
                IndexedFamilyAdapter.try_create(family))
        self._indexed_runtime_error = ""
        self._last_indexed_stats = {}
        self._last_effective_renderer = "not_rendered"
        self._last_render_error = ""
        self._visible_owners = visible_owners
        self._selected_owner = selected
        self._primary_owner = primary_owner
        material_index: dict[str, int] = {}
        self._diagnostics = list(family.textured_diagnostics)

        primary_obj = None
        if primary_owner is not None:
            primary_obj = next(
                (o for o in family.all_objects()
                 if getattr(o, "owner_path", None) == primary_owner
                 and o.skeleton is not None), None
            )
        if primary_obj is None:
            primary_obj = next(
                (o for o in family.all_objects() if o.skeleton is not None),
                None,
            )
        for fam_obj in family.all_objects():
            owner = getattr(fam_obj, "owner_path", "root")
            visible = (visible_owners is None or owner in visible_owners)
            if not visible and owner in previous_bounds:
                self._owner_bounds[owner] = previous_bounds[owner]
                continue
            self._load_object(fam_obj, family, material_index,
                              primary=fam_obj is primary_obj,
                              owner=owner, build_faces=visible)

        points = [v for face in self._faces for v in face.vertices]
        points.extend(p for box in self._sen_boxes for p in box)
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            zs = [p[2] for p in points]
            self._center = (
                (min(xs) + max(xs)) / 2,
                (min(ys) + max(ys)) / 2,
                (min(zs) + max(zs)) / 2,
            )
            extent = max(max(xs) - min(xs), max(ys) - min(ys),
                         max(zs) - min(zs), 1e-6)
            self._scale = 2.0 / extent
        if not keep_camera:
            self.reset_view()
        self._reset_animation_states()

    # -- object selection / isolation ---------------------------------------------

    def owners(self) -> list[str]:
        return sorted(self._owner_bounds)

    def visible_owners(self) -> set[str] | None:
        return set(self._visible_owners) if self._visible_owners else None

    def set_visible_owners(self, owners: set[str] | None) -> None:
        if self._family_ref is None:
            return
        # Rebuilding the visible owner scope calls ``clear()`` internally.
        # Keep the current interaction mode stable while the user switches
        # models: Edit stays Edit, View stays View.  Polygon/vertex selection is
        # intentionally reset because it belongs to the previous owner.
        was_editing = self._edit_session is not None
        target_owner = self._selected_owner if was_editing else None
        self.load_family(self._family_ref, owners, keep_camera=True,
                         primary_owner=self._primary_owner,
                         reuse_indexed_adapter=True)
        if was_editing and target_owner is not None:
            self.enter_edit_mode(target_owner)
        self.update()

    def refresh_family_materials(self) -> None:
        """Rebuild textures/materials while preserving active edit history."""

        if self._family_ref is None:
            return
        session = self._edit_session
        edit_state = None
        if session is not None and self._edit_owner is not None:
            if session.modal_active:
                session.cancel_modal()
            edit_state = {
                "owner": self._edit_owner,
                "selection": set(session.selection),
                "undo": [list(points) for points in session._undo],
                "redo": [list(points) for points in session._redo],
                "dirty": session.dirty,
                "selected_only": self._direct_grab_selected_only,
                "pick_polygons": self._edit_pick_polygons,
            }
        owners = (set(self._visible_owners)
                  if self._visible_owners is not None else None)
        self.load_family(self._family_ref, owners, keep_camera=True,
                         primary_owner=self._primary_owner)
        if edit_state is not None \
                and self.enter_edit_mode(edit_state["owner"]):
            restored = self._edit_session
            restored.set_selection(edit_state["selection"])
            restored._undo = edit_state["undo"]
            restored._redo = edit_state["redo"]
            restored.dirty = edit_state["dirty"]
            self.configure_edit_interaction(
                selected_only=edit_state["selected_only"],
                pick_polygons=edit_state["pick_polygons"])
        self.update()

    def descendants_of(self, owner: str) -> set[str]:
        prefix = owner + "/"
        return {o for o in self._owner_bounds
                if o == owner or o.startswith(prefix)}

    def isolate_owner(self, owner: str, include_children: bool = True) -> None:
        owners = (self.descendants_of(owner) if include_children
                  else {owner})
        self.set_visible_owners(owners)
        self.frame_owner(owner)

    def clear_isolation(self) -> None:
        self.set_visible_owners(None)

    def set_selected_owner(self, owner: str | None) -> None:
        self._selected_owner = owner
        self.update()

    def frame_owner(self, owner: str) -> None:
        bounds = self._owner_bounds.get(owner)
        if bounds is None:
            return
        x0, y0, z0, x1, y1, z1 = bounds
        self._center = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        extent = max(x1 - x0, y1 - y0, z1 - z0, 1e-6)
        self._scale = 2.0 / extent
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def frame_all(self) -> None:
        if not self._owner_bounds:
            return
        xs0, ys0, zs0, xs1, ys1, zs1 = zip(*self._owner_bounds.values())
        self._center = ((min(xs0) + max(xs1)) / 2,
                        (min(ys0) + max(ys1)) / 2,
                        (min(zs0) + max(zs1)) / 2)
        extent = max(max(xs1) - min(xs0), max(ys1) - min(ys0),
                     max(zs1) - min(zs0), 1e-6)
        self._scale = 2.0 / extent
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def set_overlay_visible(self, visible: bool) -> None:
        self._show_diag_overlay = visible
        self.update()

    # -- geometry Edit Mode (Blender-style vertex editing) ------------------------

    @property
    def is_edit_mode(self) -> bool:
        return self._edit_session is not None

    @property
    def edit_session(self) -> GeometryEditSession | None:
        return self._edit_session

    @property
    def edit_owner(self) -> str | None:
        return self._edit_owner

    def configure_edit_interaction(self, *, selected_only: bool,
                                   pick_polygons: bool) -> None:
        """Set mouse behavior without replacing the active edit session."""

        self._direct_grab_selected_only = selected_only
        self._edit_pick_polygons = pick_polygons

    @property
    def direct_transform_mode(self) -> str:
        return self._direct_transform_mode

    @property
    def direct_transform_intensity(self) -> float:
        return self._direct_transform_intensity

    def set_direct_transform(self, mode: str, intensity: float) -> None:
        """Configure direct vertex dragging without affecting camera controls."""

        normalized = str(mode).casefold()
        if normalized not in ("move", "rotate", "scale"):
            return
        self._direct_transform_mode = normalized
        self._direct_transform_intensity = max(0.001, float(intensity))
        self.update()

    def set_edit_read_only_vertices(self, indices) -> None:
        """Exclude unsafe shared FX vertices from every edit selection path."""

        protected = {
            int(index) for index in indices
            if isinstance(index, int) and index >= 0
        }
        self._edit_read_only_vertices = protected
        session = self._edit_session
        if session is not None:
            previous = set(session.selection)
            session.selection.difference_update(protected)
            if session.selection != previous:
                self._emit_selection_hint()
        self.update()

    @property
    def camera_orientation(self) -> tuple[float, float]:
        return self._yaw, self._pitch

    @property
    def camera_zoom(self) -> float:
        """Return the current camera zoom for shared UI synchronisation."""

        return float(self._zoom)

    @property
    def has_loaded_resource(self) -> bool:
        """Return whether the viewport currently owns a loaded asset family."""

        return self._family_ref is not None

    @property
    def camera_is_reset(self) -> bool:
        """Return whether orbit, pan and zoom match the canonical view."""

        epsilon = self.CAMERA_EPSILON
        return (
            abs(self._yaw - self.RESET_YAW) <= epsilon
            and abs(self._pitch - self.RESET_PITCH) <= epsilon
            and abs(self._zoom - self.RESET_ZOOM) <= epsilon
            and abs(self._pan.x()) <= epsilon
            and abs(self._pan.y()) <= epsilon
        )

    @property
    def can_reset_camera(self) -> bool:
        """Reset is meaningful only for a loaded, displaced camera."""

        return self.has_loaded_resource and not self.camera_is_reset

    @property
    def edit_model_matrix(self):
        session = self._edit_session
        return session.matrix if session is not None else (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )

    def set_auto_align_enabled(self, enabled: bool) -> None:
        self._auto_align_enabled = bool(enabled)
        if not enabled:
            self._precision_guides = ()
        self.update()

    @staticmethod
    def _bounds_features(points) -> tuple[tuple[float, float, float], ...]:
        values = list(points)
        if not values:
            return ()
        features = []
        for axis in range(3):
            coords = [point[axis] for point in values]
            features.append((
                min(coords),
                (min(coords) + max(coords)) * 0.5,
                max(coords),
            ))
        return tuple(features)

    def _prepare_alignment_targets(
            self, selection_points, selected_indices=()) -> None:
        session = self._edit_session
        selected = set(selected_indices)
        targets: list[list[AlignmentTarget]] = [
            [AlignmentTarget(0.0, f"{axis_name} origin")]
            for axis_name in "XYZ"
        ]
        if session is not None:
            all_points = list(session.model.points)
            unselected = [
                point for index, point in enumerate(all_points)
                if index not in selected
            ]
            polygons = session.model.polygons
            usable_faces: list[tuple[int, tuple[int, ...]]] = []
            unique_edges: set[tuple[int, int]] = set()
            for poly_id, polygon in enumerate(polygons):
                winding = tuple(polygon)
                if len(winding) < 3 or selected.intersection(winding) \
                        or any(index < 0 or index >= len(all_points)
                               for index in winding):
                    continue
                usable_faces.append((poly_id, winding))
                for edge_index, first in enumerate(winding):
                    second = winding[(edge_index + 1) % len(winding)]
                    if first != second:
                        unique_edges.add(tuple(sorted((first, second))))

            owner_features = self._bounds_features(all_points)
            owner_center = tuple(
                owner_features[axis][1] if owner_features else 0.0
                for axis in range(3))

            # Add richer topology targets first so a coincident scalar keeps
            # the most useful deterministic label after de-duplication.
            derived_points: list[tuple[tuple[float, float, float], str]] = []
            for poly_id, winding in usable_faces:
                face_points = [all_points[index] for index in winding]
                center = tuple(
                    sum(point[axis] for point in face_points)
                    / len(face_points)
                    for axis in range(3))
                derived_points.append((center, f"face #{poly_id} center"))
                for axis in range(3):
                    values = [point[axis] for point in face_points]
                    scale = max(
                        1.0, max(abs(value) for value in values))
                    if max(values) - min(values) <= 1e-8 * scale:
                        targets[axis].append(AlignmentTarget(
                            sum(values) / len(values),
                            f"face #{poly_id} coplanar {'XYZ'[axis]}"))
            for first, second in sorted(unique_edges):
                midpoint = tuple(
                    (all_points[first][axis] + all_points[second][axis]) * 0.5
                    for axis in range(3))
                derived_points.append((
                    midpoint, f"edge {first}-{second} midpoint"))

            # Continue only an already-demonstrated regular spacing at either
            # outer edge.  Three consecutive reference coordinates and two
            # equal gaps are required; two arbitrary points or an internal
            # hole are deliberately insufficient evidence for a snap target.
            for axis, axis_name in enumerate("XYZ"):
                coordinates = sorted({
                    round(point[axis], 9) for point in unselected
                    if math.isfinite(point[axis])
                })
                if len(coordinates) < 3:
                    continue
                lower, upper = coordinates[0], coordinates[-1]
                scale = max(1.0, abs(lower), abs(upper))
                minimum_gap = 1e-9 * scale
                for index in range(len(coordinates) - 2):
                    first, second, third = coordinates[index:index + 3]
                    first_gap = second - first
                    second_gap = third - second
                    if min(first_gap, second_gap) <= minimum_gap:
                        continue
                    tolerance = max(
                        minimum_gap,
                        max(first_gap, second_gap) * 1e-6)
                    if abs(first_gap - second_gap) > tolerance:
                        continue
                    gap = (first_gap + second_gap) * 0.5
                    for candidate in (first - gap, third + gap):
                        if candidate >= lower - tolerance \
                                and candidate <= upper + tolerance:
                            continue
                        targets[axis].append(AlignmentTarget(
                            candidate,
                            f"equal spacing/distance {axis_name} "
                            f"({gap:g})"))

            reference_points = [
                (tuple(point), "reference vertex") for point in unselected
            ]
            for point, label in (*derived_points, *reference_points):
                for axis in range(3):
                    targets[axis].append(
                        AlignmentTarget(point[axis], label))

            if unselected:
                for axis, features in enumerate(
                        self._bounds_features(unselected)):
                    for feature, value in zip(
                            ("min", "center", "max"), features):
                        targets[axis].append(AlignmentTarget(
                            value, f"unselected bounds {feature}"))

            # Symmetry candidates are snapshots of reference geometry around
            # both the model origin and the current owner's cached bbox center.
            # Selected geometry is intentionally excluded to prevent snap-back
            # false positives while it is being transformed.
            mirror_sources = (*derived_points, *reference_points)
            for point, label in mirror_sources:
                for axis, axis_name in enumerate("XYZ"):
                    for mirror_center, mirror_label in (
                            (0.0, "origin"),
                            (owner_center[axis], "owner bounds center")):
                        mirrored = 2.0 * mirror_center - point[axis]
                        if abs(mirrored - point[axis]) <= 1e-9:
                            continue
                        targets[axis].append(AlignmentTarget(
                            mirrored,
                            f"{label} mirror {axis_name} at {mirror_label}"))
        selection = tuple(tuple(point) for point in selection_points)
        deduplicated = []
        for axis_targets in targets:
            seen = {}
            for target in axis_targets:
                seen.setdefault(round(target.value, 9), target)
            deduplicated.append(tuple(sorted(
                seen.values(), key=lambda item: item.value)))
        self._alignment_targets = tuple(deduplicated)
        self._alignment_values = tuple(
            tuple(target.value for target in axis_targets)
            for axis_targets in self._alignment_targets)
        self._alignment_selection_points = selection
        self._auto_axis_lock = None
        self._precision_guides = ()
        self._precision_bbox_points = selection

    def _near_alignment_targets(self, axis: int, value: float):
        values = self._alignment_values[axis]
        targets = self._alignment_targets[axis]
        if not values:
            return ()
        position = bisect.bisect_left(values, value)
        indices = {
            max(0, min(len(values) - 1, position - 1)),
            max(0, min(len(values) - 1, position)),
        }
        return tuple(targets[index] for index in sorted(indices))

    def _axis_pixels_per_model_unit(self, axis: int, pivot) -> float:
        session = self._edit_session
        if session is None:
            return 0.0
        world = _apply(session.matrix, pivot, session.position)
        axis_world = mat_apply(session.matrix, AXIS_VECTORS["XYZ"[axis]])
        end = tuple(world[index] + axis_world[index] for index in range(3))
        a = self._project(self._camera_vertex(world))
        b = self._project(self._camera_vertex(end))
        return math.hypot(b.x() - a.x(), b.y() - a.y())

    @staticmethod
    def _feature_label(index: int) -> str:
        return ("min", "center", "max")[index]

    def _auto_axis_delta(self, delta, pivot):
        if not self._auto_align_enabled:
            self._auto_axis_lock = None
            return delta
        pixels = [
            abs(delta[axis])
            * self._axis_pixels_per_model_unit(axis, pivot)
            for axis in range(3)
        ]
        dominant = max(range(3), key=pixels.__getitem__)
        largest = pixels[dominant]
        other = max(
            (pixels[index] for index in range(3) if index != dominant),
            default=0.0)
        if self._auto_axis_lock is None:
            if largest >= 8.0 and other <= largest * 0.16:
                self._auto_axis_lock = dominant
        else:
            locked = self._auto_axis_lock
            locked_value = pixels[locked]
            locked_other = max(
                (pixels[index] for index in range(3) if index != locked),
                default=0.0)
            if locked_value < 4.0 or locked_other > locked_value * 0.28:
                self._auto_axis_lock = None
        if self._auto_axis_lock is None:
            return delta
        return tuple(
            value if axis == self._auto_axis_lock else 0.0
            for axis, value in enumerate(delta))

    def _snap_translation(
            self, points, raw_delta, axis_name: str | None = None,
            *, allow_axis_lock: bool = False):
        values = tuple(tuple(point) for point in points)
        if not values:
            return tuple(raw_delta)
        pivot = tuple(
            sum(point[axis] for point in values) / len(values)
            for axis in range(3))
        delta = tuple(raw_delta)
        bypass = bool(QApplication.keyboardModifiers()
                      & Qt.KeyboardModifier.AltModifier)
        if axis_name is not None:
            active_axis = "XYZ".index(axis_name)
            delta = tuple(
                value if axis == active_axis else 0.0
                for axis, value in enumerate(delta))
        elif allow_axis_lock and not bypass:
            delta = self._auto_axis_delta(delta, pivot)
        elif bypass:
            self._auto_axis_lock = None
        guides = []
        if self._auto_align_enabled and not bypass:
            source_features = self._bounds_features(values)
            axes = (
                ("XYZ".index(axis_name),) if axis_name is not None
                else (
                    (self._auto_axis_lock,)
                    if allow_axis_lock and self._auto_axis_lock is not None
                    else range(3))
            )
            mutable = list(delta)
            for axis in axes:
                pixels_per_unit = self._axis_pixels_per_model_unit(axis, pivot)
                if pixels_per_unit <= 1e-9:
                    continue
                best = None
                for feature_index, source_value in enumerate(
                        source_features[axis]):
                    moved = source_value + mutable[axis]
                    near_targets = self._near_alignment_targets(axis, moved)
                    if any(
                            abs(target.value - moved) * pixels_per_unit < 1e-6
                            for target in near_targets):
                        continue
                    for target in near_targets:
                        correction = target.value - moved
                        pixels = abs(correction) * pixels_per_unit
                        if pixels < 1e-6 or pixels > 8.0:
                            continue
                        candidate = (
                            pixels, correction, feature_index, target)
                        if best is None or candidate[0] < best[0]:
                            best = candidate
                if best is None:
                    continue
                _pixels, correction, feature_index, target = best
                mutable[axis] += correction
                guides.append(PrecisionGuide(
                    axis, target.value,
                    f"Auto Align: selection "
                    f"{'XYZ'[axis]} {self._feature_label(feature_index)} "
                    f"-> {target.label}"))
            delta = tuple(mutable)
        self._precision_guides = tuple(guides)
        self._precision_bbox_points = tuple(
            tuple(point[axis] + delta[axis] for axis in range(3))
            for point in values)
        if guides:
            self.statusMessage.emit(guides[0].label)
        return delta

    def _snap_scale_factor(self, factor: float, axis_name: str | None) -> float:
        session = self._edit_session
        origin = session.modal_origin_points() if session is not None else None
        if session is None or origin is None or not session.selection:
            return factor
        bypass = bool(QApplication.keyboardModifiers()
                      & Qt.KeyboardModifier.AltModifier)
        selected = [
            origin[index] for index in sorted(session.selection)
            if index < len(origin)
        ]
        if not selected:
            return factor
        pivot = tuple(
            sum(point[axis] for point in selected) / len(selected)
            for axis in range(3))
        axes = (
            ("XYZ".index(axis_name),) if axis_name is not None
            else range(3)
        )
        best = None
        snap_threshold = 8.0 if axis_name is not None else 6.0
        if self._auto_align_enabled and not bypass:
            features = self._bounds_features(selected)
            for axis in axes:
                pixels_per_unit = self._axis_pixels_per_model_unit(axis, pivot)
                if pixels_per_unit <= 1e-9:
                    continue
                for feature_index, source in enumerate(features[axis]):
                    denominator = source - pivot[axis]
                    if abs(denominator) <= 1e-12:
                        continue
                    current = pivot[axis] + denominator * factor
                    near_targets = self._near_alignment_targets(axis, current)
                    if any(
                            abs(target.value - current) * pixels_per_unit < 1e-6
                            for target in near_targets):
                        continue
                    for target in near_targets:
                        pixels = abs(target.value - current) * pixels_per_unit
                        exact = (target.value - pivot[axis]) / denominator
                        if exact <= 0.0 or pixels < 1e-6 \
                                or pixels > snap_threshold:
                            continue
                        candidate = (
                            pixels, exact, axis, feature_index, target)
                        if best is None or candidate[0] < best[0]:
                            best = candidate
        if best is not None:
            _pixels, factor, guide_axis, feature_index, target = best
            self._precision_guides = (PrecisionGuide(
                guide_axis, target.value,
                f"Auto Align: selection {'XYZ'[guide_axis]} "
                f"{self._feature_label(feature_index)} -> {target.label}"),)
            self.statusMessage.emit(self._precision_guides[0].label)
        else:
            self._precision_guides = ()
        scaled = []
        active = None if axis_name is None else "XYZ".index(axis_name)
        for point in selected:
            scaled.append(tuple(
                pivot[axis] + (point[axis] - pivot[axis])
                * (factor if active is None or axis == active else 1.0)
                for axis in range(3)))
        self._precision_bbox_points = tuple(scaled)
        return factor

    def _clear_precision_guides(self) -> None:
        self._precision_guides = ()
        self._precision_bbox_points = ()
        self._alignment_selection_points = ()
        self._auto_axis_lock = None
        self.update()

    @property
    def paste_preview_active(self) -> bool:
        return self._paste_preview is not None

    @property
    def paste_preview_delta(self) -> tuple[float, float, float] | None:
        state = self._paste_preview
        return state.delta_model if state is not None else None

    def begin_paste_preview(self, clipboard: object,
                            position: QPoint) -> bool:
        session = self._edit_session
        if session is None or self._edit_owner != clipboard.owner \
                or id(session.model) != clipboard.model_identity:
            self.statusMessage.emit(
                "Paste Geometry: activate the source owner in Edit Mode.")
            return False
        if self._modal_op is not None or session.modal_active:
            self.statusMessage.emit(
                "Paste Geometry: finish the current modal operation first.")
            return False
        material_ids = []
        for polygon in clipboard.polygons:
            material_id = self._block_material_ids.get(
                (clipboard.owner, polygon.block_index))
            if material_id is None:
                face = next(
                    (candidate for candidate in self._faces
                     if candidate.owner == clipboard.owner
                     and candidate.poly_id == polygon.source_poly_id),
                    None,
                )
                material_id = face.material if face is not None else None
            if material_id is None:
                self.statusMessage.emit(
                    "Paste Geometry: a copied material is no longer renderable.")
                return False
            material_ids.append(material_id)

        source_world = _apply(
            session.matrix, clipboard.pivot, session.position)
        source_screen = self._project(self._camera_vertex(source_world))
        denominator = self._camera_denominator(source_world)
        world_delta = self._screen_delta_to_world_at_depth(
            position.x() - source_screen.x(),
            position.y() - source_screen.y(), denominator)
        raw_delta = session.world_delta_to_model(world_delta)
        source_indices = {
            original for original, local in clipboard.original_to_local
            if 0 <= original < len(session.model.points)
            and 0 <= local < len(clipboard.points)
            and tuple(session.model.points[original])
            == tuple(clipboard.points[local])
        }
        self._prepare_alignment_targets(
            clipboard.points, source_indices)
        delta = self._snap_translation(
            clipboard.points, raw_delta, allow_axis_lock=True)
        previous = self.hasMouseTracking()
        self.setMouseTracking(True)
        self._paste_preview = PastePreviewState(
            clipboard=clipboard,
            delta_model=delta,
            raw_delta_model=raw_delta,
            source_pivot_world=source_world,
            mouse_anchor=QPoint(position),
            anchor_delta_model=delta,
            anchor_raw_delta_model=raw_delta,
            last_mouse=QPoint(position),
            material_ids=tuple(material_ids),
            previous_mouse_tracking=previous,
        )
        self.pastePreviewActiveChanged.emit(True)
        self._emit_paste_hint()
        self.update()
        return True

    def cancel_paste_preview(self) -> bool:
        state = self._paste_preview
        if state is None:
            return False
        self._paste_preview = None
        self.setMouseTracking(state.previous_mouse_tracking)
        self.editHint.emit("")
        self.pastePreviewActiveChanged.emit(False)
        self.statusMessage.emit("Paste Geometry cancelled.")
        self._clear_precision_guides()
        self.update()
        return True

    def finish_paste_preview(self) -> None:
        state = self._paste_preview
        if state is None:
            return
        self._paste_preview = None
        self.setMouseTracking(state.previous_mouse_tracking)
        self.editHint.emit("")
        self.pastePreviewActiveChanged.emit(False)
        self._clear_precision_guides()
        self.update()

    def consume_context_menu_suppression(self) -> bool:
        suppress = self._suppress_next_context_menu
        self._suppress_next_context_menu = False
        return suppress

    def _camera_denominator(self, world_point) -> float:
        return max(_PICK_NEAR_DISTANCE,
                   _PICK_CAMERA_DISTANCE
                   - self._camera_vertex(world_point)[2])

    def _reanchor_paste_mouse(self, position: QPoint | None = None) -> None:
        state = self._paste_preview
        if state is None:
            return
        current = QPoint(position or state.last_mouse)
        state.mouse_anchor = current
        state.last_mouse = current
        state.anchor_delta_model = state.delta_model
        state.anchor_raw_delta_model = state.raw_delta_model

    def _update_paste_preview(self, position: QPoint) -> None:
        state = self._paste_preview
        session = self._edit_session
        if state is None or session is None:
            return
        state.last_mouse = QPoint(position)
        if state.numeric and state.axis is not None:
            try:
                value = float(state.numeric)
            except ValueError:
                value = None
            if value is not None:
                values = list(state.raw_delta_model)
                values["XYZ".index(state.axis)] = value
                state.raw_delta_model = tuple(values)
                state.delta_model = tuple(
                    component if index == "XYZ".index(state.axis) else 0.0
                    for index, component in enumerate(values))
                self._precision_guides = ()
                self._precision_bbox_points = tuple(
                    tuple(point[axis] + state.delta_model[axis]
                          for axis in range(3))
                    for point in state.clipboard.points)
                self._emit_paste_hint()
                self.update()
                return
        denominator = self._camera_denominator(state.source_pivot_world)
        world_delta = self._screen_delta_to_world_at_depth(
            position.x() - state.mouse_anchor.x(),
            position.y() - state.mouse_anchor.y(), denominator)
        step = session.world_delta_to_model(world_delta)
        if state.axis is not None:
            axis = "XYZ".index(state.axis)
            step = tuple(value if index == axis else 0.0
                         for index, value in enumerate(step))
        state.raw_delta_model = tuple(
            state.anchor_raw_delta_model[index] + step[index]
            for index in range(3))
        state.delta_model = self._snap_translation(
            state.clipboard.points, state.raw_delta_model, state.axis,
            allow_axis_lock=state.axis is None)
        self._emit_paste_hint()
        self.update()

    def nudge_paste_preview(self, dx_pixels: float,
                            dy_pixels: float) -> bool:
        state = self._paste_preview
        session = self._edit_session
        if state is None or session is None:
            return False
        denominator = self._camera_denominator(state.source_pivot_world)
        world = self._screen_delta_to_world_at_depth(
            dx_pixels, dy_pixels, denominator)
        step = session.world_delta_to_model(world)
        if state.axis is not None:
            axis = "XYZ".index(state.axis)
            step = tuple(value if index == axis else 0.0
                         for index, value in enumerate(step))
        state.delta_model = tuple(
            state.delta_model[index] + step[index] for index in range(3))
        state.raw_delta_model = state.delta_model
        state.numeric = ""
        self._reanchor_paste_mouse(state.last_mouse)
        self._emit_paste_hint()
        self.update()
        return True

    def nudge_paste_preview_model(
            self, dx: float, dy: float, dz: float) -> bool:
        """Move the ghost by an explicit model-space vector."""

        state = self._paste_preview
        if state is None:
            return False
        raw = tuple(
            state.delta_model[index] + (dx, dy, dz)[index]
            for index in range(3))
        state.raw_delta_model = raw
        # Model-space gizmo movement is exact and never uses Auto Align.
        state.delta_model = raw
        self._precision_guides = ()
        self._precision_bbox_points = ()
        state.numeric = ""
        self._reanchor_paste_mouse(state.last_mouse)
        self._emit_paste_hint()
        self.update()
        return True

    def _emit_paste_hint(self) -> None:
        state = self._paste_preview
        if state is None:
            return
        dx, dy, dz = state.delta_model
        mode = f"Axis {state.axis}" if state.axis else "View Plane"
        aligned = (
            f" | {self._precision_guides[0].label}"
            if self._precision_guides else "")
        label = (
            f"Paste {getattr(state.clipboard, 'fx_name')} FX Preview"
            if getattr(state.clipboard, "fx_name", None)
            else "Copy Preview")
        self.editHint.emit(
            f"{label} | X {dx:+.2f} Y {dy:+.2f} Z {dz:+.2f} | "
            f"{mode}{aligned} | Paste/LMB/Enter confirm | RMB/Esc cancel")

    def toggle_edit_mode(self) -> bool:
        if self._snapshot_active:
            return self._edit_session is not None
        if self._edit_session is not None:
            self.exit_edit_mode()
            self.selectionCleared.emit()
            return False
        return self.enter_edit_mode()

    def enter_edit_mode(self, owner: str | None = None) -> bool:
        """Start vertex editing on ``owner`` (default: the selected object).

        Falls back to the first skeleton-bearing object.  Edits happen in
        model space (raw POO2 coordinates). Normal transforms remain
        coordinate-only; Paste Geometry uses the separate verified
        topological path."""

        if self._snapshot_active:
            return self._edit_session is not None
        if self._edit_session is not None:
            return True
        if self._family_ref is None:
            self.statusMessage.emit("Edit Mode: load an asset first.")
            return False
        objects = {getattr(o, "owner_path", "root"): o
                   for o in self._family_ref.all_objects()}
        target = owner or self._selected_owner or self._primary_owner
        fam_obj = objects.get(target) if target else None
        if fam_obj is None or fam_obj.skeleton is None:
            target, fam_obj = next(
                ((o.owner_path, o) for o in self._family_ref.all_objects()
                 if o.skeleton is not None),
                (None, None),
            )
        if fam_obj is None or target is None:
            self.statusMessage.emit(
                "Edit Mode: this family has no editable skeleton.")
            return False
        # The owner must be visible so its ViewFaces exist for live update.
        if self._visible_owners is not None \
                and target not in self._visible_owners:
            self.set_visible_owners(set(self._visible_owners) | {target})

        transform = fam_obj.base_object.transform
        if transform is not None:
            matrix = _rotation_matrix(transform.euler, transform.scale)
            pos = transform.position
        else:
            matrix = _rotation_matrix((0, 0, 0), (1.0, 1.0, 1.0))
            pos = (0.0, 0.0, 0.0)
        self._edit_session = GeometryEditSession(fam_obj, matrix, pos)
        self._edit_owner = target
        self._edit_visible_vertices.clear()
        self._edit_visibility_ready = False
        self._edit_visibility_view_key = None
        self._direct_grab_selected_only = False
        self._edit_pick_polygons = False
        self._active_edit_vertex = None
        self._selected_owner = target
        polygons = fam_obj.skeleton.polygons
        self._edit_faces = [
            (face, list(polygons[face.poly_id]))
            for face in self._faces
            if face.owner == target and 0 <= face.poly_id < len(polygons)
        ]
        if self._edit_session.degenerate_transform:
            self.statusMessage.emit(
                "Edit Mode: object transform is not invertible; screen "
                "deltas fall back to an identity mapping.")
        self.editModeChanged.emit(True)
        self.editHint.emit(
            f"Edit Mode [{target}]: drag selected vertex move | "
            "Ctrl+click add | Ctrl+drag empty space box-select | "
            "A all | Alt+A none | G move | "
            "R rotate | S scale | X/Y/Z axis | Ctrl+Z undo | Tab exit")
        self.update()
        return True

    def enter_edit_mode_with_vertices(self, owner: str, indices,
                                      pick_polygons: bool = False) -> bool:
        """Enter the existing Edit Mode and select only validated POO2 indices."""

        started_here = self._edit_session is None or self._edit_owner != owner
        if self._edit_session is not None and self._edit_owner != owner:
            self.exit_edit_mode()
        if not self.enter_edit_mode(owner):
            return False
        requested = [
            index for index in indices
            if index not in self._edit_read_only_vertices
        ]
        try:
            self._edit_session.set_selection(requested)
        except ValueError as exc:
            self.statusMessage.emit(f"Edit Mode: {exc}")
            if started_here:
                self.exit_edit_mode()
            return False
        if not self._edit_session.selection:
            self.statusMessage.emit(
                "Edit Mode: the selected geometry has no editable vertices.")
            if started_here:
                self.exit_edit_mode()
            return False
        self._direct_grab_selected_only = True
        self._active_edit_vertex = min(
            self._edit_session.selection, default=None)
        self._edit_pick_polygons = pick_polygons
        self._emit_selection_hint()
        self.update()
        return True

    def exit_edit_mode(self) -> None:
        self.cancel_paste_preview()
        session = self._edit_session
        if session is None:
            return
        if session.modal_active:
            session.cancel_modal()
            self._refresh_edit_faces()
        self._edit_session = None
        self._edit_owner = None
        self._edit_faces = []
        self._edit_visible_vertices.clear()
        self._edit_visibility_ready = False
        self._edit_visibility_view_key = None
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._direct_grab_active = False
        self._active_edit_vertex = None
        self._direct_grab_selected_only = False
        self._edit_pick_polygons = False
        self._box_start = None
        self._box_rect = None
        self._marquee_candidate = False
        self._vertex_press = None
        self._model_nudge_before = None
        self._model_nudge_delta = (0.0, 0.0, 0.0)
        self._reset_model_transform()
        self._clear_precision_guides()
        self.editModeChanged.emit(False)
        self.editHint.emit("")
        self.update()

    def edit_undo(self) -> None:
        session = self._edit_session
        if session is not None and session.undo():
            self._refresh_edit_faces()
            self.geometryEdited.emit(self._edit_owner or "")
            self.statusMessage.emit("Geometry edit: undo")
            self.update()

    def edit_redo(self) -> None:
        session = self._edit_session
        if session is not None and session.redo():
            self._refresh_edit_faces()
            self.geometryEdited.emit(self._edit_owner or "")
            self.statusMessage.emit("Geometry edit: redo")
            self.update()

    def select_all_edit_vertices(self) -> None:
        session = self._edit_session
        if session is None:
            self.statusMessage.emit("Edit Mode: no editable model is active.")
            return
        self._select_all_editable_vertices()
        self._selected_owner = self._edit_owner
        self._active_edit_vertex = min(session.selection, default=None)
        self._emit_selection_hint("vertex")
        self.update()

    def _select_all_editable_vertices(self) -> bool:
        """Select every safe POO2 index, excluding protected FX vertices."""

        session = self._edit_session
        if session is None:
            return False
        session.select_all()
        session.selection.difference_update(self._edit_read_only_vertices)
        return bool(session.selection)

    def select_no_edit_vertices(self) -> None:
        session = self._edit_session
        if session is not None:
            session.select_none()
        self._active_edit_vertex = None
        self._selected_poly = None
        self._selected_owner = None
        self._highlight_polys.clear()
        self._emit_selection_hint("vertex")
        self.selectionCleared.emit()
        self.update()

    def deselect_at(self, point: QPoint) -> bool:
        """Deselect the visible polygon below ``point`` and its vertices."""

        hit = resolve_pick_shape(self._pick_shapes, QPointF(point))
        if hit is not None:
            face = hit.face
            session = self._edit_session
            if session is not None and face.owner == self._edit_owner:
                model = session.model
                if 0 <= face.poly_id < len(model.polygons):
                    session.selection.difference_update(
                        model.polygons[face.poly_id])
                    self._emit_selection_hint("vertex")
            self._highlight_polys.discard(face.poly_id)
            if self._selected_poly == face.poly_id:
                self._selected_poly = None
            if session is None or not session.selection:
                self._selected_owner = None
            if face.primary:
                self.polygonDeselected.emit(face.poly_id)
            self.update()
            return True
        self.select_no_edit_vertices()
        return False

    def selected_edit_points(self):
        session = self._edit_session
        if session is None:
            return []
        return [(index, session.model.points[index])
                for index in sorted(session.selection)
                if index < len(session.model.points)]

    def replace_selected_edit_points(self, points) -> bool:
        session = self._edit_session
        replacements = list(points)
        if session is None or not session.selection:
            self.statusMessage.emit(
                "Paste: select the destination vertices first.")
            return False
        if len(replacements) != len(session.selection):
            self.statusMessage.emit(
                "Paste: copied and selected vertex counts do not match.")
            return False
        if not session.begin_modal():
            return False
        before = session.modal_origin_points()
        try:
            session.preview_replace_selected(replacements)
        except ValueError as exc:
            session.cancel_modal()
            self.statusMessage.emit(f"Paste: {exc}")
            return False
        changed = session.commit_modal()
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
            self.statusMessage.emit(
                f"Pasted {len(replacements)} vertex position(s).")
        else:
            self.statusMessage.emit("Paste made no coordinate changes.")
        self.update()
        return changed

    def nudge_edit_selection(self, dx_pixels: float,
                             dy_pixels: float) -> bool:
        """Move selected vertices in the current screen plane."""

        session = self._edit_session
        if session is None or not session.selection:
            self.statusMessage.emit(
                "Nudge: enable Edit Mode and select one or more vertices.")
            return False
        pivot = session.selection_pivot()
        if pivot is None or session.modal_active:
            return False
        pivot_world = _apply(session.matrix, pivot, session.position)
        cam = self._camera_vertex(pivot_world)
        self._modal_denom = max(0.2, 4.0 - cam[2])
        world = self._screen_delta_to_world(dx_pixels, dy_pixels)
        delta = session.world_delta_to_model(world)
        return self.nudge_edit_selection_model(*delta)

    def begin_model_nudge(self) -> bool:
        """Begin one grouped undo command for gizmo auto-repeat."""

        session = self._edit_session
        if session is None or not session.selection:
            self.statusMessage.emit(
                "Gizmo: enable Edit Mode and select polygon geometry.")
            return False
        if self._model_nudge_before is not None:
            return True
        if not session.begin_modal():
            return False
        self._model_nudge_before = session.modal_origin_points()
        self._model_nudge_delta = (0.0, 0.0, 0.0)
        return True

    def nudge_edit_selection_model(
            self, dx: float, dy: float, dz: float) -> bool:
        """Move selected vertices by an explicit model-space vector."""

        session = self._edit_session
        started_here = self._model_nudge_before is None
        if started_here and not self.begin_model_nudge():
            return False
        if session is None or self._model_nudge_before is None:
            return False
        raw = tuple(
            self._model_nudge_delta[index] + (dx, dy, dz)[index]
            for index in range(3))
        self._model_nudge_delta = raw
        # Auto Align is intentionally limited to direct viewport editing.
        # Gizmo actions must apply the exact configured step on every axis.
        session.preview_grab(raw)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.editHint.emit(
            f"Move Gizmo | X {raw[0]:+.3f} "
            f"Y {raw[1]:+.3f} Z {raw[2]:+.3f}")
        self.update()
        if started_here:
            return self.end_model_nudge()
        return True

    def end_model_nudge(self) -> bool:
        session = self._edit_session
        before = self._model_nudge_before
        if session is None or before is None:
            return False
        changed = session.commit_modal()
        self._model_nudge_before = None
        self._model_nudge_delta = (0.0, 0.0, 0.0)
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
            self.statusMessage.emit(
                f"Moved {len(session.selection)} selected vertex/vertices "
                "in model space.")
        self.editHint.emit("")
        self._clear_precision_guides()
        self.update()
        return changed

    def _reset_model_transform(self) -> None:
        self._model_transform_mode = None
        self._model_transform_before = None
        self._model_transform_direction = (0, 0, 0)
        self._model_transform_angle = 0.0
        self._model_transform_factors = (1.0, 1.0, 1.0)

    def begin_model_transform(self, mode: str, direction) -> bool:
        """Begin one grouped Rotate or Scale gizmo command."""

        session = self._edit_session
        normalized = str(mode).lower()
        try:
            vector = tuple(int(value) for value in direction)
        except (TypeError, ValueError):
            return False
        if normalized not in ("rotate", "scale") or len(vector) != 3:
            return False
        if session is None or not session.selection:
            self.statusMessage.emit(
                "Transform: enable Edit Mode and select polygon geometry.")
            return False
        if self._model_transform_before is not None:
            return True
        if not session.begin_modal():
            return False
        self._model_transform_mode = normalized
        self._model_transform_before = session.modal_origin_points()
        self._model_transform_direction = vector
        self._model_transform_angle = 0.0
        self._model_transform_factors = (1.0, 1.0, 1.0)
        return True

    def rotate_edit_selection_model(self, angle_radians: float) -> bool:
        """Preview cumulative right-handed rotation from the initial snapshot."""

        session = self._edit_session
        direction = self._model_transform_direction
        active = [index for index, value in enumerate(direction) if value]
        if session is None or self._model_transform_mode != "rotate" \
                or len(active) != 1 or not math.isfinite(angle_radians):
            return False
        axis_index = active[0]
        sign = 1.0 if direction[axis_index] > 0 else -1.0
        angle = self._model_transform_angle + sign * float(angle_radians)
        if not math.isfinite(angle):
            return False
        self._model_transform_angle = angle
        axis = ("X", "Y", "Z")[axis_index]
        session.preview_rotate(AXIS_VECTORS[axis],
                               self._model_transform_angle)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.editHint.emit(
            f"Rotate Gizmo | {axis} "
            f"{math.degrees(self._model_transform_angle):+.2f}°")
        self.update()
        return True

    def scale_edit_selection_model(self, step_fraction: float) -> bool:
        """Preview cumulative positive scale factors from the initial snapshot."""

        session = self._edit_session
        direction = self._model_transform_direction
        if session is None or self._model_transform_mode != "scale" \
                or not math.isfinite(step_fraction) \
                or not 0.0 < step_fraction < 1.0:
            return False
        active = [index for index, value in enumerate(direction) if value]
        if len(active) not in (1, 3):
            return False
        factors = list(self._model_transform_factors)
        for index in active:
            increment = 1.0 + step_fraction
            if direction[index] < 0:
                increment = 1.0 / increment
            factors[index] *= increment
        if not all(math.isfinite(value) and value > 1e-9
                   for value in factors):
            return False
        self._model_transform_factors = tuple(factors)
        session.preview_scale_axes(self._model_transform_factors)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.editHint.emit(
            "Scale Gizmo | "
            + " ".join(
                f"{axis} {factor:.4g}x"
                for axis, factor in zip("XYZ", factors)))
        self.update()
        return True

    def end_model_transform(self) -> bool:
        """Commit one completed Rotate or Scale gizmo interaction."""

        session = self._edit_session
        before = self._model_transform_before
        mode = self._model_transform_mode
        if session is None or before is None or mode is None:
            return False
        changed = session.commit_modal()
        self._reset_model_transform()
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
            self.statusMessage.emit(
                f"{mode.capitalize()}d "
                f"{len(session.selection)} selected vertex/vertices "
                "in model space.")
        self.editHint.emit("")
        self.update()
        return changed

    def scale_edit_selection(self, factor: float,
                             select_all_if_empty: bool = True) -> bool:
        session = self._edit_session
        if session is None:
            self.statusMessage.emit("Scale: enable Edit Mode first.")
            return False
        selected_all = False
        if not session.selection and select_all_if_empty:
            selected_all = self._select_all_editable_vertices()
        if not session.selection or not session.begin_modal():
            self.statusMessage.emit("Scale: no vertices selected.")
            return False
        before = session.modal_origin_points()
        session.preview_scale(factor)
        changed = session.commit_modal()
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
            scope = "entire model" if selected_all else (
                f"{len(session.selection)} selected vertex/vertices")
            self.statusMessage.emit(f"Scaled {scope} by {factor:g}.")
        self.update()
        return changed

    def begin_scale_preview(self, select_all_if_empty: bool = True) -> bool:
        session = self._edit_session
        if session is None:
            return False
        if not session.selection and select_all_if_empty:
            self._select_all_editable_vertices()
            self._emit_selection_hint()
        return bool(session.selection and session.begin_modal())

    def update_scale_preview(self, factor: float) -> None:
        session = self._edit_session
        if session is None or not session.modal_active:
            return
        session.preview_scale(factor)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.update()

    def begin_rotate_preview(self, select_all_if_empty: bool = True) -> bool:
        session = self._edit_session
        if session is None:
            return False
        if not session.selection and select_all_if_empty:
            self._select_all_editable_vertices()
            self._emit_selection_hint()
        return bool(session.selection and session.begin_modal())

    def update_rotate_preview(self, axis: str,
                              angle_degrees: float) -> None:
        session = self._edit_session
        axis_name = str(axis).upper()
        if session is None or not session.modal_active \
                or axis_name not in AXIS_VECTORS \
                or not math.isfinite(angle_degrees):
            return
        session.preview_rotate(
            AXIS_VECTORS[axis_name], math.radians(angle_degrees))
        self._refresh_edit_faces(invalidate_visibility=False)
        self.update()

    def _finish_transform_preview(self, accept: bool) -> bool:
        session = self._edit_session
        if session is None or not session.modal_active:
            return False
        before = session.modal_origin_points()
        if not accept:
            session.cancel_modal()
            self._refresh_edit_faces()
            self.update()
            return False
        changed = session.commit_modal()
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
        self.update()
        return changed

    def finish_scale_preview(self, accept: bool) -> bool:
        return self._finish_transform_preview(accept)

    def finish_rotate_preview(self, accept: bool) -> bool:
        return self._finish_transform_preview(accept)

    def apply_geometry_snapshot(self, owner: str, points) -> bool:
        """Apply a window-level undo snapshot to the active edited owner."""

        session = self._edit_session
        values = [tuple(point) for point in points]
        if session is None or self._edit_owner != owner \
                or len(values) != len(session.model.points):
            return False
        session.cancel_modal()
        session.model.points[:] = values
        session.dirty = True
        self._refresh_edit_faces()
        self.geometryEdited.emit(owner)
        self.update()
        return True

    def _emit_geometry_command(self, before) -> None:
        owner = self._edit_owner or ""
        session = self._edit_session
        self.geometryEdited.emit(owner)
        if session is not None and before is not None:
            self.geometryCommandCommitted.emit(
                owner, list(before), list(session.model.points))

    def _refresh_edit_faces(
            self, invalidate_visibility: bool = True) -> None:
        """Push the session's current coordinates into the owner's faces."""

        session = self._edit_session
        if session is None:
            return
        if invalidate_visibility:
            self._edit_visibility_ready = False
            self._edit_visibility_view_key = None
        world = session.world_points()
        for face, indices in self._edit_faces:
            face.vertices = [world[i] for i in indices if i < len(world)]
        if self._edit_owner is not None and world:
            xs = [p[0] for p in world]
            ys = [p[1] for p in world]
            zs = [p[2] for p in world]
            self._owner_bounds[self._edit_owner] = (
                min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
        self._invalidate_indexed_view_cache()

    def _edit_screen_points(self, target: QRectF | None = None,
                            camera: dict | None = None) -> list[QPointF]:
        session = self._edit_session
        if session is None:
            return []
        return [self._project(self._camera_vertex(p, camera), target, camera)
                for p in session.world_points()]

    def _refresh_edit_vertex_visibility(
            self, screen_points=None, target: QRectF | None = None,
            view_key: tuple | None = None) -> set[int]:
        """Cache non-selected handles proven visible by rendered pick pieces."""

        session = self._edit_session
        if session is None:
            self._edit_visible_vertices.clear()
            self._edit_visibility_ready = False
            self._edit_visibility_view_key = None
            return set()
        points = (
            list(screen_points) if screen_points is not None
            else self._edit_screen_points())
        self._edit_visible_vertices = resolve_visible_edit_vertices(
            self._pick_shapes,
            points,
            session.model.polygons,
            self._edit_owner or "",
            target=target if target is not None else QRectF(self.rect()),
        )
        self._edit_visibility_ready = True
        self._edit_visibility_view_key = view_key
        return set(self._edit_visible_vertices)

    def _available_edit_vertices(self, screen_points=None) -> set[int]:
        session = self._edit_session
        if session is None:
            return set()
        if not self._edit_visibility_ready:
            self._refresh_edit_vertex_visibility(screen_points)
        return (
            set(self._edit_visible_vertices)
            | set(session.selection)
        ) - self._edit_read_only_vertices

    def _nearest_edit_vertex(self, point: QPoint) -> int | None:
        """Return the vertex under the cursor using the visible pick radius."""

        session = self._edit_session
        if session is None:
            return None
        screen_points = self._edit_screen_points()
        available = self._available_edit_vertices(screen_points)
        best = None
        for index, screen in enumerate(screen_points):
            if index not in available:
                continue
            dx = screen.x() - point.x()
            dy = screen.y() - point.y()
            dist = dx * dx + dy * dy
            if dist >= 12.0 ** 2:
                continue
            candidate = (
                dist,
                0 if index in session.selection else 1,
                index,
            )
            if best is None or candidate < best:
                best = candidate
        return best[2] if best is not None else None

    def _pick_edit_vertex(self, point: QPoint, extend: bool) -> int | None:
        session = self._edit_session
        if session is None:
            return None
        best = self._nearest_edit_vertex(point)
        if best is None:
            if not extend:
                session.select_none()
        elif extend:
            session.toggle(best)
        else:
            session.selection = {best}
        self._active_edit_vertex = (
            best if best is not None and best in session.selection
            else min(session.selection, default=None))
        self._emit_selection_hint("vertex")
        self.update()
        return best

    def _apply_box_select(self, extend: bool) -> None:
        session = self._edit_session
        rect = self._box_rect
        self._box_start = None
        self._box_rect = None
        if session is None or rect is None:
            self.update()
            return
        screen_points = self._edit_screen_points()
        available = self._available_edit_vertices(screen_points)
        hits = {
            index for index, screen in enumerate(screen_points)
            if index in available
            if rect.contains(int(screen.x()), int(screen.y()))
        }
        session.selection = (session.selection | hits) if extend else hits
        self._active_edit_vertex = min(hits, default=min(
            session.selection, default=None))
        self._emit_selection_hint("vertex")
        self.update()

    def _emit_selection_hint(self, source: str = "programmatic") -> None:
        session = self._edit_session
        if session is None:
            return
        self.statusMessage.emit(
            f"Edit Mode: {len(session.selection)}"
            f"/{len(session.model.points)} vertices selected")
        self.editSelectionChanged.emit(len(session.selection))
        self.editSelectionDetailsChanged.emit(
            set(session.selection), source)

    # -- modal transforms (G / R / S) ------------------------------------------

    def _begin_modal(self, op: str, start: QPoint | None = None,
                     direct_grab: bool = False) -> None:
        session = self._edit_session
        if session is None:
            return
        previous_selection = set(session.selection)
        session.selection.difference_update(self._edit_read_only_vertices)
        if session.selection != previous_selection:
            self._emit_selection_hint()
        if not session.selection:
            self.statusMessage.emit(
                "Edit Mode: select at least one vertex first "
                "(click, drag a selection box, or A for all).")
            return
        if not session.begin_modal():
            return
        pivot = session.selection_pivot() or (0.0, 0.0, 0.0)
        self._modal_pivot_world = _apply(session.matrix, pivot,
                                         session.position)
        cam = self._camera_vertex(self._modal_pivot_world)
        self._modal_denom = max(0.2, 4.0 - cam[2])
        self._modal_center_screen = self._project(cam)
        self._modal_op = op
        self._modal_axis = None
        self._modal_numeric = ""
        self._direct_grab_active = direct_grab
        cursor = (start if start is not None
                  else self.mapFromGlobal(QCursor.pos()))
        self._modal_start = cursor
        self._modal_last_mouse = cursor
        origin = session.modal_origin_points() or []
        selected = [
            origin[index] for index in sorted(session.selection)
            if index < len(origin)
        ]
        self._prepare_alignment_targets(selected, session.selection)
        self._update_modal(cursor)

    def _commit_modal(self) -> None:
        session = self._edit_session
        if session is None or self._modal_op is None:
            return
        before = session.modal_origin_points()
        changed = session.commit_modal()
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._direct_grab_active = False
        self._refresh_edit_faces()
        if changed:
            self._emit_geometry_command(before)
        self.editHint.emit("")
        self._clear_precision_guides()
        self.update()

    def _cancel_modal(self) -> None:
        session = self._edit_session
        if session is None:
            return
        session.cancel_modal()
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._direct_grab_active = False
        self._refresh_edit_faces()
        self.editHint.emit("")
        self._clear_precision_guides()
        self.update()

    def _numeric_value(self) -> float | None:
        if not self._modal_numeric:
            return None
        try:
            return float(self._modal_numeric)
        except ValueError:
            return None

    def _screen_delta_to_world(self, dx_px: float, dy_px: float):
        """Mouse delta (pixels) to a world-space delta in the screen plane.

        Inverts the _camera_vertex/_project chain at the modal pivot depth:
        perspective divide, then inverse pitch/yaw, then unscale and unflip
        the display-only Y."""

        return self._screen_delta_to_world_at_depth(
            dx_px, dy_px, self._modal_denom)

    def _screen_delta_to_world_at_depth(
            self, dx_px: float, dy_px: float, denominator: float):
        focal = min(self.width(), self.height()) * 1.5 * self._zoom
        if focal <= 1e-9 or self._scale <= 1e-12:
            return (0.0, 0.0, 0.0)
        dx_cam = dx_px * denominator / focal
        dy_cam = -dy_px * denominator / focal
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        y_flipped = math.cos(pitch) * dy_cam
        xz_z = -math.sin(pitch) * dy_cam
        x = math.cos(yaw) * dx_cam - math.sin(yaw) * xz_z
        z = math.sin(yaw) * dx_cam + math.cos(yaw) * xz_z
        return (x / self._scale, -y_flipped / self._scale, z / self._scale)

    def _view_axis_world(self):
        """Camera forward axis (toward the viewer) in world coordinates."""

        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        return (-math.sin(yaw) * math.cos(pitch),
                -math.sin(pitch),
                math.cos(yaw) * math.cos(pitch))

    def _world_dir_to_camera(self, direction):
        x, y, z = direction[0], -direction[1], direction[2]
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        xz_x = x * math.cos(yaw) + z * math.sin(yaw)
        xz_z = -x * math.sin(yaw) + z * math.cos(yaw)
        return (xz_x,
                y * math.cos(pitch) - xz_z * math.sin(pitch),
                y * math.sin(pitch) + xz_z * math.cos(pitch))

    def _rotate_axis_model(self):
        """Rotation axis in model space plus the screen-direction sign.

        The world -> camera map flips Y (display-only), so a right-handed
        model rotation appears mirrored; the sign keeps the on-screen motion
        following the mouse regardless of the axis orientation."""

        session = self._edit_session
        if self._modal_axis is not None:
            axis_model = AXIS_VECTORS[self._modal_axis]
        else:
            axis_model = session.world_dir_to_model(self._view_axis_world()) \
                or (0.0, 0.0, 1.0)
        axis_world = mat_apply(session.matrix, axis_model)
        cam = self._world_dir_to_camera(axis_world)
        sign = 1.0 if cam[2] >= 0.0 else -1.0
        return axis_model, sign

    def _update_modal(self, pos: QPoint) -> None:
        session = self._edit_session
        if session is None or self._modal_op is None:
            return
        numeric = self._numeric_value()
        suffix = " | X/Y/Z axis - type a number - LMB/Enter ok, RMB/Esc cancel"
        if self._modal_numeric:
            suffix += f" | typed: {self._modal_numeric}"

        if self._modal_op == "grab":
            if numeric is not None and self._modal_axis is not None:
                unit = AXIS_VECTORS[self._modal_axis]
                delta = (unit[0] * numeric, unit[1] * numeric,
                         unit[2] * numeric)
            else:
                world = self._screen_delta_to_world(
                    pos.x() - self._modal_start.x(),
                    pos.y() - self._modal_start.y())
                delta = session.world_delta_to_model(world)
            if self._direct_grab_active:
                step = self._direct_transform_intensity
                length = math.sqrt(sum(value * value for value in delta))
                if length > 1e-12:
                    snapped_length = max(step, round(length / step) * step)
                    delta = tuple(
                        value * snapped_length / length for value in delta)
            origin = session.modal_origin_points() or []
            selected = [
                origin[index] for index in sorted(session.selection)
                if index < len(origin)
            ]
            delta = self._snap_translation(
                selected, delta, self._modal_axis,
                allow_axis_lock=self._modal_axis is None)
            session.preview_grab(delta, self._modal_axis)
            dx, dy, dz = delta
            if self._modal_axis == "X":
                dy = dz = 0.0
            elif self._modal_axis == "Y":
                dx = dz = 0.0
            elif self._modal_axis == "Z":
                dx = dy = 0.0
            self.editHint.emit(
                f"Move [{self._modal_axis or 'free'}] "
                f"d=({dx:.2f}, {dy:.2f}, {dz:.2f}) model units"
                + (f" | {self._precision_guides[0].label}"
                   if self._precision_guides else "")
                + suffix)
        elif self._modal_op == "rotate":
            axis_model, sign = self._rotate_axis_model()
            if numeric is not None:
                visual_deg = numeric
            else:
                center = self._modal_center_screen
                a0 = math.atan2(-(self._modal_start.y() - center.y()),
                                self._modal_start.x() - center.x())
                a1 = math.atan2(-(pos.y() - center.y()),
                                pos.x() - center.x())
                visual_deg = math.degrees(a1 - a0)
            if self._direct_grab_active:
                step = self._direct_transform_intensity
                visual_deg = round(visual_deg / step) * step
            session.preview_rotate(axis_model,
                                   -math.radians(visual_deg) * sign)
            self.editHint.emit(
                f"Rotate [{self._modal_axis or 'view'}] "
                f"{visual_deg:+.1f} deg{suffix}")
        elif self._modal_op == "scale":
            if numeric is not None:
                factor = numeric
            else:
                center = self._modal_center_screen
                d0 = math.hypot(self._modal_start.x() - center.x(),
                                self._modal_start.y() - center.y())
                d1 = math.hypot(pos.x() - center.x(),
                                pos.y() - center.y())
                factor = d1 / max(d0, 8.0)
            if self._direct_grab_active:
                step = self._direct_transform_intensity
                percent = round(((factor - 1.0) * 100.0) / step) * step
                factor = max(0.01, 1.0 + percent / 100.0)
            factor = self._snap_scale_factor(factor, self._modal_axis)
            session.preview_scale(factor, self._modal_axis)
            self.editHint.emit(
                f"Scale [{self._modal_axis or 'uniform'}] "
                f"x{factor:.3f}"
                + (f" | {self._precision_guides[0].label}"
                   if self._precision_guides else "")
                + suffix)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.update()

    def _load_object(self, fam_obj: FamilyObject, family: AssetFamily,
                     material_index: dict[str, int],
                     primary: bool = True, owner: str = "root",
                     build_faces: bool = True) -> None:
        skeleton = fam_obj.skeleton
        if skeleton is None:
            return

        transform = fam_obj.base_object.transform
        if transform is not None:
            matrix = _rotation_matrix(transform.euler, transform.scale)
            pos = transform.position
        else:
            matrix = _rotation_matrix((0, 0, 0), (1.0, 1.0, 1.0))
            pos = (0.0, 0.0, 0.0)

        world_points = [_apply(matrix, p, pos) for p in skeleton.points]
        polygons = skeleton.polygons

        if world_points:
            xs = [p[0] for p in world_points]
            ys = [p[1] for p in world_points]
            zs = [p[2] for p in world_points]
            self._owner_bounds[owner] = (min(xs), min(ys), min(zs),
                                         max(xs), max(ys), max(zs))
        if not build_faces:
            return

        if skeleton.sensors:
            self._sen_boxes.append(
                [_apply(matrix, p, pos) for p in skeleton.sensors]
            )

        groups = fam_obj.materials
        if not groups:
            # No BASE mapping (manual mode): one synthetic material, all polys.
            key = f"__geometry__{id(fam_obj)}"
            mat_id = self._ensure_material(key, "geometry only", "", family,
                                           material_index)
            for poly_id, polygon in enumerate(polygons):
                if len(polygon) >= 3:
                    self._faces.append(ViewFace(
                        vertices=[world_points[i] for i in polygon],
                        uvs=[], material=mat_id, shade=0, poly_id=poly_id,
                        primary=primary, owner=owner,
                        block_index=-1,
                    ))
            return

        if primary:
            covered: dict[int, int] = {}
            for group in groups:
                for poly_id, _uvs, _shade in group.faces:
                    covered[poly_id] = covered.get(poly_id, 0) + 1
            self._duplicate_polys = {p for p, n in covered.items() if n > 1}
            unmapped = [p for p in range(len(polygons))
                        if p not in covered and len(polygons[p]) >= 3]
            if unmapped:
                unmapped_mat = self._ensure_material(
                    "__unmapped__", "UNMAPPED (no ATTS entry)", "", family,
                    material_index,
                )
                self._materials[unmapped_mat].color = QColor(255, 40, 200)
                for poly_id in unmapped:
                    polygon = polygons[poly_id]
                    self._faces.append(ViewFace(
                        vertices=[world_points[i] for i in polygon],
                        uvs=[], material=unmapped_mat, shade=0,
                        poly_id=poly_id, mapped=False, primary=True,
                        owner=owner,
                    ))

        for block_index, group in enumerate(groups):
            block = group.block
            tracy_key = block.tracy_mode if block is not None else "none"
            # Material animation state and UV groups belong to one structural
            # ADES block.  Same-name FX1/FX2 blocks must not share a runtime
            # material merely because they reference the same resource.
            key = (f"{owner}|{block_index}|{group.texture_name}|"
                   f"{group.kind}|{tracy_key}"
                   if group.texture_name else f"__block__{id(group)}")
            mat_id = self._ensure_material(key, group.label, group.kind,
                                           family, material_index,
                                           anim_name=group.texture_name
                                           if group.kind == "bmpanim" else "",
                                           block=block)
            self._block_material_ids[(owner, block_index)] = mat_id
            animated = bool(self._materials[mat_id].anim_frames)
            for poly_id, uvs, shade in group.faces:
                if poly_id >= len(polygons):
                    continue
                polygon = polygons[poly_id]
                if len(polygon) < 3:
                    continue
                self._faces.append(ViewFace(
                    vertices=[world_points[i] for i in polygon],
                    uvs=list(uvs), material=mat_id, shade=shade,
                    poly_id=poly_id, animated=animated, primary=primary,
                    owner=owner,
                    block_index=block_index,
                ))

    def _ensure_material(self, key: str, label: str, kind: str,
                         family: AssetFamily, material_index: dict[str, int],
                         anim_name: str = "", block=None) -> int:
        existing = material_index.get(key)
        if existing is not None:
            return existing

        mat = ViewMaterial(label=label, kind=kind)
        mat.color = MATERIAL_COLORS[len(self._materials) % len(MATERIAL_COLORS)]
        if block is not None:
            mat.tracy_mode = block.tracy_mode
            mat.tracy_light = block.tracy_light

        if kind == "bmpanim" and anim_name:
            anm = family.animations.get(anim_name)
            if anm is not None:
                mat.anim_bitmap_names = list(anm.bitmap_names)
                for bitmap_name in anm.bitmap_names:
                    img = family.textures.get(bitmap_name)
                    override = (family.effect_override_paths.get(
                        bitmap_name.lower()) if mat.additive else None)
                    qimage = self._cached_texture_image(
                        family, bitmap_name, img, override)
                    mat.anim_images.append(qimage if qimage else QImage())
                mat.anim_uv_groups = [list(g) for g in anm.texcoord_groups]
                mat.anim_frames = [
                    (f.frame_time, f.frame_id, f.texcoords_id) for f in anm.frames
                ]
                if block is not None and block.texture is not None \
                        and block.texture.anim_type is not None:
                    mat.anim_type = block.texture.anim_type
                if mat.anim_images and not mat.anim_images[0].isNull():
                    mat.image = mat.anim_images[0]
        elif label:
            img = family.textures.get(label)
            override = (family.effect_override_paths.get(label.lower())
                        if mat.additive else None)
            mat.image = self._cached_texture_image(
                family, label, img, override)

        material_index[key] = len(self._materials)
        self._materials.append(mat)
        return material_index[key]

    def _cached_texture_image(
            self, family: AssetFamily, name: str, image,
            override) -> QImage:
        """Decode each immutable family texture once across scene refreshes."""

        external_palette = (
            family.external_palette
            if image is not None and not image.palette else None)
        cache_key = (
            id(family),
            str(name).casefold(),
            id(image),
            id(external_palette),
            str(override) if override is not None else "",
        )
        cached = self._texture_image_cache.get(cache_key)
        if cached is not None:
            return QImage(cached)
        decoded = (
            _image_from_effect_png(override)
            if override is not None else
            _image_from_ilbm(image, external_palette))
        cached = QImage(decoded) if decoded is not None else QImage()
        self._texture_image_cache[cache_key] = cached
        return QImage(cached)

    def _invalidate_indexed_view_cache(self) -> None:
        """Invalidate only the derived Retail-indexed viewport image."""

        self._indexed_render_revision += 1
        self._indexed_view_cache_key = None
        self._indexed_view_cache_image = QImage()
        self._last_indexed_stats = {}

    def _indexed_view_cache_signature(
            self, width: int, height: int, camera: dict) -> tuple:
        pan = camera["pan"]
        return (
            self._indexed_render_revision,
            id(self._indexed_adapter),
            int(width), int(height),
            float(camera["yaw"]), float(camera["pitch"]),
            float(camera["zoom"]), float(pan.x()), float(pan.y()),
            tuple(float(value) for value in camera["center"]),
            float(camera["scale"]),
            tuple(sorted(
                (int(material_id), int(state[0]), int(state[1]))
                for material_id, state in self._anim_states.items()
            )),
            bool(self._retail_distance_fade_enabled),
        )

    # -- public view controls --------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode in VIEW_MODES:
            if mode != self._mode:
                self._invalidate_indexed_view_cache()
            self._mode = mode
            if mode == "textured":
                if self._indexed_adapter is not None:
                    backend = IndexedRasterizer.acceleration_backend
                    suffix = (
                        " NumPy acceleration active."
                        if backend == "numpy"
                        else " Portable Python backend active; install NumPy "
                             "for the supported realtime configuration."
                    )
                    self.statusMessage.emit(
                        "Textured: Retail indexed SHADERMP/TRACYRMP renderer."
                        f"{suffix}")
                else:
                    self.statusMessage.emit(
                        "Textured renderer unavailable: "
                        f"{self._indexed_unavailable_reason}")
            elif mode in ("materials", "wireframe", "solid"):
                label = {
                    "materials": "BASE/material groups",
                    "wireframe": "SKLT/wireframe",
                    "solid": "Solid",
                }[mode]
                self.statusMessage.emit(
                    f"{label}: shared Retail geometry renderer "
                    "(source-face culling, near clip, exact BSP).")
            self.update()

    @property
    def indexed_rendering_reason(self) -> str:
        return self._indexed_runtime_error or self._indexed_unavailable_reason

    def indexed_renderer_info(self) -> dict:
        adapter = self._indexed_adapter
        return {
            "available": bool(adapter is not None and not self._indexed_runtime_error),
            "availability_reason": self.indexed_rendering_reason,
            "requested_mode": self._mode,
            "effective_renderer": self._last_effective_renderer,
            "render_error": self._last_render_error,
            "acceleration_backend": IndexedRasterizer.acceleration_backend,
            "max_safe_pixels": IndexedRasterizer.max_safe_pixels,
            "viewport_full_resolution": True,
            "last_render_stats": dict(self._last_indexed_stats),
            "sources": dict(adapter.source_info) if adapter is not None else {},
            "retail_distance_fade": {
                "enabled": bool(self._retail_distance_fade_enabled),
                "vis_limit": VANILLA_GAMEPLAY_VIS_LIMIT,
                "fade_start": VANILLA_GAMEPLAY_FADE_START,
                "fade_length": VANILLA_FADE_LENGTH,
            },
        }

    def set_retail_distance_fade(self, enabled: bool) -> None:
        """Enable the recovered vanilla gameplay AREA distance fade."""

        updated = bool(enabled)
        if updated == self._retail_distance_fade_enabled:
            return
        self._retail_distance_fade_enabled = updated
        self._invalidate_indexed_view_cache()
        self.update()

    def set_show_sen(self, enabled: bool) -> None:
        self._show_sen = enabled
        self.update()

    def set_show_axes(self, enabled: bool) -> None:
        self._show_axes = enabled
        self.update()

    def set_show_grid(self, enabled: bool) -> None:
        self._show_grid = enabled
        self.update()

    def set_wire_overlay(self, enabled: bool) -> None:
        self._show_wire_overlay = enabled
        self.update()

    def set_backface_cull(self, enabled: bool) -> None:
        self._backface_cull = enabled
        self.update()

    @property
    def has_model(self) -> bool:
        return bool(self._faces)

    def _camera_state(self) -> dict:
        return {
            "yaw": self._yaw,
            "pitch": self._pitch,
            "zoom": self._zoom,
            "pan": QPointF(self._pan),
            "center": self._center,
            "scale": self._scale,
        }

    def _set_camera_state(self, state: dict) -> None:
        self._yaw = state["yaw"]
        self._pitch = state["pitch"]
        self._zoom = state["zoom"]
        self._pan = QPointF(state["pan"])
        self._center = state["center"]
        self._scale = state["scale"]

    def begin_snapshot_mode(self, background: QColor | None = None) -> None:
        """Enter the temporary clean Photo Studio view without editing data."""

        if self._snapshot_saved_state is None:
            camera = self._camera_state()
            self._snapshot_saved_state = {
                "camera": camera,
                "mode": self._mode,
                "show_sen": self._show_sen,
                "show_axes": self._show_axes,
                "show_grid": self._show_grid,
                "show_wire_overlay": self._show_wire_overlay,
                "mapping_diagnostics": self._mapping_diagnostics,
                "show_diag_overlay": self._show_diag_overlay,
            }
            self._snapshot_current_camera = camera.copy()
            self._snapshot_current_camera["pan"] = QPointF(camera["pan"])
        self._snapshot_active = True
        self._snapshot_show_guides = False
        self._snapshot_background = (QColor(background)
                                     if background is not None else None)
        self._mode = "textured"
        self._show_sen = False
        self._show_axes = False
        self._show_grid = False
        self._show_wire_overlay = False
        self._mapping_diagnostics = False
        self._show_diag_overlay = False
        self.update()

    def end_snapshot_mode(self) -> str:
        """Leave Photo Studio and restore the exact prior viewport state."""

        state = self._snapshot_saved_state
        if state is not None:
            self._set_camera_state(state["camera"])
            self._mode = state["mode"]
            self._show_sen = state["show_sen"]
            self._show_axes = state["show_axes"]
            self._show_grid = state["show_grid"]
            self._show_wire_overlay = state["show_wire_overlay"]
            self._mapping_diagnostics = state["mapping_diagnostics"]
            self._show_diag_overlay = state["show_diag_overlay"]
        self._snapshot_active = False
        self._snapshot_show_guides = False
        self._snapshot_background = None
        self._snapshot_saved_state = None
        self._snapshot_current_camera = None
        self.update()
        return self._mode

    def set_snapshot_background(self, background: QColor | None) -> None:
        self._snapshot_background = (QColor(background)
                                     if background is not None else None)
        if self._snapshot_active:
            self.update()

    def set_snapshot_guides_visible(self, visible: bool) -> None:
        """Show the saved regular overlays in legacy/batch Snapshot paths."""

        self._snapshot_show_guides = bool(visible)
        state = self._snapshot_saved_state
        if self._snapshot_active and state is not None:
            self._show_sen = state["show_sen"] if visible else False
            self._show_axes = state["show_axes"] if visible else False
            self._show_grid = state["show_grid"] if visible else False
            self._show_wire_overlay = (
                state["show_wire_overlay"] if visible else False)
            self._mapping_diagnostics = (
                state["mapping_diagnostics"] if visible else False)
            self._show_diag_overlay = (
                state["show_diag_overlay"] if visible else False)
        self.update()

    def set_snapshot_shared_overlays_enabled(self, enabled: bool) -> None:
        """Let shared View actions control Snapshot without restoring old state."""

        self._snapshot_show_guides = bool(enabled)
        if self._snapshot_active:
            self.update()

    def adjust_snapshot_zoom(self, factor: float) -> None:
        if factor > 0:
            self._zoom = max(0.08, min(30.0, self._zoom * factor))
            self.update()

    def apply_snapshot_preset(self, preset: str, target_size,
                              zoom_percent: int = 100) -> None:
        """Apply a camera-only canonical view and frame visible geometry."""

        if preset == "Current View":
            if self._snapshot_current_camera is not None:
                self._set_camera_state(self._snapshot_current_camera)
            self.update()
            return
        self.apply_view_preset(preset, target_size, zoom_percent)

    def apply_view_preset(self, preset: str, target_size,
                          zoom_percent: int = 100) -> None:
        """Apply a canonical camera view in either regular or Snapshot mode."""

        if preset == "Current View":
            return
        angles = VIEW_PRESET_ANGLES.get(preset)
        if angles is None:
            return
        self._yaw, self._pitch = angles
        self._fit_view_to_model(target_size, zoom_percent)

    def _fit_view_to_model(self, target_size,
                           zoom_percent: int = 100) -> None:
        """Center canonical presets on all visible geometry."""

        points = [vertex for face in self._faces for vertex in face.vertices]
        width = max(1, int(target_size.width()))
        height = max(1, int(target_size.height()))
        if not points:
            return
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        zs = [point[2] for point in points]
        self._center = ((min(xs) + max(xs)) / 2,
                        (min(ys) + max(ys)) / 2,
                        (min(zs) + max(zs)) / 2)
        extent = max(max(xs) - min(xs), max(ys) - min(ys),
                     max(zs) - min(zs), 1e-6)
        self._scale = 2.0 / extent
        self._pan = QPointF(0.0, 0.0)
        camera_points = [self._camera_vertex(point) for point in points]
        base_focal = min(width, height) * 1.5
        # Keep a small fixed safety border; the public control is a direct
        # zoom percentage rather than an implementation-oriented margin.
        usable = 0.92
        half_width = width * 0.5 * usable
        half_height = height * 0.5 * usable
        limits = [30.0]
        for x, y, z in camera_points:
            denominator = max(0.2, 4.0 - z)
            if abs(x) > 1e-9:
                limits.append(half_width * denominator
                              / (abs(x) * base_focal))
            if abs(y) > 1e-9:
                limits.append(half_height * denominator
                              / (abs(y) * base_focal))
        zoom_factor = max(25, min(300, zoom_percent)) / 100.0
        self._zoom = max(0.08, min(30.0, min(limits) * zoom_factor))
        self.update()

    def render_snapshot(self, target_size, background: QColor | None,
                        include_guides: bool = False) -> QImage:
        """Render the visible model directly into an alpha-capable QImage."""

        width = int(target_size.width())
        height = int(target_size.height())
        if not self._faces or width <= 0 or height <= 0:
            return QImage()
        image = QImage(width, height,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        self._render_scene(painter, QRectF(0, 0, width, height), background,
                           clean=not include_guides,
                           camera=self._camera_state(),
                           allow_transparent_background=True,
                           force_textured=True)
        painter.end()
        if self._last_effective_renderer != "retail_indexed_reconstructed":
            reason = self._last_render_error or self.indexed_rendering_reason
            raise RuntimeError(
                "Retail indexed export failed closed: " + (reason or "unknown reason"))
        return image

    def reset_view(self) -> None:
        self._yaw = self.RESET_YAW
        self._pitch = self.RESET_PITCH
        self._zoom = self.RESET_ZOOM
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def fit_view(self) -> None:
        self.reset_view()

    @property
    def has_animation(self) -> bool:
        return any(m.anim_frames for m in self._materials)

    def play_animation(self, playing: bool) -> None:
        self._anim_playing = playing and self.has_animation
        if self._anim_playing:
            self._anim_timer.start()
        else:
            self._anim_timer.stop()
        self.update()

    def stop_animation(self) -> None:
        self._anim_playing = False
        self._anim_timer.stop()
        self._reset_animation_states()

    def set_animation_speed(self, speed: float) -> None:
        self._anim_speed = max(0.05, min(8.0, speed))

    def step_animation(self) -> None:
        for mat_id, mat in enumerate(self._materials):
            if not mat.anim_frames:
                continue
            frame, direction = self._anim_states.get(mat_id, (0, 1))
            frame, direction = self._next_frame(mat, frame, direction)
            self._anim_states[mat_id] = (frame, direction)
            self._anim_left_ms[mat_id] = 0.0
        self.update()
        self.animationFrameChanged.emit(self.current_frame_text())

    def step_animation_backward(self) -> None:
        """Move every active animation to the previous visible frame."""

        for mat_id, mat in enumerate(self._materials):
            if not mat.anim_frames:
                continue
            frame, direction = self._anim_states.get(mat_id, (0, 1))
            frame = (frame - 1) % len(mat.anim_frames)
            # Manual stepping selects an explicit frame.  Resume forward from
            # there instead of inheriting a stale ping-pong direction.
            self._anim_states[mat_id] = (frame, 1 if direction >= 0 else -1)
            self._anim_left_ms[mat_id] = 0.0
        self.update()
        self.animationFrameChanged.emit(self.current_frame_text())

    def reset_animation(self) -> None:
        self._reset_animation_states()
        self.update()
        self.animationFrameChanged.emit(self.current_frame_text())

    def current_frame_text(self) -> str:
        parts = []
        for mat_id, mat in enumerate(self._materials):
            if mat.anim_frames:
                frame, _ = self._anim_states.get(mat_id, (0, 1))
                parts.append(f"{mat.label}: frame {frame + 1}/{len(mat.anim_frames)}")
        return "; ".join(parts)

    def current_animation_uvs(
            self, owner: str, block_index: int,
            vertex_count: int) -> list[tuple[int, int]]:
        """Return the VANM group currently rendered by one material block."""

        data = self.current_animation_frame_data(owner, block_index)
        if data is None:
            return []
        _image, uvs, _description = data
        return list(uvs[:vertex_count])

    def current_animation_frame_data(
            self, owner: str, block_index: int
            ) -> tuple[QImage | None, list[tuple[int, int]], str] | None:
        """Return the exact VANM image/UV group active in the viewport."""

        material_id = self._block_material_ids.get((owner, block_index))
        if material_id is None or not (
                0 <= material_id < len(self._materials)):
            return None
        material = self._materials[material_id]
        if not material.anim_frames:
            return None
        frame_index, _direction = self._anim_states.get(material_id, (0, 1))
        if not (0 <= frame_index < len(material.anim_frames)):
            return None
        _duration, image_id, uv_id = material.anim_frames[frame_index]
        if not (0 <= uv_id < len(material.anim_uv_groups)):
            return None
        image = (
            material.anim_images[image_id]
            if 0 <= image_id < len(material.anim_images)
            and not material.anim_images[image_id].isNull()
            else None)
        bitmap_name = (
            material.anim_bitmap_names[image_id]
            if 0 <= image_id < len(material.anim_bitmap_names)
            else f"bitmap #{image_id}")
        description = (
            f"frame {frame_index + 1}/{len(material.anim_frames)}, "
            f"{bitmap_name}, UV group #{uv_id}")
        return (
            image,
            [tuple(uv) for uv in material.anim_uv_groups[uv_id]],
            description,
        )

    def current_animation_group(
            self, owner: str, block_index: int
            ) -> tuple[str, int] | None:
        """Return the logical VANM name and UV-group index being rendered."""

        material_id = self._block_material_ids.get((owner, block_index))
        if material_id is None or not (
                0 <= material_id < len(self._materials)):
            return None
        material = self._materials[material_id]
        if not material.anim_frames:
            return None
        frame_index, _direction = self._anim_states.get(material_id, (0, 1))
        if not (0 <= frame_index < len(material.anim_frames)):
            return None
        _duration, _image_id, uv_id = material.anim_frames[frame_index]
        if not (0 <= uv_id < len(material.anim_uv_groups)):
            return None
        return material.label, uv_id

    def _reset_animation_states(self) -> None:
        self._anim_states = {}
        self._anim_left_ms = {}
        self._anim_clock_ms = 0.0
        for mat_id, mat in enumerate(self._materials):
            if mat.anim_frames:
                self._anim_states[mat_id] = (0, 1)
                self._anim_left_ms[mat_id] = 0.0

    def _next_frame(self, mat: ViewMaterial, frame: int, direction: int):
        # Mirrors NC_STACK_bmpanim::SetTime (CONFIRMED): loop wraps to 0,
        # ping-pong reverses at the ends.
        frame += direction
        if frame >= len(mat.anim_frames):
            if mat.anim_type:
                frame = len(mat.anim_frames) - 1
                direction = -1
            else:
                frame = 0
        elif frame < 0:
            frame = 0
            direction = 1
        return frame, direction

    def _advance_animation(self) -> None:
        if not self._anim_playing:
            return
        delta_ms = self._anim_timer.interval() * self._anim_speed
        changed = False
        for mat_id, mat in enumerate(self._materials):
            if not mat.anim_frames:
                continue
            frame, direction = self._anim_states.get(mat_id, (0, 1))
            left = self._anim_left_ms.get(mat_id, 0.0) + delta_ms
            # frame_time is in 1024 Hz game ticks (CONFIRMED)
            while True:
                duration_ms = mat.anim_frames[frame][0] * 1000.0 / 1024.0
                if left < duration_ms or duration_ms <= 0:
                    break
                left -= duration_ms
                frame, direction = self._next_frame(mat, frame, direction)
                changed = True
            self._anim_states[mat_id] = (frame, direction)
            self._anim_left_ms[mat_id] = left
        if changed:
            self.update()
            text = self.current_frame_text()
            self.statusMessage.emit(text)
            self.animationFrameChanged.emit(text)

    # -- projection --------------------------------------------------------------

    def _camera_vertex(self, point, camera: dict | None = None
                       ) -> tuple[float, float, float]:
        camera = camera or self._camera_state()
        center = camera["center"]
        scale = camera["scale"]
        x = (point[0] - center[0]) * scale
        y = -(point[1] - center[1]) * scale  # UA -Y is up
        z = (point[2] - center[2]) * scale

        yaw = math.radians(camera["yaw"])
        pitch = math.radians(camera["pitch"])
        xz_x = x * math.cos(yaw) + z * math.sin(yaw)
        xz_z = -x * math.sin(yaw) + z * math.cos(yaw)
        yz_y = y * math.cos(pitch) - xz_z * math.sin(pitch)
        yz_z = y * math.sin(pitch) + xz_z * math.cos(pitch)
        return (xz_x, yz_y, yz_z)

    def _project(self, camera_point, target: QRectF | None = None,
                 camera: dict | None = None) -> QPointF:
        camera = camera or self._camera_state()
        target = target or QRectF(self.rect())
        x, y, z = camera_point
        distance = 4.0
        denominator = max(0.2, distance - z)
        focal = min(target.width(), target.height()) * 1.5 * camera["zoom"]
        pan = camera["pan"]
        return QPointF(
            target.center().x() + pan.x() + x * focal / denominator,
            target.center().y() + pan.y() - y * focal / denominator,
        )

    # -- painting ------------------------------------------------------------------

    def _front_facing_from_screen_area(self, area: float) -> bool:
        """Return the regular editor front-face convention for screen area.

        Runtime-specific workspaces may override this without changing the
        shared Model Editor / Snapshot rendering contract.
        """

        return float(area) >= 0.0

    def paintGL_stub(self):  # pragma: no cover - kept for API parity
        pass

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        background = self._snapshot_background
        if self._snapshot_active and background is None:
            background = QColor(24, 26, 32)
        self._render_scene(painter, QRectF(self.rect()), background,
                           clean=(self._snapshot_active
                                  and not self._snapshot_show_guides),
                           camera=self._camera_state())
        painter.end()

    def _render_scene(self, painter: QPainter, target: QRectF,
                      background: QColor | None, clean: bool,
                      camera: dict,
                      allow_transparent_background: bool = False,
                      force_textured: bool = False) -> None:
        """Shared QWidget/QImage renderer; ``clean`` draws model pixels only."""

        # While the camera is actively moving, favor response time over
        # sub-pixel filtering.  Mouse release immediately requests a normal
        # repaint, so the exact visual quality is restored at rest.
        camera_preview = self._camera_interacting and not clean
        painter.setRenderHint(
            QPainter.RenderHint.Antialiasing, not camera_preview)
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, not camera_preview)
        if background is not None:
            painter.fillRect(target, background)
        elif not allow_transparent_background:
            painter.fillRect(target, QColor(24, 26, 32))

        if not self._faces:
            if not clean:
                painter.setPen(QColor(180, 185, 192))
                painter.drawText(target, Qt.AlignmentFlag.AlignCenter,
                                 "Open a .base to assemble resources "
                                 "automatically,\nor a .bas to browse all "
                                 "packed resources.")
            return

        if self._show_grid and not clean:
            self._draw_grid(painter, target, camera)
        if self._show_axes and not clean:
            self._draw_axes(painter, target, camera)

        mode = "textured" if clean or force_textured else self._mode
        self._last_effective_renderer = (
            "not_rendered" if mode == "textured"
            else f"retail_geometry_{mode}")
        self._last_render_error = ""
        if mode != "textured":
            self._last_indexed_stats = {}
        pick_shapes = []
        selected_shape = None
        source_polygons: dict[int, QPolygonF] = {}
        visible_faces: set[int] = set()
        highlight_paths: dict[int, QPainterPath] = {}
        def draw_piece(face: ViewFace, piece_camera, piece_uvs,
                       front_facing: bool, *, draw_surface: bool = True,
                       surface_mode: str | None = None) -> None:
            screen = [
                self._project(point, target, camera)
                for point in piece_camera
            ]
            polygon = QPolygonF(screen)
            if draw_surface:
                if not face.mapped and not clean:
                    # ATTS coverage holes are diagnostic only.
                    painter.setPen(QPen(QColor(155, 160, 170, 115), 1.0))
                    painter.setBrush(QColor(105, 110, 120, 48))
                    painter.drawPolygon(polygon)
                else:
                    self._draw_face(
                        painter, face, screen, mode=surface_mode or mode,
                        draw_wire=False)
            if not clean and self._mapping_diagnostics and face.mapped \
                    and face.primary and face.poly_id in self._duplicate_polys:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 230, 40, 130))
                painter.drawPolygon(polygon)
            whole_edit_owner_selected = bool(
                self._edit_session is not None
                and self._edit_owner == face.owner
                and len(self._edit_session.selection)
                == len(self._edit_session.model.points)
            )
            poly_highlighted = bool(
                not clean and not self._snapshot_active and face.primary
                and face.poly_id in self._highlight_polys)
            owner_highlighted = bool(
                not clean and not self._snapshot_active
                and self._selected_owner is not None
                and face.owner == self._selected_owner
                and (self._mode != "textured"
                     or whole_edit_owner_selected))
            if poly_highlighted or owner_highlighted:
                highlight_color = (
                    QColor(90, 230, 255, 90)
                    if poly_highlighted
                    else QColor(
                        60, 185, 255,
                        82 if whole_edit_owner_selected else 60))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(highlight_color)
                painter.drawPolygon(polygon)
            if poly_highlighted:
                piece_path = QPainterPath()
                piece_path.addPolygon(polygon)
                piece_path.closeSubpath()
                key = id(face)
                previous = highlight_paths.get(key)
                highlight_paths[key] = (
                    piece_path if previous is None
                    else previous.united(piece_path))
            if not clean and mode != "wireframe" \
                    and self._show_wire_overlay and front_facing:
                source_polygon = source_polygons.get(id(face))
                if source_polygon is not None:
                    clip = QPainterPath()
                    clip.addPolygon(polygon)
                    clip.closeSubpath()
                    painter.save()
                    painter.setClipPath(
                        clip, Qt.ClipOperation.IntersectClip)
                    painter.setPen(QPen(QColor(0, 0, 0, 130), 0.75))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPolygon(source_polygon)
                    painter.restore()
            if not clean:
                pick_shapes.append(PickShape(
                    polygon=polygon,
                    face=face,
                    camera_vertices=tuple(piece_camera),
                    draw_order=len(pick_shapes),
                    front_facing=front_facing,
                ))
            visible_faces.add(id(face))

        # BASE/material and SKLT/wireframe previews intentionally share the
        # same Retail geometry path as Textured: whole-source-face visibility,
        # runtime fan triangulation, near-plane clipping and exact BSP order.
        # Their presentation remains editor-specific (flat material colors or
        # source-polygon outlines) and does not require indexed SET resources.
        triangles: list[CameraPolygon] = []
        source_order = 0
        wireframe_polygons: dict[int, QPolygonF] = {}
        for face_order, face in enumerate(self._faces):
            if (not clean and not face.mapped
                    and not self._mapping_diagnostics):
                continue
            cam = tuple(
                self._camera_vertex(vertex, camera)
                for vertex in face.vertices)
            source_front_facing = retail_source_face_front_facing(cam)
            if mode == "textured":
                if not source_front_facing:
                    continue
            elif self._backface_cull and not source_front_facing:
                # Preserve the explicit editor override for BASE/SKLT while
                # using the exact same Retail visibility decision by default.
                continue

            mat = self._materials[face.material]
            face_uvs = []
            if mode == "textured" and face.mapped:
                face_uvs = self._face_uvs(face, mat)
            uv_mapping_valid = len(face_uvs) == len(cam)
            if not uv_mapping_valid:
                face_uvs = [(0.0, 0.0)] * len(cam)

            source_polygon = CameraPolygon(
                cam, tuple(tuple(uv) for uv in face_uvs), None, source_order)
            clipped_source = clip_camera_polygon_near(
                source_polygon,
                minimum_distance=float(camera.get("near_distance", 0.2)),
            )
            if clipped_source is not None:
                projected_source = QPolygonF([
                    self._project(point, target, camera)
                    for point in clipped_source.vertices])
                source_polygons[id(face)] = projected_source
                if mode == "wireframe":
                    wireframe_polygons[id(face)] = projected_source

            for index in range(2, len(cam)):
                indices = (0, index, index - 1)
                tri_camera = tuple(cam[item] for item in indices)
                clipped = clip_camera_polygon_near(
                    CameraPolygon(
                        tri_camera,
                        tuple(tuple(face_uvs[item]) for item in indices),
                        None,
                        source_order,
                    ),
                    minimum_distance=float(camera.get("near_distance", 0.2)),
                )
                if clipped is None:
                    continue
                triangles.append(CameraPolygon(
                    clipped.vertices,
                    clipped.attributes,
                    _RenderPayload(
                        face, source_front_facing, uv_mapping_valid,
                        face_order,
                        max(4.0 - point[2] for point in cam)),
                    source_order,
                ))
                source_order += 1

        # Do not switch BASE/SKLT to the old camera-drag approximation.  The
        # fork-derived Retail BSP path is now the single geometry truth for all
        # three public preview modes.
        ordered = order_camera_polygons(triangles)
        if mode == "textured":
            previous_error = self._indexed_runtime_error
            try:
                target_width = max(1, int(round(target.width())))
                target_height = max(1, int(round(target.height())))
                if target_width * target_height \
                        > IndexedRasterizer.max_safe_pixels:
                    raise RuntimeError(
                        f"requested {target_width}x{target_height} frame "
                        "exceeds the active backend safety limit of "
                        f"{IndexedRasterizer.max_safe_pixels:,} pixels")
                render_target = QRectF(
                    0.0, 0.0, float(target_width), float(target_height))
                use_view_cache = not clean
                cache_key = (
                    self._indexed_view_cache_signature(
                        target_width, target_height, camera)
                    if use_view_cache else None
                )
                if (use_view_cache
                        and cache_key == self._indexed_view_cache_key
                        and not self._indexed_view_cache_image.isNull()):
                    indexed_image = QImage(self._indexed_view_cache_image)
                else:
                    indexed_image = self._render_indexed_model(
                        render_target, ordered, camera,
                        collect_diagnostics=clean)
                    self._last_indexed_stats.update({
                        "viewport_target_width": target_width,
                        "viewport_target_height": target_height,
                        "viewport_render_width": target_width,
                        "viewport_render_height": target_height,
                    })
                    if use_view_cache:
                        self._indexed_view_cache_key = cache_key
                        self._indexed_view_cache_image = QImage(
                            indexed_image)
            except Exception as exc:
                self._indexed_runtime_error = str(exc)
                self._last_indexed_stats = {}
                self._last_render_error = str(exc)
                self._last_effective_renderer = "retail_indexed_error"
                self._indexed_view_cache_key = None
                self._indexed_view_cache_image = QImage()
                if self._indexed_runtime_error != previous_error:
                    self.statusMessage.emit(
                        f"Textured renderer failed closed: {exc}")
            else:
                painter.drawImage(target, indexed_image)
                self._indexed_runtime_error = ""
                self._last_render_error = ""
                self._last_effective_renderer = (
                    "retail_indexed_reconstructed")

        for piece in ordered:
            payload = piece.payload
            draw_piece(
                payload.face, piece.vertices, piece.attributes,
                payload.front_facing,
                draw_surface=mode not in ("textured", "wireframe"))

        if mode == "wireframe":
            # Keep SKLT presentation as source-polygon outlines rather than
            # exposing runtime fan/BSP split edges.  Visibility and clipping
            # still come from the shared Retail geometry pass above.
            for face in self._faces:
                polygon = wireframe_polygons.get(id(face))
                if polygon is None or id(face) not in visible_faces:
                    continue
                self._draw_face(
                    painter, face, list(polygon),
                    mode="wireframe", draw_wire=False)

        if mode == "textured" and self._last_render_error and not clean:
            painter.save()
            painter.setPen(QColor(255, 185, 120))
            painter.drawText(
                target.adjusted(24, 24, -24, -24),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Textured renderer unavailable\n" + self._last_render_error,
            )
            painter.restore()

        if not clean and highlight_paths:
            painter.setPen(QPen(QColor(90, 230, 255), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for path in highlight_paths.values():
                painter.drawPath(path.simplified())

        if not clean and self._show_wire_overlay and mode == "wireframe":
            painter.setPen(QPen(QColor(0, 0, 0, 130), 0.75))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for face in self._faces:
                polygon = source_polygons.get(id(face))
                if polygon is not None and id(face) in visible_faces:
                    painter.drawPolygon(polygon)

        if not clean and not self._snapshot_active \
                and self._selected_poly is not None:
            selected_face = next(
                (face for face in self._faces
                 if face.primary and face.poly_id == self._selected_poly
                 and id(face) in visible_faces),
                None,
            )
            if selected_face is not None:
                selected_shape = source_polygons.get(id(selected_face))

        if not clean:
            self._pick_shapes = pick_shapes
            if self._edit_session is not None:
                pan = camera["pan"]
                visibility_view_key = (
                    target.x(), target.y(), target.width(), target.height(),
                    camera["yaw"], camera["pitch"], camera["zoom"],
                    pan.x(), pan.y(),
                    tuple(camera["center"]), camera["scale"],
                    len(pick_shapes),
                )
                if not self._edit_visibility_ready \
                        or self._edit_visibility_view_key \
                        != visibility_view_key:
                    self._refresh_edit_vertex_visibility(
                        self._edit_screen_points(target, camera), target,
                        visibility_view_key)
            else:
                self._edit_visible_vertices.clear()
                self._edit_visibility_ready = False
                self._edit_visibility_view_key = None

        if selected_shape is not None:
            painter.setPen(QPen(QColor(255, 255, 255), 2.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(selected_shape)
            painter.setPen(QPen(QColor(40, 220, 255), 1.2,
                                Qt.PenStyle.DashLine))
            painter.drawPolygon(selected_shape)

        if not clean and self._paste_preview is not None:
            self._draw_paste_preview(painter, target, camera)

        if not clean and self._precision_bbox_points and (
                self._paste_preview is not None
                or self._modal_op in ("grab", "scale")
                or self._model_nudge_before is not None):
            self._draw_precision_guides(painter, target, camera)

        if not clean and self._show_sen:
            self._draw_sen(painter, target, camera)

        if not clean and self._mode == "textured" and self._diagnostics \
                and self._show_diag_overlay:
            self._draw_diagnostics_overlay(painter)

        if not clean and not self._snapshot_active \
                and self._edit_session is not None:
            self._draw_edit_overlay(painter, target, camera)
        elif not clean and not self._snapshot_active:
            self._draw_mode_label(painter, target, False)

    def _render_indexed_model(
            self, target: QRectF, ordered, camera: dict, *,
            collect_diagnostics: bool = True) -> QImage:
        """Render BSP pieces through the sole palette-index textured backend."""

        adapter = self._indexed_adapter
        if adapter is None:
            raise RuntimeError(self._indexed_unavailable_reason)
        unmapped = [face for face in self._faces if not face.mapped]
        if unmapped:
            examples = ", ".join(
                f"{face.owner} polygon {face.poly_id}"
                for face in unmapped[:4])
            more = (
                f" (+{len(unmapped) - 4} more)"
                if len(unmapped) > 4 else "")
            raise RuntimeError(
                "retail indexed rendering requires complete ATTS material "
                f"mappings; found {len(unmapped)} unmapped polygon(s): "
                f"{examples}{more}")
        width = max(1, int(round(target.width())))
        height = max(1, int(round(target.height())))
        indexed_pieces = []
        distance_scale = 1.0 / max(
            abs(float(camera.get("scale", 1.0))), 1e-12)
        for piece in ordered:
            payload = piece.payload
            face = payload.face
            material = self._materials[face.material]
            frame_index, _direction = self._anim_states.get(
                face.material, (0, 1))
            surface = adapter.resolve_surface(face, material, frame_index)
            if surface.kind == "texture" and not payload.uv_mapping_valid:
                raise UnsupportedIndexedMaterialError(
                    f"polygon {face.poly_id} has incomplete source UV mapping")
            screen = tuple(
                (point.x() - target.left(), point.y() - target.top())
                for point in (
                    self._project(vertex, target, camera)
                    for vertex in piece.vertices
                )
            )
            fade_profile = adapter.distance_fade_profile(
                face, self._retail_distance_fade_enabled)
            vertex_distances = ()
            if fade_profile is not None:
                vertex_distances = tuple(
                    math.sqrt(
                        vertex[0] * vertex[0]
                        + vertex[1] * vertex[1]
                        + (_PICK_CAMERA_DISTANCE - vertex[2])
                        * (_PICK_CAMERA_DISTANCE - vertex[2]))
                    * distance_scale
                    for vertex in piece.vertices)
            indexed_pieces.append(IndexedPiece(
                source_face_id=id(face),
                polygon_id=face.poly_id,
                screen=screen,
                uvs=tuple(tuple(uv) for uv in piece.attributes),
                camera_vertices=tuple(tuple(vertex) for vertex in piece.vertices),
                surface=surface,
                source_order=payload.source_order,
                sort_depth=payload.sort_depth,
                distance_fade=fade_profile is not None,
                fade_start=(fade_profile["fade_start"]
                            if fade_profile is not None else 0.0),
                fade_length=(fade_profile["fade_length"]
                             if fade_profile is not None
                             else VANILLA_FADE_LENGTH),
                vertex_distances=vertex_distances,
            ))
        result = IndexedRasterizer.render(
            width, height, indexed_pieces, adapter.tables, background_index=0,
            collect_diagnostics=collect_diagnostics,
            track_polygon_owner=False)
        self._last_indexed_stats = dict(result.stats)
        rgba = result.to_rgba(adapter.tables, transparent_background=True)
        image = QImage(
            rgba, width, height, width * 4, QImage.Format.Format_RGBA8888)
        return image.copy()

    def _draw_edit_overlay(
            self, painter: QPainter, target: QRectF,
            camera: dict) -> None:
        session = self._edit_session
        if session is None:
            return
        screen = self._edit_screen_points(target, camera)
        available = self._available_edit_vertices(screen)

        # Constraint guide: model axis through the pivot, in the axis color.
        if self._modal_op is not None and self._modal_axis is not None:
            axis_world = session.model_axis_world(self._modal_axis)
            if axis_world is not None:
                length = 2.0 / max(self._scale, 1e-9)
                px, py, pz = self._modal_pivot_world
                a = self._project(self._camera_vertex((
                    px - axis_world[0] * length,
                    py - axis_world[1] * length,
                    pz - axis_world[2] * length), camera), target, camera)
                b = self._project(self._camera_vertex((
                    px + axis_world[0] * length,
                    py + axis_world[1] * length,
                    pz + axis_world[2] * length), camera), target, camera)
                color = {"X": QColor(240, 100, 100),
                         "Y": QColor(110, 230, 110),
                         "Z": QColor(110, 160, 250)}[self._modal_axis]
                painter.setPen(QPen(color, 1.2, Qt.PenStyle.DashLine))
                painter.drawLine(a, b)

        painter.setPen(QPen(QColor(20, 22, 26), 1.0))
        painter.setBrush(QColor(208, 212, 220))
        for index, point in enumerate(screen):
            if index in session.selection or index not in available:
                continue
            painter.drawRect(QRectF(point.x() - 2.5, point.y() - 2.5,
                                    5.0, 5.0))
        painter.setPen(QPen(QColor(90, 92, 100), 1.0))
        painter.setBrush(QColor(105, 108, 118))
        for index in sorted(
                self._edit_read_only_vertices
                & self._edit_visible_vertices):
            if index < len(screen):
                point = screen[index]
                painter.drawRect(QRectF(
                    point.x() - 3.0, point.y() - 3.0, 6.0, 6.0))
        mode_color = {
            "move": QColor(255, 32, 48),
            "rotate": QColor(70, 130, 255),
            "scale": QColor(62, 205, 105),
        }[self._direct_transform_mode]
        painter.setPen(QPen(mode_color.lighter(165), 1.8))
        painter.setBrush(mode_color)
        for index in session.selection:
            if index < len(screen):
                point = screen[index]
                radius = 5.5 if index == self._active_edit_vertex else 4.5
                painter.drawRect(QRectF(
                    point.x() - radius, point.y() - radius,
                    radius * 2.0, radius * 2.0))

        pivot = session.selection_pivot()
        if pivot is not None:
            world = _apply(session.matrix, pivot, session.position)
            center = self._project(self._camera_vertex(world, camera),
                                   target, camera)
            painter.setPen(QPen(QColor(255, 255, 255), 1.2))
            painter.drawLine(QPointF(center.x() - 6, center.y()),
                             QPointF(center.x() + 6, center.y()))
            painter.drawLine(QPointF(center.x(), center.y() - 6),
                             QPointF(center.x(), center.y() + 6))

        if self._box_rect is not None:
            painter.setPen(QPen(QColor(90, 230, 255), 1.0,
                                Qt.PenStyle.DashLine))
            painter.setBrush(QColor(90, 230, 255, 40))
            scale_x = target.width() / max(1, self.width())
            scale_y = target.height() / max(1, self.height())
            box = QRectF(
                target.left() + self._box_rect.left() * scale_x,
                target.top() + self._box_rect.top() * scale_y,
                self._box_rect.width() * scale_x,
                self._box_rect.height() * scale_y)
            painter.drawRect(box)

        self._draw_mode_label(painter, target, True)

    def _draw_paste_preview(self, painter: QPainter, target: QRectF,
                            camera: dict) -> None:
        state = self._paste_preview
        session = self._edit_session
        if state is None or session is None:
            return
        delta = state.delta_model
        model_points = [
            tuple(point[axis] + delta[axis] for axis in range(3))
            for point in state.clipboard.points
        ]
        world_points = [
            _apply(session.matrix, point, session.position)
            for point in model_points
        ]
        painter.save()
        painter.setOpacity(PASTE_GHOST_OPACITY)
        for copied, material_id in zip(
                state.clipboard.polygons, state.material_ids):
            camera_points = [
                self._camera_vertex(world_points[index], camera)
                for index in copied.local_indices
            ]
            screen = [
                self._project(point, target, camera)
                for point in camera_points
            ]
            ghost = ViewFace(
                vertices=[],
                uvs=list(copied.uvs),
                material=material_id,
                shade=copied.shade_val,
            )
            self._draw_face(
                painter, ghost, screen, mode="materials", draw_wire=False)
            painter.setPen(QPen(QColor(255, 235, 95, 225), 1.2,
                                Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(QPolygonF(screen))
        painter.restore()

        source = state.source_pivot_world
        destination_model = tuple(
            state.clipboard.pivot[axis] + delta[axis] for axis in range(3))
        destination = _apply(
            session.matrix, destination_model, session.position)
        source_screen = self._project(
            self._camera_vertex(source, camera), target, camera)
        destination_screen = self._project(
            self._camera_vertex(destination, camera), target, camera)
        painter.setPen(QPen(QColor(245, 245, 245, 190), 1.0,
                            Qt.PenStyle.DashLine))
        painter.drawLine(source_screen, destination_screen)
        for point, color in (
                (source_screen, QColor(255, 255, 255)),
                (destination_screen, QColor(255, 235, 95))):
            painter.setPen(QPen(color, 1.4))
            painter.drawLine(point + QPointF(-6, 0), point + QPointF(6, 0))
            painter.drawLine(point + QPointF(0, -6), point + QPointF(0, 6))

        dx, dy, dz = delta
        mode = f"Axis {state.axis}" if state.axis else "View Plane"
        label = (
            f"Paste {getattr(state.clipboard, 'fx_name')} FX Preview"
            if getattr(state.clipboard, "fx_name", None)
            else "Copy Preview")
        lines = [
            label,
            f"X: {dx:+.2f}",
            f"Y: {dy:+.2f}",
            f"Z: {dz:+.2f}",
            f"Mode: {mode}",
            "Paste/LMB/Enter Confirm | RMB/Esc Cancel",
        ]
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 12
        x = int(target.left()) + 8
        y = int(target.bottom()) - height - 8
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 210))
        painter.drawRect(x, y, width, height)
        painter.setPen(QColor(235, 238, 244))
        baseline = y + metrics.ascent() + 6
        for line in lines:
            painter.drawText(x + 8, baseline, line)
            baseline += metrics.height()

    def _draw_precision_guides(self, painter: QPainter, target: QRectF,
                               camera: dict) -> None:
        session = self._edit_session
        points = self._precision_bbox_points
        if session is None or not points:
            return
        bounds = self._bounds_features(points)
        mins = tuple(bounds[axis][0] for axis in range(3))
        maxs = tuple(bounds[axis][2] for axis in range(3))
        pivot = tuple(bounds[axis][1] for axis in range(3))
        corners_model = [
            (x, y, z)
            for x in (mins[0], maxs[0])
            for y in (mins[1], maxs[1])
            for z in (mins[2], maxs[2])
        ]
        corners = [
            self._project(
                self._camera_vertex(
                    _apply(session.matrix, point, session.position), camera),
                target, camera)
            for point in corners_model
        ]
        edges = (
            (0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3),
            (2, 6), (3, 7), (4, 5), (4, 6), (5, 7), (6, 7),
        )
        painter.setPen(QPen(QColor(255, 222, 105, 175), 1.0,
                            Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for first, second in edges:
            painter.drawLine(corners[first], corners[second])

        colors = (
            QColor(240, 90, 90, 190),
            QColor(90, 220, 110, 190),
            QColor(80, 145, 250, 190),
        )
        highlighted = {guide.axis for guide in self._precision_guides}
        span = max(
            maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2],
            1.5 / max(self._scale, 1e-9))
        for axis in range(3):
            start = list(pivot)
            end = list(pivot)
            start[axis] -= span
            end[axis] += span
            a = self._project(self._camera_vertex(
                _apply(session.matrix, start, session.position), camera),
                target, camera)
            b = self._project(self._camera_vertex(
                _apply(session.matrix, end, session.position), camera),
                target, camera)
            painter.setPen(QPen(
                colors[axis], 2.2 if axis in highlighted else 1.0,
                Qt.PenStyle.DashLine))
            painter.drawLine(a, b)
        for guide in self._precision_guides:
            marker_model = list(pivot)
            marker_model[guide.axis] = guide.value
            marker = self._project(self._camera_vertex(
                _apply(session.matrix, marker_model, session.position),
                camera), target, camera)
            painter.setPen(QPen(QColor(45, 255, 95), 2.0))
            painter.drawLine(marker + QPointF(-6, 0),
                             marker + QPointF(6, 0))
            painter.drawLine(marker + QPointF(0, -6),
                             marker + QPointF(0, 6))

    def _draw_mode_label(self, painter: QPainter, target: QRectF,
                         editing: bool) -> None:
        label = "Edit Mode" if editing else "View Mode"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label) + 16
        height = metrics.height() + 10
        x = int(target.center().x() - width / 2)
        y = int(target.top()) + 6
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(36, 58, 38, 215) if editing
                         else QColor(30, 32, 38, 205))
        painter.drawRect(x, y, width, height)
        painter.setPen(QColor(170, 240, 170) if editing
                       else QColor(255, 255, 255))
        painter.drawText(x + 8, y + metrics.ascent() + 5, label)
        if (int(target.left()), int(target.top()), int(target.width()),
                int(target.height())) == (0, 0, self.width(), self.height()):
            self._mode_label_rect = QRect(x, y, width, height)

    def _draw_diagnostics_overlay(self, painter: QPainter) -> None:
        # Compact badge: first two issues only; the full list lives in the
        # bottom Warnings panel (View menu can hide this overlay entirely).
        lines = [f"Preview issues ({len(self._diagnostics)}):"]
        lines.extend(f"- {d[:80]}" for d in self._diagnostics[:2])
        if len(self._diagnostics) > 2:
            lines.append(f"... +{len(self._diagnostics) - 2} more "
                         "(see Warnings)")
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(line) for line in lines) + 16
        height = metrics.height() * len(lines) + 12
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(40, 20, 20, 200))
        painter.drawRect(8, 8, width, height)
        painter.setPen(QColor(255, 190, 120))
        y = 8 + metrics.ascent() + 6
        for line in lines:
            painter.drawText(16, y, line)
            y += metrics.height()

    def _face_brightness(self, face: ViewFace) -> float:
        # CONFIRMED: brightness = clamp(1 - shade/256) (amesh.cpp GenMesh)
        value = 1.0 - face.shade / 256.0
        return max(0.15, min(1.0, value))  # 0.15 floor for visibility

    def _draw_face(self, painter: QPainter, face: ViewFace,
                   screen: list[QPointF], mode: str | None = None,
                   draw_wire: bool | None = None) -> None:
        polygon = QPolygonF(screen)
        mat = self._materials[face.material]
        mode = mode or self._mode
        draw_wire = self._show_wire_overlay if draw_wire is None else draw_wire

        if mode == "wireframe":
            painter.setPen(QPen(QColor(112, 210, 255), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(polygon)
            return

        if mode == "solid":
            level = int(210 * self._face_brightness(face))
            painter.setPen(QPen(QColor(30, 32, 38), 0.5))
            painter.setBrush(QColor(level, level, level))
            painter.drawPolygon(polygon)
        elif mode == "materials":
            brightness = self._face_brightness(face)
            color = QColor(int(mat.color.red() * brightness),
                           int(mat.color.green() * brightness),
                           int(mat.color.blue() * brightness))
            painter.setPen(QPen(QColor(30, 32, 38), 0.5))
            painter.setBrush(color)
            painter.drawPolygon(polygon)
        else:
            # Textured surfaces are drawn once, before overlays, by the shared
            # Retail-indexed framebuffer pass in ``_render_scene``.
            return

        if draw_wire:
            painter.setPen(QPen(QColor(0, 0, 0, 130), 0.75))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(polygon)

    def _face_uvs(self, face: ViewFace, mat: ViewMaterial):
        """Return texture-byte UVs for the currently active VANM frame."""

        if mat.anim_frames:
            frame_index, _ = self._anim_states.get(face.material, (0, 1))
            _, _image_id, uv_id = mat.anim_frames[frame_index]
            group = (mat.anim_uv_groups[uv_id]
                     if 0 <= uv_id < len(mat.anim_uv_groups) else [])
            # Runtime remaps animated UVs by vertex order index (TexCoordId=j,
            # base.cpp GenerateMeshCoordsCache, CONFIRMED).
            uvs = [group[j] if j < len(group) else (0, 0)
                   for j in range(len(face.vertices))]
            return uvs
        return face.uvs

    def _draw_sen(self, painter: QPainter, target: QRectF,
                  camera: dict) -> None:
        painter.setPen(QPen(QColor(255, 170, 60, 200), 1.4,
                            Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for box in self._sen_boxes:
            pts = [self._project(self._camera_vertex(p, camera),
                                 target, camera) for p in box]
            if len(pts) == 8:
                edges = [(0, 1), (1, 3), (3, 2), (2, 0),
                         (4, 5), (5, 7), (7, 6), (6, 4),
                         (0, 4), (1, 5), (2, 6), (3, 7)]
                for a, b in edges:
                    painter.drawLine(pts[a], pts[b])
            else:
                for point in pts:
                    painter.drawEllipse(point, 3.0, 3.0)
        # The orange volume is self-explanatory.  The old bottom-left label
        # covered textured FX and fought with the status bar, so keep the
        # viewport clean and expose the feature through View > SEN2 volume.

    def _draw_axes(self, painter: QPainter, target: QRectF,
                   camera: dict) -> None:
        compact = target.width() < 220 or target.height() < 180
        anchor = QPointF(
            target.right() - (45 if compact else 62),
            target.top() + 45 if compact else target.bottom() - 55)
        length = 25.0 if compact else 34.0
        axes = [
            ((1, 0, 0), QColor(240, 100, 100), "X"),
            ((0, -1, 0), QColor(110, 230, 110), "-Y"),
            ((0, 0, 1), QColor(110, 160, 250), "Z"),
        ]
        yaw = math.radians(camera["yaw"])
        pitch = math.radians(camera["pitch"])
        projected = []
        for direction, color, label in axes:
            x, y, z = direction[0], -direction[1], direction[2]
            xz_x = x * math.cos(yaw) + z * math.sin(yaw)
            xz_z = -x * math.sin(yaw) + z * math.cos(yaw)
            camera_y = y * math.cos(pitch) - xz_z * math.sin(pitch)
            camera_z = y * math.sin(pitch) + xz_z * math.cos(pitch)
            projected.append((
                camera_z,
                QPointF(anchor.x() + xz_x * length,
                        anchor.y() - camera_y * length),
                color, label))
        for _depth, endpoint, color, label in sorted(
                projected, key=lambda item: item[0]):
            painter.setPen(QPen(color, 2.0))
            painter.drawLine(anchor, endpoint)
            painter.setBrush(color)
            painter.drawEllipse(endpoint, 2.5, 2.5)
            painter.drawText(endpoint + QPointF(4, -3), label)
        painter.setPen(QPen(QColor(225, 228, 235), 1.0))
        painter.setBrush(QColor(65, 70, 82, 230))
        painter.drawEllipse(anchor, 4.0, 4.0)

    def _draw_grid(self, painter: QPainter, target: QRectF,
                   camera: dict) -> None:
        painter.setPen(QPen(QColor(55, 60, 70), 0.8))
        steps = 8
        size = 1.4 / self._scale
        for i in range(-steps, steps + 1):
            offset = i * size / steps
            for start, end in (
                ((offset, 0, -size), (offset, 0, size)),
                ((-size, 0, offset), (size, 0, offset)),
            ):
                a = self._project(self._camera_vertex((
                    self._center[0] + start[0], self._center[1] + start[1],
                    self._center[2] + start[2]), camera), target, camera)
                b = self._project(self._camera_vertex((
                    self._center[0] + end[0], self._center[1] + end[1],
                    self._center[2] + end[2]), camera), target, camera)
                painter.drawLine(a, b)

    # -- interaction ------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._camera_interacting = False
        pos = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_additive = bool(
                event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if self._paste_preview is not None:
            if event.button() == Qt.MouseButton.LeftButton \
                    and event.modifiers() & Qt.KeyboardModifier.AltModifier:
                self._paste_preview.camera_inspect = True
                self._last_mouse = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.MiddleButton:
                self._paste_preview.camera_inspect = True
                self._last_mouse = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                self.pastePreviewConfirmRequested.emit()
            elif event.button() == Qt.MouseButton.RightButton:
                self._suppress_next_context_menu = True
                self.cancel_paste_preview()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._mode_label_rect is not None \
                and self._mode_label_rect.contains(pos) \
                and self._family_ref is not None \
                and not self._snapshot_active:
            self.toggle_edit_mode()
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._modal_op is not None:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._commit_modal()
                elif event.button() == Qt.MouseButton.RightButton:
                    self._cancel_modal()
                event.accept()
                return
            if self._edit_session.modal_active:
                # A live scale preview owns the geometry, but the viewport
                # remains free for camera inspection until the dialog closes.
                self._last_mouse = pos
                self._press_pos = pos
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                vertex = self._nearest_edit_vertex(pos)
                additive = bool(event.modifiers()
                                & Qt.KeyboardModifier.ControlModifier)
                if additive:
                    self._marquee_candidate = True
                    self._last_mouse = pos
                    self._press_pos = pos
                    event.accept()
                    return
                if vertex is not None:
                    selection = set(self._edit_session.selection)
                    self._vertex_press = VertexPressState(
                        vertex=vertex,
                        position=QPoint(pos),
                        selection=selection,
                        was_selected=vertex in selection,
                    )
                    self._active_edit_vertex = vertex
                    self._last_mouse = pos
                    self._press_pos = pos
                    event.accept()
                    return
        self._last_mouse = pos
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = self._last_mouse
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        was_camera_interacting = self._camera_interacting
        self._camera_interacting = False
        if was_camera_interacting:
            self.update()
        if self._paste_preview is not None:
            self._paste_preview.camera_inspect = False
            self._reanchor_paste_mouse(event.position().toPoint())
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._edit_session.modal_active and self._modal_op is None:
                self._press_pos = None
                self._press_additive = False
                event.accept()
                return
            if self._direct_grab_active \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._commit_modal()
                self._vertex_press = None
                self._press_pos = None
                self._press_additive = False
                event.accept()
                return
            if self._vertex_press is not None \
                    and event.button() == Qt.MouseButton.LeftButton:
                pressed = self._vertex_press
                self._vertex_press = None
                self._edit_session.selection = {pressed.vertex}
                self._active_edit_vertex = pressed.vertex
                self._emit_selection_hint("vertex")
                self._press_pos = None
                self._press_additive = False
                self.update()
                event.accept()
                return
            additive = self._press_additive
            if self._box_start is not None \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._apply_box_select(additive)
                self._marquee_candidate = False
                self._press_additive = False
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton \
                    and self._press_pos is not None:
                delta = event.position().toPoint() - self._press_pos
                if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                    point = event.position().toPoint()
                    nearest = self._nearest_edit_vertex(point)
                    if self._edit_pick_polygons \
                            and (nearest is None
                                 or (self._direct_grab_selected_only
                                     and nearest not in
                                     self._edit_session.selection)):
                        self.pick_at(point, additive)
                    else:
                        self._pick_edit_vertex(point, additive)
            self._press_pos = None
            self._marquee_candidate = False
            self._press_additive = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._press_pos is not None and not self._snapshot_active:
            delta = event.position().toPoint() - self._press_pos
            if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                additive = self._press_additive
                self.pick_at(event.position().toPoint(), additive)
        self._press_pos = None
        self._press_additive = False
        event.accept()

    def pick_at(self, point: QPoint, additive: bool = False) -> int | None:
        """Select the geometrically nearest rendered polygon under the cursor."""

        if self._edit_session is None or self._snapshot_active:
            return None
        hit = resolve_pick_shape(self._pick_shapes, QPointF(point))
        if hit is not None:
            face = hit.face
            self._selected_owner = face.owner
            self.objectPicked.emit(face.owner)
            if face.primary:
                self._selected_poly = face.poly_id
                self.polygonStructurePicked.emit(
                    face.owner, face.poly_id, face.block_index)
                self.polygonPicked.emit(face.poly_id)
                self.polygonPickedDetailed.emit(face.poly_id, additive)
            self.update()
            return face.poly_id if face.primary else None
        return None

    def event(self, ev) -> bool:  # noqa: N802 - Qt override
        # Tab toggles Edit Mode; it must be caught before Qt's focus chain.
        if ev.type() == QEvent.Type.KeyPress \
                and ev.key() == Qt.Key.Key_Tab \
                and self._family_ref is not None and not self._snapshot_active:
            if self._paste_preview is not None:
                self.statusMessage.emit(
                    "Finish or cancel Copy Preview before leaving Edit Mode.")
                return True
            self.toggle_edit_mode()
            return True
        return super().event(ev)

    def _edit_key_press(self, event) -> bool:
        session = self._edit_session
        if session is None:
            return False
        key = event.key()
        mods = event.modifiers()

        if self._modal_op is not None:
            if key == Qt.Key.Key_Escape:
                self._cancel_modal()
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._commit_modal()
            elif key in (Qt.Key.Key_X, Qt.Key.Key_Y, Qt.Key.Key_Z):
                name = {Qt.Key.Key_X: "X", Qt.Key.Key_Y: "Y",
                        Qt.Key.Key_Z: "Z"}[key]
                self._modal_axis = None if self._modal_axis == name else name
                self._update_modal(self._modal_last_mouse)
            elif key == Qt.Key.Key_Backspace:
                self._modal_numeric = self._modal_numeric[:-1]
                self._update_modal(self._modal_last_mouse)
            else:
                text = event.text()
                if text and (text.isdigit() or text in ".-"):
                    self._modal_numeric += text
                    self._update_modal(self._modal_last_mouse)
            return True

        if key == Qt.Key.Key_A:
            if mods & Qt.KeyboardModifier.AltModifier:
                session.select_none()
            else:
                session.select_all()
            self._emit_selection_hint("vertex")
            self.update()
            return True
        if key == Qt.Key.Key_G:
            self._begin_modal("grab")
            return True
        if key == Qt.Key.Key_R:
            self._begin_modal("rotate")
            return True
        if key == Qt.Key.Key_S:
            self._begin_modal("scale")
            return True
        if key == Qt.Key.Key_Z \
                and mods & Qt.KeyboardModifier.ControlModifier:
            if mods & Qt.KeyboardModifier.ShiftModifier:
                self.redoRequested.emit()
            else:
                self.undoRequested.emit()
            return True
        if key == Qt.Key.Key_Y \
                and mods & Qt.KeyboardModifier.ControlModifier:
            self.redoRequested.emit()
            return True
        return False

    def _paste_key_press(self, event) -> bool:
        state = self._paste_preview
        if state is None:
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.cancel_paste_preview()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.pastePreviewConfirmRequested.emit()
        elif key in (Qt.Key.Key_X, Qt.Key.Key_Y, Qt.Key.Key_Z):
            name = {Qt.Key.Key_X: "X", Qt.Key.Key_Y: "Y",
                    Qt.Key.Key_Z: "Z"}[key]
            state.axis = None if state.axis == name else name
            state.numeric = ""
            self._reanchor_paste_mouse(state.last_mouse)
            self._emit_paste_hint()
            self.update()
        elif key == Qt.Key.Key_G:
            state.axis = None
            state.numeric = ""
            self._reanchor_paste_mouse(state.last_mouse)
            self._emit_paste_hint()
            self.update()
        elif key == Qt.Key.Key_Backspace:
            state.numeric = state.numeric[:-1]
            self._update_paste_preview(state.last_mouse)
        else:
            text = event.text()
            if text and (text.isdigit() or text in ".-"):
                state.numeric += text
                self._update_paste_preview(state.last_mouse)
            else:
                return False
        return True

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._paste_key_press(event):
            return
        if not self._snapshot_active and self._edit_key_press(event):
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        current = event.position().toPoint()
        if self._paste_preview is not None:
            state = self._paste_preview
            if state.camera_inspect:
                delta = current - self._last_mouse
                self._last_mouse = current
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self._yaw += delta.x() * 0.6
                    self._pitch = max(
                        -89.0, min(89.0, self._pitch + delta.y() * 0.6))
                elif event.buttons() & Qt.MouseButton.MiddleButton:
                    self._pan += QPointF(delta.x(), delta.y())
                self._reanchor_paste_mouse(current)
                self.manualCameraChanged.emit()
                self.update()
            elif not event.buttons():
                self._update_paste_preview(current)
            event.accept()
            return
        if self._edit_session is not None and not self._snapshot_active:
            if self._modal_op is not None:
                self._modal_last_mouse = current
                self._last_mouse = current
                self._update_modal(current)
                event.accept()
                return
            if self._box_start is not None:
                self._box_rect = QRect(self._box_start, current).normalized()
                self._last_mouse = current
                self.update()
                event.accept()
                return
            if self._vertex_press is not None \
                    and event.buttons() & Qt.MouseButton.LeftButton:
                pressed = self._vertex_press
                delta = current - pressed.position
                if math.hypot(delta.x(), delta.y()) >= 5.0:
                    if pressed.was_selected:
                        self._edit_session.selection = set(pressed.selection)
                    else:
                        self._edit_session.selection = {pressed.vertex}
                    self._emit_selection_hint("vertex")
                    self._begin_modal(
                        "grab" if self._direct_transform_mode == "move"
                        else self._direct_transform_mode,
                        pressed.position, direct_grab=True)
                    if self._modal_op is not None:
                        self._modal_last_mouse = current
                        self._update_modal(current)
                    self._vertex_press = None
                self._last_mouse = current
                event.accept()
                return
            if self._marquee_candidate \
                    and event.buttons() & Qt.MouseButton.LeftButton \
                    and self._press_pos is not None:
                delta = current - self._press_pos
                if abs(delta.x()) > 4 or abs(delta.y()) > 4:
                    self._box_start = self._press_pos
                    self._box_rect = QRect(
                        self._box_start, current).normalized()
                    self._marquee_candidate = False
                    self._last_mouse = current
                    self.update()
                event.accept()
                return
        delta = current - self._last_mouse
        self._last_mouse = current
        if event.buttons() & Qt.MouseButton.LeftButton:
            old_yaw = self._yaw
            old_pitch = self._pitch
            self._yaw += delta.x() * 0.6
            self._pitch = max(-89.0, min(89.0, self._pitch + delta.y() * 0.6))
            if self._yaw != old_yaw or self._pitch != old_pitch:
                self._camera_interacting = True
                self.manualCameraChanged.emit()
            self.update()
        elif event.buttons() & (Qt.MouseButton.RightButton
                                | Qt.MouseButton.MiddleButton):
            if not delta.isNull():
                self._camera_interacting = True
                self._pan += QPointF(delta.x(), delta.y())
                self.manualCameraChanged.emit()
            self.update()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = math.pow(1.0015, event.angleDelta().y())
        old_zoom = self._zoom
        self._zoom = max(0.08, min(30.0, self._zoom * factor))
        if self._zoom != old_zoom:
            self.manualCameraChanged.emit()
            self._reanchor_paste_mouse(
                event.position().toPoint()
                if self._paste_preview is not None else None)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.deselect_at(event.position().toPoint())
        event.accept()
