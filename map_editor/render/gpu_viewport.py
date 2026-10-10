"""Live GPU canvas; editor interaction and document semantics stay shared."""
from __future__ import annotations

import os
import time
from collections import deque
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QSurfaceFormat, QOpenGLContext, QOffscreenSurface
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from types import SimpleNamespace

from .map_viewport import MapViewport as SoftwareMapViewport
from .gpu_scene import WorldScene
from .special_scene import scene_object_styles
from .gpu_renderer import GpuRenderer
from .camera import IsoCamera
from .terrain_mesh import HEIGHT_UNIT
from ..core.ldf_model import DEFAULT_HGT


def gl_format():
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSamples(0)
    fmt.setSwapInterval(1)
    return fmt


def available():
    if QApplication.platformName() in ('offscreen', 'minimal'):
        return False
    surface = QOffscreenSurface()
    surface.setFormat(gl_format()); surface.create()
    context = QOpenGLContext()
    context.setFormat(gl_format())
    ok = context.create() and context.makeCurrent(surface)
    if ok:
        version = (context.format().majorVersion(), context.format().minorVersion())
        ok = version >= (3, 3)
        context.doneCurrent()
    surface.destroy()
    return ok


class Canvas(QOpenGLWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setFormat(gl_format())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def initializeGL(self):
        from OpenGL import GL
        unpack_alignment = int(GL.glGetIntegerv(GL.GL_UNPACK_ALIGNMENT))
        try:
            self.owner.renderer = GpuRenderer()
            self.owner.renderer_name = 'OpenGL — ' + self.owner.renderer.name
            self.owner.backendChanged.emit(self.owner.renderer_name)
            self.owner._geometry_dirty = self.owner._heights_dirty = self.owner._sky_dirty = True
            self.owner._dirty_cells = None
            self.owner._states_key = None
            self.owner._previews_primed = False
            self.context().aboutToBeDestroyed.connect(self.cleanup)
        except Exception as exc:
            self.owner._fallback(exc)
        finally:
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, unpack_alignment)

    def paintGL(self):
        if self.owner.renderer is not None and not self.owner._software_fallback:
            from OpenGL import GL
            painter = QPainter(self)
            try:
                painter.beginNativePainting()
                unpack_alignment = int(GL.glGetIntegerv(GL.GL_UNPACK_ALIGNMENT))
                GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
                try:
                    self.owner._paint_gpu()
                finally:
                    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, unpack_alignment)
                    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
                    GL.glActiveTexture(GL.GL_TEXTURE0)
                    painter.endNativePainting()
                overlay = getattr(self.owner, 'building_overlay', None)
                if overlay is not None and not self.owner.camera.perspective:
                    overlay.draw(painter)
                overlay = getattr(self.owner, 'special_overlay', None)
                if overlay is not None:
                    overlay.draw(painter)
                self.owner.draw_interaction_overlay(painter)
                overlay = getattr(self.owner, 'squad_overlay', None)
                if overlay is not None:
                    overlay.draw(painter)
            except Exception as exc:
                self.owner._fallback(exc)
            finally:
                painter.end()

    def cleanup(self):
        if self.owner.renderer is not None:
            self.makeCurrent()
            if self.owner._preview_renderer is not None:
                self.owner._preview_renderer.delete()
                self.owner._preview_renderer = None
            self.owner.renderer.delete()
            self.owner.renderer = None
            self.doneCurrent()


class GpuMapViewport(SoftwareMapViewport):
    def __init__(self, parent=None):
        self._canvas = None
        self._software_fallback = False
        super().__init__(parent)
        self.renderer = None
        self._preview_renderer = None
        self._preview_scene = WorldScene()
        self._previews_primed = False
        self.renderer_name = 'OpenGL 3.3'
        self.world_scene = WorldScene()
        self._geometry_dirty = True
        self._dirty_cells = None
        self._heights_dirty = True
        self._sky_dirty = True
        self._states_key = None
        self._owner_revision = 0
        self._gpu_camera_key = None
        self.frame_ms = deque(maxlen=600)
        self._canvas = Canvas(self)
        self._canvas.setGeometry(self.rect())

    def _fallback(self, error):
        self._software_fallback = True
        self.renderer_name = 'Software fallback'
        self.backendChanged.emit(self.renderer_name)
        self._canvas.hide()
        self.statusMessage.emit(f'GPU rendering unavailable: {error}. Using software rendering.')
        super().update()

    def update(self, *args):
        if self._canvas is not None and not self._software_fallback:
            self._canvas.update()
        else:
            super().update(*args)

    def paintEvent(self, event):
        if self._software_fallback:
            super().paintEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._canvas is not None:
            self._canvas.setGeometry(self.rect())
        self.camera.width, self.camera.height = self.width(), self.height()

    def _launch_render(self):
        if self._software_fallback:
            super()._launch_render()
        else:
            self.update()

    def set_library(self, lib):
        if lib is not self.lib:
            self.world_scene = WorldScene()
            self.world_scene.set_library(lib)
        super().set_library(lib)

    def set_document(self, doc):
        self.world_scene.clear()
        self._heights_dirty = True
        self._states_key = None
        super().set_document(doc)

    def scene_changed(self, cells=None):
        self._states_key = None
        if cells is None:
            self._dirty_cells = None
        elif not self._geometry_dirty:
            self._dirty_cells = set(cells)
        elif self._dirty_cells is not None:
            self._dirty_cells.update(cells)
        self._geometry_dirty = True
        super().scene_changed()

    def terrain_changed(self, cells=None):
        if self._software_fallback:
            return super().terrain_changed(cells)
        if self.doc is not None:
            self.terrain.rebuild(self.doc.grids['hgt'])
        self._heights_dirty = True
        if self.doc.squads:
            self._geometry_dirty = True
        self._scene_revision += 1
        self.update()

    def owners_changed(self):
        self._owner_revision += 1
        super().owners_changed()

    def set_sky_image(self, image):
        self._sky_dirty = True
        super().set_sky_image(image)

    def _paint_gpu(self):
        start = time.perf_counter()
        renderer = self.renderer
        self.camera.width, self.camera.height = self.width(), self.height()
        if self.doc is None or self.lib is None:
            from OpenGL import GL
            GL.glClearColor(18/255, 22/255, 28/255, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return
        if self._geometry_dirty:
            changed = self.world_scene.update(self.doc, self.terrain, self._dirty_cells)
            renderer.set_scene(self.world_scene, changed)
            self._geometry_dirty = False
            self._dirty_cells = set()
        if self._heights_dirty:
            renderer.set_heights(-(self.terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
            self._heights_dirty = False
        state_key = (self._owner_revision, self.doc.mw, self.doc.mh,
                     frozenset(self.brush_cells), frozenset(self.selection), frozenset(self.preview_cells))
        if state_key != self._states_key:
            states = np.zeros((self.doc.mh, self.doc.mw, 4), np.uint8)
            states[:, :, 0] = self.doc.grids['own']
            for c,r in self.preview_cells:
                states[r,c,3] = 2
                states[r,c,0] = 0
            for channel, cells in ((1, self.brush_cells), (2, self.selection)):
                if channel == 1 and self.active_tool == 'terrain':
                    continue
                for c, r in cells:
                    if 0 <= c < self.doc.mw and 0 <= r < self.doc.mh:
                        states[r, c, channel] = 1
            renderer.set_states(states)
            self._states_key = state_key
        if self._sky_dirty:
            renderer.set_sky(self._sky_image)
            self._sky_dirty = False
        if not self._previews_primed:
            # Drivers may compile a separate non-instanced shader variant on its
            # first draw. Pay that startup cost before interactive palette scrolling.
            for typ in (0, 4):
                if typ in self.lib.assets.sdf.sectors:
                    self.render_icon(self.lib, typ, 1.1, current=True)
            self._previews_primed = True
        ratio = self._canvas.devicePixelRatioF()
        styles = [(*(c / 255 for c in row[:3]), row[3]) for row in scene_object_styles(self)]
        renderer.set_unit_styles(styles)
        hover = self.hover[1]*self.doc.mw+self.hover[0]+1 if self.hover else 0
        renderer.render(self.camera, round(self.width()*ratio), round(self.height()*ratio),
                        self._canvas.defaultFramebufferObject(), owner_colors=self.owner_colors,
                        grid=self.show_grid, sky=self.show_sky, hover=hover, pixel_scale=ratio,
                        overlays=not self.camera.perspective,
                        cursor_color=self.current_cursor_color())
        self._gpu_camera_key = (self._camera_key(), ratio)
        self._frame_key = self._render_key()
        self.frame_ms.append((time.perf_counter()-start)*1000)

    def _pick_code(self, x, y):
        if self._software_fallback:
            return 0
        if self.renderer is None or self.doc is None:
            return 0
        self._canvas.makeCurrent()
        try:
            ratio = self._canvas.devicePixelRatioF()
            if self._gpu_camera_key != (self._camera_key(), ratio) or self._geometry_dirty or self._heights_dirty:
                self._paint_gpu()
            code = self.renderer.pick(int(x*ratio), int(y*ratio))
        finally:
            self._canvas.doneCurrent()
        return code

    def pick_cell(self, x, y):
        if self._software_fallback:
            return super().pick_cell(x, y)
        code = self._pick_code(x, y)
        if code == 0 or abs(code) > self.doc.mw * self.doc.mh:
            return None
        cell = abs(code)-1
        return cell % self.doc.mw, cell // self.doc.mw

    def pick_squad(self, x, y):
        if self._software_fallback:
            return super().pick_squad(x, y)
        if self.doc is None:
            return None
        code = abs(self._pick_code(x, y)) - self.doc.mw * self.doc.mh - 1
        if 0 <= code < len(self.doc.squads):
            return code
        overlay = getattr(self, 'squad_overlay', None)
        return overlay.pick(x, y) if overlay is not None else None

    def pick_host(self, x, y):
        if self._software_fallback:
            return super().pick_host(x, y)
        if self.doc is None:
            return None
        code = abs(self._pick_code(x, y)) - self.doc.mw * self.doc.mh - len(self.doc.squads) - 1
        return code if 0 <= code < len(self.doc.host_stations) else None

    def render_icon(self, lib, typ, scale, *, current=False, building_id=None, vehicle_id=None, max_dimension=512):
        """Small GPU readback only for cached palette icons, never map frames."""
        if self.renderer is None or self._software_fallback:
            return None
        if not current:
            self._canvas.makeCurrent()
        from OpenGL import GL
        unpack_alignment = int(GL.glGetIntegerv(GL.GL_UNPACK_ALIGNMENT))
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        target = None
        try:
            if self._preview_renderer is None:
                self._preview_renderer = GpuRenderer((self.renderer.opaque_program,
                                                     self.renderer.flat_program,
                                                     self.renderer.screen_program))
            if self._preview_scene.lib is not lib:
                self._preview_scene = WorldScene()
                self._preview_scene.set_library(lib)
            scene = self._preview_scene
            changed = scene.preview(typ, building_id, vehicle_id)
            renderer = self._preview_renderer
            renderer.set_scene(scene, changed)
            geometry = scene.chunks[0, 0]
            vertices = np.concatenate((geometry.opaque[:, :3], geometry.flat[:, :3]))
            if not len(vertices):
                return None
            camera = IsoCamera(center=(600, 0, -600), zoom=.13*scale,
                               width=0, height=0)
            cy, sy, cp, sp = camera._trig()
            projection = np.asarray(((cy, 0, sy), (-sp*sy, cp, sp*cy)))
            points = (vertices-np.asarray(camera.center)) @ projection.T * camera.zoom
            lo, hi = points.min(axis=0), points.max(axis=0)
            factor = (max_dimension-4)/max(1.0, float(max(hi-lo)))
            if vehicle_id is None:
                factor = min(1.0, factor)
            camera.zoom *= factor
            lo *= factor; hi *= factor
            camera.width, camera.height = np.ceil(hi-lo+4).astype(int)
            camera.pan = tuple(-(lo+hi)*.5)
            target = QOpenGLFramebufferObject(int(camera.width), int(camera.height))
            renderer.render(camera, int(camera.width), int(camera.height), target.handle(),
                            owner_colors={}, sky=False, overlays=False, transparent_background=True)
            return SimpleNamespace(image=target.toImage())
        finally:
            # Destroy the temporary FBO while its context is still current.
            del target
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, unpack_alignment)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._canvas.defaultFramebufferObject())
            GL.glActiveTexture(GL.GL_TEXTURE0)
            if not current:
                self._canvas.doneCurrent()


def create_viewport():
    if os.environ.get('NME_RENDERER', '').casefold() == 'software' or not available():
        view = SoftwareMapViewport()
        view.renderer_name = 'Software'
        return view
    return GpuMapViewport()
