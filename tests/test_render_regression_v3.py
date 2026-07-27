import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    AssetViewport,
    PrecisionGuide,
)
from geometry_editor import GeometryEditSession
from sklt_parser import SkltModel


def _opaque_source():
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor(220, 45, 30, 255))
    return image


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
        viewport = AssetViewport()
        output = QImage(120, 120, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._draw_textured(
            painter,
            [
                QPointF(10, 10),
                QPointF(10, 110),
                QPointF(110, 110),
                QPointF(110, 10),
            ],
            [(32, 32), (32, 32), (32, 32), (32, 32)],
            _opaque_source(),
        )
        painter.end()
        self.assertGreater(_alpha_count(output), 9000)
        self.assertEqual(output.pixelColor(25, 75).alpha(), 255)
        self.assertEqual(output.pixelColor(75, 25).alpha(), 255)

    def test_inclined_quad_keeps_both_fan_triangles_visible(self):
        viewport = AssetViewport()
        output = QImage(140, 130, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._draw_textured(
            painter,
            [
                QPointF(16, 20),
                QPointF(9, 112),
                QPointF(118, 104),
                QPointF(130, 14),
            ],
            [(0, 0), (0, 255), (255, 255), (255, 0)],
            _opaque_source(),
        )
        painter.end()
        self.assertGreater(_alpha_count(output), 9000)
        self.assertEqual(output.pixelColor(35, 75).alpha(), 255)
        self.assertEqual(output.pixelColor(95, 42).alpha(), 255)

    def test_degenerate_fallback_preserves_source_alpha(self):
        viewport = AssetViewport()
        source = QImage(2, 2, QImage.Format.Format_ARGB32)
        source.fill(QColor(120, 60, 30, 128))
        output = QImage(50, 50, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._draw_textured(
            painter,
            [QPointF(5, 5), QPointF(5, 45), QPointF(45, 45)],
            [(10, 10), (10, 10), (10, 10)],
            source,
        )
        painter.end()
        sample = output.pixelColor(15, 30)
        self.assertEqual(sample.alpha(), 128)
        self.assertLessEqual(abs(sample.red() - 120), 1)
        self.assertLessEqual(abs(sample.green() - 60), 1)
        self.assertLessEqual(abs(sample.blue() - 30), 1)

    def test_orientation_triad_stays_at_margin_and_tracks_camera(self):
        viewport = AssetViewport()
        target = QRectF(0, 0, 360, 260)

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

    def test_snap_changes_selected_vertices_to_bright_green_temporarily(self):
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
        restored = render()

        green = QColor(45, 255, 95).rgba()
        red = QColor(255, 32, 48).rgba()
        self.assertGreater(sum(
            snapped.pixel(x, y) == green
            for y in range(snapped.height())
            for x in range(snapped.width())), 20)
        self.assertGreater(sum(
            normal.pixel(x, y) == red
            for y in range(normal.height())
            for x in range(normal.width())), 20)
        self.assertEqual(
            sum(restored.pixel(x, y) == green
                for y in range(restored.height())
                for x in range(restored.width())),
            0)


if __name__ == "__main__":
    unittest.main()
