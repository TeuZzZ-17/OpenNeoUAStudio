"""GPU regression for ownership borders over a building sector."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication

from map_editor.render.gpu_renderer import GpuRenderer, gl
from map_editor.render.gpu_viewport import gl_format


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('actor_preview', [None, 'squad', 'host'])
def test_owner_color_and_actor_draft_grayscale_are_independent(app, actor_preview):
    surface = QOffscreenSurface()
    surface.setFormat(gl_format())
    surface.create()
    if not surface.isValid():
        pytest.skip("OpenGL offscreen surface is unavailable")

    context = QOpenGLContext()
    context.setFormat(gl_format())
    if not context.create() or not context.isValid():
        surface.destroy()
        pytest.skip("OpenGL context is unavailable")
    actual = context.format()
    if ((actual.majorVersion(), actual.minorVersion()) < (3, 3)
            or actual.profile() != gl_format().profile()):
        surface.destroy()
        pytest.skip("OpenGL 3.3 Core is unavailable")

    previous_context = QOpenGLContext.currentContext()
    previous_surface = previous_context.surface() if previous_context else None
    if not context.makeCurrent(surface):
        surface.destroy()
        pytest.skip("OpenGL context could not be made current")

    renderer = None
    target = target_texture = 0
    width, height = 8, 4
    owner_color = (240, 12, 36)
    palette_color = (42, 76, 104)
    try:
        renderer = GpuRenderer()
        renderer._resize(width, height)

        # A building is encoded in state.a; its visible terrain sector pixels
        # still have positive cell IDs. Negative IDs represent the building
        # silhouette and must keep the normal palette color.
        states = np.array([[[6, 0, 0, 1]]], dtype=np.uint8)
        renderer.set_states(states)
        palette = np.array([[[0, 0, 0, 0], [*palette_color, 255]]],
                           dtype=np.uint8).reshape(1, 2, 4)
        renderer._image(renderer.palette, palette, gl.GL_RGBA8, gl.GL_RGBA)

        indices = np.ones((height, width), dtype=np.uint8)
        cells = np.ones((height, width), dtype=np.int32)
        cells[:, width // 2:] = -1
        if actor_preview is not None:
            # Actor IDs follow the terrain IDs. Only the actual draft is gray,
            # even when adding a squad changes the existing host's ID.
            cells[:, :width // 2] = -2
            cells[:, width // 2:] = -3
            flags = (3, 1) if actor_preview == 'squad' else (1, 3)
            renderer.set_unit_styles([(1, 0, 0, flag) for flag in flags])
        edges = np.ones((height, width, 4), dtype=np.float32)

        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.indices)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, indices)
        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.ids)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RED_INTEGER, gl.GL_INT, cells)
        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.edges)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RGBA, gl.GL_FLOAT, edges)

        target_texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, target_texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        target = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, target_texture, 0)
        gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
        assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE

        gl.glDisable(gl.GL_FRAMEBUFFER_SRGB)
        renderer._present(
            target, width, height, owner_colors={6: owner_color}, grid=False,
            sky=False, hover=0, overlays=actor_preview is None)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE, pixels)

        if actor_preview is None:
            assert tuple(pixels[1, 1, :3]) == owner_color
            assert tuple(pixels[1, width - 2, :3]) == palette_color
        else:
            draft_x, existing_x = (1, width-2) if actor_preview == 'squad' else (width-2, 1)
            draft = pixels[1, draft_x, :3]
            assert max(draft) == min(draft)
            assert tuple(pixels[1, existing_x, :3]) == palette_color
    finally:
        if renderer is not None:
            renderer.delete()
        if target:
            gl.glDeleteFramebuffers(1, [target])
        if target_texture:
            gl.glDeleteTextures(1, [target_texture])
        if previous_context is not None and previous_surface is not None:
            previous_context.makeCurrent(previous_surface)
        else:
            context.doneCurrent()
        surface.destroy()

