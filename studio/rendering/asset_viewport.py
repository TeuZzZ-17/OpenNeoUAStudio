"""GPU-accelerated 3D preview widget (AssetViewport) shared by every workspace.
"""

from __future__ import annotations


from PySide6.QtCore import QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
)

from studio.assets.family import AssetFamily
from studio.rendering.gpu import AcceleratedWidget
from studio.ui.cursor_themes import studio_cursor
from studio.rendering.indexed import (
    IndexedFamilyAdapter,
)
from studio.rendering.geometry import GeometryEditSession
from studio.rendering.particles import ParticleEmitter
from studio.rendering.asset_viewport_common import (  # re-exported for existing callers
    MATERIAL_COLORS as MATERIAL_COLORS,
    VIEW_MODES as VIEW_MODES,
    VIEW_PRESET_ANGLES as VIEW_PRESET_ANGLES,
    VIEW_PRESETS as VIEW_PRESETS,
    PREVIEW_REFERENCE_EXTENT as PREVIEW_REFERENCE_EXTENT,
    PREVIEW_SCALE as PREVIEW_SCALE,
    _PICK_CAMERA_DISTANCE as _PICK_CAMERA_DISTANCE,
    _PICK_NEAR_DISTANCE as _PICK_NEAR_DISTANCE,
    _PICK_DEPTH_EPSILON as _PICK_DEPTH_EPSILON,
    PASTE_GHOST_OPACITY as PASTE_GHOST_OPACITY,
    MODEL_EDIT_SELECTION_RED as MODEL_EDIT_SELECTION_RED,
    ViewMaterial as ViewMaterial,
    ViewFace as ViewFace,
    _RenderPayload as _RenderPayload,
    PickShape as PickShape,
    PastePreviewState as PastePreviewState,
    AlignmentTarget as AlignmentTarget,
    PrecisionGuide as PrecisionGuide,
    VertexPressState as VertexPressState,
    _triangle_barycentric as _triangle_barycentric,
    _pick_shape_depth as _pick_shape_depth,
    resolve_pick_shape as resolve_pick_shape,
    resolve_visible_edit_vertices as resolve_visible_edit_vertices,
    _rotation_matrix as _rotation_matrix,
    _apply as _apply,
    _image_from_ilbm as _image_from_ilbm,
    _image_from_effect_png as _image_from_effect_png,
)
from studio.rendering.asset_viewport_edit import _EditModeMixin
from studio.rendering.asset_viewport_handles import _AxisHandlesMixin
from studio.rendering.asset_viewport_view import _ViewControlsMixin
from studio.rendering.asset_viewport_paint import _PaintingMixin
from studio.rendering.asset_viewport_input import _InteractionMixin


class AssetViewport(
    _EditModeMixin,
    _AxisHandlesMixin,
    _ViewControlsMixin,
    _PaintingMixin,
    _InteractionMixin,
    AcceleratedWidget,
):
    """3D preview of an asset family with polygon picking."""

    def unsetCursor(self) -> None:  # noqa: N802
        """Back to the idle cursor: the pointing hand, never the system arrow."""

        self.setCursor(studio_cursor("point"))

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
    movePlacementRequested = Signal()

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
        self._home_camera_state: dict | None = None

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
        self._modal_keyboard_delta = (0.0, 0.0, 0.0)
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
        self._gizmo_drag_axis: str | None = None
        self._gizmo_hover_axis: str | None = None
        self._direct_transform_mode = "move"
        self._direct_transform_intensity = 0.5
        self._move_nudge_step = 0.5
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
        self._modal_mouse_tracking_before = None
        self._paste_left_pressed_at = None
        self._vertex_press: VertexPressState | None = None
        self._mode_label_rect: QRect | None = None

        self._mode = "textured"
        self._show_sen = False
        self._show_axes = True
        self._show_grid = True
        self._show_wire_overlay = False
        self._backface_cull = True
        self._empty_hint_suppressed = False

        self._yaw = self.RESET_YAW
        self._pitch = self.RESET_PITCH
        self._zoom = self.RESET_ZOOM
        self._pan = QPointF(0.0, 0.0)
        self._last_mouse = QPoint()
        self._camera_interacting = False
        self._snapshot_active = False
        self.unsetCursor()
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
        self._particle_emitters: list[ParticleEmitter] = []
        self._particle_bounds_points = []
        self._particle_owner_bounds: dict[str, tuple] = {}
        self._ground_y: float | None = None
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
        self._particle_emitters = []
        self._particle_bounds_points = []
        self._particle_owner_bounds = {}
        self._ground_y = None
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


    def set_diagnostics(self, diagnostics: list[str]) -> None:
        self._diagnostics = list(diagnostics)
        self.update()

    def load_family(self, family: AssetFamily,
                    visible_owners: set[str] | None = None,
                    keep_camera: bool = False,
                    primary_owner: str | None = None,
                    reuse_indexed_adapter: bool = False) -> None:
        same_family = family is self._family_ref
        previous_scope = (
            None if self._visible_owners is None
            else frozenset(self._visible_owners)
        )
        next_scope = (
            None if visible_owners is None
            else frozenset(visible_owners)
        )
        same_scope = previous_scope == next_scope
        preserved_camera = (
            self._camera_state()
            if keep_camera and same_family and same_scope else None
        )
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

        self._refresh_particle_faces()
        self._build_particle_bounds()
        points = self._framing_points()
        points.extend(p for box in self._sen_boxes for p in box)
        if preserved_camera is None:
            if points:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                zs = [p[2] for p in points]
                self._center = (
                    (min(xs) + max(xs)) / 2,
                    (min(ys) + max(ys)) / 2,
                    (min(zs) + max(zs)) / 2,
                )
                self._scale = PREVIEW_SCALE
            else:
                self._center = (0.0, 0.0, 0.0)
                self._scale = PREVIEW_SCALE
        if preserved_camera is not None:
            # A geometry/material refresh of the same visible object is not a
            # camera command. Keep its exact framing even if a moved/copied
            # piece expands the bounds, otherwise untouched geometry appears
            # to jump. Changing the visible owner/resource is different: that
            # must recompute center/scale so the newly selected object is framed.
            self._set_camera_state(preserved_camera)
        elif not keep_camera:
            self._yaw = self.RESET_YAW
            self._pitch = self.RESET_PITCH
            self._zoom = self.RESET_ZOOM
            self._pan = QPointF(0.0, 0.0)
            self._home_camera_state = self._camera_state()
        elif same_family and not same_scope:
            # A real owner/resource scope change deliberately re-frames the
            # newly visible object.  That newly presented view is now the
            # correct Reset View destination for this scope.  Geometry and
            # material refreshes keep the same scope and therefore never
            # overwrite the stored home camera.
            self._home_camera_state = self._camera_state()
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


    def set_selected_owner(self, owner: str | None) -> None:
        self._selected_owner = owner
        self.update()

    def frame_owner(self, owner: str) -> None:
        bounds = self._framing_bounds(owner)
        if bounds is None:
            return
        x0, y0, z0, x1, y1, z1 = bounds
        self._center = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        self._scale = PREVIEW_SCALE
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def frame_all(self) -> None:
        all_bounds = [bounds for bounds in (
            self._framing_bounds(owner) for owner in self._owner_bounds)
            if bounds is not None]
        if not all_bounds:
            return
        xs0, ys0, zs0, xs1, ys1, zs1 = zip(*all_bounds)
        self._center = ((min(xs0) + max(xs1)) / 2,
                        (min(ys0) + max(ys1)) / 2,
                        (min(zs0) + max(zs1)) / 2)
        self._scale = PREVIEW_SCALE
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def set_overlay_visible(self, visible: bool) -> None:
        self._show_diag_overlay = visible
        self.update()

    def set_empty_hint_suppressed(self, suppressed: bool) -> None:
        self._empty_hint_suppressed = bool(suppressed)
        self.update()

    # -- in-viewport axis handles ----------------------------------------------

    _GIZMO_LENGTH = 64.0
    _GIZMO_DEAD_ZONE = 14.0
    _GIZMO_GRAB_RADIUS = 7.0
    _GIZMO_COLORS = {
        "X": QColor(240, 100, 100),
        "Y": QColor(110, 230, 110),
        "Z": QColor(110, 160, 250),
    }
