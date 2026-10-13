"""Destruction FX drawing for the Entity Designer viewport.

The viewport keeps its normal SET family loaded with the unit owner plus the
VP owners used by the effects. Each frame, preview items from
``destruction_fx.PreviewSimulation`` become transformed copies of those VP
faces, so geometry, BSP order and palette textures stay on the existing
Retail render path.

Faded or tinted instances are drawn as separate textured layers and then
composited with the engine shader rule for tint (``rgb = tint *
max(r, g, b)``, ``a *= tint.a``) and the fade factor as opacity.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPen, QPolygonF, QTransform,
)

from assembly_viewer import AssetViewport, ViewFace, _apply, _rotation_matrix
from entity_designer.destruction_fx import EmitterSpec

try:  # numpy is already required by the indexed renderer.
    import numpy as _np
except ImportError:  # pragma: no cover - optional acceleration only
    _np = None

# Opacity steps used to group faded instances into shared layers.
_ALPHA_STEPS = 20
_BOX_EDGES = (
    (0, 1), (1, 3), (3, 2), (2, 0), (4, 5), (5, 7), (7, 6), (6, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
)


def anim_state_at(frames, anim_type: int, time_ms: float) -> tuple[int, int]:
    """Frame/direction of a VANM material ``time_ms`` after it started.

    Uses the same stepping as AssetViewport._next_frame (loop or ping-pong,
    durations in 1024 Hz ticks), so FX textures follow the preview clock,
    including slow motion and scrubbing.
    """

    if not frames:
        return 0, 1
    frame, direction = 0, 1
    left = max(0.0, time_ms)
    for _guard in range(100000):
        duration = frames[frame][0] * 1000.0 / 1024.0
        if duration <= 0 or left < duration:
            break
        left -= duration
        frame += direction
        if frame >= len(frames):
            if anim_type:
                frame, direction = len(frames) - 1, -1
            else:
                frame = 0
        elif frame < 0:
            frame, direction = 0, 1
    return frame, direction


def _transform(item, vertex):
    sx, sy, sz = item.scale
    local = (vertex[0] * sx, vertex[1] * sy, vertex[2] * sz)
    rot = item.rotation
    return (
        item.position[0] + rot[0][0] * local[0] + rot[0][1] * local[1] + rot[0][2] * local[2],
        item.position[1] + rot[1][0] * local[0] + rot[1][1] * local[1] + rot[1][2] * local[2],
        item.position[2] + rot[2][0] * local[0] + rot[2][1] * local[1] + rot[2][2] * local[2],
    )


def colorize_image(image: QImage, tint) -> QImage:
    """Apply the engine Colorize shader rule to an RGBA layer."""

    if tint is None:
        return image
    r, g, b, _a = (value / 255.0 for value in tint)
    if (r, g, b) == (1.0, 1.0, 1.0):
        return image
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if _np is None:
        return image
    width, height = image.width(), image.height()
    buffer = image.bits()
    pixels = _np.frombuffer(buffer, dtype=_np.uint8).reshape(
        height, image.bytesPerLine() // 4, 4)[:, :width]
    intensity = pixels[..., :3].max(axis=2).astype(_np.float32)
    colored = _np.empty((height, width, 4), dtype=_np.uint8)
    colored[..., 0] = _np.clip(intensity * r, 0, 255)
    colored[..., 1] = _np.clip(intensity * g, 0, 255)
    colored[..., 2] = _np.clip(intensity * b, 0, 255)
    colored[..., 3] = pixels[..., 3]
    result = QImage(colored.tobytes(), width, height, width * 4,
                    QImage.Format.Format_RGBA8888)
    return result.copy()


class FxPreviewViewportMixin:
    """Adds the Destruction FX preview pass to a CollisionViewport."""

    def _fx_init(self) -> None:
        self._fx_active = False
        self._fx_home_camera = None
        self._fx_follow_enabled = False
        self._fx_tracking_item = None
        self._fx_tracking_owner = None
        self._fx_items = []
        self._fx_owner_by_vp: dict[int, str] = {}
        self._fx_unit_owners: set[str] = set()
        self._fx_boxes = []
        self._fx_time_ms = 0.0
        self._fx_show_unit = True
        self._fx_revision = 0
        self._fx_data_root: Path | None = None
        self._fx_decal_cache: dict[str, QImage | None] = {}
        self._fx_highlight: int | None = None
        self._fx_particles = []
        # owner -> [(EmitterSpec, [stage look])]; rebuilt with the family.
        self._fx_emitter_cache: dict[str, list] = {}
        self._fx_emitter_family = None
        self._fx_tinted_stage_cache: dict[tuple, QImage] = {}
        # External 3DS/BASE models: path -> hidden renderer (None = failed).
        self._fx_external_loader = None
        self._fx_external_cache: dict[str, AssetViewport | None] = {}

    def set_fx_preview(self, *, active: bool, items=(), owner_by_vp=None,
                       unit_owners=None, boxes=(), time_ms: float = 0.0,
                       show_unit: bool = True, data_root=None,
                       highlight: int | None = None, particles=(),
                       external_loader=None) -> None:
        self._fx_active = bool(active)
        if external_loader is not None:
            self._fx_external_loader = external_loader
        self._fx_items = list(items)
        self._fx_particles = list(particles)
        self._fx_owner_by_vp = dict(owner_by_vp or {})
        self._fx_unit_owners = set(unit_owners or ())
        self._fx_boxes = list(boxes)
        self._fx_time_ms = float(time_ms)
        self._fx_show_unit = bool(show_unit)
        self._fx_highlight = highlight
        if data_root is not None and Path(data_root) != self._fx_data_root:
            self._fx_data_root = Path(data_root)
            self._fx_decal_cache.clear()
        self._fx_revision += 1
        self.update()

    def set_fx_follow_unit(self, enabled, item, fallback_owner=None):
        """Follow the unit's transformed model center without changing zoom or time."""
        self._fx_follow_enabled = enabled
        self._fx_tracking_item = item
        self._fx_tracking_owner = fallback_owner
        if not enabled or item is None:
            return
        owner = self._fx_owner_by_vp.get(int(item.vp), fallback_owner)
        bounds = self._model_preview_base_owner_bounds.get(owner)
        center = tuple((bounds[axis] + bounds[axis + 3]) * .5 for axis in range(3)) if bounds else (0., 0., 0.)
        self._center = _transform(item, center)
        self.update()

    def reset_view(self) -> None:
        """Restore the FX scene camera while keeping its models and clock unchanged."""
        if self._fx_active and self._fx_home_camera is not None:
            previous_center = self._center
            self._set_camera_state(self._fx_home_camera)
            if self._fx_follow_enabled and self._fx_tracking_item is None:
                self._center = previous_center
            self.set_fx_follow_unit(self._fx_follow_enabled, self._fx_tracking_item,
                                    self._fx_tracking_owner)
            self.update()
        else:
            super().reset_view()

    @property
    def fx_preview_active(self) -> bool:
        return self._fx_active

    def _fx_cache_signature(self) -> tuple:
        return ("fx", self._fx_revision) if self._fx_active else ()

    # -- geometry -------------------------------------------------------------

    def _fx_templates(self) -> dict[str, list]:
        templates: dict[str, list] = {}
        fx_owners = set(self._fx_owner_by_vp.values())
        base_faces = self._model_preview_base_faces
        if len(base_faces) != len(self._faces):
            return templates
        for face, vertices in zip(self._faces, base_faces):
            if face.owner in fx_owners:
                templates.setdefault(face.owner, []).append((face, vertices))
        return templates

    def _fx_item_faces(self, item, templates) -> list[ViewFace]:
        owner = self._fx_owner_by_vp.get(int(item.vp)) if item.vp else None
        template = templates.get(owner) if owner else None
        if not template:
            return []
        faces = []
        for face, vertices in template:
            faces.append(ViewFace(
                vertices=[_transform(item, vertex) for vertex in vertices],
                uvs=face.uvs, material=face.material, shade=face.shade,
                poly_id=face.poly_id, animated=face.animated,
                # The source owner is kept: the indexed renderer resolves
                # each material block by (owner, block index).
                mapped=face.mapped, primary=False, owner=face.owner,
                block_index=face.block_index))
        return faces

    # -- particle emitters -------------------------------------------------------

    def fx_emitter_specs(self, owner: str | None) -> list[EmitterSpec]:
        """particle.class emitters of a VP owner, in model space."""

        return [spec for spec, _stages in self._fx_emitters(owner)]

    def _fx_emitters(self, owner: str | None) -> list:
        family = self._family_ref
        if owner is None or family is None:
            return []
        if family is not self._fx_emitter_family:
            self._fx_emitter_family = family
            self._fx_emitter_cache.clear()
            self._fx_tinted_stage_cache.clear()
        cached = self._fx_emitter_cache.get(owner)
        if cached is not None:
            return cached
        emitters = []
        obj = next((candidate for candidate in family.all_objects()
                    if getattr(candidate, "owner_path", None) == owner), None)
        skeleton = getattr(obj, "skeleton", None)
        if obj is not None and skeleton is not None:
            transform = obj.base_object.transform
            if transform is not None:
                matrix = _rotation_matrix(transform.euler, transform.scale)
                origin = transform.position
            else:
                matrix = _rotation_matrix((0, 0, 0), (1.0, 1.0, 1.0))
                origin = (0.0, 0.0, 0.0)
            for block in obj.base_object.ades or ():
                attributes = getattr(block, "particle_attributes", None)
                if (block.class_id or "").casefold() != "particle.class" \
                        or attributes is None:
                    continue
                point_id = block.ade_point_id
                local = (skeleton.points[point_id]
                         if 0 <= point_id < len(skeleton.points) else (0, 0, 0))
                point = _apply(matrix, local, origin)
                stages = [self._particle_stage_look(family, stage)
                          for stage in block.particle_stages]
                emitters.append((EmitterSpec.from_attributes(
                    point, attributes, max(1, len(stages))), stages))
        self._fx_emitter_cache[owner] = emitters
        return emitters

    def _particle_stage_look(self, family, stage) -> dict | None:
        """Texture, source rectangle(s) and blending of one life stage."""

        texture = getattr(stage, "texture", None)
        if texture is None:
            return None
        additive = getattr(stage, "tracy_mode", "none") == "flat"

        def image_for(name: str) -> QImage:
            override = (family.effect_override_paths.get(name.lower())
                        if additive else None)
            return self._cached_texture_image(
                family, name, family.textures.get(name), override)

        def rect_for(image: QImage, uvs) -> QRectF:
            if not uvs or image.isNull():
                return QRectF(0, 0, max(1, image.width()), max(1, image.height()))
            us = [uv[0] for uv in uvs]
            vs = [uv[1] for uv in uvs]
            sx, sy = image.width() / 256.0, image.height() / 256.0
            return QRectF(min(us) * sx, min(vs) * sy,
                          max(1.0, (max(us) - min(us)) * sx),
                          max(1.0, (max(vs) - min(vs)) * sy))

        if texture.kind == "bmpanim":
            animation = family.animations.get(texture.name)
            if animation is None:
                return None
            images = [image_for(name) for name in animation.bitmap_names]
            frames = [(frame.frame_time, frame.frame_id, frame.texcoords_id)
                      for frame in animation.frames]
            groups = list(animation.texcoord_groups)
            return {"additive": additive, "frames": frames, "images": images,
                    "groups": groups, "anim_type": texture.anim_type or 0}
        image = image_for(texture.name)
        if image.isNull():
            return None
        return {"additive": additive, "image": image,
                "rect": rect_for(image, texture.outline_uvs)}

    def _particle_frame(self, look: dict) -> tuple[QImage, QRectF] | None:
        if "image" in look:
            return look["image"], look["rect"]
        frames = look["frames"]
        if not frames:
            return None
        frame, _direction = anim_state_at(frames, look["anim_type"],
                                          self._fx_time_ms)
        _duration, image_id, group_id = frames[frame]
        if not (0 <= image_id < len(look["images"])):
            return None
        image = look["images"][image_id]
        if image.isNull():
            return None
        uvs = look["groups"][group_id] if 0 <= group_id < len(look["groups"]) else ()
        if not uvs:
            return image, QRectF(0, 0, image.width(), image.height())
        us = [uv[0] for uv in uvs]
        vs = [uv[1] for uv in uvs]
        sx, sy = image.width() / 256.0, image.height() / 256.0
        return image, QRectF(min(us) * sx, min(vs) * sy,
                             max(1.0, (max(us) - min(us)) * sx),
                             max(1.0, (max(vs) - min(vs)) * sy))

    def _draw_fx_particles(self, painter, target, camera) -> None:
        """Camera-facing particle quads, far to near (ParticleSystem::Render)."""

        if not self._fx_particles:
            return
        scale = float(camera["scale"])
        drawn = []
        for sprite in self._fx_particles:
            owner = self._fx_owner_by_vp.get(int(sprite.vp))
            emitters = self._fx_emitters(owner)
            if not 0 <= sprite.emitter < len(emitters):
                continue
            stages = emitters[sprite.emitter][1]
            look = stages[min(sprite.stage, len(stages) - 1)] if stages else None
            if look is None:
                continue
            center = self._camera_vertex(sprite.position, camera)
            if not self._camera_point_is_projectable(center):
                continue
            drawn.append((center[2], sprite, look, center))
        drawn.sort(key=lambda entry: entry[0])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for _depth, sprite, look, center in drawn:
            frame = self._particle_frame(look)
            if frame is None:
                continue
            image, source = frame
            middle = self._project(center, target, camera)
            edge_point = (center[0] + sprite.size * 0.5 * scale,
                          center[1], center[2])
            edge = self._project(edge_point, target, camera)
            half = math.hypot(edge.x() - middle.x(), edge.y() - middle.y())
            if half < 0.3 or half > target.width() * 4:
                continue
            tint = sprite.tint
            if tint is not None and tuple(tint[:3]) != (255, 255, 255):
                key = (id(image), tuple(tint[:3]))
                tinted = self._fx_tinted_stage_cache.get(key)
                if tinted is None:
                    tinted = colorize_image(image, (*tint[:3], 255))
                    self._fx_tinted_stage_cache[key] = tinted
                image = tinted
            opacity = sprite.alpha * ((tint[3] / 255.0) if tint else 1.0)
            painter.setOpacity(max(0.0, min(1.0, opacity)))
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Plus
                if look["additive"] else
                QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.drawImage(
                QRectF(middle.x() - half, middle.y() - half, half * 2, half * 2),
                image, source)
        painter.restore()

    # -- external 3DS/BASE models ---------------------------------------------

    def _fx_external_for(self, item) -> AssetViewport | None:
        """Hidden renderer of the first loadable external model of an item.

        Like the engine, 3DS is tried before BASE and a model that cannot be
        loaded falls back to the item's VP.
        """

        for path in item.external:
            if path not in self._fx_external_cache:
                helper = None
                loader = self._fx_external_loader
                family = loader(path) if loader is not None else None
                if family is not None:
                    helper = AssetViewport()
                    helper.load_family(family, None)
                    helper._fx_base_faces = [
                        (face, [tuple(vertex) for vertex in face.vertices])
                        for face in helper._faces]
                self._fx_external_cache[path] = helper
            helper = self._fx_external_cache[path]
            if helper is not None:
                return helper
        return None

    def _draw_fx_external(self, painter, target, camera, helper, items) -> None:
        """Render external-model instances with their own family palette."""

        helper._faces = [
            ViewFace(vertices=[_transform(item, vertex) for vertex in vertices],
                     uvs=face.uvs, material=face.material, shade=face.shade,
                     poly_id=face.poly_id, animated=face.animated,
                     mapped=face.mapped, primary=False, owner=face.owner,
                     block_index=face.block_index)
            for item in items for face, vertices in helper._fx_base_faces]
        width = max(1, int(round(target.width())))
        height = max(1, int(round(target.height())))
        layer = QImage(width, height, QImage.Format.Format_RGBA8888)
        layer.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(layer)
        try:
            helper._render_scene(
                layer_painter, QRectF(0, 0, width, height), None, clean=True,
                camera=camera, allow_transparent_background=True)
        finally:
            layer_painter.end()
        painter.drawImage(target, layer)

    def _fx_material_ids(self, templates) -> set[int]:
        return {face.material for faces in templates.values()
                for face, _vertices in faces}

    # -- painting -------------------------------------------------------------

    def _paint_fx_viewport(self, painter: QPainter) -> None:
        target = QRectF(self.rect())
        camera = self._camera_state()
        saved_faces = self._faces
        saved_anim = dict(self._anim_states)
        templates = self._fx_templates()
        fx_owners = set(templates)
        # The unit can also be an effect template (vp_normal phase items), so
        # it is chosen by owner; effect-only owners are never drawn in place.
        unit_owners = self._fx_unit_owners
        unit_faces = [
            face for face in saved_faces
            if (face.owner in unit_owners if unit_owners
                else face.owner not in fx_owners)
        ] if self._fx_show_unit and self._collision_show_model else []

        main_faces: list[ViewFace] = []
        layers: dict[tuple, list[ViewFace]] = {}
        markers = []
        decals = []
        externals: dict[int, tuple[AssetViewport, list]] = {}
        for item in self._fx_items:
            if item.kind == "ground_decal":
                decals.append(item)
                continue
            helper = self._fx_external_for(item)
            if helper is not None:
                externals.setdefault(id(helper), (helper, []))[1].append(item)
                continue
            faces = self._fx_item_faces(item, templates)
            if not faces:
                # A VP found in the SET but without faces is either drawn by
                # its particle emitters or is an intentionally empty model
                # (DUMMY). Only VPs missing from the SET get a marker.
                if not item.vp or int(item.vp) not in self._fx_owner_by_vp:
                    markers.append(item)
                continue
            tinted = item.tint is not None and tuple(item.tint) != (255, 255, 255, 255)
            opacity = item.alpha * ((item.tint[3] / 255.0) if item.tint else 1.0)
            if not tinted and opacity >= 0.999:
                main_faces.extend(faces)
                continue
            rgb = tuple(item.tint[:3]) if tinted else None
            step = max(0, min(_ALPHA_STEPS, round(opacity * _ALPHA_STEPS)))
            if step == 0:
                continue
            layers.setdefault((step, rgb), []).extend(faces)

        # The toolbar controls unit VANM textures independently of the FX clock.
        # Effect textures still follow scene playback and timeline scrubbing.
        animated_unit_owners = set(unit_owners)
        animated_unit_owners.update(
            self._fx_owner_by_vp.get(int(item.vp))
            for item in self._fx_items if item.kind == "unit" and item.vp)
        unit_materials = {face.material for face in saved_faces
                          if face.owner in animated_unit_owners}
        for material_id in self._fx_material_ids(templates) - unit_materials:
            material = self._materials[material_id]
            if material.anim_frames:
                self._anim_states[material_id] = anim_state_at(
                    material.anim_frames, material.anim_type, self._fx_time_ms)

        textured = self._mode == "textured"
        try:
            self._faces = unit_faces + main_faces
            if not textured:
                for faces in layers.values():
                    self._faces.extend(faces)
            # Empty FX phases still need the ground; the shared renderer
            # returns before drawing guides when no faces remain.
            if not self._faces:
                painter.fillRect(target, QColor(24, 26, 32))
                if self._show_grid:
                    self._draw_grid(painter, target, camera)
            else:
                AssetViewport._paint_viewport(self, painter)
            self._draw_fx_decals(painter, target, camera, decals)
            for helper, helper_items in externals.values():
                self._draw_fx_external(painter, target, camera, helper,
                                       helper_items)
            if textured:
                for (step, rgb), faces in sorted(
                        layers.items(), key=lambda entry: entry[0][0]):
                    self._faces = faces
                    self._draw_fx_layer(
                        painter, target, camera, step / _ALPHA_STEPS, rgb)
        finally:
            self._faces = saved_faces
            self._anim_states = saved_anim
        self._draw_fx_particles(painter, target, camera)
        self._draw_fx_boxes(painter, target, camera)
        self._draw_fx_markers(painter, target, camera, markers)

    def _draw_fx_layer(self, painter, target, camera, opacity, rgb) -> None:
        width = max(1, int(round(target.width())))
        height = max(1, int(round(target.height())))
        layer = QImage(width, height, QImage.Format.Format_RGBA8888)
        layer.fill(Qt.GlobalColor.transparent)
        layer_painter = QPainter(layer)
        try:
            self._render_scene(
                layer_painter, QRectF(0, 0, width, height), None, clean=True,
                camera=camera, allow_transparent_background=True)
        finally:
            layer_painter.end()
        if rgb is not None:
            layer = colorize_image(layer, (*rgb, 255))
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.drawImage(target, layer)
        painter.restore()

    def _fx_project(self, point, target, camera) -> QPointF | None:
        camera_point = self._camera_vertex(point, camera)
        if not self._camera_point_is_projectable(camera_point):
            return None
        return self._project(camera_point, target, camera)

    def _decal_image(self, texture: str) -> QImage | None:
        if texture in self._fx_decal_cache:
            return self._fx_decal_cache[texture]
        image = None
        root = self._fx_data_root
        if root is not None and texture:
            relative = texture.replace("\\", "/")
            if relative.casefold().startswith("data/"):
                relative = relative[5:]
            candidate = root / relative
            if candidate.is_file():
                loaded = QImage(str(candidate))
                image = None if loaded.isNull() else loaded.convertToFormat(
                    QImage.Format.Format_ARGB32_Premultiplied)
        self._fx_decal_cache[texture] = image
        return image

    def _draw_fx_decals(self, painter, target, camera, decals) -> None:
        for item in decals:
            half_x = item.decal_size[0] * 0.5
            half_z = item.decal_size[1] * 0.5
            corners_local = ((-half_x, 0.0, -half_z), (half_x, 0.0, -half_z),
                             (half_x, 0.0, half_z), (-half_x, 0.0, half_z))
            corners = [self._fx_project(_transform(item, corner), target, camera)
                       for corner in corners_local]
            if any(point is None for point in corners):
                continue
            polygon = QPolygonF(corners)
            tint = item.tint or (255, 255, 255, 255)
            opacity = item.alpha * tint[3] / 255.0
            image = self._decal_image(item.texture)
            painter.save()
            painter.setOpacity(max(0.0, min(1.0, opacity)))
            if image is not None:
                image = colorize_image(image, tint)
                source = QPolygonF([
                    QPointF(0, 0), QPointF(image.width(), 0),
                    QPointF(image.width(), image.height()),
                    QPointF(0, image.height())])
                transform = QTransform()
                if QTransform.quadToQuad(source, polygon, transform):
                    painter.setTransform(transform, True)
                    painter.drawImage(0, 0, image)
            else:
                painter.setPen(QPen(QColor(230, 180, 90), 1.2, Qt.PenStyle.DashLine))
                painter.setBrush(QColor(tint[0], tint[1], tint[2], 70))
                painter.drawPolygon(polygon)
            painter.restore()

    def _draw_fx_boxes(self, painter, target, camera) -> None:
        if not self._fx_boxes:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for source, low, high in self._fx_boxes:
            selected = source == self._fx_highlight
            color = QColor(255, 214, 90, 210 if selected else 120)
            painter.setPen(QPen(color, 1.4 if selected else 1.0,
                                Qt.PenStyle.DashLine))
            corners = [
                (x, y, z) for x in (low[0], high[0])
                for y in (low[1], high[1]) for z in (low[2], high[2])]
            points = [self._fx_project(corner, target, camera)
                      for corner in corners]
            for a, b in _BOX_EDGES:
                if points[a] is not None and points[b] is not None:
                    painter.drawLine(points[a], points[b])
        painter.restore()

    def _draw_fx_markers(self, painter, target, camera, markers) -> None:
        if not markers:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for item in markers:
            point = self._fx_project(item.position, target, camera)
            if point is None:
                continue
            size = 7.0
            diamond = QPolygonF([
                point + QPointF(0, -size), point + QPointF(size, 0),
                point + QPointF(0, size), point + QPointF(-size, 0)])
            painter.setPen(QPen(QColor(255, 120, 90), 1.4))
            painter.setBrush(QColor(255, 120, 90, 60))
            painter.drawPolygon(diamond)
            painter.setPen(QColor(255, 190, 170))
            painter.drawText(point + QPointF(size + 3, 4),
                             item.label or "model unavailable")
        painter.restore()
