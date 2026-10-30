"""AssetViewport mixin: geometry Edit Mode (Blender-style vertex editing) and the modal
G / R / S transforms."""

from __future__ import annotations

import bisect
import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QCursor,
    QPainterPath,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication

from studio.rendering.geometry import AXIS_VECTORS, GeometryEditSession, mat_apply
from studio.rendering.asset_viewport_common import (
    _PICK_CAMERA_DISTANCE,
    _PICK_NEAR_DISTANCE,
    PastePreviewState,
    AlignmentTarget,
    PrecisionGuide,
    resolve_pick_shape,
    resolve_visible_edit_vertices,
    _rotation_matrix,
    _apply,
)


class _EditModeMixin:
    """Part of AssetViewport; `self` is always the AssetViewport."""

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
        if normalized == "move":
            self._move_nudge_step = self._direct_transform_intensity
        self.update()

    def set_edit_read_only_vertices(self, indices) -> None:
        """Mark unsafe shared FX vertices.

        Only single-vertex picking and the FX commands stay clear of them: the
        mass selections (select all, selection box) include them on purpose, so
        the whole model shape can be selected and transformed as one piece.
        """

        protected = {
            int(index) for index in indices
            if isinstance(index, int) and index >= 0
        }
        self._edit_read_only_vertices = protected
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
        """Return whether the camera matches the captured Reset View."""

        epsilon = self.CAMERA_EPSILON
        home = self._home_camera_state
        if home is not None:
            center = home["center"]
            return (
                abs(self._yaw - home["yaw"]) <= epsilon
                and abs(self._pitch - home["pitch"]) <= epsilon
                and abs(self._zoom - home["zoom"]) <= epsilon
                and abs(self._pan.x() - home["pan"].x()) <= epsilon
                and abs(self._pan.y() - home["pan"].y()) <= epsilon
                and abs(self._center[0] - center[0]) <= epsilon
                and abs(self._center[1] - center[1]) <= epsilon
                and abs(self._center[2] - center[2]) <= epsilon
                and abs(self._scale - home["scale"]) <= epsilon
            )
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

    def capture_reset_view(self) -> None:
        """Capture the current camera as the stable Reset View destination."""

        if self.has_loaded_resource:
            self._home_camera_state = self._camera_state()

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
        return {0: "min", 1: "center", 2: "max", 3: "vertex"}.get(
            index, "feature")

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
            bounds_features = self._bounds_features(values)
            # BBox min/center/max are useful for broad alignment, but edited
            # UA pieces are often wedges and irregular panels. Include the
            # actual selected vertex coordinates too so corners can magnetise
            # to reference vertices, edge midpoints and coplanar faces.
            source_features = []
            for axis in range(3):
                candidates = [
                    (bounds_features[axis][0], 0),
                    (bounds_features[axis][1], 1),
                    (bounds_features[axis][2], 2),
                ]
                unique_vertices = sorted({
                    round(point[axis], 9) for point in values
                    if math.isfinite(point[axis])
                })
                # Keep snapping predictable on very large selections while
                # retaining all coordinates for the usual small UA polygons.
                if len(unique_vertices) > 48:
                    stride = max(1, len(unique_vertices) // 48)
                    unique_vertices = unique_vertices[::stride][:48]
                candidates.extend((float(value), 3)
                                  for value in unique_vertices)
                source_features.append(tuple(candidates))
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
                for source_value, feature_index in source_features[axis]:
                    moved = source_value + mutable[axis]
                    near_targets = self._near_alignment_targets(axis, moved)
                    if any(
                            abs(target.value - moved) * pixels_per_unit < 1e-6
                            for target in near_targets):
                        continue
                    for target in near_targets:
                        correction = target.value - moved
                        pixels = abs(correction) * pixels_per_unit
                        if pixels < 1e-6 or pixels > 10.0:
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

    @property
    def paste_preview_operation(self) -> str | None:
        state = self._paste_preview
        return state.operation if state is not None else None

    @property
    def paste_preview_clipboard(self):
        state = self._paste_preview
        return state.clipboard if state is not None else None

    def begin_paste_preview(self, clipboard: object,
                            position: QPoint, *,
                            operation: str = "copy") -> bool:
        session = self._edit_session
        operation = str(operation).casefold()
        if operation not in ("copy", "move"):
            operation = "copy"
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
        self._paste_left_pressed_at = None
        self.setMouseTracking(True)
        self._paste_preview = PastePreviewState(
            clipboard=clipboard,
            operation=operation,
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
        if operation == "move":
            # Textured mode caches the full indexed frame. Move Preview hides
            # the source faces, so its start/end must invalidate that derived
            # image or the old source can remain visible from cache.
            self._invalidate_indexed_view_cache()
        self.pastePreviewActiveChanged.emit(True)
        self._emit_paste_hint()
        self.update()
        return True

    def cancel_paste_preview(self) -> bool:
        self._paste_left_pressed_at = None
        state = self._paste_preview
        if state is None:
            return False
        self._paste_preview = None
        if state.operation == "move":
            self._invalidate_indexed_view_cache()
        self.setMouseTracking(state.previous_mouse_tracking)
        self.editHint.emit("")
        self.pastePreviewActiveChanged.emit(False)
        self.statusMessage.emit(
            f"{state.operation.title()} Geometry cancelled.")
        self._clear_precision_guides()
        self.update()
        return True

    def finish_paste_preview(self) -> None:
        self._paste_left_pressed_at = None
        state = self._paste_preview
        if state is None:
            return
        self._paste_preview = None
        if state.operation == "move":
            self._invalidate_indexed_view_cache()
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

    def _paste_destination_world(self):
        """Return the current paste-preview pivot in world coordinates."""

        state = self._paste_preview
        session = self._edit_session
        if state is None or session is None:
            return None
        destination_model = tuple(
            state.clipboard.pivot[axis] + state.delta_model[axis]
            for axis in range(3))
        return _apply(session.matrix, destination_model, session.position)

    def _begin_paste_camera_inspect(self) -> None:
        """Start orbit inspection without changing the viewport framing.

        The preview pivot is captured at its current screen position.  Camera
        rotation will keep that screen position stable by correcting only pan;
        the model-wide center/scale are never replaced with the preview pivot.
        This avoids the old one-frame jump of all untouched geometry.
        """

        state = self._paste_preview
        destination = self._paste_destination_world()
        if state is None or destination is None:
            return
        state.camera_inspect = True
        state.camera_inspect_anchor_screen = QPointF(
            self._project(self._camera_vertex(destination)))

    def _stabilize_paste_orbit_pivot(self) -> None:
        """Keep the placement pivot fixed on screen after yaw/pitch changes."""

        state = self._paste_preview
        destination = self._paste_destination_world()
        if state is None or destination is None:
            return
        anchor = state.camera_inspect_anchor_screen
        if anchor is None:
            anchor = QPointF(self._project(self._camera_vertex(destination)))
            state.camera_inspect_anchor_screen = QPointF(anchor)
            return
        projected = self._project(self._camera_vertex(destination))
        self._pan += QPointF(
            anchor.x() - projected.x(),
            anchor.y() - projected.y(),
        )

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
        pivot_world = self._paste_destination_world() or state.source_pivot_world
        denominator = self._camera_denominator(pivot_world)
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

    def _screen_direction_model_delta(
            self, dx_sign: float, dy_sign: float, amount: float,
            pivot_world) -> tuple[float, float, float]:
        """Return an exact model-space step pointing along the screen plane."""

        session = self._edit_session
        if session is None:
            return (0.0, 0.0, 0.0)
        denominator = self._camera_denominator(pivot_world)
        world = self._screen_delta_to_world_at_depth(
            float(dx_sign), float(dy_sign), denominator)
        model = session.world_delta_to_model(world)
        length = math.sqrt(sum(value * value for value in model))
        if length <= 1e-12:
            return (0.0, 0.0, 0.0)
        scale = max(0.001, float(amount)) / length
        return tuple(value * scale for value in model)

    def nudge_paste_preview_step(
            self, dx_sign: float, dy_sign: float, amount: float) -> bool:
        state = self._paste_preview
        if state is None:
            return False
        pivot_world = self._paste_destination_world() or state.source_pivot_world
        step = self._screen_direction_model_delta(
            dx_sign, dy_sign, amount, pivot_world)
        if state.axis is not None:
            axis = "XYZ".index(state.axis)
            sign = 1.0 if step[axis] >= 0.0 else -1.0
            step = tuple(
                sign * max(0.001, float(amount)) if index == axis else 0.0
                for index in range(3))
        return self.nudge_paste_preview_model(*step)

    def nudge_paste_preview(self, dx_pixels: float,
                            dy_pixels: float) -> bool:
        state = self._paste_preview
        session = self._edit_session
        if state is None or session is None:
            return False
        pivot_world = self._paste_destination_world() or state.source_pivot_world
        denominator = self._camera_denominator(pivot_world)
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
        state.delta_model = self._snap_translation(
            state.clipboard.points, raw, state.axis, allow_axis_lock=False)
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
        action = "Move" if state.operation == "move" else "Copy"
        label = (
            f"{action} {getattr(state.clipboard, 'fx_name')} FX Preview"
            if getattr(state.clipboard, "fx_name", None)
            else f"{action} Preview")
        self.editHint.emit(
            f"{label} | X {dx:+.2f} Y {dy:+.2f} Z {dz:+.2f} | "
            f"{mode}{aligned} | Arrows nudge | Shift+arrows faster | "
            "Click/Enter confirm | Hold LMB 350 ms + drag: orbit | "
            "RMB/Esc cancel")

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
        # Axis handles highlight on hover, so move events are needed.
        self.setMouseTracking(True)
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
            "drag an axis arrow to lock the axis | "
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
        self._gizmo_drag_axis = None
        self._gizmo_hover_axis = None
        self.unsetCursor()
        self.setMouseTracking(False)
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


    def select_all_edit_vertices(self) -> None:
        session = self._edit_session
        if session is None:
            self.statusMessage.emit("Edit Mode: no editable model is active.")
            return
        self._select_all_model_vertices()
        self._selected_owner = self._edit_owner
        self._active_edit_vertex = min(session.selection, default=None)
        self._emit_selection_hint("vertex")
        self.update()

    def _select_all_model_vertices(self) -> bool:
        """Select every POO2 index, protected FX vertices included.

        Mass selections cover the whole model shape on purpose: transforms can
        then move the model as one piece instead of leaving the FX behind.
        """

        session = self._edit_session
        if session is None:
            return False
        session.select_all()
        return bool(session.selection)

    def select_edit_vertices(self, indices) -> None:
        """Select only the given POO2 indices in the active Edit Mode."""

        session = self._edit_session
        if session is None:
            self.statusMessage.emit("Edit Mode: no editable model is active.")
            return
        requested = [
            index for index in indices
            if index not in self._edit_read_only_vertices
        ]
        try:
            session.set_selection(requested)
        except ValueError as exc:
            self.statusMessage.emit(f"Edit Mode: {exc}")
            return
        self._active_edit_vertex = min(session.selection, default=None)
        # Only real vertex gestures replace the explicit polygon pick intent,
        # so this programmatic source keeps the polygon selection alive.
        self._emit_selection_hint("programmatic")
        self.update()

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


    def nudge_edit_selection_step(
            self, dx_sign: float, dy_sign: float, amount: float) -> bool:
        """Nudge the active selection by the configured Move-step magnitude."""

        session = self._edit_session
        if session is None or not session.selection:
            self.statusMessage.emit(
                "Nudge: enable Edit Mode and select one or more vertices.")
            return False
        pivot = session.selection_pivot()
        if pivot is None or session.modal_active:
            return False
        pivot_world = _apply(session.matrix, pivot, session.position)
        delta = self._screen_direction_model_delta(
            dx_sign, dy_sign, amount, pivot_world)
        return self.nudge_edit_selection_model(*delta)

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
        origin = self._model_nudge_before or []
        selected = [
            origin[index] for index in sorted(session.selection)
            if index < len(origin)
        ]
        self._prepare_alignment_targets(selected, session.selection)
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
        origin = self._model_nudge_before or []
        selected = [
            origin[index] for index in sorted(session.selection)
            if index < len(origin)
        ]
        snapped = self._snap_translation(
            selected, raw, allow_axis_lock=False) if selected else raw
        session.preview_grab(snapped)
        self._refresh_edit_faces(invalidate_visibility=False)
        self.editHint.emit(
            f"Move Gizmo | X {snapped[0]:+.3f} "
            f"Y {snapped[1]:+.3f} Z {snapped[2]:+.3f}")
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


    def begin_scale_preview(self, select_all_if_empty: bool = True) -> bool:
        session = self._edit_session
        if session is None:
            return False
        if not session.selection and select_all_if_empty:
            self._select_all_model_vertices()
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
            self._select_all_model_vertices()
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

    def center_edit_model_at_origin(self) -> bool:
        """Move the whole edited skeleton so its bounding-box center is (0,0,0).

        Every vertex gets the same offset, so shape, polygon indices and FX
        mapping stay identical. Only vertices used by a polygon define the
        bounds: leftovers of deleted parts must not shift the center.
        """

        session = self._edit_session
        if session is None or session.modal_active:
            return False
        model = session.model
        points = model.points
        used = {
            index for polygon in model.polygons for index in polygon
            if 0 <= index < len(points)
        } or set(range(len(points)))
        if not used:
            return False
        offset = tuple(
            -(min(points[i][axis] for i in used)
              + max(points[i][axis] for i in used)) / 2.0
            for axis in range(3))
        if all(abs(value) < 1e-6 for value in offset):
            return False
        before = [tuple(point) for point in points]
        points[:] = [
            (x + offset[0], y + offset[1], z + offset[2])
            for x, y, z in before
        ]
        session.dirty = True
        self._refresh_edit_faces()
        self._emit_geometry_command(before)
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
        # Mass selection covers the whole shape: protected FX vertices belong
        # to it, so the covered model can be transformed as one piece.
        available = set(range(len(screen_points)))
        hits = {
            index for index, screen in enumerate(screen_points)
            if index in available
            if rect.contains(int(screen.x()), int(screen.y()))
        }
        if self._edit_pick_polygons:
            region = QPainterPath()
            region.addRect(QRectF(rect))
            for polygon in session.model.polygons:
                if len(polygon) < 3 or not set(polygon).issubset(available):
                    continue
                path = QPainterPath()
                path.addPolygon(QPolygonF([screen_points[i] for i in polygon]))
                path.closeSubpath()
                if region.intersects(path) or region.contains(path):
                    hits.update(polygon)
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
        # Transforms move exactly what is selected.  Mass selections include
        # protected FX vertices on purpose, so nothing is stripped here.
        if not session.selection:
            self.statusMessage.emit(
                "Edit Mode: select at least one vertex first "
                "(click, drag a selection box, or A for all).")
            return
        if not session.begin_modal():
            return
        self._modal_mouse_tracking_before = self.hasMouseTracking()
        self.setMouseTracking(True)
        pivot = session.selection_pivot() or (0.0, 0.0, 0.0)
        self._modal_pivot_world = _apply(session.matrix, pivot,
                                         session.position)
        cam = self._camera_vertex(self._modal_pivot_world)
        self._modal_denom = max(0.2, 4.0 - cam[2])
        self._modal_center_screen = self._project(cam)
        self._modal_op = op
        self._modal_axis = None
        self._modal_numeric = ""
        self._modal_keyboard_delta = (0.0, 0.0, 0.0)
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
        if self._modal_mouse_tracking_before is not None:
            self.setMouseTracking(self._modal_mouse_tracking_before)
            self._modal_mouse_tracking_before = None
        self._modal_op = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._modal_keyboard_delta = (0.0, 0.0, 0.0)
        self._direct_grab_active = False
        self._gizmo_drag_axis = None
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
        if self._modal_mouse_tracking_before is not None:
            self.setMouseTracking(self._modal_mouse_tracking_before)
            self._modal_mouse_tracking_before = None
        self._modal_axis = None
        self._modal_numeric = ""
        self._modal_keyboard_delta = (0.0, 0.0, 0.0)
        self._direct_grab_active = False
        self._gizmo_drag_axis = None
        self._refresh_edit_faces()
        self.editHint.emit("")
        self._clear_precision_guides()
        self.update()
