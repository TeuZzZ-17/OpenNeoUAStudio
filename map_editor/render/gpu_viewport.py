"""Live GPU canvas; editor interaction and document semantics stay shared."""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path
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
from .camera import ISO_PITCH, ISO_YAW
from .terrain_mesh import HEIGHT_UNIT
from ..core.ldf_model import DEFAULT_HGT


def gl_format():
    fmt = QSurfaceFormat()
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
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
                if not self.owner.clear_view:
                    for name in ('building_overlay', 'special_overlay', 'squad_overlay', 'cloud_overlay'):
                        overlay = getattr(self.owner, name, None)
                        if overlay is None or (name == 'building_overlay' and self.owner.camera.perspective):
                            continue
                        try:
                            overlay.draw(painter)
                        except Exception as exc:
                            self.owner._overlay_warning(name, exc)
                try:
                    self.owner.draw_interaction_overlay(painter)
                except Exception as exc:
                    self.owner._overlay_warning('interaction_overlay', exc)
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
            self.owner._preview_target = None
            self.owner._preview_atlas = None
            self.owner.renderer.delete()
            self.owner.renderer = None
            self.doneCurrent()

    def release_resources(self):
        self.cleanup()
        context = self.context()
        if context is not None:
            context.aboutToBeDestroyed.disconnect(self.cleanup)


class GpuMapViewport(SoftwareMapViewport):
    def __init__(self, parent=None, *, enable_gpu=True):
        self._canvas = None
        self._software_fallback = not enable_gpu
        self.renderer_error = ""
        self._overlay_errors = set()
        super().__init__(parent)
        self.renderer = None
        self._preview_renderer = None
        self._preview_target = None
        self._preview_atlas = None
        self._preview_revision = None
        self._preview_scene = WorldScene()
        self._previews_primed = False
        self.renderer_name = 'OpenGL 3.3' if enable_gpu else 'Software'
        self.world_scene = WorldScene()
        self._geometry_dirty = True
        self._dirty_cells = None
        self._heights_dirty = True
        self._sky_dirty = True
        self._states_key = None
        self._owner_revision = 0
        self._gpu_camera_key = None
        self.frame_ms = deque(maxlen=600)
        if enable_gpu:
            self._canvas = Canvas(self)
            self._canvas.setGeometry(self.rect())

    @property
    def renderer_mode(self):
        return 'software' if self._software_fallback else 'gpu'

    def set_renderer_mode(self, mode):
        """Keep the document, camera, selection and history in this viewport."""
        if mode not in {'gpu', 'software'}:
            raise ValueError(mode)
        if mode == self.renderer_mode:
            return True
        self.renderer_error = ''
        if mode == 'gpu' and not available():
            self.renderer_error = 'OpenGL 3.3 is unavailable. Software rendering remains active.'
            return False
        if self._canvas is not None:
            self._canvas.release_resources()
            self._canvas.hide()
            self._canvas.deleteLater()
            self._canvas = None
        self._software_fallback = mode == 'software'
        self._generation += 1
        self._frame = self._image = self._frame_key = None
        self._owner_marks = self._annotation_base = None
        self._gpu_camera_key = None
        self._geometry_dirty = self._heights_dirty = self._sky_dirty = True
        self._dirty_cells = None
        self._states_key = None
        self._previews_primed = False
        self.renderer_name = 'Software' if self._software_fallback else 'OpenGL 3.3'
        if mode == 'gpu':
            self._canvas = Canvas(self)
            self._canvas.setGeometry(self.rect())
            self._canvas.show()
        self.backendChanged.emit(self.renderer_name)
        self.update()
        return True

    def _overlay_warning(self, name, error):
        """An optional UI overlay must never disable GPU map rendering."""
        key = (name, type(error).__name__, str(error))
        if key in self._overlay_errors:
            return
        self._overlay_errors.add(key)
        log = Path.home() / 'OpenNeoUAStudio_Overlay_error.log'
        try:
            with log.open('a', encoding='utf-8') as stream:
                stream.write(f'{name}: {type(error).__name__}: {error}\n{traceback.format_exc()}\n')
        except OSError:
            pass
        self.statusMessage.emit(f'{name} unavailable: {error}. GPU rendering continues.')

    def _fallback(self, error):
        # Keep the original exception: the cause matters when a particular map fails.
        self._software_fallback = True
        self.renderer_error = str(error)
        self.renderer_name = 'Software fallback'
        self.backendChanged.emit(self.renderer_name)
        self._canvas.hide()
        log = Path.home() / 'OpenNeoUAStudio_GPU_error.log'
        doc = self.doc
        context = (f'SET: {doc.set_number}, map: {doc.lvl_info.get("title", "unknown")}, '
                   f'size: {doc.mw}x{doc.mh}\n') if doc is not None else 'No map loaded\n'
        try:
            log.write_text(context + f'{type(error).__name__}: {error}\n{traceback.format_exc()}', encoding='utf-8')
            detail = f' Details: {log}'
        except OSError:
            detail = ''
        self.statusMessage.emit(f'GPU rendering unavailable: {error}. Using software rendering.{detail}')
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
            self.world_scene.border_walls = self.border_walls
            self.world_scene.set_library(lib)
        super().set_library(lib)

    def set_border_walls(self, visible):
        # Border cells use other templates: rebuild the whole scene once.
        self.world_scene.border_walls = bool(visible)
        self.world_scene.clear()
        self._dirty_cells = None
        super().set_border_walls(visible)

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
                     frozenset(self.brush_cells), frozenset(self.selection), frozenset(self.preview_cells),
                     self.selected_building, self.active_tool == 'terrain')
        if state_key != self._states_key:
            states = np.zeros((self.doc.mh, self.doc.mw, 4), np.uint8)
            states[:, :, 0] = self.doc.grids['own']
            for r in range(self.doc.mh):
                for c in range(self.doc.mw):
                    if int(str(self.doc.grids['blg'][r][c]), 16):
                        states[r, c, 3] = 1
            if self.selected_building is not None:
                c, r = self.selected_building
                if 0 <= c < self.doc.mw and 0 <= r < self.doc.mh and states[r, c, 3] == 1:
                    states[r, c, 3] = 3
            for c,r in self.preview_cells:
                if 0 <= c < self.doc.mw and 0 <= r < self.doc.mh:
                    states[r,c,3] = 2
                    states[r,c,0] = 0
            for channel, cells in ((1, self.brush_cells), (2, self.selection)):
                # The terrain tool draws only its brush shape: no selection or brush-cell marks.
                if self.active_tool == 'terrain':
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
            # A palette thumbnail failure must not disable the whole GPU map.
            self._previews_primed = True
            for typ in (0, 4):
                if typ in self.lib.assets.sdf.sectors:
                    try:
                        self.render_icon(self.lib, typ, 1.1, current=True)
                    except Exception:
                        self.statusMessage.emit('GPU palette preview unavailable; map rendering continues.')
                        break
        ratio = self._canvas.devicePixelRatioF()
        styles = [(*(c / 255 for c in row[:3]), row[3]) for row in scene_object_styles(self)]
        renderer.set_unit_styles(styles)
        hover = (self.hover[1]*self.doc.mw+self.hover[0]+1
                 if self.hover and self.active_tool != 'terrain' else 0)
        renderer.render(self.camera, round(self.width()*ratio), round(self.height()*ratio),
                        self._canvas.defaultFramebufferObject(), owner_colors=self.owner_colors,
                        grid=self.show_grid, sky=self.show_sky, hover=hover, pixel_scale=ratio,
                        overlays=not self.camera.perspective,
                        cursor_color=self.current_cursor_color(), clear_view=self.clear_view,
                        preview_tint=self.preview_tint())
        self._gpu_camera_key = (self._camera_key(), ratio, self.clear_view)
        self._frame_key = self._render_key()
        self.frame_ms.append((time.perf_counter()-start)*1000)

    def _pick_code(self, x, y):
        if self._software_fallback:
            return super()._pick_code(x, y)
        if self.renderer is None or self.doc is None:
            return 0
        self._canvas.makeCurrent()
        try:
            ratio = self._canvas.devicePixelRatioF()
            if (self._gpu_camera_key != (self._camera_key(), ratio, self.clear_view)
                    or self._geometry_dirty or self._heights_dirty):
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
        if self.doc is None or self.clear_view:
            return None
        code = abs(self._pick_code(x, y)) - self.doc.mw * self.doc.mh - 1
        if 0 <= code < len(self.doc.squads):
            return code
        overlay = getattr(self, 'squad_overlay', None)
        return overlay.pick(x, y) if overlay is not None else None

    def pick_host(self, x, y):
        if self._software_fallback:
            return super().pick_host(x, y)
        if self.doc is None or self.clear_view:
            return None
        code = abs(self._pick_code(x, y)) - self.doc.mw * self.doc.mh - len(self.doc.squads) - 1
        if 0 <= code < len(self.doc.host_stations):
            return code
        overlay = getattr(self, "squad_overlay", None)
        return overlay.pick_host_badge(x,y) if overlay is not None else None

    def render_palette_icons(self, lib, requests, target_size, *, yaw, pitch, revision):
        """Read all visible thumbnails together, avoiding one GPU wait per card."""
        if self.renderer is None or self._software_fallback:
            return None
        self._canvas.makeCurrent()
        from OpenGL import GL
        width, height = target_size
        atlas_size = (width, height * len(requests))
        try:
            if (self._preview_atlas is None or
                    (self._preview_atlas.width(), self._preview_atlas.height()) != atlas_size):
                self._preview_atlas = QOpenGLFramebufferObject(*atlas_size)
            atlas = self._preview_atlas
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, atlas.handle())
            GL.glDisable(GL.GL_SCISSOR_TEST)
            GL.glClearColor(0,0,0,0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            valid = []
            for index, (typ, building_id) in enumerate(requests):
                target = self.render_icon(lib, typ, 1, current=True, building_id=building_id,
                                          target_size=target_size, yaw=yaw, pitch=pitch,
                                          revision=revision, readback=False)
                valid.append(target is not None)
                if target is not None:
                    GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, target.handle())
                    GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, atlas.handle())
                    GL.glBlitFramebuffer(0,0,width,height, 0,index*height,width,(index+1)*height,
                                         GL.GL_COLOR_BUFFER_BIT,GL.GL_NEAREST)
            # Qt readback expects read and draw bindings to refer to the same framebuffer.
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, atlas.handle())
            image = atlas.toImage()
            return [image.copy(0,(len(requests)-1-index)*height,width,height) if present else None
                    for index,present in enumerate(valid)]
        finally:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._canvas.defaultFramebufferObject())
            self._canvas.doneCurrent()

    def render_icon(self, lib, typ, scale, *, current=False, building_id=None, vehicle_id=None, max_dimension=512,
                    yaw=ISO_YAW, pitch=ISO_PITCH, target_size=None, revision=None, readback=True):
        """Small GPU readback only for cached palette icons, never map frames.

        ``yaw`` / ``pitch`` let a sector preview match the map camera.
        """
        if self.renderer is None or self._software_fallback:
            return None
        if not current:
            self._canvas.makeCurrent()
        from OpenGL import GL
        unpack_alignment = int(GL.glGetIntegerv(GL.GL_UNPACK_ALIGNMENT))
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        try:
            stamp = (id(lib), revision)
            if self._preview_revision != stamp:
                if self._preview_renderer is not None:
                    self._preview_renderer.delete()
                    self._preview_renderer = None
                self._preview_scene = WorldScene()
                self._preview_scene.set_library(lib)
                self._preview_revision = stamp
            if self._preview_renderer is None:
                self._preview_renderer = GpuRenderer((self.renderer.opaque_program,
                                                     self.renderer.flat_program,
                                                     self.renderer.screen_program))
            if self._preview_scene.lib is not lib:
                self._preview_scene = WorldScene()
                self._preview_scene.set_library(lib)
            scene = self._preview_scene
            scene.preview(typ, building_id, vehicle_id)
            renderer = self._preview_renderer
            renderer.set_preview(scene, (typ, building_id, vehicle_id))
            geometry = scene.chunks[0, 0]
            vertices = np.concatenate((geometry.opaque[:, :3], geometry.flat[:, :3]))
            if not len(vertices):
                return None
            from .sector_sprite import fitted_preview_camera
            if target_size is None:
                # Other Studio tools keep their square, fitted thumbnail contract.
                target_size = (max_dimension, max_dimension)
            camera = fitted_preview_camera(vertices, yaw, pitch, target_size, (600, 0, -600))
            if (self._preview_target is None or self._preview_target.width() != camera.width
                    or self._preview_target.height() != camera.height):
                self._preview_target = QOpenGLFramebufferObject(camera.width, camera.height)
            target = self._preview_target
            renderer.render(camera, int(camera.width), int(camera.height), target.handle(),
                            owner_colors={}, sky=False, overlays=False, transparent_background=True)
            return SimpleNamespace(image=target.toImage()) if readback else target
        finally:
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, unpack_alignment)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._canvas.defaultFramebufferObject())
            GL.glActiveTexture(GL.GL_TEXTURE0)
            if not current:
                self._canvas.doneCurrent()


def create_viewport():
    enabled = os.environ.get('NME_RENDERER', '').casefold() != 'software' and available()
    return GpuMapViewport(enable_gpu=enabled)
