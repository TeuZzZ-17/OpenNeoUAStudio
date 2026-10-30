"""AssetViewport mixin: in-viewport axis handles."""

from __future__ import annotations

import math
import zlib

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPen,
)

from studio.assets.family import AssetFamily, FamilyObject
from studio.rendering.geometry import AXIS_VECTORS, mat_apply
from studio.rendering.particles import ParticleEmitter
from studio.rendering.asset_viewport_common import (
    MATERIAL_COLORS,
    ViewMaterial,
    ViewFace,
    _rotation_matrix,
    _apply,
    _image_from_ilbm,
    _image_from_effect_png,
)


class _AxisHandlesMixin:
    """Part of AssetViewport; `self` is always the AssetViewport."""

    def _axis_screen_vector(self, axis: str, pivot) -> tuple[float, float]:
        """Screen pixels covered by one model unit along ``axis`` at ``pivot``."""

        session = self._edit_session
        world = _apply(session.matrix, pivot, session.position)
        axis_world = mat_apply(session.matrix, AXIS_VECTORS[axis])
        end = tuple(world[index] + axis_world[index] for index in range(3))
        a = self._project(self._camera_vertex(world))
        b = self._project(self._camera_vertex(end))
        return b.x() - a.x(), b.y() - a.y()

    def _gizmo_handles(self):
        """Axis handles as ``{axis: (center, tip)}`` in widget pixels.

        Empty while nothing is selected or another operation owns the mouse.
        An axis pointing straight at the camera has no usable direction and
        gets no handle.
        """

        session = self._edit_session
        if (session is None or self._snapshot_active
                or not session.selection or self._modal_op is not None
                or session.modal_active or self._paste_preview is not None):
            return {}
        pivot = session.selection_pivot()
        if pivot is None:
            return {}
        world = _apply(session.matrix, pivot, session.position)
        center = self._project(self._camera_vertex(world))
        handles = {}
        for axis in "XYZ":
            dx, dy = self._axis_screen_vector(axis, pivot)
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            tip = QPointF(
                center.x() + dx / length * self._GIZMO_LENGTH,
                center.y() + dy / length * self._GIZMO_LENGTH)
            handles[axis] = (center, tip)
        return handles

    @staticmethod
    def _distance_along_segment(point, start, end) -> tuple[float, float]:
        """Distance of ``point`` to the segment and how far along it (pixels)."""

        sx, sy = end.x() - start.x(), end.y() - start.y()
        length = math.hypot(sx, sy)
        if length < 1e-9:
            return math.inf, 0.0
        along = ((point.x() - start.x()) * sx
                 + (point.y() - start.y()) * sy) / length
        along_clamped = max(0.0, min(length, along))
        px = start.x() + sx / length * along_clamped
        py = start.y() + sy / length * along_clamped
        return math.hypot(point.x() - px, point.y() - py), along

    def _gizmo_axis_at(self, position: QPoint) -> str | None:
        """Axis handle under ``position``; the part near the pivot is skipped."""

        best = None
        point = QPointF(position)
        for axis, (center, tip) in self._gizmo_handles().items():
            distance, along = self._distance_along_segment(point, center, tip)
            if along < self._GIZMO_DEAD_ZONE \
                    or distance > self._GIZMO_GRAB_RADIUS:
                continue
            if best is None or distance < best[0]:
                best = (distance, axis)
        return best[1] if best is not None else None

    def _begin_gizmo_drag(self, axis: str, position: QPoint) -> None:
        """Start the active direct transform locked to one model axis."""

        op = ("grab" if self._direct_transform_mode == "move"
              else self._direct_transform_mode)
        self._begin_modal(op, position, direct_grab=True)
        if self._modal_op is None:
            return
        self._modal_axis = axis
        self._gizmo_drag_axis = axis
        self._gizmo_hover_axis = None
        self._modal_last_mouse = position
        self._update_modal(position)

    def _gizmo_drag_delta(self, position: QPoint):
        """Model-space move along the dragged axis, following the mouse."""

        session = self._edit_session
        axis = self._gizmo_drag_axis
        pivot = session.selection_pivot() if session is not None else None
        if axis is None or pivot is None:
            return None
        dx, dy = self._axis_screen_vector(axis, pivot)
        pixels_per_unit = math.hypot(dx, dy)
        if pixels_per_unit < 1e-6:
            return (0.0, 0.0, 0.0)
        moved = ((position.x() - self._modal_start.x()) * dx
                 + (position.y() - self._modal_start.y()) * dy
                 ) / (pixels_per_unit * pixels_per_unit)
        unit = AXIS_VECTORS[axis]
        return (unit[0] * moved, unit[1] * moved, unit[2] * moved)

    def _draw_gizmo_handles(self, painter: QPainter, target: QRectF) -> None:
        handles = self._gizmo_handles()
        if not handles:
            return
        scale_x = target.width() / max(1, self.width())
        scale_y = target.height() / max(1, self.height())

        def to_target(point: QPointF) -> QPointF:
            return QPointF(target.left() + point.x() * scale_x,
                           target.top() + point.y() * scale_y)

        for axis, (center, tip) in handles.items():
            color = self._GIZMO_COLORS[axis]
            hovered = axis == self._gizmo_hover_axis
            start, end = to_target(center), to_target(tip)
            painter.setPen(QPen(
                color.lighter(130) if hovered else color,
                3.4 if hovered else 2.2))
            painter.drawLine(start, end)
            direction = end - start
            length = math.hypot(direction.x(), direction.y())
            if length < 1e-9:
                continue
            ux, uy = direction.x() / length, direction.y() / length
            head = 11.0 if hovered else 9.0
            base = QPointF(end.x() - ux * head, end.y() - uy * head)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color.lighter(130) if hovered else color)
            painter.drawPolygon([
                end,
                QPointF(base.x() - uy * head * 0.45,
                        base.y() + ux * head * 0.45),
                QPointF(base.x() + uy * head * 0.45,
                        base.y() - ux * head * 0.45),
            ])

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
        suffix = " | X/Y/Z axis"
        if self._modal_op == "grab":
            suffix += " - arrows nudge - Shift+arrows faster"
        suffix += " - type a number - LMB/Enter ok, RMB/Esc cancel"
        if self._modal_numeric:
            suffix += f" | typed: {self._modal_numeric}"

        if self._modal_op == "grab":
            axis_drag = (self._gizmo_drag_delta(pos)
                         if self._gizmo_drag_axis is not None else None)
            if numeric is not None and self._modal_axis is not None:
                unit = AXIS_VECTORS[self._modal_axis]
                delta = (unit[0] * numeric, unit[1] * numeric,
                         unit[2] * numeric)
            elif axis_drag is not None:
                delta = axis_drag
            else:
                world = self._screen_delta_to_world(
                    pos.x() - self._modal_start.x(),
                    pos.y() - self._modal_start.y())
                delta = session.world_delta_to_model(world)
            delta = tuple(
                delta[index] + self._modal_keyboard_delta[index]
                for index in range(3))
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

        if build_faces:
            self._load_particle_emitters(
                fam_obj, family, material_index, world_points, matrix, owner)

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
            emitter_polys = {
                block.ade_poly_id for block in fam_obj.base_object.ades
                if block.particle_attributes is not None}
            # Native BASE mesh generation skips particle ADEs. Their polygon
            # selectors are emitter metadata, not missing static surfaces.
            unmapped = [p for p in range(len(polygons))
                        if p not in covered and p not in emitter_polys
                        and len(polygons[p]) >= 3]
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

    def _load_particle_emitters(self, fam_obj, family, material_index,
                                world_points, matrix, owner) -> None:
        """Reuse the normal texture/material path for native PTCL life stages."""
        stage_id = len(fam_obj.materials)
        for block_index, block in enumerate(fam_obj.base_object.ades):
            attributes = block.particle_attributes
            if attributes is None:
                continue
            if (attributes.birth_rate <= 0 or attributes.lifetime <= 0
                    or attributes.context_stop_generation
                    <= attributes.context_start_generation):
                self._diagnostics.append(
                    f"{owner}: particle emitter has no active generation window.")
                continue
            if not 0 <= block.ade_point_id < len(world_points):
                self._diagnostics.append(
                    f"{owner}: particle emitter has an invalid source point.")
                continue
            stages = []
            for stage in block.particle_stages[:8]:
                texture = stage.texture
                if stage.class_id == "particle.class" or texture is None:
                    self._diagnostics.append(
                        f"{owner}: particle life stage has no drawable texture.")
                    stages.append((-1, stage_id, stage))
                    stage_id += 1
                    continue
                name, kind = texture.name, texture.kind
                mat_id = self._ensure_material(
                    f"{owner}|particle|{stage_id}", name, kind, family,
                    material_index, anim_name=name if kind == "bmpanim" else "",
                    block=stage)
                if self._indexed_adapter is not None:
                    self._indexed_adapter.block_map[(owner, stage_id)] = stage
                stages.append((mat_id, stage_id, stage))
                stage_id += 1
            if stages:
                self._particle_emitters.append(ParticleEmitter(
                    attributes, world_points[block.ade_point_id], matrix,
                    stages, owner,
                    zlib.crc32(f"{owner}:{block_index}".encode("utf-8"))))

    @property
    def _particle_duration_ms(self) -> float:
        return max((emitter.duration_ticks for emitter in self._particle_emitters),
                   default=0.0) * 1000.0 / 1024.0

    def _particle_faces_at(self, time_ms):
        period = self._particle_duration_ms
        ticks = (time_ms % period if period > 0 else 0) * 1024.0 / 1000.0
        for emitter in self._particle_emitters:
            for position, size, (mat_id, stage_id, stage) in emitter.samples(ticks):
                if mat_id < 0:
                    continue
                radius = size * 0.5
                x, y, z = position
                vertices = [(x - radius, y - radius, z),
                            (x - radius, y + radius, z),
                            (x + radius, y + radius, z),
                            (x + radius, y - radius, z)]
                uvs = (stage.texture.outline_uvs if stage.texture else [])
                yield ViewFace(
                    vertices, list(uvs), mat_id, shade=stage.shade_val,
                    animated=bool(self._materials[mat_id].anim_frames),
                    primary=False, owner=emitter.owner, block_index=stage_id,
                    billboard_center=position)

    def _refresh_particle_faces(self) -> None:
        self._faces = [face for face in self._faces
                       if face.billboard_center is None]
        self._faces.extend(self._particle_faces_at(self._anim_clock_ms))
        self._invalidate_indexed_view_cache()

    def _build_particle_bounds(self) -> None:
        """Measure one full effect burst and the ground, so both stay fixed.

        The envelope is kept apart from ``_owner_bounds``: those are the
        bounds of the model itself and also drive selection framing,
        collision generation and Follow unit. A smoke column rising from a
        wreck must not move or resize the wreck's own bounds.
        """

        points = []
        owner_bounds: dict[str, tuple] = {}
        duration = self._particle_duration_ms
        if duration > 0:
            for index in range(17):
                for face in self._particle_faces_at(duration * index / 17):
                    points.extend(face.vertices)
                    x, y, z = face.billboard_center
                    radius = abs(face.vertices[0][0] - x)
                    points.extend(((x, y, z - radius), (x, y, z + radius)))
                    old = owner_bounds.get(face.owner, (x, y, z, x, y, z))
                    owner_bounds[face.owner] = (
                        min(old[0], x - radius), min(old[1], y - radius),
                        min(old[2], z - radius), max(old[3], x + radius),
                        max(old[4], y + radius), max(old[5], z + radius))
        self._particle_bounds_points = points
        self._particle_owner_bounds = owner_bounds
        # UA +Y points down, so the largest Y is the lowest point. Every VP
        # stands on the same ground: the bottom of its model. Only an effect
        # without any surface (an explosion) stands on its lowest particle.
        low = [point[1] for point in (self._static_points() or points)]
        self._ground_y = max(low) if low else None

    def _static_points(self):
        return [v for face in self._faces if face.billboard_center is None
                for v in face.vertices]

    def _framing_points(self):
        """Points the camera frames: the model, or the effect if that is all.

        A VP made only of emitters (an explosion) has no surface, so its
        particle envelope is framed. A model that merely emits smoke is
        framed on its own geometry; the smoke may leave the view.
        """

        return self._static_points() or list(self._particle_bounds_points)

    def _framing_bounds(self, owner: str):
        """Bounds used to frame one owner (see ``_framing_points``)."""

        has_surface = any(face.owner == owner and face.billboard_center is None
                          for face in self._faces)
        if not has_surface and owner in self._particle_owner_bounds:
            return self._particle_owner_bounds[owner]
        return self._owner_bounds.get(owner)

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
