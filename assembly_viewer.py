"""Software-rendered 3D preview widget for assembled Urban Assault assets.

Renders the polygons of an :class:`asset_family.AssetFamily` with QPainter:
painter's-algorithm depth sort, optional back-face culling, and per-triangle
affine texture mapping for the textured mode.  This is an approximate preview
renderer (affine, not perspective-correct texture interpolation), clearly
labelled as such in the UI.

View modes:
  - wireframe        outline of every polygon
  - solid            flat shading from the ATTS shade value (CONFIRMED
                     formula: brightness = 1 - shade/256, amesh.cpp)
  - materials        each texture/material block gets a distinct color
  - textured         textured preview using OLPL UVs (u/256)

Extras: SEN2 bounding/culling volume overlay (read-only), axes, grid,
VANM texture animation playback (play/pause, loop or ping-pong).

UA model space uses negative Y as "up"; the widget flips Y for display only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from asset_family import AssetFamily, FamilyObject
from geometry_editor import AXIS_VECTORS, GeometryEditSession, mat_apply

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


@dataclass
class ViewMaterial:
    label: str
    kind: str = ""                  # "ilbm" | "bmpanim" | other
    color: QColor = field(default_factory=lambda: QColor(150, 150, 150))
    image: QImage | None = None     # static texture (ARGB32, chroma applied)
    anim_images: list[QImage] = field(default_factory=list)
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
    """Load an OpenUA HI/ALPHA effect PNG with engine chroma handling."""

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

    statusMessage = Signal(str)
    polygonPicked = Signal(int)     # poly_id of the primary skeleton object
    objectPicked = Signal(str)      # owner_path of the clicked object
    editModeChanged = Signal(bool)  # geometry Edit Mode entered / left
    geometryEdited = Signal(str)    # owner_path whose vertices changed
    editHint = Signal(str)          # live hint text for the active tool
    animationFrameChanged = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Keep the viewport comfortably usable, but do not force the whole
        # application beyond the available desktop height when optional
        # panels (for example Diagnostics) are opened.
        self.setMinimumSize(420, 260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._faces: list[ViewFace] = []
        self._materials: list[ViewMaterial] = []
        self._sen_boxes: list[list[tuple[float, float, float]]] = []
        self._diagnostics: list[str] = []
        self._center = (0.0, 0.0, 0.0)
        self._scale = 1.0

        # Polygon Mapping Workbench state
        self._selected_poly: int | None = None
        self._mapping_diagnostics = False
        self._highlight_polys: set[int] = set()
        self._duplicate_polys: set[int] = set()
        self._pick_shapes: list[tuple[QPolygonF, ViewFace]] = []
        self._press_pos: QPoint | None = None

        # Object selection / isolation (Blender-style)
        self._family_ref = None
        self._visible_owners: set[str] | None = None   # None = all visible
        self._selected_owner: str | None = None
        self._primary_owner: str | None = None
        self._owner_bounds: dict[str, tuple] = {}
        self._show_diag_overlay = True

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
        self._box_armed = False
        self._box_start: QPoint | None = None
        self._box_rect: QRect | None = None

        self._mode = "textured"
        self._show_sen = True
        self._show_axes = True
        self._show_grid = True
        self._show_wire_overlay = True
        self._backface_cull = True

        self._yaw = -35.0
        self._pitch = 20.0
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._last_mouse = QPoint()
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
        if self._edit_session is not None:
            self._edit_session.cancel_modal()
            self._edit_session = None
            self._edit_owner = None
            self.editModeChanged.emit(False)
        self._edit_faces = []
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._box_armed = False
        self._box_start = None
        self._box_rect = None
        self._faces = []
        self._materials = []
        self._sen_boxes = []
        self._diagnostics = []
        self._selected_poly = None
        self._highlight_polys = set()
        self._duplicate_polys = set()
        self._pick_shapes = []
        self._owner_bounds = {}
        self._selected_owner = None
        self.stop_animation()
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
                    primary_owner: str | None = None) -> None:
        selected = self._selected_owner
        self.clear()
        self._family_ref = family
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
        self.load_family(self._family_ref, owners, keep_camera=True,
                         primary_owner=self._primary_owner)
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

    def toggle_edit_mode(self) -> bool:
        if self._snapshot_active:
            return self._edit_session is not None
        if self._edit_session is not None:
            self.exit_edit_mode()
            return False
        return self.enter_edit_mode()

    def enter_edit_mode(self, owner: str | None = None) -> bool:
        """Start vertex editing on ``owner`` (default: the selected object).

        Falls back to the first skeleton-bearing object.  Edits happen in
        model space (raw POO2 coordinates); the point count and the POL2
        topology never change, so the file save path stays byte-safe."""

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
            f"Edit Mode [{target}]: click select | Shift+click add | B box "
            "| A all | Alt+A none | G move | R rotate | S scale | X/Y/Z "
            "axis | Ctrl+Z undo | Tab exit")
        self.update()
        return True

    def exit_edit_mode(self) -> None:
        session = self._edit_session
        if session is None:
            return
        if session.modal_active:
            session.cancel_modal()
            self._refresh_edit_faces()
        self._edit_session = None
        self._edit_owner = None
        self._edit_faces = []
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._box_armed = False
        self._box_start = None
        self._box_rect = None
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

    def _refresh_edit_faces(self) -> None:
        """Push the session's current coordinates into the owner's faces."""

        session = self._edit_session
        if session is None:
            return
        world = session.world_points()
        for face, indices in self._edit_faces:
            face.vertices = [world[i] for i in indices if i < len(world)]
        if self._edit_owner is not None and world:
            xs = [p[0] for p in world]
            ys = [p[1] for p in world]
            zs = [p[2] for p in world]
            self._owner_bounds[self._edit_owner] = (
                min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def _edit_screen_points(self, target: QRectF | None = None,
                            camera: dict | None = None) -> list[QPointF]:
        session = self._edit_session
        if session is None:
            return []
        return [self._project(self._camera_vertex(p, camera), target, camera)
                for p in session.world_points()]

    def _pick_edit_vertex(self, point: QPoint, extend: bool) -> None:
        session = self._edit_session
        if session is None:
            return
        best = None
        best_dist = 12.0 ** 2
        for index, screen in enumerate(self._edit_screen_points()):
            dx = screen.x() - point.x()
            dy = screen.y() - point.y()
            dist = dx * dx + dy * dy
            if dist < best_dist:
                best = index
                best_dist = dist
        if best is None:
            if not extend:
                session.select_none()
        elif extend:
            session.toggle(best)
        else:
            session.selection = {best}
        self._emit_selection_hint()
        self.update()

    def _apply_box_select(self, extend: bool) -> None:
        session = self._edit_session
        rect = self._box_rect
        self._box_armed = False
        self._box_start = None
        self._box_rect = None
        if session is None or rect is None:
            self.update()
            return
        hits = {
            index for index, screen in enumerate(self._edit_screen_points())
            if rect.contains(int(screen.x()), int(screen.y()))
        }
        session.selection = (session.selection | hits) if extend else hits
        self._emit_selection_hint()
        self.update()

    def _emit_selection_hint(self) -> None:
        session = self._edit_session
        if session is None:
            return
        self.statusMessage.emit(
            f"Edit Mode: {len(session.selection)}"
            f"/{len(session.model.points)} vertices selected")

    # -- modal transforms (G / R / S) ------------------------------------------

    def _begin_modal(self, op: str) -> None:
        session = self._edit_session
        if session is None:
            return
        if not session.selection:
            self.statusMessage.emit(
                "Edit Mode: select at least one vertex first "
                "(click, B box select, or A for all).")
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
        cursor = self.mapFromGlobal(QCursor.pos())
        self._modal_start = cursor
        self._modal_last_mouse = cursor
        self._update_modal(cursor)

    def _commit_modal(self) -> None:
        session = self._edit_session
        if session is None or self._modal_op is None:
            return
        changed = session.commit_modal()
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._refresh_edit_faces()
        if changed:
            self.geometryEdited.emit(self._edit_owner or "")
        self.editHint.emit("")
        self.update()

    def _cancel_modal(self) -> None:
        session = self._edit_session
        if session is None:
            return
        session.cancel_modal()
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._refresh_edit_faces()
        self.editHint.emit("")
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

        focal = min(self.width(), self.height()) * 1.5 * self._zoom
        if focal <= 1e-9 or self._scale <= 1e-12:
            return (0.0, 0.0, 0.0)
        dx_cam = dx_px * self._modal_denom / focal
        dy_cam = -dy_px * self._modal_denom / focal
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
                f"d=({dx:.2f}, {dy:.2f}, {dz:.2f}) model units{suffix}")
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
            session.preview_scale(factor, self._modal_axis)
            self.editHint.emit(
                f"Scale [{self._modal_axis or 'uniform'}] "
                f"x{factor:.3f}{suffix}")
        self._refresh_edit_faces()
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

        for group in groups:
            block = group.block
            tracy_key = block.tracy_mode if block is not None else "none"
            key = (f"{group.texture_name}|{group.kind}|{tracy_key}"
                   if group.texture_name else f"__block__{id(group)}")
            mat_id = self._ensure_material(key, group.label, group.kind,
                                           family, material_index,
                                           anim_name=group.texture_name
                                           if group.kind == "bmpanim" else "",
                                           block=block)
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
                for bitmap_name in anm.bitmap_names:
                    img = family.textures.get(bitmap_name)
                    override = (family.effect_override_paths.get(
                        bitmap_name.lower()) if mat.additive else None)
                    qimage = (_image_from_effect_png(override)
                              if override is not None else
                              _image_from_ilbm(
                                  img, family.external_palette
                                  if img and not img.palette else None))
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
            mat.image = (_image_from_effect_png(override)
                         if override is not None else
                         _image_from_ilbm(
                             img, family.external_palette
                             if img and not img.palette else None))

        material_index[key] = len(self._materials)
        self._materials.append(mat)
        return material_index[key]

    # -- public view controls --------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode in VIEW_MODES:
            self._mode = mode
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
        """Show the saved regular overlays in preview and snapshot export."""

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
                           allow_transparent_background=True)
        painter.end()
        return image

    def reset_view(self) -> None:
        self._yaw = -35.0
        self._pitch = 20.0
        self._zoom = 1.0
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
                      allow_transparent_background: bool = False) -> None:
        """Shared QWidget/QImage renderer; ``clean`` draws model pixels only."""

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
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

        # Depth sort (painter's algorithm, mean camera z).  Additive
        # (flat-tracy) faces are drawn in a second pass on top, matching the
        # engine's blended-after-opaque ordering.
        opaque = []
        additive = []
        for face in self._faces:
            if not clean and not face.mapped and not self._mapping_diagnostics:
                continue  # unmapped polys appear only in diagnostics mode
            cam = [self._camera_vertex(v, camera) for v in face.vertices]
            depth = sum(p[2] for p in cam) / len(cam)
            mat = self._materials[face.material]
            mode = "textured" if clean else self._mode
            if mode == "textured" and mat.additive and face.mapped:
                additive.append((depth, face, cam))
            else:
                opaque.append((depth, face, cam))
        opaque.sort(key=lambda item: item[0])
        additive.sort(key=lambda item: item[0])

        pick_shapes = []
        selected_shape = None
        for _, face, cam in opaque + additive:
            screen = [self._project(p, target, camera) for p in cam]
            if self._backface_cull and mode != "wireframe":
                area = 0.0
                for i in range(len(screen)):
                    j = (i + 1) % len(screen)
                    area += (screen[i].x() * screen[j].y()
                             - screen[j].x() * screen[i].y())
                # UA polygons wind clockwise on screen when facing the camera
                if area > 0:
                    continue
            polygon = QPolygonF(screen)
            if not face.mapped and not clean:
                # bright magenta overlay for ATTS coverage holes
                painter.setPen(QPen(QColor(255, 255, 255), 1.0))
                painter.setBrush(QColor(255, 40, 200, 230))
                painter.drawPolygon(polygon)
            else:
                self._draw_face(painter, face, screen, mode=mode,
                                draw_wire=(self._show_wire_overlay
                                           and not clean))
            if not clean and self._mapping_diagnostics and face.mapped \
                    and face.primary and face.poly_id in self._duplicate_polys:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 230, 40, 130))
                painter.drawPolygon(polygon)
            if not clean and face.primary \
                    and face.poly_id in self._highlight_polys:
                painter.setPen(QPen(QColor(90, 230, 255), 1.2))
                painter.setBrush(QColor(90, 230, 255, 90))
                painter.drawPolygon(polygon)
            if not clean and self._selected_owner is not None \
                    and face.owner == self._selected_owner \
                    and self._mode != "textured":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(90, 230, 255, 60))
                painter.drawPolygon(polygon)
            if not clean:
                pick_shapes.append((polygon, face))
            if not clean and face.primary and face.poly_id == self._selected_poly:
                selected_shape = polygon

        if not clean:
            self._pick_shapes = pick_shapes

        if selected_shape is not None:
            painter.setPen(QPen(QColor(255, 255, 255), 2.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(selected_shape)
            painter.setPen(QPen(QColor(40, 220, 255), 1.2,
                                Qt.PenStyle.DashLine))
            painter.drawPolygon(selected_shape)

        if not clean and self._selected_owner is not None:
            self._draw_owner_bbox(painter, self._selected_owner,
                                  target, camera)

        if not clean and self._show_sen:
            self._draw_sen(painter, target, camera)

        if not clean and self._mode == "textured" and self._diagnostics \
                and self._show_diag_overlay:
            self._draw_diagnostics_overlay(painter)

        if not clean and self._edit_session is not None:
            self._draw_edit_overlay(painter, target, camera)

    def _draw_edit_overlay(self, painter: QPainter, target: QRectF,
                           camera: dict) -> None:
        session = self._edit_session
        if session is None:
            return
        screen = self._edit_screen_points(target, camera)

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
            if index in session.selection:
                continue
            painter.drawRect(QRectF(point.x() - 2.5, point.y() - 2.5,
                                    5.0, 5.0))
        painter.setBrush(QColor(255, 160, 50))
        for index in session.selection:
            if index < len(screen):
                point = screen[index]
                painter.drawRect(QRectF(point.x() - 3.5, point.y() - 3.5,
                                        7.0, 7.0))

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

        label = (f"EDIT MODE - {self._edit_owner} - "
                 f"{len(session.selection)}/{len(session.model.points)} "
                 "vertices")
        if session.dirty:
            label += " (modified)"
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label) + 16
        height = metrics.height() + 10
        x = int(target.center().x() - width / 2)
        y = int(target.top()) + 6
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(36, 58, 38, 215))
        painter.drawRect(x, y, width, height)
        painter.setPen(QColor(170, 240, 170))
        painter.drawText(x + 8, y + metrics.ascent() + 5, label)

    def _draw_owner_bbox(self, painter: QPainter, owner: str,
                         target: QRectF, camera: dict) -> None:
        bounds = self._owner_bounds.get(owner)
        if bounds is None:
            return
        x0, y0, z0, x1, y1, z1 = bounds
        corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1)
                   for z in (z0, z1)]
        pts = [self._project(self._camera_vertex(c, camera), target, camera)
               for c in corners]
        edges = [(0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3), (2, 6),
                 (3, 7), (4, 5), (4, 6), (5, 7), (6, 7)]
        painter.setPen(QPen(QColor(90, 230, 255, 220), 1.4,
                            Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for a, b in edges:
            painter.drawLine(pts[a], pts[b])

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
        else:  # textured
            image, uvs = self._face_texture(face, mat)
            if image is None or image.isNull() or len(uvs) < 3:
                brightness = self._face_brightness(face)
                color = QColor(int(mat.color.red() * brightness),
                               int(mat.color.green() * brightness),
                               int(mat.color.blue() * brightness))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawPolygon(polygon)
            else:
                self._draw_textured(painter, screen, uvs, image,
                                    additive=mat.additive)

        if draw_wire:
            painter.setPen(QPen(QColor(0, 0, 0, 130), 0.75))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(polygon)

    def _face_texture(self, face: ViewFace, mat: ViewMaterial):
        """Return (QImage, uv list in texture bytes) honoring VANM playback."""

        if mat.anim_frames:
            frame_index, _ = self._anim_states.get(face.material, (0, 1))
            _, image_id, uv_id = mat.anim_frames[frame_index]
            image = (mat.anim_images[image_id]
                     if 0 <= image_id < len(mat.anim_images) else None)
            group = (mat.anim_uv_groups[uv_id]
                     if 0 <= uv_id < len(mat.anim_uv_groups) else [])
            # Runtime remaps animated UVs by vertex order index (TexCoordId=j,
            # base.cpp GenerateMeshCoordsCache, CONFIRMED).
            uvs = [group[j] if j < len(group) else (0, 0)
                   for j in range(len(face.vertices))]
            return image, uvs
        return mat.image, face.uvs

    def _draw_textured(self, painter: QPainter, screen: list[QPointF],
                       uvs: list[tuple[int, int]], image: QImage,
                       additive: bool = False) -> None:
        # Fan triangulation (0, j, j-1), same as the runtime (CONFIRMED).
        # ARGB images honor per-texel alpha (chroma-transparent yellow).
        # Additive = flat-tracy glow faces, approximating the engine's
        # GL_ONE/GL_ONE blending with QPainter CompositionMode_Plus.
        width = image.width()
        height = image.height()
        for j in range(2, len(screen)):
            tri_screen = (screen[0], screen[j], screen[j - 1])
            tri_uv = (uvs[0], uvs[j], uvs[j - 1])
            # UV bytes -> pixel coordinates (u/256 * width) (CONFIRMED /256)
            src = [QPointF(u / 256.0 * width, v / 256.0 * height)
                   for u, v in tri_uv]
            transform = _affine_from_triangles(src, tri_screen)
            if transform is None:
                continue
            path = QPainterPath()
            path.moveTo(tri_screen[0])
            path.lineTo(tri_screen[1])
            path.lineTo(tri_screen[2])
            path.closeSubpath()
            painter.save()
            painter.setClipPath(path)
            if additive:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Plus
                )
            painter.setTransform(transform, True)
            painter.drawImage(0, 0, image)
            painter.restore()

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
        origin3 = self._camera_vertex(self._center, camera)
        length = 0.6
        axes = [
            ((length / self._scale, 0, 0), QColor(240, 100, 100), "X"),
            ((0, -length / self._scale, 0), QColor(110, 230, 110), "Y (up)"),
            ((0, 0, length / self._scale), QColor(110, 160, 250), "Z"),
        ]
        origin_screen = self._project(origin3, target, camera)
        for offset, color, label in axes:
            end3 = self._camera_vertex((
                self._center[0] + offset[0],
                self._center[1] + offset[1],
                self._center[2] + offset[2],
            ), camera)
            end_screen = self._project(end3, target, camera)
            painter.setPen(QPen(color, 1.2))
            painter.drawLine(origin_screen, end_screen)
            painter.drawText(end_screen + QPointF(3, -3), label)

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
        pos = event.position().toPoint()
        if self._edit_session is not None and not self._snapshot_active:
            if self._modal_op is not None:
                if event.button() == Qt.MouseButton.LeftButton:
                    self._commit_modal()
                elif event.button() == Qt.MouseButton.RightButton:
                    self._cancel_modal()
                event.accept()
                return
            if self._box_armed \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._box_start = pos
                self._box_rect = QRect(pos, pos)
                event.accept()
                return
        self._last_mouse = pos
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = self._last_mouse
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._edit_session is not None and not self._snapshot_active:
            shift = bool(event.modifiers()
                         & Qt.KeyboardModifier.ShiftModifier)
            if self._box_start is not None \
                    and event.button() == Qt.MouseButton.LeftButton:
                self._apply_box_select(shift)
                event.accept()
                return
            if event.button() == Qt.MouseButton.LeftButton \
                    and self._press_pos is not None:
                delta = event.position().toPoint() - self._press_pos
                if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                    self._pick_edit_vertex(event.position().toPoint(), shift)
            self._press_pos = None
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._press_pos is not None and not self._snapshot_active:
            delta = event.position().toPoint() - self._press_pos
            if abs(delta.x()) <= 4 and abs(delta.y()) <= 4:
                self.pick_at(event.position().toPoint())
        self._press_pos = None
        event.accept()

    def pick_at(self, point: QPoint) -> int | None:
        """Select the topmost polygon under the cursor.

        Faces were queued back-to-front (painter's algorithm), so the last
        shape containing the point is the visible one.  Emits objectPicked
        for every hit; polygonPicked only for the primary (workbench)
        object."""

        pos = QPointF(point)
        for polygon, face in reversed(self._pick_shapes):
            if polygon.containsPoint(pos, Qt.FillRule.OddEvenFill):
                self._selected_owner = face.owner
                self.objectPicked.emit(face.owner)
                if face.primary:
                    self._selected_poly = face.poly_id
                    self.polygonPicked.emit(face.poly_id)
                self.update()
                return face.poly_id if face.primary else None
        return None

    def event(self, ev) -> bool:  # noqa: N802 - Qt override
        # Tab toggles Edit Mode; it must be caught before Qt's focus chain.
        if ev.type() == QEvent.Type.KeyPress \
                and ev.key() == Qt.Key.Key_Tab \
                and self._family_ref is not None and not self._snapshot_active:
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
            self._emit_selection_hint()
            self.update()
            return True
        if key == Qt.Key.Key_B:
            self._box_armed = True
            self.statusMessage.emit(
                "Box select: drag with the left mouse button "
                "(Shift extends, Esc cancels).")
            return True
        if key == Qt.Key.Key_Escape and self._box_armed:
            self._box_armed = False
            self._box_start = None
            self._box_rect = None
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
                self.edit_redo()
            else:
                self.edit_undo()
            return True
        if key == Qt.Key.Key_Y \
                and mods & Qt.KeyboardModifier.ControlModifier:
            self.edit_redo()
            return True
        return False

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._snapshot_active and self._edit_key_press(event):
            return
        key = event.key()
        if key == Qt.Key.Key_F and self._selected_owner:
            self.frame_owner(self._selected_owner)
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        current = event.position().toPoint()
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
        delta = current - self._last_mouse
        self._last_mouse = current
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._yaw += delta.x() * 0.6
            self._pitch = max(-89.0, min(89.0, self._pitch + delta.y() * 0.6))
            self.update()
        elif event.buttons() & (Qt.MouseButton.RightButton
                                | Qt.MouseButton.MiddleButton):
            self._pan += QPointF(delta.x(), delta.y())
            self.update()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        factor = math.pow(1.0015, event.angleDelta().y())
        self._zoom = max(0.08, min(30.0, self._zoom * factor))
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.reset_view()
        event.accept()


def _affine_from_triangles(src: list[QPointF],
                           dst: tuple[QPointF, QPointF, QPointF]) -> QTransform | None:
    """Affine transform mapping texture triangle ``src`` onto screen ``dst``."""

    x0, y0 = src[0].x(), src[0].y()
    x1, y1 = src[1].x(), src[1].y()
    x2, y2 = src[2].x(), src[2].y()
    u0, v0 = dst[0].x(), dst[0].y()
    u1, v1 = dst[1].x(), dst[1].y()
    u2, v2 = dst[2].x(), dst[2].y()

    det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(det) < 1e-9:
        return None
    a = ((u1 - u0) * (y2 - y0) - (u2 - u0) * (y1 - y0)) / det
    c = ((u2 - u0) * (x1 - x0) - (u1 - u0) * (x2 - x0)) / det
    e = u0 - a * x0 - c * y0
    b = ((v1 - v0) * (y2 - y0) - (v2 - v0) * (y1 - y0)) / det
    d = ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0)) / det
    f = v0 - b * x0 - d * y0
    return QTransform(a, b, c, d, e, f)
