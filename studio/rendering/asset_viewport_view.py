"""AssetViewport mixin: public view controls and projection."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
)

from studio.rendering.indexed import (
    IndexedRasterizer,
    VANILLA_FADE_LENGTH,
    VANILLA_GAMEPLAY_FADE_START,
    VANILLA_GAMEPLAY_VIS_LIMIT,
)
from studio.rendering.asset_viewport_common import (
    VIEW_MODES,
    VIEW_PRESET_ANGLES,
    ViewMaterial,
)


class _ViewControlsMixin:
    """Part of AssetViewport; `self` is always the AssetViewport."""

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
        return bool(self._faces or self._particle_emitters)

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
        self.setCursor(Qt.CursorShape.OpenHandCursor)
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
        self.unsetCursor()
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

        points = self._framing_points()
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
        if not self.has_model or width <= 0 or height <= 0:
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
        if self._last_effective_renderer not in (
                "retail_indexed_reconstructed", "retail_indexed_with_particle_overrides"):
            reason = self._last_render_error or self.indexed_rendering_reason
            raise RuntimeError(
                "Retail indexed export failed closed: " + (reason or "unknown reason"))
        return image

    def capture_wireframe_geometry(self, target_size, camera: dict):
        """Capture the current pose through the shared Retail geometry pass."""

        image = QImage(target_size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        pieces = []
        previous = (self._last_effective_renderer, self._last_render_error,
                    dict(self._last_indexed_stats))
        try:
            self._render_scene(
                painter, QRectF(0, 0, target_size.width(), target_size.height()),
                None, clean=True, camera=camera,
                allow_transparent_background=True, force_textured=True,
                geometry_capture=pieces)
        finally:
            painter.end()
            (self._last_effective_renderer, self._last_render_error,
             self._last_indexed_stats) = previous
        return pieces

    def reset_view(self) -> None:
        if self._home_camera_state is not None:
            self._set_camera_state(self._home_camera_state)
        else:
            self._yaw = self.RESET_YAW
            self._pitch = self.RESET_PITCH
            self._zoom = self.RESET_ZOOM
            self._pan = QPointF(0.0, 0.0)
        self.update()


    @property
    def has_animation(self) -> bool:
        return bool(self._particle_emitters) or any(
            m.anim_frames for m in self._materials)

    @property
    def animation_duration_ms(self) -> float:
        """Return one visible effect burst or the longest material cycle."""
        static_materials = {face.material for face in self._faces
                            if face.billboard_center is None}
        return max([self._particle_duration_ms, *(
            sum(max(0, frame[0]) for frame in mat.anim_frames)
            * (2 if mat.anim_type else 1) * 1000.0 / 1024.0
            for index, mat in enumerate(self._materials)
            if index in static_materials)])

    def capture_animation_state(self) -> dict:
        """Save the displayed frame and playback for a temporary export."""
        return dict(clock=self._anim_clock_ms, states=dict(self._anim_states),
                    left=dict(self._anim_left_ms), speed=self._anim_speed,
                    playing=self._anim_playing)

    def restore_animation_state(self, state: dict) -> None:
        self._anim_clock_ms = state["clock"]
        self._anim_states = dict(state["states"])
        self._anim_left_ms = dict(state["left"])
        self._anim_speed = state["speed"]
        self._refresh_particle_faces()
        self.play_animation(state["playing"])
        self.animationFrameChanged.emit(self.current_frame_text())

    def seek_animation(self, time_ms: float) -> None:
        """Sample the same timeline for interactive playback and image export."""
        self._anim_clock_ms = max(0.0, float(time_ms))
        for mat_id, mat in enumerate(self._materials):
            if not mat.anim_frames:
                continue
            sequence = [(i, 1) for i in range(len(mat.anim_frames))]
            if mat.anim_type:
                sequence += [(i, -1) for i in reversed(range(len(mat.anim_frames)))]
            durations = [mat.anim_frames[i][0] * 1000.0 / 1024.0
                         for i, _ in sequence]
            remaining = self._anim_clock_ms
            if all(duration > 0 for duration in durations):
                remaining %= sum(durations)
            for (frame, direction), duration in zip(sequence, durations):
                if duration <= 0 or remaining < duration:
                    self._anim_states[mat_id] = frame, direction
                    self._anim_left_ms[mat_id] = remaining
                    break
                remaining -= duration
        self._refresh_particle_faces()
        self.update()
        self.animationFrameChanged.emit(self.current_frame_text())

    def play_animation(self, playing: bool) -> None:
        self._anim_playing = playing and self.has_animation
        if self._anim_playing:
            self._anim_last_time = time.perf_counter()
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
        if self._particle_emitters:
            self.seek_animation(self._anim_clock_ms + 40.0)
            return
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

        if self._particle_emitters:
            self.seek_animation(max(0.0, self._anim_clock_ms - 40.0))
            return

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
        if self._particle_emitters:
            live = sum(face.billboard_center is not None for face in self._faces)
            parts.append(
                f"Particles: {self._anim_clock_ms % self._particle_duration_ms:.0f} ms, "
                f"{live} live")
        static_materials = {face.material for face in self._faces
                            if face.billboard_center is None}
        for mat_id, mat in enumerate(self._materials):
            if mat.anim_frames and mat_id in static_materials:
                frame, _ = self._anim_states.get(mat_id, (0, 1))
                parts.append(f"{mat.label}: frame {frame + 1}/{len(mat.anim_frames)}")
        return "; ".join(parts)


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
        self._refresh_particle_faces()

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
        now = time.perf_counter()
        delta_ms = (now - self._anim_last_time) * 1000.0 * self._anim_speed
        self._anim_last_time = now
        self._anim_clock_ms += delta_ms
        for mat_id, mat in enumerate(self._materials):
            if not mat.anim_frames:
                continue
            frame, direction = self._anim_states.get(mat_id, (0, 1))
            elapsed = self._anim_left_ms.get(mat_id, 0.0) + delta_ms
            period = sum(max(0, f[0]) for f in mat.anim_frames)
            if period and all(f[0] > 0 for f in mat.anim_frames):
                elapsed %= period * (2 if mat.anim_type else 1) * 1000.0 / 1024.0
            while True:
                duration = mat.anim_frames[frame][0] * 1000.0 / 1024.0
                if duration <= 0 or elapsed < duration:
                    break
                elapsed -= duration
                frame, direction = self._next_frame(mat, frame, direction)
            self._anim_states[mat_id] = frame, direction
            self._anim_left_ms[mat_id] = elapsed
        self._refresh_particle_faces()
        self.update()
        self.animationFrameChanged.emit(self.current_frame_text())

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
