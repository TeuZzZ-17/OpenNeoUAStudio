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


def test_flat_tracy_matches_cpu_with_seeded_destination_and_transparent_border(app):
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
        background_array = np.fromfunction(
            lambda y, x: np.where((x + y) % 2 == 0, 10, 20),
            (height, width), dtype=int).astype(np.uint8)
        background_indices = background_array.tobytes()
        tracy = bytearray(identity)
        # The same raw flat source is a no-op over destination 10 and changes
        # destination 20 to 40, exercising both TRACY outcomes in one frame.
        tracy[10 * 256 + 7] = 10
        tracy[20 * 256 + 7] = 40
        tables = IndexedTables(palette, identity, bytes(tracy))

        cpu = IndexedRasterizer.render(
            width, height, [piece], tables,
            background_indices=background_indices,
        ).to_rgba(tables, transparent_background=True)
        renderer.render_pieces(
            [piece], tables, width, height, target, blend=False,
            background_indices=background_indices,
        )
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, target)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
        gpu = np.empty((height, width, 4), dtype=np.uint8)
        gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE, gpu)
        gpu = gpu[::-1]
        cpu = np.frombuffer(cpu, dtype=np.uint8).reshape(
            height, width, 4).copy()

        assert np.array_equal(gpu[:, :, 3], cpu[:, :, 3])
        covered = cpu[:, :, 3] != 0
        assert np.array_equal(gpu[:, :, :3][covered], cpu[:, :, :3][covered])
        # Transparent RGB is not observable; the CPU conversion preserves its
        # palette value while the GPU screen pass emits zero RGB.
        cpu[:, :, :3][~covered] = 0
        assert np.array_equal(gpu, cpu)
        # (3, 3) is inside the flat face and remains an uncovered no-op;
        # (4, 3) changes; (0, 0) is outside and remains transparent.
        assert tuple(gpu[3, 3]) == (0, 0, 0, 0)
        assert tuple(gpu[3, 4]) == (*palette[40], 255)
        assert tuple(gpu[0, 0]) == (0, 0, 0, 0)
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
