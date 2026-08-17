import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    AssetViewport,
    PrecisionGuide,
)
from geometry_editor import GeometryEditSession
from indexed_renderer import (
    IndexedPiece,
    IndexedRasterizer,
    IndexedSurface,
    IndexedTables,
)
from sklt_parser import SkltModel


def _indexed_tables(color=(220, 45, 30)):
    palette = [(0, 0, 0)] * 256
    palette[37] = color
    shader = bytes(range(256)) * 256
    tracy = b"".join(bytes((background,)) * 256 for background in range(256))
    return IndexedTables(tuple(palette), shader, tracy)


def _indexed_image(width, height, screen, uvs, *, source_index=37,
                   tracy_mode="none", camera_vertices=None):
    surface = IndexedSurface(
        "regression", "texture", bytes((source_index,)) * 16, 4, 4,
        None, "none", 0,
        tracy_mode, "depth")
    camera_vertices = camera_vertices or ((0.0, 0.0, 0.0),) * len(screen)
    piece = IndexedPiece(
        "face", 1, tuple(screen), tuple(uvs), tuple(camera_vertices),
        surface)
    tables = _indexed_tables()
    result = IndexedRasterizer.render(width, height, (piece,), tables)
    rgba = result.to_rgba(tables)
    return QImage(
        rgba, width, height, width * 4,
        QImage.Format.Format_RGBA8888).copy()


def _alpha_count(image, rect=None):
    rect = rect or image.rect()
    return sum(
        image.pixelColor(x, y).alpha() > 0
        for y in range(rect.top(), rect.bottom() + 1)
        for x in range(rect.left(), rect.right() + 1)
    )


class RenderRegressionV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_degenerate_source_uv_triangle_is_rasterized_not_skipped(self):
        output = _indexed_image(
            120, 120,
            [(10, 10), (10, 110), (110, 110), (110, 10)],
            [(32, 32), (32, 32), (32, 32), (32, 32)])
        self.assertGreater(_alpha_count(output), 9000)
        self.assertEqual(output.pixelColor(25, 75).alpha(), 255)
        self.assertEqual(output.pixelColor(75, 25).alpha(), 255)

    def test_inclined_quad_keeps_both_fan_triangles_visible(self):
        output = _indexed_image(
            140, 130,
            [(16, 20), (9, 112), (118, 104), (130, 14)],
            [(0, 0), (0, 255), (255, 255), (255, 0)],
            camera_vertices=(
                (-1.0, 1.0, 0.0), (-1.0, -1.0, 0.5),
                (1.0, -1.0, 1.0), (1.0, 1.0, 0.5)))
        self.assertGreater(_alpha_count(output), 9000)
        self.assertEqual(output.pixelColor(35, 75).alpha(), 255)
        self.assertEqual(output.pixelColor(95, 42).alpha(), 255)

    def test_clear_tracy_source_zero_preserves_transparent_destination(self):
        output = _indexed_image(
            50, 50,
            [(5, 5), (5, 45), (45, 45)],
            [(10, 10), (10, 10), (10, 10)],
            source_index=0, tracy_mode="clear")
        self.assertEqual(output.pixelColor(15, 30).alpha(), 0)

    def test_orientation_triad_stays_at_margin_and_tracks_camera(self):
        viewport = AssetViewport()
        target = QRectF(0, 0, 360, 260)
        painter_probe = MagicMock()
        viewport._draw_axes(
            painter_probe, target, {"yaw": 0.0, "pitch": 0.0})
        labels = [call.args[-1]
                  for call in painter_probe.drawText.call_args_list]
        self.assertIn("-Y", labels)
        self.assertNotIn("-Y Up", labels)

        def render(yaw, pitch):
            image = QImage(360, 260, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            viewport._draw_axes(
                painter, target,
                {"yaw": yaw, "pitch": pitch})
            painter.end()
            return image

        first = render(0.0, 0.0)
        second = render(70.0, -25.0)
        center = QRectF(140, 95, 80, 70).toRect()
        margin = QRectF(250, 160, 110, 100).toRect()
        self.assertEqual(_alpha_count(first, center), 0)
        self.assertGreater(_alpha_count(first, margin), 20)
        differences = sum(
            first.pixel(x, y) != second.pixel(x, y)
            for y in range(first.height())
            for x in range(first.width()))
        self.assertGreater(differences, 50)
        marker = object()
        viewport._pick_shapes = [marker]
        image = render(15.0, 20.0)
        self.assertFalse(image.isNull())
        self.assertEqual(viewport._pick_shapes, [marker])

    def test_move_vertices_stay_red_while_auto_align_is_active(self):
        model = SkltModel(
            source_name="SNAP.SKLT",
            points=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0)],
            polygons=[[0, 1, 2]],
            parsed_polygon_count=1,
        )
        obj = SimpleNamespace(
            skeleton=model,
            base_object=SimpleNamespace(ades=[]),
        )
        viewport = AssetViewport()
        viewport._edit_session = GeometryEditSession(
            obj,
            [[1.0, 0.0, 0.0],
             [0.0, 1.0, 0.0],
             [0.0, 0.0, 1.0]],
            (0.0, 0.0, 0.0),
        )
        viewport.edit_session.selection = {0, 1}
        target = QRectF(0, 0, 320, 240)
        camera = viewport._camera_state()

        def render():
            image = QImage(320, 240, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            viewport._draw_edit_overlay(
                painter, target, camera)
            painter.end()
            return image

        normal = render()
        viewport._precision_guides = (
            PrecisionGuide(0, 0.0, "status-only guide"),)
        snapped = render()
        viewport._clear_precision_guides()
        green = QColor(45, 255, 95).rgba()
        red = QColor(255, 32, 48).rgba()
        for image in (normal, snapped):
            self.assertGreater(sum(
                image.pixel(x, y) == red
                for y in range(image.height())
                for x in range(image.width())), 20)
            self.assertEqual(sum(
                image.pixel(x, y) == green
                for y in range(image.height())
                for x in range(image.width())), 0)


if __name__ == "__main__":
    unittest.main()
