"""AssetViewport mixin: painting (OpenGL and overlay drawing)."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)

from studio.rendering.depth import CameraPolygon, clip_camera_polygon_near, order_camera_polygons
from studio.rendering.indexed import (
    IndexedFamilyAdapter,
    IndexedPiece,
    IndexedRasterizer,
    retail_source_face_front_facing,
    UnsupportedIndexedMaterialError,
    VANILLA_FADE_LENGTH,
)
from studio.rendering.asset_viewport_common import (
    _PICK_CAMERA_DISTANCE,
    PASTE_GHOST_OPACITY,
    MODEL_EDIT_SELECTION_RED,
    ViewMaterial,
    ViewFace,
    _RenderPayload,
    PickShape,
    _apply,
)


class _PaintingMixin:
    """Part of AssetViewport; `self` is always the AssetViewport."""

    # -- painting ------------------------------------------------------------------

    def _front_facing_from_screen_area(self, area: float) -> bool:
        """Return the regular editor front-face convention for screen area.

        Runtime-specific workspaces may override this without changing the
        shared Model Editor / Snapshot rendering contract.
        """

        return float(area) >= 0.0

    def _uses_retail_source_face_culling(self) -> bool:
        """Whether this viewport uses Retail whole-source-face culling.

        The normal Model Editor/Snapshot path stays exactly Retail-faithful.
        Runtime-specific views may opt out and perform their visibility test
        on the clipped fan triangles instead, without introducing a second
        renderer.
        """

        return True

    def _render_piece_front_facing(
            self, camera_vertices, target: QRectF, camera: dict,
            source_front_facing: bool) -> bool:
        """Return the front-face decision for one clipped render piece.

        Retail geometry normally culls the complete source polygon before
        clipping, so every emitted fan piece inherits that decision. Runtime
        views that use GPU-style post-projection winding can override only this
        hook while retaining the shared clipping/BSP/indexed renderer.
        """

        return bool(source_front_facing)


    def _paint_viewport(self, painter: QPainter) -> None:
        background = self._snapshot_background
        if self._snapshot_active and background is None:
            background = QColor(24, 26, 32)
        self._render_scene(painter, QRectF(self.rect()), background,
                           clean=(self._snapshot_active
                                  and not self._snapshot_show_guides),
                           camera=self._camera_state())

    @property
    def renderer_name(self):
        effective = self._last_effective_renderer
        if effective == 'retail_indexed_gpu':
            return 'OpenGL indexed renderer'
        if effective == 'retail_indexed_reconstructed':
            return f'Software indexed rasterizer; presentation: {super().renderer_name}'
        if effective == 'retail_indexed_with_particle_overrides':
            return 'Indexed renderer with native particle textures'
        if effective == 'retail_indexed_error':
            return f'Software renderer error: {self._indexed_runtime_error}'
        return super().renderer_name

    def _renderer_mode_changed(self):
        self._last_effective_renderer = "not_rendered"
        self._gpu_indexed_unavailable = False

    def _render_scene(self, painter: QPainter, target: QRectF,
                      background: QColor | None, clean: bool,
                      camera: dict,
                      allow_transparent_background: bool = False,
                      force_textured: bool = False,
                      geometry_capture: list | None = None) -> None:
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
            if self._particle_emitters:
                # A frame after the last particle dies is a valid empty frame.
                self._last_effective_renderer = "retail_indexed_reconstructed"
                self._last_render_error = ""
                return
            if not clean and not self._empty_hint_suppressed:
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
        placement = self._paste_preview
        hidden_move_owner = None
        hidden_move_polys: set[int] = set()
        if placement is not None and placement.operation == "move":
            hidden_move_owner = getattr(placement.clipboard, "owner", None)
            source_poly_ids = getattr(
                placement.clipboard, "source_poly_ids", None)
            if source_poly_ids is not None:
                hidden_move_polys.update(int(poly_id) for poly_id in source_poly_ids)
            else:
                hidden_move_polys.update(
                    int(copied.source_poly_id)
                    for copied in getattr(placement.clipboard, "polygons", ()))
        for face_order, face in enumerate(self._faces):
            if hidden_move_owner == face.owner and face.poly_id in hidden_move_polys:
                continue
            if (not clean and not face.mapped
                    and not self._mapping_diagnostics):
                continue
            if face.billboard_center is None:
                cam = tuple(self._camera_vertex(vertex, camera)
                            for vertex in face.vertices)
            else:
                center = self._camera_vertex(face.billboard_center, camera)
                x, y, _ = face.billboard_center
                # Particle quads face the camera, as in ParticleSystem::Render.
                cam = tuple((center[0] + (v[0] - x) * camera["scale"],
                             center[1] - (v[1] - y) * camera["scale"], center[2])
                            for v in face.vertices)
            source_front_facing = retail_source_face_front_facing(cam)
            retail_source_cull = self._uses_retail_source_face_culling()
            if retail_source_cull:
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
                piece_front_facing = self._render_piece_front_facing(
                    clipped.vertices, target, camera, source_front_facing)
                if not retail_source_cull:
                    if mode == "textured":
                        if not piece_front_facing:
                            continue
                    elif self._backface_cull and not piece_front_facing:
                        continue
                triangles.append(CameraPolygon(
                    clipped.vertices,
                    clipped.attributes,
                    _RenderPayload(
                        face, piece_front_facing, uv_mapping_valid,
                        face_order,
                        max(4.0 - point[2] for point in cam)),
                    source_order,
                ))
                source_order += 1

        # Do not switch BASE/SKLT to the old camera-drag approximation.  The
        # fork-derived Retail BSP path is now the single geometry truth for all
        # three public preview modes.
        ordered = order_camera_polygons(triangles)
        if geometry_capture is not None:
            geometry_capture.extend(ordered)
            return
        particle_layers = bool(
            self._family_ref and any(
                face.billboard_center is not None
                and self._materials[face.material].additive
                and any(name.casefold() in self._family_ref.effect_override_paths
                        for name in (self._materials[face.material].anim_bitmap_names
                                     or [self._materials[face.material].label]))
                for face in self._faces))
        indexed_ordered = (
            [piece for piece in ordered if piece.payload.face.billboard_center is None]
            if particle_layers else ordered)
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
                # Flat TRACY reads the actual scene destination, including the grid.
                backdrop = None
                if (self._indexed_adapter is not None
                        and any(mat.tracy_mode == "flat" for mat in self._materials)
                        and (background is not None or not allow_transparent_background)):
                    backdrop = self._indexed_scene_background(
                        target, camera, background, clean, camera_preview)
                if cache_key is not None:
                    cache_key += (background.rgba() if background is not None else None,
                                  self._show_grid, self._show_axes, camera_preview,
                                  allow_transparent_background)
                gpu_rendered = self._draw_gpu_indexed(
                    painter, target, indexed_ordered, camera, backdrop=backdrop)
                if gpu_rendered:
                    indexed_image = None
                elif (use_view_cache
                        and cache_key == self._indexed_view_cache_key
                        and not self._indexed_view_cache_image.isNull()):
                    indexed_image = QImage(self._indexed_view_cache_image)
                else:
                    indexed_image = self._render_indexed_model(
                        render_target, indexed_ordered, camera,
                        collect_diagnostics=clean,
                        background_indices=self._indexed_background_bytes(backdrop))
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
                if indexed_image is not None:
                    painter.drawImage(target, indexed_image)
                self._indexed_runtime_error = ""
                self._last_render_error = ""
                self._last_effective_renderer = (
                    "retail_indexed_gpu" if gpu_rendered else "retail_indexed_reconstructed")
                if particle_layers:
                    self._draw_particle_layers(painter, source_polygons, camera)
                    self._last_effective_renderer = "retail_indexed_with_particle_overrides"
                    self._last_indexed_stats["particle_rendering"] = "native_rgba"

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
                and self._edit_session is not None \
                and not (self._paste_preview is not None
                         and self._paste_preview.operation == "move"):
            self._draw_edit_overlay(painter, target, camera)
        elif not clean and not self._snapshot_active:
            self._draw_mode_label(painter, target, False)

    def _indexed_scene_background(self, target, camera, background, clean,
                                  camera_preview=False):
        """Draw the viewport destination using the same grid and axes as the scene."""
        width, height = max(1, round(target.width())), max(1, round(target.height()))
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(background if background is not None else QColor(24, 26, 32))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, not camera_preview)
        painter.translate(-target.x(), -target.y())
        if self._show_grid and not clean:
            self._draw_grid(painter, target, camera)
        if self._show_axes and not clean:
            self._draw_axes(painter, target, camera)
        painter.end()
        return image

    def _indexed_background_bytes(self, image):
        """Map the viewport background to SET palette indices for TRACY lookup."""
        if image is None:
            return None
        colors = [QColor(*rgb).rgb()
                  for rgb in self._indexed_adapter.tables.display_palette]
        indexed = image.convertToFormat(
            QImage.Format.Format_Indexed8, colors,
            Qt.ImageConversionFlag.AvoidDither)
        data = bytes(indexed.constBits())
        width, stride = indexed.width(), indexed.bytesPerLine()
        return b"".join(data[y * stride:y * stride + width]
                        for y in range(indexed.height()))

    def _draw_particle_layers(self, painter, polygons, camera) -> None:
        """Use the engine's RGBA FX overrides with the usual particle blending."""
        faces = sorted(
            (face for face in self._faces if face.billboard_center is not None
             and id(face) in polygons),
            key=lambda face: self._camera_vertex(face.billboard_center, camera)[2])
        for face in faces:
            material = self._materials[face.material]
            image = material.image
            if material.anim_frames:
                frame, _ = self._anim_states.get(face.material, (0, 1))
                image_id = material.anim_frames[frame][1]
                image = (material.anim_images[image_id]
                         if 0 <= image_id < len(material.anim_images) else None)
            uvs = self._face_uvs(face, material)
            target = polygons[id(face)]
            if image is None or image.isNull() or len(uvs) != 4 or len(target) != 4:
                continue
            source = QPolygonF([QPointF(u * image.width() / 256.0,
                                       v * image.height() / 256.0) for u, v in uvs])
            transform = QTransform()
            if not QTransform.quadToQuad(source, target, transform):
                continue
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            clip = QPainterPath()
            clip.addPolygon(target)
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Plus if material.additive
                else QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setWorldTransform(transform, True)
            painter.drawImage(QPointF(0, 0), image)
            painter.restore()

    def _render_indexed_model(
            self, target: QRectF, ordered, camera: dict, *,
            collect_diagnostics: bool = True,
            background_indices: bytes | None = None) -> QImage:
        """Render BSP pieces through the sole palette-index textured backend."""

        adapter = self._indexed_adapter
        indexed_pieces = self._indexed_pieces(target, ordered, camera)
        width = max(1, int(round(target.width())))
        height = max(1, int(round(target.height())))
        result = IndexedRasterizer.render(
            width, height, indexed_pieces, adapter.tables, background_index=0,
            collect_diagnostics=collect_diagnostics,
            track_polygon_owner=False, background_indices=background_indices)
        self._last_indexed_stats = dict(result.stats)
        rgba = result.to_rgba(adapter.tables, transparent_background=True)
        image = QImage(
            rgba, width, height, width * 4, QImage.Format.Format_RGBA8888)
        return image.copy()

    def _indexed_pieces(self, target, ordered, camera):
        """Keep CPU and GPU backends on identical Retail material/UV semantics."""
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
        indexed_pieces = []
        distance_scale = 1.0 / max(
            abs(float(camera.get("scale", 1.0))), 1e-12)
        for piece in ordered:
            payload = piece.payload
            face = payload.face
            material = self._materials[face.material]
            frame_index, _direction = self._anim_states.get(
                face.material, (0, 1))
            surface = self._resolve_indexed_surface(
                adapter, face, material, frame_index)
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
        return indexed_pieces

    def _draw_gpu_indexed(self, painter, target, ordered, camera, *, backdrop=None):
        canvas = getattr(self, '_gpu_canvas', None)
        if (canvas is None or painter.device() is not canvas
                or self._retail_distance_fade_enabled
                or getattr(self, '_gpu_indexed_unavailable', False)):
            return False
        # Cockpit subrect rendering keeps its existing clip and exact raster path.
        if target != QRectF(self.rect()):
            return False
        pieces = self._indexed_pieces(target, ordered, camera)
        from studio.rendering.indexed import StudioGpuRenderer
        from OpenGL import GL as gl
        painter.beginNativePainting()
        unpack_alignment = int(gl.glGetIntegerv(gl.GL_UNPACK_ALIGNMENT))
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        try:
            if getattr(self, '_gpu_indexed', None) is None:
                self._gpu_indexed = StudioGpuRenderer()
            ratio = canvas.devicePixelRatioF()
            if ratio != 1:
                from dataclasses import replace
                pieces = [replace(p, screen=tuple((x*ratio,y*ratio) for x,y in p.screen))
                          for p in pieces]
                if backdrop is not None:
                    backdrop = backdrop.scaled(round(target.width()*ratio),
                                               round(target.height()*ratio))
            self._last_indexed_stats = self._gpu_indexed.render_pieces(
                pieces, self._indexed_adapter.tables,
                round(target.width()*ratio), round(target.height()*ratio),
                canvas.defaultFramebufferObject(),
                background_indices=self._indexed_background_bytes(backdrop))
        except Exception as exc:
            self._cleanup_gpu()
            self._gpu_indexed_unavailable = True
            self.statusMessage.emit(f"GPU textured rendering unavailable: {exc}. Using software rendering.")
            return False
        finally:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, unpack_alignment)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, canvas.defaultFramebufferObject())
            gl.glActiveTexture(gl.GL_TEXTURE0)
            painter.endNativePainting()
        return True

    def _cleanup_gpu(self):
        backend = getattr(self, '_gpu_indexed', None)
        if backend is not None:
            backend.delete()
            self._gpu_indexed = None

    def _resolve_indexed_surface(
            self, adapter: IndexedFamilyAdapter, face: ViewFace,
            material: ViewMaterial, frame_index: int):
        """Resolve one indexed surface for the active viewport semantics."""

        return adapter.resolve_surface(face, material, frame_index)

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
            # A selected handle is drawn like any other selection below.
            if index in session.selection or index >= len(screen):
                continue
            point = screen[index]
            painter.drawRect(QRectF(
                point.x() - 3.0, point.y() - 3.0, 6.0, 6.0))
        mode_color = {
            "move": MODEL_EDIT_SELECTION_RED,
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

        self._draw_gizmo_handles(painter, target)

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
        action = "Move" if state.operation == "move" else "Copy"
        label = (
            f"{action} {getattr(state.clipboard, 'fx_name')} FX Preview"
            if getattr(state.clipboard, "fx_name", None)
            else f"{action} Preview")
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
        # The grid is the shared ground: the bottom of the shown model.
        height = self._center[1] if self._ground_y is None else self._ground_y
        for i in range(-steps, steps + 1):
            offset = i * size / steps
            for start, end in (
                ((offset, 0, -size), (offset, 0, size)),
                ((-size, 0, offset), (size, 0, offset)),
            ):
                a = self._project(self._camera_vertex((
                    self._center[0] + start[0], height,
                    self._center[2] + start[2]), camera), target, camera)
                b = self._project(self._camera_vertex((
                    self._center[0] + end[0], height,
                    self._center[2] + end[2]), camera), target, camera)
                painter.drawLine(a, b)
