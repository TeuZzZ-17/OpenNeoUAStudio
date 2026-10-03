"""OpenGL regression for unchanged and changed flat TRACY pixels."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from OpenGL import GL as gl
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication

from gpu_indexed_backend import StudioGpuRenderer
from indexed_renderer import IndexedPiece, IndexedRasterizer, IndexedSurface, IndexedTables
from map_editor.render.gpu_viewport import gl_format


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_flat_tracy_matches_cpu_for_noop_and_changed_pixels(app):
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
    width = height = 8
    try:
        renderer = StudioGpuRenderer()
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

        piece_surface = IndexedSurface(
            "flat-test", "texture", bytes([7]), 1, 1, None,
            "none", 0, "flat", "linear")
        piece = IndexedPiece(
            "flat-test-face", 0,
            ((1, 1), (7, 1), (7, 7), (1, 7)),
            ((0, 0), (256, 0), (256, 256), (0, 256)),
            ((0, 0, 0),) * 4, piece_surface)
        identity = bytes(range(256)) * 256
        palette = tuple((index, index // 2, 255 - index) for index in range(256))

        for changed in (False, True):
            tracy = bytearray(identity)
            for background in range(256):
                tracy[background * 256 + 7] = background
            if changed:
                tracy[7] = 19
            tables = IndexedTables(palette, identity, bytes(tracy))

            cpu = IndexedRasterizer.render(
                width, height, [piece], tables, background_index=0
            ).to_rgba(tables, transparent_background=True)
            renderer.render_pieces([piece], tables, width, height, target,
                                   blend=False)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, target)
            gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            gpu = np.empty((height, width, 4), dtype=np.uint8)
            gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                            gl.GL_UNSIGNED_BYTE, gpu)
            gpu = gpu[::-1]
            cpu = np.frombuffer(cpu, dtype=np.uint8).reshape(height, width, 4)

            assert tuple(gpu[3, 3]) == tuple(cpu[3, 3])
            assert tuple(gpu[0, 0]) == tuple(cpu[0, 0])
            if changed:
                assert tuple(gpu[3, 3]) == (*palette[19], 255)
            else:
                assert tuple(gpu[3, 3]) == (0, 0, 0, 0)
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
