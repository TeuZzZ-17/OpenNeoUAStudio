import numpy as np
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from .test_special_objects import app, win  # noqa: F401 - pytest fixtures defined in that file
from map_editor.render.camera import IsoCamera
from map_editor.render.gpu_renderer import GpuRenderer
from map_editor.ui.special_overlay import GLYPH_DEPTH_TOLERANCE, SpecialOverlay


def stub_renderer(depth):
    """A renderer whose last frame has the given window depth; no OpenGL context is needed."""
    renderer = GpuRenderer.__new__(GpuRenderer)
    renderer.size = depth.shape[::-1]
    renderer.map_size = (4, 4)
    renderer.read_depth = lambda left, top, right, bottom: depth[top:bottom, left:right]
    return renderer


def centred_camera():
    return IsoCamera(width=64, height=64, zoom=.5, center=(0, 0, 0))


def test_glyph_stays_visible_over_empty_frame():
    renderer = stub_renderer(np.ones((64, 64), np.float32))
    mask = renderer.glyph_visibility(centred_camera(), -8, (10, 10, 50, 50), GLYPH_DEPTH_TOLERANCE)
    assert mask.shape == (40, 40)
    assert mask.all()


def test_geometry_nearer_than_glyph_hides_it():
    renderer = stub_renderer(np.zeros((64, 64), np.float32))
    mask = renderer.glyph_visibility(centred_camera(), -8, (10, 10, 50, 50), GLYPH_DEPTH_TOLERANCE)
    assert not mask.any()


def test_only_covered_columns_are_hidden():
    depth = np.ones((64, 64), np.float32)
    depth[:, :30] = 0.0
    renderer = stub_renderer(depth)
    mask = renderer.glyph_visibility(centred_camera(), -8, (10, 10, 50, 50), GLYPH_DEPTH_TOLERANCE)
    assert not mask[:, :20].any()
    assert mask[:, 20:].all()


def test_own_sector_geometry_never_hides_glyph():
    renderer = stub_renderer(np.zeros((64, 64), np.float32))
    ids = np.full((64, 64), 7, np.int32)
    ids[:, :30] = -9
    renderer.read_ids_rect = lambda left, top, right, bottom: ids[top:bottom, left:right]
    mask = renderer.glyph_visibility(centred_camera(), -8, (10, 10, 50, 50), GLYPH_DEPTH_TOLERANCE, {-9})
    assert mask[:, :20].all()
    assert not mask[:, 20:].any()


def test_partly_hidden_glyph_draws_only_visible_pixels(app):
    class HalfHidden:
        def glyph_visibility(self, camera, height, rect, tolerance, own_ids=()):
            left, top, right, bottom = rect
            mask = np.ones((bottom - top, right - left), np.uint8)
            mask[:, (right - left) // 2:] = 0
            return mask

    glyph = QImage(20, 20, QImage.Format.Format_ARGB32)
    glyph.fill(QColor(255, 0, 0))
    canvas = QImage(100, 100, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor(0, 0, 0, 0))
    painter = QPainter(canvas)
    SpecialOverlay._draw_ground_glyph(painter, IsoCamera(width=100, height=100), HalfHidden(), glyph,
                                      QTransform.fromTranslate(20, 20), -8)
    painter.end()
    assert canvas.pixelColor(25, 25).alpha() == 255
    assert canvas.pixelColor(35, 25).alpha() == 0
