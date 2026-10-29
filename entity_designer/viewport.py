"""The Entity Designer 3D viewport (CollisionViewport) with its move gizmo.
"""

from __future__ import annotations

from dataclasses import replace
import math

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)

from entity_designer import fire_area as fa
from entity_designer.widgets import _point_segment_distance, FIRE_POINT_COLOR, GUN_POINT_COLOR, TURRET_DOWN_COLOR, TURRET_SIDE_COLOR, TURRET_UP_COLOR, TYPE_COLORS
from entity_designer.fx_viewport import FxPreviewViewportMixin
from entity_designer.script_model import (
    _clamp_gun_angle,
    _number,
    CollisionSphere,
    GUN_FREE_SIDE_THRESHOLD,
    LEGACY,
    turret_limit_grid,
    TurretLimits,
    VEHICLE,
    WEAPON,
)
from entity_designer.shape import (
    CollisionShape,
    visual_transform_point,
)
from studio.rendering.asset_viewport import (
    _pick_shape_depth,
    AssetViewport,
    PickShape,
)
from studio.rendering.gizmo import ModelSpaceGizmo

class CollisionMoveGizmo(ModelSpaceGizmo):
    """Move gizmo with an independently orbitable presentation.

    The arrows always emit the same model-space X/Y/Z directions.  Dragging
    empty gizmo space only rotates the *drawing* so depth-aligned handles can
    be brought into view; it never rotates the model, viewport camera or any
    authored script data.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # The gizmo now lives below the property tabs, where the full width and
        # remaining lower-right height are available.  Use that room instead
        # of the old compact viewport-overlay geometry.
        self.set_visual_scale(
            extent_ratio=1.10, margin=12.0,
            handle_scale=1.35, line_scale=1.30)
        self._anchor_yaw = -35.0
        self._anchor_pitch = 20.0
        self._view_yaw_offset = 0.0
        self._view_pitch_offset = 0.0
        self._view_dragging = False
        self._view_drag_last = QPointF()
        self.setToolTip(
            "Move in model space. Drag empty gizmo space to rotate only the "
            "gizmo view; double-click empty space to realign it. Red=X, "
            "green=Y, blue=Z.")

    @property
    def directions(self) -> tuple[tuple[int, int, int], ...]:
        return (
            (0, 0, 1), (0, 1, 0), (1, 0, 0),
            (0, 0, -1), (0, -1, 0), (-1, 0, 0),
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(330, 285)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(285, 220)

    def set_camera_orientation(self, yaw: float, pitch: float) -> None:
        """Set the reference orientation without discarding user orbit."""

        self._anchor_yaw = float(yaw)
        self._anchor_pitch = float(pitch)
        self._apply_gizmo_view_orientation()

    def _apply_gizmo_view_orientation(self) -> None:
        pitch = max(
            -89.0,
            min(89.0, self._anchor_pitch + self._view_pitch_offset),
        )
        super().set_camera_orientation(
            self._anchor_yaw + self._view_yaw_offset, pitch)

    def reset_view_orientation(self) -> None:
        self._view_yaw_offset = 0.0
        self._view_pitch_offset = 0.0
        self._apply_gizmo_view_orientation()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.isEnabled()
                and self.hit_test(event.position()) is None):
            self._repeat_timer.stop()
            self._view_dragging = True
            self._view_drag_last = QPointF(event.position())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (self._view_dragging
                and event.buttons() & Qt.MouseButton.LeftButton):
            current = QPointF(event.position())
            delta = current - self._view_drag_last
            self._view_drag_last = current
            self._view_yaw_offset += delta.x() * 0.55
            target_pitch = max(
                -89.0,
                min(
                    89.0,
                    self._anchor_pitch + self._view_pitch_offset
                    + delta.y() * 0.55,
                ),
            )
            self._view_pitch_offset = target_pitch - self._anchor_pitch
            self._apply_gizmo_view_orientation()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self._view_dragging):
            self._view_dragging = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.hit_test(event.position()) is None):
            self.reset_view_orientation()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _make_shape_component_idle_cursor() -> QCursor:
    pixmap = QPixmap(23, 23)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(18, 25, 35), 3.0))
    painter.drawRect(7.0, 7.0, 9.0, 9.0)
    painter.drawLine(11, 1, 11, 6)
    painter.drawLine(11, 17, 11, 22)
    painter.drawLine(1, 11, 6, 11)
    painter.drawLine(17, 11, 22, 11)
    painter.setPen(QPen(QColor(83, 224, 255), 1.5))
    painter.drawRect(7.0, 7.0, 9.0, 9.0)
    painter.drawLine(11, 1, 11, 6)
    painter.drawLine(11, 17, 11, 22)
    painter.drawLine(1, 11, 6, 11)
    painter.drawLine(17, 11, 22, 11)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255))
    painter.drawEllipse(QPointF(11, 11), 1.0, 1.0)
    painter.end()
    return QCursor(pixmap, 11, 11)


# Camera-space depth beyond which AssetViewport's projection folds points
# back (its denominator is clamped); shapes are cut here before projecting.
_NEAR_CAMERA_Z = 3.65


def _near_clip_segment(a, b):
    """Part of a camera-space segment in front of the near plane, or None."""

    a_in, b_in = a[2] < _NEAR_CAMERA_Z, b[2] < _NEAR_CAMERA_Z
    if a_in and b_in:
        return a, b
    if not a_in and not b_in:
        return None
    ratio = (_NEAR_CAMERA_Z - a[2]) / (b[2] - a[2])
    cut = tuple(a[axis] + (b[axis] - a[axis]) * ratio for axis in range(3))
    return (a, cut) if a_in else (cut, b)


def _near_clip_polygon(points):
    """Sutherland-Hodgman cut of a camera-space polygon at the near plane."""

    clipped = []
    for index, current in enumerate(points):
        previous = points[index - 1]
        current_in = current[2] < _NEAR_CAMERA_Z
        if current_in != (previous[2] < _NEAR_CAMERA_Z):
            ratio = (_NEAR_CAMERA_Z - previous[2]) / (current[2] - previous[2])
            clipped.append(tuple(previous[axis] + (current[axis] - previous[axis]) * ratio
                                 for axis in range(3)))
        if current_in:
            clipped.append(current)
    return clipped


class CollisionViewport(FxPreviewViewportMixin, AssetViewport):
    """Existing AssetViewport plus the exact F10 three-ring overlay.

    Right-drag pans the camera exactly like the Model Viewer.  The contextual
    menu is therefore deferred to release and only opens when the pointer
    stayed still, so a real drag never pops the menu.
    """

    spherePicked = Signal(int)
    shapeComponentPicked = Signal(str, bool)
    shapeDeselectRequested = Signal()
    sphereToggleRequested = Signal(int)
    sphereBoxSelectionRequested = Signal(object, bool)
    firePointPicked = Signal(int)
    gunPointPicked = Signal(int)
    sphereContextMenuRequested = Signal(int, QPoint)
    sphereNudgeRequested = Signal(object)
    cockpitNudgeRequested = Signal(object)
    # Fire Area grips: picked key, drag start, and the total drag (key, world delta).
    fireAreaHandlePicked = Signal(str)
    fireAreaDragStarted = Signal(str)
    fireAreaDragged = Signal(str, float)
    # Double middle click in Fire Area: switch between Focus Unit and Focus Volume.
    fireAreaFocusToggled = Signal()
    RING_SEGMENTS = 12
    # Same orange as the engine's New Debug fire volume (yw_debug.cpp).
    FIRE_AREA_COLOR = QColor(255, 150, 30)
    FIRE_AREA_TILT_COLOR = QColor(255, 220, 130)
    FIRE_AREA_HANDLE_RADIUS = 7.0
    SELECTED_HALO_WIDTH = 6.0
    SELECTED_COLOR_WIDTH = 3.4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._collision_spheres: list[CollisionSphere] = []
        self._collision_selected = -1
        self._collision_selected_indices: set[int] = set()
        self._sphere_marquee_candidate = False
        self._sphere_press_pos: QPointF | None = None
        self._sphere_box_start: QPointF | None = None
        self._sphere_box_rect: QRectF | None = None
        # Screen position of the last right-button press, kept so release can
        # tell a plain context-click from a camera pan.
        self._context_press_pos: QPointF | None = None
        self._collision_show = {
            LEGACY: True, VEHICLE: True, WEAPON: True,
        }
        self._collision_show_model = True
        self._pick_cycle_point: QPointF | None = None
        self._pick_cycle_candidates: tuple[int, ...] = ()
        self._pick_cycle_offset = -1
        self._model_preview_scale = (1.0, 1.0, 1.0)
        self._model_preview_rotation = (0.0, 0.0, 0.0)
        self._collision_shape: CollisionShape | None = None
        self._collision_shape_overlay_visible = False
        self._shape_cursor_mouse_tracking_previous = None
        self._shape_component_idle_cursor = _make_shape_component_idle_cursor()
        self._shape_selected_indices = set()
        self._shape_press_pos = None
        self._geometry_component_preview: tuple | None = None
        self._ground_alignment_available = True
        self._ground_alignment_authored = False
        self._ground_alignment_source_loaded = False
        self._overeof = 0.0
        self._ground_simulation_visible = True
        self._overeof_visible = True
        self._fire_points: list[tuple[float, float, float]] = []
        self._fire_point_selected = -1
        self._fire_points_visible = True
        self._gun_points: list[tuple[float, float, float]] = []
        self._gun_directions: list[tuple[float, float, float]] = []
        self._gun_point_selected = -1
        self._gun_points_visible = True
        self._turret_limits: tuple[
            tuple[float, float, float], tuple[float, float, float],
            int, int, int,
        ] | None = None
        self._turret_limits_visible = True
        self._fire_area_visible = False
        self._fire_area = fa.FireArea()
        # Volume whose grips can be dragged, and the grip currently selected.
        self._fire_area_shape: str | None = None
        self._fire_area_handle = ""
        self._fire_area_muzzle = (0.0, 0.0, 0.0)
        self._fire_area_x_span = (0.0, 0.0)
        self._fire_area_focus: list[tuple[float, float, float]] = []
        # (key, press position, screen pixels per world unit) while dragging a grip.
        self._fire_area_drag: tuple[str, QPointF, QPointF] | None = None
        self._cockpit_preview_active = False
        self._cockpit_offset = (0.0, 0.0, 0.0)
        # Cockpit preview always fills the editor viewport. The only aspect
        # control left is OpenNeoUA's logical runtime aspect, which drives the
        # legacy matrixAspectCorrection() emulation. 4:3 is vanilla-safe.
        self._cockpit_runtime_aspect: float = 4.0 / 3.0
        self._model_preview_base_faces: list[
            tuple[tuple[float, float, float], ...]] = []
        self._model_preview_base_sen_boxes: list[
            tuple[tuple[float, float, float], ...]] = []
        self._model_preview_base_owner_bounds: dict[str, tuple] = {}
        self._fx_init()

    @property
    def model_preview_scale(self) -> tuple[float, float, float]:
        return self._model_preview_scale

    @property
    def model_preview_rotation(self) -> tuple[float, float, float]:
        return self._model_preview_rotation

    @property
    def overeof_preview_offset(self) -> float:
        """World-Y translation matching ``ground_y - overeof``."""

        if not (self._ground_alignment_available
                and self._ground_alignment_authored):
            return 0.0
        return -self._overeof


    @property
    def cockpit_preview_active(self) -> bool:
        return self._cockpit_preview_active


    def set_cockpit_preview_active(self, active: bool) -> None:
        """Switch only the Entity Designer preview into OpenNeoUA cockpit view.

        The regular orbit camera state is never overwritten, so leaving this
        mode restores the exact previous editor view.
        """

        active = bool(active)
        if self._cockpit_preview_active == active:
            return
        self._cockpit_preview_active = active
        self.update()

    def set_cockpit_camera_offset(
            self, x: float, y: float, z: float) -> None:
        values = tuple(float(value) for value in (x, y, z))
        if not all(math.isfinite(value) for value in values):
            return
        self._cockpit_offset = values
        self.update()

    def set_cockpit_runtime_aspect(self, aspect: float) -> None:
        """Set OpenNeoUA's logical aspect used for matrix aspect correction."""

        value = float(aspect)
        if not math.isfinite(value) or value <= 0.0:
            return
        if value == self._cockpit_runtime_aspect:
            return
        self._cockpit_runtime_aspect = value
        self.update()

    def _cockpit_render_rect(self) -> QRectF:
        """Use the complete editor viewport for Cockpit View."""

        return QRectF(self.rect())

    def _camera_state(self) -> dict:
        state = super()._camera_state()
        if getattr(self, "_cockpit_preview_active", False):
            # Cockpit View has no authorable FOV, orbit or pan in OpenNeoUA.
            # Keep a stable editor-only preview instead of leaking the prior
            # inspection camera into the generated result.
            state["yaw"] = 180.0
            state["pitch"] = 0.0
            state["zoom"] = 1.0
            state["pan"] = QPointF(0.0, 0.0)
            # assembly_viewer clips against ``near_distance``. OpenNeoUA starts
            # gameplay with _setFrustumClip(1.0, WORLD_FAR_CLIP).
            state["near_distance"] = 1.0
        return state

    def _camera_vertex(self, point, camera: dict | None = None):
        if not getattr(self, "_cockpit_preview_active", False):
            return super()._camera_vertex(point, camera)

        # OpenNeoUA uses actor + rotation.Transpose() * cockpit_camera_offset,
        # while the camera orientation stays the vehicle orientation. With an
        # identity actor rotation the forward axis is +Z and UA -Y is up.
        # AssetViewport's perspective expects the camera near plane at z=4,
        # so +Z world distance becomes a decreasing camera-space Z value.
        ox, oy, oz = self._cockpit_offset
        oy += self.overeof_preview_offset
        # Cockpit offsets and rendered VP vertices are already in the same
        # vehicle-local units. Do not apply AssetViewport's editor-only
        # auto-fit scale here: the runtime near plane is expressed in real UA
        # units and applying that scale made clipping model-dependent.
        dx = float(point[0]) - ox
        dy = float(point[1]) - oy
        dz = float(point[2]) - oz
        return (dx, -dy, 4.0 - dz)

    def _front_facing_from_screen_area(self, area: float) -> bool:
        """Match OpenNeoUA GL_CCW culling in the cockpit-only Qt view.

        OpenNeoUA renders the vehicle body with OpenGL's default CCW
        front-face convention. Cockpit projection is converted to Qt's
        top-left screen coordinates, which invert window Y; therefore a
        runtime front-facing triangle has a negative signed area here.
        The regular editor keeps its historical winding unchanged.
        """

        if self._cockpit_preview_active:
            return float(area) <= 0.0
        return super()._front_facing_from_screen_area(area)

    def _uses_retail_source_face_culling(self) -> bool:
        # The normal Collision/Model Editor view remains on the shared Retail
        # whole-source-face rule. OpenNeoUA cockpit rendering is OpenGL-facing:
        # culling is determined from each clipped fan triangle after projection.
        return not self._cockpit_preview_active

    def _render_piece_front_facing(
            self, camera_vertices, target: QRectF, camera: dict,
            source_front_facing: bool) -> bool:
        if not self._cockpit_preview_active:
            return super()._render_piece_front_facing(
                camera_vertices, target, camera, source_front_facing)
        if len(camera_vertices) < 3:
            return False
        screen = [
            self._project(point, target, camera)
            for point in camera_vertices
        ]
        area = sum(
            screen[index].x() * screen[(index + 1) % len(screen)].y()
            - screen[(index + 1) % len(screen)].x() * screen[index].y()
            for index in range(len(screen))
        )
        return self._front_facing_from_screen_area(area)

    def _project(self, camera_point, target: QRectF | None = None,
                 camera: dict | None = None) -> QPointF:
        if not getattr(self, "_cockpit_preview_active", False):
            return super()._project(camera_point, target, camera)

        # Exact OpenNeoUA projection used by GFXEngine:
        # UAFrustum gives clip x=x, y=-y, w=z and the engine applies legacy
        # matrixAspectCorrection() to camera X/Y. ``camera_point`` retains the
        # shared AssetViewport z=4 depth convention so BSP/near clipping can
        # be reused without introducing a parallel renderer.
        target = target or QRectF(self.rect())
        x, y, z = camera_point
        depth = 4.0 - z
        if depth <= 1e-9:
            depth = 1e-9
        width = max(1.0, float(target.width()))
        height = max(1.0, float(target.height()))
        # OpenNeoUA computes corrW/corrH from its logical graphics mode, while
        # glViewport() uses the physical drawable. Do not derive the runtime
        # correction from this editor rectangle: that was the source of the
        # remaining Y/Z mismatch on 4:3 logical modes shown on widescreen.
        runtime_aspect = max(1e-9, float(self._cockpit_runtime_aspect))
        corr_w = 1.0
        corr_h = 1.0
        if runtime_aspect >= 1.4:
            logical_width = runtime_aspect
            logical_height = 1.0
            half = (logical_width + logical_height) * 0.5
            corr_w = half * 1.1429 / logical_width
            corr_h = half * 0.85715 / logical_height
        focal_x = width * 0.5 * corr_w
        focal_y = height * 0.5 * corr_h
        return QPointF(
            target.center().x() + x * focal_x / depth,
            target.center().y() - y * focal_y / depth,
        )

    def _indexed_view_cache_signature(
            self, width: int, height: int, camera: dict) -> tuple:
        signature = super()._indexed_view_cache_signature(
            width, height, camera)
        if not self._cockpit_preview_active:
            return signature + ("collision-editor",) + self._fx_cache_signature()
        return signature + (
            "cockpit",
            tuple(float(value) for value in self._cockpit_offset),
            float(self._cockpit_runtime_aspect),
        )

    def _resolve_indexed_surface(
            self, adapter, face, material, frame_index: int):
        surface = super()._resolve_indexed_surface(
            adapter, face, material, frame_index)
        if (self._cockpit_preview_active
                and surface.kind == "texture"
                and surface.map_mode != "depth"):
            # OpenNeoUA maps both legacy LINEARMAPPED and DEPTHMAPPED faces to
            # the same GPU textured path. GLSL then perspective-corrects UVs,
            # which is essential for polygons viewed from inside the vehicle.
            return replace(surface, map_mode="depth")
        return surface

    def clear(self) -> None:
        super().clear()
        self._ground_alignment_source_loaded = False
        self._fire_points = []
        self._fire_point_selected = -1
        self._gun_points = []
        self._gun_directions = []
        self._gun_point_selected = -1
        self._turret_limits = None
        self._collision_selected = -1
        self._collision_selected_indices.clear()
        self._sphere_marquee_candidate = False
        self._sphere_press_pos = None
        self._sphere_box_start = None
        self._sphere_box_rect = None
        self._context_press_pos = None
        self._model_preview_base_faces = []
        self._model_preview_base_sen_boxes = []
        self._model_preview_base_owner_bounds = {}
        self._collision_shape = None

    def load_family(self, family, visible_owners=None, keep_camera=False,
                    primary_owner=None) -> None:
        """Load through AssetViewport, then apply a non-destructive scale.

        The base viewport already resolves BASE/SKLT transforms, children and
        textures.  Entity Designer only adds one final global axis scale,
        matching the visual effect of scale_xyz while leaving collision
        sphere coordinates untouched.
        """

        super().load_family(
            family, visible_owners, keep_camera=keep_camera,
            primary_owner=primary_owner)
        self._ground_alignment_source_loaded = True
        self._capture_unscaled_model_geometry()
        self._apply_model_preview_scale()

    def _refresh_particle_faces(self) -> None:
        """Keep the unscaled-geometry list aligned while particles animate.

        The base viewport swaps the billboard faces on every animation tick.
        Without this, ``_faces`` and ``_model_preview_base_faces`` differ in
        length and every consumer that pairs them (FX templates, preview
        scale) silently loses the model.
        """

        aligned = len(self._model_preview_base_faces) == len(self._faces)
        known = ({id(face): vertices for face, vertices in zip(
            self._faces, self._model_preview_base_faces)} if aligned else {})
        super()._refresh_particle_faces()
        if not aligned:
            return
        base = []
        for face in self._faces:
            vertices = known.get(id(face))
            if vertices is None:
                vertices = tuple(tuple(vertex) for vertex in face.vertices)
                face.vertices = [self._preview_point(v) for v in vertices]
            base.append(vertices)
        self._model_preview_base_faces = base

    def _capture_unscaled_model_geometry(self) -> None:
        self._model_preview_base_faces = [
            tuple(tuple(vertex) for vertex in face.vertices)
            for face in self._faces
        ]
        self._model_preview_base_sen_boxes = [
            tuple(tuple(point) for point in box)
            for box in self._sen_boxes
        ]
        self._model_preview_base_owner_bounds = dict(self._owner_bounds)

    def local_owner_triangles(self, owner: str | set[str] | None, *, animated_indices=None):
        """Return selected-owner triangles before preview scale and overeof."""

        triangles = []
        owners = ({owner} if isinstance(owner, str) else owner)
        for face, vertices in zip(
                self._faces, self._model_preview_base_faces):
            if owners is not None and face.owner not in owners:
                continue
            if len(vertices) < 3:
                continue
            first = vertices[0]
            if animated_indices is not None and face.animated:
                animated_indices.extend(range(
                    len(triangles), len(triangles) + len(vertices) - 2))
            triangles.extend(
                (first, vertices[index], vertices[index + 1])
                for index in range(1, len(vertices) - 1)
            )
        return triangles


    def _preview_point(self, point):
        transformed = visual_transform_point(
            point, self._model_preview_scale, self._model_preview_rotation)
        return (
            transformed[0],
            transformed[1] + self.overeof_preview_offset,
            transformed[2],
        )


    def local_scaled_owner_bounds(self, owner: str | None):
        """Return scaled model-space bounds without the visual ground offset.

        Collision coordinates are authored relative to the actor origin.
        Generated spheres and validation must therefore ignore the temporary
        world placement produced by ``overeof``.
        """

        bounds = self._model_preview_base_owner_bounds.get(owner)
        if bounds is None:
            return None
        x0, y0, z0, x1, y1, z1 = bounds
        corners = [
            visual_transform_point((x, y, z), self._model_preview_scale,
                                   self._model_preview_rotation)
            for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)
        ]
        return (
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            min(point[2] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
            max(point[2] for point in corners),
        )

    def _apply_model_preview_scale(self) -> None:
        if len(self._model_preview_base_faces) == len(self._faces):
            for face, vertices in zip(
                    self._faces, self._model_preview_base_faces):
                face.vertices = [
                    self._preview_point(vertex) for vertex in vertices
                ]
        if len(self._model_preview_base_sen_boxes) == len(self._sen_boxes):
            self._sen_boxes = [
                [self._preview_point(point) for point in box]
                for box in self._model_preview_base_sen_boxes
            ]
        preview_bounds = {}
        offset_y = self.overeof_preview_offset
        for owner in self._model_preview_base_owner_bounds:
            bounds = self.local_scaled_owner_bounds(owner)
            if bounds is None:
                continue
            x0, y0, z0, x1, y1, z1 = bounds
            preview_bounds[owner] = (
                x0, y0 + offset_y, z0,
                x1, y1 + offset_y, z1,
            )
        self._owner_bounds = preview_bounds
        self.update()

    def set_model_preview_scale(
            self, x: float, y: float, z: float) -> None:
        values = tuple(float(value) for value in (x, y, z))
        if not all(math.isfinite(value) and value >= 0.0 for value in values):
            return
        self._model_preview_scale = values
        self._apply_model_preview_scale()

    def set_model_preview_rotation(
            self, x: float, y: float, z: float) -> None:
        values = tuple(float(value) for value in (x, y, z))
        if not all(math.isfinite(value) for value in values):
            return
        self._model_preview_rotation = values
        self._apply_model_preview_scale()

    def set_collision_shape(self, shape: CollisionShape | None) -> None:
        self._collision_shape = shape
        if shape is None:
            self.unsetCursor()
        self.set_empty_hint_suppressed(shape is not None)
        self.update()

    def _update_shape_component_cursor(self, position: QPointF) -> None:
        if not self._collision_shape_overlay_visible:
            self.unsetCursor()
        elif self._hit_shape_component(position) is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(self._shape_component_idle_cursor)

    def set_collision_shape_overlay_visible(self, visible: bool) -> None:
        visible = bool(visible)
        was_visible = self._collision_shape_overlay_visible
        if visible and not was_visible:
            self._shape_cursor_mouse_tracking_previous = self.hasMouseTracking()
            self.setMouseTracking(True)
        elif not visible and was_visible:
            if self._shape_cursor_mouse_tracking_previous is not None:
                self.setMouseTracking(
                    self._shape_cursor_mouse_tracking_previous)
                self._shape_cursor_mouse_tracking_previous = None
            self.unsetCursor()
        self._collision_shape_overlay_visible = visible
        if visible and not was_visible:
            self._update_shape_component_cursor(
                QPointF(self.mapFromGlobal(QCursor.pos())))
        self.update()

    def set_shape_selected_indices(self, indices) -> None:
        self._shape_selected_indices = set(indices)
        self.update()

    def _hit_shape_component(self, position):
        if self._collision_shape is None:
            return None
        hits = []
        for index, hull in enumerate(self._collision_shape.hulls):
            for face in hull.faces:
                worlds = tuple((hull.vertices[i][0],
                                hull.vertices[i][1] + self.overeof_preview_offset,
                                hull.vertices[i][2]) for i in face)
                projected = [self._project_visible_world(p) for p in worlds]
                if any(p is None for p in projected):
                    continue
                depth = _pick_shape_depth(PickShape(
                    QPolygonF(projected), None,
                    tuple(self._camera_vertex(p) for p in worlds), index), position)
                if depth is not None:
                    hits.append((depth, index))
        if not hits:
            return None
        index = min(hits)[1]
        return self._collision_shape.hulls[index].source_component or f"@hull:{index}"

    def set_geometry_component_preview(self, triangles) -> None:
        self._geometry_component_preview = (
            tuple(tuple(tuple(point) for point in triangle)
                  for triangle in triangles)
            if triangles else None)
        self.update()

    def set_ground_alignment(
            self, available: bool, authored: bool, overeof: float) -> None:
        value = float(overeof)
        if not math.isfinite(value):
            return
        available = bool(available)
        authored = bool(authored)
        if (self._ground_alignment_available == available
                and self._ground_alignment_authored == authored
                and abs(self._overeof - value) < 1e-9):
            return
        self._ground_alignment_available = available
        self._ground_alignment_authored = authored
        self._overeof = value
        # Textured previews cache the indexed model frame. Overeof changes the
        # actual preview vertices, so invalidate that frame before repainting;
        # otherwise overlays move live while the textured model stays at the
        # previous height until another event happens to refresh the cache.
        self._invalidate_indexed_view_cache()
        self._apply_model_preview_scale()

    def set_ground_simulation_visible(self, visible: bool) -> None:
        self._ground_simulation_visible = bool(visible)
        self.update()

    def set_overeof_visible(self, visible: bool) -> None:
        self._overeof_visible = bool(visible)
        self.update()

    def set_fire_points(
            self, points: list[tuple[float, float, float]],
            selected: int = -1) -> None:
        self._fire_points = [
            tuple(float(value) for value in point) for point in points
        ]
        self._fire_point_selected = (
            selected if 0 <= selected < len(self._fire_points) else -1)
        self.update()

    def set_fire_points_visible(self, visible: bool) -> None:
        self._fire_points_visible = bool(visible)
        self.update()

    def set_gun_points(
            self, points: list[tuple[float, float, float]],
            directions: list[tuple[float, float, float]] | None = None,
            selected: int = -1) -> None:
        self._gun_points = [
            tuple(float(value) for value in point) for point in points
        ]
        raw_directions = directions or []
        self._gun_directions = [
            tuple(float(value) for value in direction)
            for direction in raw_directions
        ]
        if len(self._gun_directions) < len(self._gun_points):
            self._gun_directions.extend(
                [(0.0, 0.0, 0.0)]
                * (len(self._gun_points) - len(self._gun_directions)))
        self._gun_point_selected = (
            selected if 0 <= selected < len(self._gun_points) else -1)
        self.update()

    def set_gun_points_visible(self, visible: bool) -> None:
        self._gun_points_visible = bool(visible)
        self.update()

    def set_turret_limits(
            self, origin, direction, limits: TurretLimits | None) -> None:
        if limits is None:
            self._turret_limits = None
        else:
            self._turret_limits = (
                tuple(float(value) for value in origin),
                tuple(float(value) for value in direction),
                _clamp_gun_angle("side", limits.side),
                _clamp_gun_angle("up", limits.up),
                _clamp_gun_angle("down", limits.down),
            )
        self.update()

    def set_turret_limits_visible(self, visible: bool) -> None:
        self._turret_limits_visible = bool(visible)
        self.update()


    def set_collision_spheres(
        self, spheres: list[CollisionSphere], selected: int = -1,
        selected_indices: set[int] | None = None,
    ) -> None:
        self._collision_spheres = list(spheres)
        if selected_indices is None:
            selected_indices = {selected} if selected >= 0 else set()
        self._collision_selected_indices = {
            int(index) for index in selected_indices
            if 0 <= int(index) < len(self._collision_spheres)
        }
        self._collision_selected = (
            selected if selected in self._collision_selected_indices
            else min(self._collision_selected_indices, default=-1)
        )
        self.update()

    def _sphere_selection_in_box(self, rect: QRectF) -> set[int]:
        """Return visible sphere centres inside the shared marquee rectangle."""

        hits: set[int] = set()
        normalized = rect.normalized()
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            center = self._project_visible_world(sphere.center)
            if center is not None and normalized.contains(center):
                hits.add(index)
        return hits

    def set_collision_category_visible(self, category: str, visible: bool):
        self._collision_show[category] = bool(visible)
        self.update()

    def set_model_visible(self, visible: bool):
        self._collision_show_model = bool(visible)
        self.update()

    def set_textures_visible(self, visible: bool):
        self.set_mode("textured" if visible else "solid")

    def _ground_grid_spec(self) -> tuple[float, float, float]:
        if self._fx_active or self._fire_area_visible:
            return 0.0, 0.0, 2000.0
        size = 1.4 / max(abs(self._scale), 1e-9)
        return self._center[0], self._center[2], size

    def _draw_grid(self, painter: QPainter, target: QRectF, camera: dict) -> None:
        """Draw a fixed Y=0 ground plane instead of a model-centered grid."""

        # Destruction FX always needs the ground, weapons included.
        if not ((self._ground_alignment_available or self._fx_active
                 or self._fire_area_visible)
                and self._ground_alignment_source_loaded
                and self._ground_simulation_visible):
            return
        center_x, center_z, size = self._ground_grid_spec()
        corners_world = (
            (center_x - size, 0.0, center_z - size),
            (center_x + size, 0.0, center_z - size),
            (center_x + size, 0.0, center_z + size),
            (center_x - size, 0.0, center_z + size),
        )
        # The plane can reach behind the camera. Every shape is cut at the
        # near plane before projecting; projecting points behind the camera
        # would fold them back and draw stray lines across the viewport.
        corners = _near_clip_polygon(
            [self._camera_vertex(point, camera) for point in corners_world])
        if len(corners) >= 3:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(34, 118, 132, 42))
            painter.drawPolygon(QPolygonF([
                self._project(point, target, camera) for point in corners]))

        steps = 8
        for i in range(-steps, steps + 1):
            offset = i * size / steps
            major = i == 0
            painter.setPen(QPen(
                QColor(70, 170, 180, 150 if major else 86),
                1.25 if major else 0.8,
            ))
            for start, end in (
                ((center_x + offset, 0.0, center_z - size),
                 (center_x + offset, 0.0, center_z + size)),
                ((center_x - size, 0.0, center_z + offset),
                 (center_x + size, 0.0, center_z + offset)),
            ):
                segment = _near_clip_segment(
                    self._camera_vertex(start, camera),
                    self._camera_vertex(end, camera))
                if segment is not None:
                    painter.drawLine(
                        self._project(segment[0], target, camera),
                        self._project(segment[1], target, camera))

    def _is_sphere_drawn(self, sphere: CollisionSphere) -> bool:
        return (
            sphere.visible and self._collision_show.get(sphere.category, True)
            and sphere.radius > 0 and math.isfinite(sphere.radius)
        )

    def _ring_world_points(
            self, sphere: CollisionSphere, axis: int
    ) -> list[tuple[float, float, float]]:
        points = []
        for step in range(self.RING_SEGMENTS + 1):
            angle = 2.0 * math.pi * step / self.RING_SEGMENTS
            ca = math.cos(angle) * sphere.radius
            sa = math.sin(angle) * sphere.radius
            if axis == 0:
                world = (sphere.x + ca, sphere.y + sa, sphere.z)
            elif axis == 1:
                world = (sphere.x + ca, sphere.y, sphere.z + sa)
            else:
                world = (sphere.x, sphere.y + ca, sphere.z + sa)
            points.append(world)
        return points

    def _ring_points(self, sphere: CollisionSphere, axis: int):
        # Public/test helper retains the historical projected-point contract.
        return [
            self._project(self._camera_vertex(world))
            for world in self._ring_world_points(sphere, axis)
        ]

    @staticmethod
    def _camera_point_is_projectable(camera_point) -> bool:
        # AssetViewport uses a camera distance of 4 and clamps its denominator
        # at 0.2. OpenNeoUA F10 instead drops points behind the near plane.
        # Applying the equivalent rejection here prevents large AoE spheres
        # from producing diagonal lines across the entire viewport.
        return 4.0 - float(camera_point[2]) > 0.2

    def _project_visible_world(self, world):
        camera_point = self._camera_vertex(world)
        if not self._camera_point_is_projectable(camera_point):
            return None
        screen = self._project(camera_point)
        # OpenNeoUA F10 also rejects projected points beyond a small screen
        # margin, then restarts the ring at the next valid point. Without
        # this guard, very large AoE radii can still draw long cross-screen
        # chords even though the world-space radius itself is correct.
        margin = 64.0
        if not (-margin <= screen.x() <= self.width() + margin
                and -margin <= screen.y() <= self.height() + margin):
            return None
        return screen

    def _screen_radius(self, sphere: CollisionSphere) -> float:
        center = self._project(self._camera_vertex(sphere.center))
        samples = [
            self._project(self._camera_vertex((
                sphere.x + sphere.radius, sphere.y, sphere.z))),
            self._project(self._camera_vertex((
                sphere.x, sphere.y + sphere.radius, sphere.z))),
            self._project(self._camera_vertex((
                sphere.x, sphere.y, sphere.z + sphere.radius))),
        ]
        return max(
            math.hypot(point.x() - center.x(), point.y() - center.y())
            for point in samples)

    def _sphere_hit_candidates(self, point: QPointF) -> list[int]:
        candidates: list[tuple[float, float, int]] = []
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            center = self._project(self._camera_vertex(sphere.center))
            center_distance = math.hypot(
                point.x() - center.x(), point.y() - center.y())
            screen_radius = self._screen_radius(sphere)
            ring_distance = math.inf
            for axis in range(3):
                points = self._ring_points(sphere, axis)
                for start, end in zip(points, points[1:]):
                    ring_distance = min(
                        ring_distance,
                        _point_segment_distance(point, start, end),
                    )
            tolerance = max(12.0, min(24.0, 10.0 + screen_radius * 0.08))
            center_tolerance = max(
                14.0, min(24.0, 11.0 + screen_radius * 0.10))
            center_hit = center_distance <= center_tolerance
            if ring_distance <= tolerance or center_hit:
                score = min(
                    ring_distance,
                    center_distance if center_hit else math.inf,
                )
                depth = self._camera_vertex(sphere.center)[2]
                candidates.append((score, -depth, index))
        candidates.sort()
        return [item[2] for item in candidates]

    def _hit_sphere(self, point: QPointF, *, cycle: bool = True) -> int:
        candidates = tuple(self._sphere_hit_candidates(point))
        if not candidates:
            self._pick_cycle_point = None
            self._pick_cycle_candidates = ()
            self._pick_cycle_offset = -1
            return -1
        if not cycle:
            return candidates[0]
        same_point = self._pick_cycle_point is not None and math.hypot(
            point.x() - self._pick_cycle_point.x(),
            point.y() - self._pick_cycle_point.y(),
        ) <= 6.0
        if same_point and candidates == self._pick_cycle_candidates:
            self._pick_cycle_offset = (
                self._pick_cycle_offset + 1) % len(candidates)
        else:
            self._pick_cycle_point = QPointF(point)
            self._pick_cycle_candidates = candidates
            self._pick_cycle_offset = 0
        return candidates[self._pick_cycle_offset]

    def _draw_mode_label(self, *_args) -> None:
        """Collision assets stay in View Mode without a mode overlay."""

        self._mode_label_rect = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Tab:
            event.accept()
            return
        if self._cockpit_preview_active and not (
                event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier)):
            directions = {
                Qt.Key.Key_Left: (-1, 0, 0),
                Qt.Key.Key_Right: (1, 0, 0),
                Qt.Key.Key_Up: (0, 0, 1),
                Qt.Key.Key_Down: (0, 0, -1),
                Qt.Key.Key_PageUp: (0, -1, 0),
                Qt.Key.Key_PageDown: (0, 1, 0),
            }
            direction = directions.get(event.key())
            if direction is not None:
                self.cockpitNudgeRequested.emit(direction)
                event.accept()
                return
        if (self._collision_selected >= 0
                or self._fire_point_selected >= 0
                or self._gun_point_selected >= 0
                or (self._fire_area_visible and self._fire_area_handle)) and not (
                event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                    | Qt.KeyboardModifier.MetaModifier)):
            directions = {
                Qt.Key.Key_Left: (-1, 0, 0),
                Qt.Key.Key_Right: (1, 0, 0),
                Qt.Key.Key_Up: (0, 0, -1),
                Qt.Key.Key_Down: (0, 0, 1),
                Qt.Key.Key_PageUp: (0, -1, 0),
                Qt.Key.Key_PageDown: (0, 1, 0),
            }
            direction = directions.get(event.key())
            if direction is not None:
                self.sphereNudgeRequested.emit(direction)
                event.accept()
                return
        super().keyPressEvent(event)

    def event(self, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Tab:
            event.accept()
            return True
        return super().event(event)

    def _draw_collision_rings(
        self, painter: QPainter, sphere: CollisionSphere, pen: QPen,
    ) -> None:
        painter.setPen(pen)
        for axis in range(3):
            projected = [
                self._project_visible_world(world)
                for world in self._ring_world_points(sphere, axis)
            ]
            for start, end in zip(projected, projected[1:]):
                # Match OpenNeoUA's F10 conversion: a segment is emitted only
                # when both endpoints survive near-plane projection.
                if start is not None and end is not None:
                    painter.drawLine(start, end)

    def _draw_ground_alignment_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_available
                and self._ground_alignment_source_loaded):
            return
        camera = self._camera_state()
        target = QRectF(self.rect())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._ground_simulation_visible:
            _center_x, _center_z, size = self._ground_grid_spec()
            label_world = (
                self._center[0] - size, 0.0, self._center[2] - size)
            label_point = self._project(
                self._camera_vertex(label_world), target, camera)
            painter.setPen(QPen(QColor(125, 225, 230, 230), 1.2))
            painter.drawText(
                label_point + QPointF(8.0, -8.0),
                "Ground Simulation / Y = 0",
            )

        if not (self._ground_alignment_authored and self._overeof_visible):
            return
        origin_world = (0.0, self.overeof_preview_offset, 0.0)
        ground_world = (0.0, 0.0, 0.0)
        origin = self._project(
            self._camera_vertex(origin_world), target, camera)
        ground = self._project(
            self._camera_vertex(ground_world), target, camera)
        pen = QPen(QColor(255, 190, 80, 235), 2.2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(origin, ground)
        painter.setBrush(QColor(255, 190, 80, 235))
        painter.drawEllipse(origin, 4.0, 4.0)
        painter.drawEllipse(ground, 4.0, 4.0)
        midpoint = QPointF(
            (origin.x() + ground.x()) * 0.5,
            (origin.y() + ground.y()) * 0.5,
        )
        painter.drawText(
            midpoint + QPointF(7.0, -6.0),
            f"overeof = {_number(self._overeof)}",
        )

    def _fire_point_screen_positions(self):
        return [self._project_visible_world(point) for point in self._fire_points]


    def _hit_fire_point(self, point: QPointF) -> int:
        if not (self._ground_alignment_source_loaded
                and self._fire_points_visible):
            return -1
        candidates = []
        for index, screen in enumerate(self._fire_point_screen_positions()):
            if screen is None:
                continue
            distance = math.hypot(point.x() - screen.x(), point.y() - screen.y())
            if distance <= 16.0:
                candidates.append((distance, index))
        return min(candidates)[1] if candidates else -1


    @staticmethod
    def _draw_fire_marker(
            painter: QPainter, screen: QPointF, pen: QPen,
            brush: QColor, extent: float) -> None:
        painter.setPen(pen)
        painter.setBrush(brush)
        painter.drawEllipse(screen, extent * 0.6, extent * 0.6)
        painter.drawLine(
            QPointF(screen.x() - extent, screen.y()),
            QPointF(screen.x() + extent, screen.y()))
        painter.drawLine(
            QPointF(screen.x(), screen.y() - extent),
            QPointF(screen.x(), screen.y() + extent))

    def _draw_fire_points_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_source_loaded
                and self._fire_points_visible and self._fire_points):
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fill = QColor(
            FIRE_POINT_COLOR.red(), FIRE_POINT_COLOR.green(),
            FIRE_POINT_COLOR.blue(), 225)
        rack_selected = (
            self._fire_point_selected >= 0 and len(self._fire_points) > 1)
        for index, screen in enumerate(self._fire_point_screen_positions()):
            if screen is None:
                continue
            is_primary = index == self._fire_point_selected
            if is_primary or rack_selected:
                self._draw_fire_marker(
                    painter, screen,
                    QPen(QColor(255, 255, 255),
                         6.0 if is_primary else 4.5),
                    QColor(255, 255, 255, 235),
                    11.0 if is_primary else 10.5)
            self._draw_fire_marker(
                painter, screen, QPen(FIRE_POINT_COLOR, 2.4), fill, 10.0)
            painter.setPen(QPen(
                QColor(255, 255, 255)
                if (is_primary or rack_selected) else FIRE_POINT_COLOR,
                1.5))
            painter.drawText(
                screen + QPointF(9.0, -9.0), f"F{index + 1}")


    # -- Fire Area (ai_fire_trigger_*) ----------------------------------------------------

    def set_fire_area(self, area: fa.FireArea, shape: str | None,
                      handle: str = "", *, origin=(0.0, 0.0, 0.0),
                      x_span=(0.0, 0.0), focus=()) -> None:
        """Volume to draw; ``origin`` is the muzzle, ``x_span`` the muzzle range
        sideways and ``focus`` extra points (unit, target) kept in frame."""

        self._fire_area = area
        self._fire_area_shape = shape
        self._fire_area_handle = handle
        self._fire_area_muzzle = tuple(origin)
        self._fire_area_x_span = tuple(x_span)
        self._fire_area_focus = [tuple(point) for point in focus]
        self.update()

    def set_fire_area_visible(self, visible: bool) -> None:
        self._fire_area_visible = bool(visible)
        if not visible:
            self._fire_area_drag = None
        self.update()

    def _fire_area_origin(self) -> tuple[float, float, float]:
        return self._fire_area_muzzle

    def _fire_area_handles(self) -> list[fa.AreaHandle]:
        shape = self._fire_area_shape
        size = self._fire_area.size(shape) if shape else None
        if size is None:
            return []
        return fa.volume_handles(
            shape, size, self._fire_area.dead_zone, self._fire_area_origin(),
            self._fire_area_x_span)

    def _fire_area_lines(self) -> list[tuple[fa.Segment, QColor, bool]]:
        """(segment, colour, dashed) of every authored volume and its tilt limit."""

        area, origin = self._fire_area, self._fire_area_origin()
        lines = []
        for shape in area.authored_shapes():
            size = area.size(shape)
            for segment in fa.volume_segments(
                    shape, size, area.dead_zone, origin, 0.0, self._fire_area_x_span):
                lines.append((segment, self.FIRE_AREA_COLOR, False))
            if not fa.is_below(shape) and area.max_inclination > 0.0:
                # Furthest the engine tilts the volume toward a ground target.
                for segment in fa.volume_segments(
                        shape, size, area.dead_zone, origin, area.max_inclination,
                        self._fire_area_x_span):
                    lines.append((segment, self.FIRE_AREA_TILT_COLOR, True))
        return lines

    def frame_fire_area(self, unit_only: bool = False) -> None:
        """Move the camera so the whole volume, the unit and the ground are visible;
        ``unit_only`` closes in on the unit and its fire point instead."""

        segments = [line[0] for line in self._fire_area_lines()]
        if not segments:
            return
        points = [(point, point) for point in self._fire_area_focus]
        if unit_only:
            center, radius = fa.bounds_of(
                [(point, point) for point in (*self._fire_area_focus[:1], self._fire_area_muzzle)])
            radius = max(radius * 2.5, 120.0)
        else:
            center, radius = fa.bounds_of(segments + points)
        self._center = center
        self._scale = 1.0 / radius
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def _fire_area_segment_screen(self, start, end):
        clipped = _near_clip_segment(
            self._camera_vertex(start), self._camera_vertex(end))
        if clipped is None:
            return None
        return self._project(clipped[0]), self._project(clipped[1])

    def _draw_fire_area_overlay(self, painter: QPainter) -> None:
        if not (self._fire_area_visible and self._ground_alignment_source_loaded):
            return
        painter.save()
        # Crisp one-pixel style lines, like the engine's debug overlay.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for (start, end), color, dashed in self._fire_area_lines():
            screen = self._fire_area_segment_screen(start, end)
            if screen is None:
                continue
            pen = QPen(color, 1.5)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(*screen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = self.FIRE_AREA_HANDLE_RADIUS
        visible = []
        for handle in self._fire_area_handles():
            screen = self._project_visible_world(handle.point)
            if screen is not None:
                visible.append((handle, screen))
        labels = self._fire_area_label_positions(painter, visible)
        for handle, screen in visible:
            selected = handle.key == self._fire_area_handle
            if selected:
                painter.setPen(QPen(QColor(255, 255, 255), 3.0))
                painter.setBrush(QColor(255, 255, 255, 235))
                painter.drawRect(QRectF(
                    screen.x() - radius - 2, screen.y() - radius - 2,
                    2 * radius + 4, 2 * radius + 4))
            painter.setPen(QPen(QColor(30, 22, 12), 1.5))
            painter.setBrush(self.FIRE_AREA_COLOR)
            painter.drawRect(QRectF(
                screen.x() - radius * 0.7, screen.y() - radius * 0.7,
                radius * 1.4, radius * 1.4))
            painter.setPen(QPen(
                QColor(255, 255, 255) if selected else self.FIRE_AREA_COLOR, 1.5))
            text, baseline = labels[handle.key]
            painter.drawText(baseline, text)
        painter.restore()

    def _fire_area_handle_text(self, handle: fa.AreaHandle) -> str:
        if handle.key == "dead_zone":
            return f"{handle.key} {round(self._fire_area.dead_zone)}"
        return f"{handle.key} {handle.label}"

    def _fire_area_label_positions(self, painter: QPainter, visible) -> dict:
        """{handle key: (text, baseline point)} with labels kept apart.

        Grips can sit close together on screen (depending on the camera angle), so each
        label tries a few spots around its grip and takes the first one that touches
        no grip square, no earlier label and stays inside the view. The selected grip
        chooses first so its label stays next to it.
        """

        metrics = painter.fontMetrics()
        radius = self.FIRE_AREA_HANDLE_RADIUS
        taken = [QRectF(s.x() - radius - 2, s.y() - radius - 2, 2 * radius + 4, 2 * radius + 4)
                 for _handle, s in visible]
        view = QRectF(self.rect())
        height = metrics.height()
        placed = {}
        ordered = sorted(visible, key=lambda item: item[0].key != self._fire_area_handle)
        for handle, screen in ordered:
            text = self._fire_area_handle_text(handle)
            width = metrics.horizontalAdvance(text)
            gap = radius + 3.0
            spots = [(gap, -gap + height * .3), (gap, gap + height * .7),
                     (-gap - width, -gap + height * .3), (-gap - width, gap + height * .7),
                     (-width / 2, -gap - 2), (-width / 2, gap + height)]
            for level in range(1, 6):
                spots += [(gap, -gap + height * .3 - level * height),
                          (-gap - width, -gap + height * .3 - level * height),
                          (gap, gap + height * .7 + level * height),
                          (-gap - width, gap + height * .7 + level * height)]
            choice = None
            for dx, dy in spots:
                baseline = screen + QPointF(dx, dy)
                box = QRectF(baseline.x(), baseline.y() - metrics.ascent(), width, height)
                if view.contains(box) and not any(box.adjusted(-2, -1, 2, 1).intersects(t) for t in taken):
                    choice = baseline, box
                    break
            if choice is None:
                baseline = screen + QPointF(*spots[0])
                choice = baseline, QRectF(baseline.x(), baseline.y() - metrics.ascent(), width, height)
            taken.append(choice[1])
            placed[handle.key] = (text, choice[0])
        return placed

    def _hit_fire_area_handle(self, point: QPointF) -> str:
        if not (self._fire_area_visible and self._ground_alignment_source_loaded):
            return ""
        best = (self.FIRE_AREA_HANDLE_RADIUS + 5.0, "")
        for handle in self._fire_area_handles():
            screen = self._project_visible_world(handle.point)
            if screen is None:
                continue
            distance = math.hypot(point.x() - screen.x(), point.y() - screen.y())
            if distance <= best[0]:
                best = (distance, handle.key)
        return best[1]

    def _begin_fire_area_drag(self, key: str, position: QPointF) -> None:
        handle = next(h for h in self._fire_area_handles() if h.key == key)
        # Pixels covered on screen by one world unit along the grip's axis.
        reach = 100.0
        start = self._project(self._camera_vertex(handle.point))
        end = self._project(self._camera_vertex(tuple(
            handle.point[i] + handle.axis[i] * reach for i in range(3))))
        per_unit = (end - start) / reach
        self._fire_area_handle = key
        self._fire_area_drag = (key, QPointF(position), per_unit)
        self.fireAreaHandlePicked.emit(key)
        self.fireAreaDragStarted.emit(key)

    def _update_fire_area_drag(self, position: QPointF) -> None:
        key, press, per_unit = self._fire_area_drag
        length_sq = per_unit.x() ** 2 + per_unit.y() ** 2
        if length_sq < 1e-9:
            return
        moved = position - press
        delta = (moved.x() * per_unit.x() + moved.y() * per_unit.y()) / length_sq
        self.fireAreaDragged.emit(key, delta)

    def _gun_point_screen_positions(self):
        return [self._project_visible_world(point) for point in self._gun_points]

    def _hit_gun_point(self, point: QPointF) -> int:
        if not (self._ground_alignment_source_loaded
                and self._gun_points_visible):
            return -1
        candidates = []
        for index, screen in enumerate(self._gun_point_screen_positions()):
            if screen is None:
                continue
            distance = math.hypot(point.x() - screen.x(), point.y() - screen.y())
            if distance <= 16.0:
                candidates.append((distance, index))
        return min(candidates)[1] if candidates else -1

    def _draw_gun_points_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_source_loaded
                and self._gun_points_visible and self._gun_points):
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fill = QColor(
            GUN_POINT_COLOR.red(), GUN_POINT_COLOR.green(),
            GUN_POINT_COLOR.blue(), 225)
        screens = self._gun_point_screen_positions()
        for index, screen in enumerate(screens):
            if screen is None:
                continue
            selected = index == self._gun_point_selected
            if selected:
                self._draw_fire_marker(
                    painter, screen, QPen(QColor(255, 255, 255), 6.0),
                    QColor(255, 255, 255, 235), 11.0)
            self._draw_fire_marker(
                painter, screen, QPen(GUN_POINT_COLOR, 2.4), fill, 10.0)

            # TRoboGun::dir is a local-space initial direction. A short
            # purple arrow makes orientation visible without pretending the
            # marker is a second piece of model geometry.
            if index < len(self._gun_directions):
                dx, dy, dz = self._gun_directions[index]
                length = math.sqrt(dx * dx + dy * dy + dz * dz)
                if math.isfinite(length) and length > 1e-9:
                    scale = 55.0 / length
                    x, y, z = self._gun_points[index]
                    endpoint = self._project_visible_world((
                        x + dx * scale, y + dy * scale, z + dz * scale))
                    if endpoint is not None:
                        painter.setPen(QPen(GUN_POINT_COLOR, 2.0))
                        painter.drawLine(screen, endpoint)
                        painter.setBrush(fill)
                        painter.drawEllipse(endpoint, 2.8, 2.8)

            painter.setPen(QPen(
                QColor(255, 255, 255) if selected else GUN_POINT_COLOR, 1.5))
            painter.drawText(screen + QPointF(9.0, -9.0), f"G{index + 1}")

    def _draw_turret_limits_overlay(self, painter: QPainter) -> None:
        if not (self._ground_alignment_source_loaded
                and self._turret_limits_visible
                and self._turret_limits is not None):
            return
        origin, direction, side, up, down = self._turret_limits
        radius = max(1.0, 1.0 / max(abs(float(self._scale)), 1e-9))
        curves = turret_limit_grid(
            origin, direction, side, up, down, radius)
        if not curves:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        def draw_curves(groups, pen):
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for points in groups:
                projected = [self._project_visible_world(point)
                             for point in points]
                for start, end in zip(projected, projected[1:]):
                    if start is not None and end is not None:
                        painter.drawLine(start, end)

        guide_pen = QPen(QColor(180, 205, 215, 155), 1.1)
        guide_pen.setStyle(Qt.PenStyle.DashLine)
        draw_curves(curves["meridians"], guide_pen)
        side_curve = curves["side"][0]
        midpoint = len(side_curve) // 2
        spoke_targets = [
            side_curve[midpoint],
            curves["up"][0][midpoint],
            curves["down"][0][midpoint],
        ]
        if side <= GUN_FREE_SIDE_THRESHOLD:
            spoke_targets.extend((side_curve[0], side_curve[-1]))
        draw_curves([[origin, target] for target in spoke_targets], guide_pen)
        draw_curves(curves["side"], QPen(TURRET_SIDE_COLOR, 2.6))
        draw_curves(curves["up"], QPen(TURRET_UP_COLOR, 2.1))
        draw_curves(curves["down"], QPen(TURRET_DOWN_COLOR, 2.1))

        origin_screen = self._project_visible_world(origin)
        if origin_screen is not None:
            painter.setPen(QPen(QColor(255, 255, 255), 1.8))
            painter.setBrush(QColor(
                TURRET_SIDE_COLOR.red(), TURRET_SIDE_COLOR.green(),
                TURRET_SIDE_COLOR.blue(), 220))
            painter.drawEllipse(origin_screen, 4.5, 4.5)

        # Angle labels are a screen-space annotation, so resolve collisions in
        # screen space too. Moving them in model XYZ would only fix one camera
        # angle and they could overlap again as soon as the view rotates.
        occupied_label_rects: list[QRectF] = []
        metrics = painter.fontMetrics()
        label_bounds = QRectF(self.rect()).adjusted(5.0, 5.0, -5.0, -5.0)

        def label(point, text, color, preferred_offset):
            screen = self._project_visible_world(point)
            if screen is None:
                return

            width = float(metrics.horizontalAdvance(text))
            height = float(metrics.height())
            ascent = float(metrics.ascent())
            left_offset = -width - 7.0
            offsets = (
                preferred_offset,
                QPointF(preferred_offset.x(), preferred_offset.y() - height - 5.0),
                QPointF(preferred_offset.x(), preferred_offset.y() + height + 5.0),
                QPointF(left_offset, preferred_offset.y()),
                QPointF(left_offset, preferred_offset.y() - height - 5.0),
                QPointF(left_offset, preferred_offset.y() + height + 5.0),
            )

            chosen_rect = None
            chosen_baseline = None
            for offset in offsets:
                baseline = screen + offset
                rect = QRectF(
                    baseline.x(), baseline.y() - ascent, width, height)

                # Keep the complete label visible even near viewport edges.
                dx = 0.0
                dy = 0.0
                if rect.left() < label_bounds.left():
                    dx = label_bounds.left() - rect.left()
                elif rect.right() > label_bounds.right():
                    dx = label_bounds.right() - rect.right()
                if rect.top() < label_bounds.top():
                    dy = label_bounds.top() - rect.top()
                elif rect.bottom() > label_bounds.bottom():
                    dy = label_bounds.bottom() - rect.bottom()
                rect.translate(dx, dy)
                padded = rect.adjusted(-3.0, -2.0, 3.0, 2.0)
                if not any(padded.intersects(other)
                           for other in occupied_label_rects):
                    chosen_rect = rect
                    chosen_baseline = QPointF(
                        rect.left(), rect.top() + ascent)
                    break

            if chosen_rect is None:
                # Three labels and six candidates make this rare, but keep a
                # deterministic final fallback rather than allowing overlap.
                baseline = screen + preferred_offset
                rect = QRectF(
                    baseline.x(), baseline.y() - ascent, width, height)
                for step in range(1, 12):
                    rect.moveTop(screen.y() + step * (height + 4.0))
                    if rect.bottom() > label_bounds.bottom():
                        rect.moveTop(screen.y() - step * (height + 4.0))
                    if (rect.top() >= label_bounds.top()
                            and rect.bottom() <= label_bounds.bottom()
                            and not any(
                                rect.adjusted(-3.0, -2.0, 3.0, 2.0).intersects(
                                    other)
                                for other in occupied_label_rects)):
                        chosen_rect = rect
                        chosen_baseline = QPointF(
                            rect.left(), rect.top() + ascent)
                        break

            if chosen_rect is None or chosen_baseline is None:
                return
            occupied_label_rects.append(
                chosen_rect.adjusted(-3.0, -2.0, 3.0, 2.0))
            painter.setPen(QPen(color, 1.0))
            painter.drawText(chosen_baseline, text)

        side_text = f"Side Angle: {side}"
        side_points = curves["side"][0]
        label(
            side_points[
                -1 if side <= GUN_FREE_SIDE_THRESHOLD
                else len(side_points) // 2],
            side_text, TURRET_SIDE_COLOR, QPointF(7.0, -7.0))
        label(
            curves["up"][0][len(curves["up"][0]) // 2],
            f"Up Angle: {up}",
            TURRET_UP_COLOR, QPointF(7.0, -7.0))
        label(
            curves["down"][0][len(curves["down"][0]) // 2],
            f"Down Angle: {down}",
            TURRET_DOWN_COLOR, QPointF(7.0, 15.0))


    def _draw_collision_shape_overlay(self, painter: QPainter) -> None:
        if self._collision_shape is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Draw the selected components last, so other hulls cannot cover their highlight.
        hulls = sorted(enumerate(self._collision_shape.hulls),
                       key=lambda pair: pair[0] in self._shape_selected_indices)
        for index, hull in hulls:
            selected = index in self._shape_selected_indices
            if not hull.enabled:
                continue
            edges = set()
            for a, b, c in hull.faces:
                edges.update((
                    (min(a, b), max(a, b)),
                    (min(b, c), max(b, c)),
                    (min(c, a), max(c, a)),
                ))
            lines = []
            for start, end in sorted(edges):
                first_local = hull.vertices[start]
                second_local = hull.vertices[end]
                first = self._project_visible_world((
                    first_local[0],
                    first_local[1] + self.overeof_preview_offset,
                    first_local[2]))
                second = self._project_visible_world((
                    second_local[0],
                    second_local[1] + self.overeof_preview_offset,
                    second_local[2]))
                if first is not None and second is not None:
                    lines.append((first, second))
            pens = [QPen(QColor(245, 55, 65) if selected
                         else QColor(70, 230, 225), 1.8)]
            for pen in pens:
                painter.setPen(pen)
                for first, second in lines:
                    painter.drawLine(first, second)
        painter.restore()

    def _draw_geometry_component_preview(self, painter: QPainter) -> None:
        if not self._geometry_component_preview:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QPen(QColor(255, 255, 255), 3.8))
        for triangle in self._geometry_component_preview:
            projected = [
                self._project_visible_world(self._preview_point(point))
                for point in triangle
            ]
            for index in range(3):
                first = projected[index]
                second = projected[(index + 1) % 3]
                if first is not None and second is not None:
                    painter.drawLine(first, second)
        painter.restore()

    def _paint_viewport(self, painter: QPainter) -> None:
        if self._cockpit_preview_active:
            painter.fillRect(self.rect(), QColor(24, 26, 32))
            target = self._cockpit_render_rect()
            painter.save()
            painter.setClipRect(target)
            if self._collision_show_model:
                self._render_scene(
                    painter, target, QColor(24, 26, 32), clean=True,
                    camera=self._camera_state())
            else:
                painter.fillRect(target, QColor(24, 26, 32))
                painter.setPen(QColor(130, 135, 145))
                painter.drawText(
                    target, Qt.AlignmentFlag.AlignCenter, "Model hidden")
            painter.restore()
            return

        if self._fx_active:
            # Destruction FX owns the whole view: unit, effects and their
            # guides. Collision and weapon overlays belong to other tabs.
            painter.save()
            suppressed = self._empty_hint_suppressed
            self._empty_hint_suppressed = True
            try:
                self._paint_fx_viewport(painter)
            finally:
                self._empty_hint_suppressed = suppressed
                painter.restore()
            return

        if self._collision_show_model:
            painter.save()
            super()._paint_viewport(painter)
            painter.restore()
        else:
            painter.fillRect(self.rect(), QColor(24, 26, 32))
            if self._show_grid:
                self._draw_grid(
                    painter, QRectF(self.rect()), self._camera_state())
            painter.setPen(QColor(130, 135, 145))
            painter.drawText(
                QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter,
                "Model hidden",
            )

        self._draw_ground_alignment_overlay(painter)
        if self._collision_shape_overlay_visible:
            self._draw_collision_shape_overlay(painter)
            self._draw_geometry_component_preview(painter)
        # OpenNeoUA F10 uses unfilled, aliased one-pixel lines and 12 segments.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        selected_pairs = []
        for index, sphere in enumerate(self._collision_spheres):
            if not self._is_sphere_drawn(sphere):
                continue
            if index in self._collision_selected_indices:
                selected_pairs.append((index, sphere))
                continue
            color = TYPE_COLORS[sphere.category]
            self._draw_collision_rings(
                painter, sphere, QPen(color, 1.0))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for index, sphere in selected_pairs:
            color = TYPE_COLORS[sphere.category]
            primary = index == self._collision_selected
            self._draw_collision_rings(
                painter, sphere, QPen(
                    QColor(255, 255, 255),
                    self.SELECTED_HALO_WIDTH if primary else 4.6))
            self._draw_collision_rings(
                painter, sphere, QPen(
                    color,
                    self.SELECTED_COLOR_WIDTH if primary else 2.6))
            center = self._project(self._camera_vertex(sphere.center))
            painter.setPen(QPen(QColor(255, 255, 255), 2.0))
            painter.setBrush(QColor(
                color.red(), color.green(), color.blue(), 235))
            extent = 6.0 if primary else 4.0
            painter.drawEllipse(center, extent, extent)
        self._draw_fire_points_overlay(painter)
        self._draw_turret_limits_overlay(painter)
        self._draw_gun_points_overlay(painter)
        self._draw_fire_area_overlay(painter)
        if self._sphere_box_rect is not None:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(
                QColor(90, 230, 255), 1.0, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(90, 230, 255, 40))
            painter.drawRect(self._sphere_box_rect.normalized())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._cockpit_preview_active:
            event.accept()
            return
        if self._fire_area_drag is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self._update_fire_area_drag(event.position())
            event.accept()
            return
        if (self._fire_area_visible and not event.buttons()):
            over = self._hit_fire_area_handle(event.position())
            self.setCursor(Qt.CursorShape.SizeAllCursor if over
                           else Qt.CursorShape.ArrowCursor)
        if self._collision_shape_overlay_visible and self._collision_shape is not None:
            if not event.buttons():
                self._update_shape_component_cursor(event.position())
            elif (self._shape_press_pos is not None
                  and event.buttons() & Qt.MouseButton.LeftButton):
                delta = event.position() - self._shape_press_pos
                if abs(delta.x()) > 4.0 or abs(delta.y()) > 4.0:
                    # A held drag orbits the view; it does not move the shape.
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if self._sphere_press_pos is not None \
                and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.position()
            if self._sphere_box_start is not None:
                self._sphere_box_rect = QRectF(
                    self._sphere_box_start, current).normalized()
                self.update()
                event.accept()
                return
            if self._sphere_marquee_candidate:
                delta = current - self._sphere_press_pos
                if abs(delta.x()) > 4.0 or abs(delta.y()) > 4.0:
                    self._sphere_box_start = QPointF(self._sphere_press_pos)
                    self._sphere_box_rect = QRectF(
                        self._sphere_box_start, current).normalized()
                    self._sphere_marquee_candidate = False
                    self.update()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self._fire_area_drag is not None):
            self._fire_area_drag = None
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._shape_press_pos is not None:
            delta = event.position() - self._shape_press_pos
            self._shape_press_pos = None
            super().mouseReleaseEvent(event)
            if abs(delta.x()) <= 4.0 and abs(delta.y()) <= 4.0:
                key = self._hit_shape_component(event.position())
                if key is not None:
                    self.shapeComponentPicked.emit(
                        key, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
            self._update_shape_component_cursor(event.position())
            return
        if event.button() == Qt.MouseButton.RightButton:
            press_pos = self._context_press_pos
            self._context_press_pos = None
            # Let the base viewport finish the camera gesture first, then ask
            # for the contextual menu only when the pointer barely moved.  The
            # 4 px tolerance matches the click/drag split used by the rest of
            # the viewport.
            super().mouseReleaseEvent(event)
            if press_pos is not None:
                delta = event.position() - press_pos
                if abs(delta.x()) <= 4.0 and abs(delta.y()) <= 4.0:
                    self._request_context_menu(
                        event.position(), event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.LeftButton \
                and self._sphere_press_pos is not None:
            if self._sphere_box_start is not None \
                    and self._sphere_box_rect is not None:
                hits = self._sphere_selection_in_box(self._sphere_box_rect)
                self.sphereBoxSelectionRequested.emit(hits, True)
            else:
                index = self._hit_sphere(event.position())
                if index >= 0:
                    self.sphereToggleRequested.emit(index)
            self._sphere_marquee_candidate = False
            self._sphere_press_pos = None
            self._sphere_box_start = None
            self._sphere_box_rect = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._cockpit_preview_active:
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._cockpit_preview_active:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton and self._fire_area_visible:
            # The base viewport sends the camera to its Reset View here, which in
            # Fire Area is far from the volume. The camera stays where it is.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._collision_shape_overlay_visible and self._collision_shape is not None:
                self._shape_press_pos = QPointF(event.position())
                super().mousePressEvent(event)
                return
            area_handle = self._hit_fire_area_handle(event.position())
            if area_handle:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self._begin_fire_area_drag(area_handle, event.position())
                self.update()
                event.accept()
                return
            gun_index = self._hit_gun_point(event.position())
            if gun_index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._gun_point_selected = gun_index
                self._fire_point_selected = -1
                self._collision_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.gunPointPicked.emit(gun_index)
                self.update()
                event.accept()
                return
            fire_index = self._hit_fire_point(event.position())
            if fire_index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._fire_point_selected = fire_index
                self._gun_point_selected = -1
                self._collision_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.firePointPicked.emit(fire_index)
                self.update()
                event.accept()
                return
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Match the existing Model/Wireframe editor convention:
                # Ctrl+click toggles one item, while Ctrl+drag grows a marquee
                # and adds every sphere whose projected centre falls inside.
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._sphere_marquee_candidate = True
                self._sphere_press_pos = QPointF(event.position())
                self._sphere_box_start = None
                self._sphere_box_rect = None
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                event.accept()
                return
            index = self._hit_sphere(event.position())
            if index >= 0:
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self._collision_selected = index
                self._fire_point_selected = -1
                self._gun_point_selected = -1
                self._camera_interacting = False
                self._press_pos = None
                self._last_mouse = event.position().toPoint()
                self.spherePicked.emit(index)
                self.update()
                event.accept()
                return
        if event.button() == Qt.MouseButton.RightButton:
            # Right-drag pans the camera exactly like the Model Viewer.  The
            # press only records where the gesture started; the base viewport
            # keeps the pan bookkeeping and the contextual menu is decided on
            # release.
            self._context_press_pos = QPointF(event.position())
            super().mousePressEvent(event)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton and self._fire_area_visible:
            self.fireAreaFocusToggled.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._fire_area_visible:
                self._fire_area_drag = None
                if not self._hit_fire_area_handle(event.position()):
                    self._fire_area_handle = ""
                    self.fireAreaHandlePicked.emit("")
                    self.update()
                event.accept()
                return
            if self._collision_shape_overlay_visible:
                self._shape_press_pos = None
                key = self._hit_shape_component(event.position())
                if key is None:
                    self.shapeDeselectRequested.emit()
                else:
                    self.shapeComponentPicked.emit(
                        key, bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
                event.accept()
                return
            gun_index = self._hit_gun_point(event.position())
            if gun_index >= 0:
                self._gun_point_selected = gun_index
                self._fire_point_selected = -1
                self._collision_selected = -1
                self.gunPointPicked.emit(gun_index)
            else:
                fire_index = self._hit_fire_point(event.position())
                if fire_index >= 0:
                    self._fire_point_selected = fire_index
                    self._gun_point_selected = -1
                    self._collision_selected = -1
                    self.firePointPicked.emit(fire_index)
                else:
                    index = self._hit_sphere(event.position(), cycle=False)
                    self._collision_selected = index
                    self._fire_point_selected = -1
                    self._gun_point_selected = -1
                    self.spherePicked.emit(index)
                    if index < 0:
                        self.firePointPicked.emit(-1)
                        self.gunPointPicked.emit(-1)
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _request_context_menu(
            self, position: QPointF, global_pos: QPoint) -> None:
        """Select the element under a still right-click, then open its menu.

        The order matters: the selection signals below run first so the menu
        actions already see the clicked element.
        """

        gun_index = self._hit_gun_point(position)
        if gun_index >= 0:
            self._gun_point_selected = gun_index
            self._fire_point_selected = -1
            self._collision_selected = -1
            self.gunPointPicked.emit(gun_index)
            self.update()
            self.sphereContextMenuRequested.emit(-1, global_pos)
            return
        fire_index = self._hit_fire_point(position)
        if fire_index >= 0:
            self._fire_point_selected = fire_index
            self._gun_point_selected = -1
            self._collision_selected = -1
            self.firePointPicked.emit(fire_index)
            self.update()
            self.sphereContextMenuRequested.emit(-1, global_pos)
            return
        index = self._hit_sphere(position, cycle=False)
        if index >= 0:
            self._fire_point_selected = -1
            self._gun_point_selected = -1
            if index not in self._collision_selected_indices:
                # Standard editor behavior: context-clicking an unselected
                # sphere makes it the sole selection. Context-clicking any
                # member of an existing multiselection must preserve the
                # whole group so Delete/Duplicate/Mirror act on it.
                self._collision_selected = index
                self.spherePicked.emit(index)
            self.update()
        self.sphereContextMenuRequested.emit(index, global_pos)
