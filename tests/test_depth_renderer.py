import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport, ViewFace, ViewMaterial
from depth_renderer import (
    CameraPolygon,
    order_camera_polygons,
    projective_texture_coefficients,
)


def _solid_image(color):
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(color)
    return image


class DepthRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_bsp_splits_intersecting_polygons(self):
        flat = CameraPolygon(
            ((-1.0, 1.0, 0.0),
             (-1.0, -1.0, 0.0),
             (1.0, -1.0, 0.0),
             (1.0, 1.0, 0.0)),
            ((0.0, 0.0),) * 4,
            "flat",
            0,
        )
        inclined = CameraPolygon(
            ((-1.0, 1.0, -1.0),
             (-1.0, -1.0, -1.0),
             (1.0, -1.0, 1.0),
             (1.0, 1.0, 1.0)),
            ((0.0, 0.0),) * 4,
            "inclined",
            1,
        )
        ordered = order_camera_polygons([flat, inclined])
        self.assertGreater(len(ordered), 2)
        self.assertEqual(
            {polygon.payload for polygon in ordered},
            {"flat", "inclined"},
        )
        # One source becomes the BSP partition; the other spans its plane and
        # must retain independently ordered pieces on both sides.
        piece_counts = {
            payload: sum(polygon.payload == payload for polygon in ordered)
            for payload in ("flat", "inclined")
        }
        self.assertGreaterEqual(max(piece_counts.values()), 2)

    def test_projective_transform_maps_vertices_and_is_not_affine(self):
        source = ((0.0, 0.0), (100.0, 0.0), (0.0, 100.0))
        screen = ((20.0, 20.0), (220.0, 35.0), (30.0, 230.0))
        camera = ((-1.0, 1.0, -1.0),
                  (1.0, 1.0, 1.0),
                  (-1.0, -1.0, 2.0))
        coefficients = projective_texture_coefficients(
            source, screen, camera)
        self.assertIsNotNone(coefficients)
        transform = QTransform(*coefficients)
        for source_point, expected in zip(source, screen):
            mapped = transform.map(QPointF(*source_point))
            self.assertAlmostEqual(mapped.x(), expected[0], places=6)
            self.assertAlmostEqual(mapped.y(), expected[1], places=6)

        mapped_center = transform.map(QPointF(100.0 / 3.0, 100.0 / 3.0))
        affine_center = (
            sum(point[0] for point in screen) / 3.0,
            sum(point[1] for point in screen) / 3.0,
        )
        self.assertGreater(
            abs(mapped_center.x() - affine_center[0])
            + abs(mapped_center.y() - affine_center[1]),
            5.0,
        )

    def test_intersecting_textured_faces_are_visible_on_correct_side(self):
        viewport = AssetViewport()
        viewport._backface_cull = False
        viewport._mode = "textured"
        viewport._show_grid = False
        viewport._show_axes = False
        viewport._materials = [
            ViewMaterial("red", image=_solid_image(
                QColor(230, 25, 20, 255))),
            ViewMaterial("blue", image=_solid_image(
                QColor(20, 45, 230, 255))),
        ]
        flat = [
            (-1.0, 1.0, 0.0),
            (-1.0, -1.0, 0.0),
            (1.0, -1.0, 0.0),
            (1.0, 1.0, 0.0),
        ]
        inclined = [
            (-1.0, 1.0, -1.0),
            (-1.0, -1.0, -1.0),
            (1.0, -1.0, 1.0),
            (1.0, 1.0, 1.0),
        ]
        uvs = [(0, 0), (0, 255), (255, 255), (255, 0)]
        viewport._faces = [
            ViewFace(flat, uvs, 0, poly_id=0),
            ViewFace(inclined, uvs, 1, poly_id=1),
        ]
        camera = viewport._camera_state()
        camera.update({
            "center": (0.0, 0.0, 0.0),
            "scale": 1.0,
            "yaw": 0.0,
            "pitch": 0.0,
            "zoom": 1.0,
            "pan": QPointF(0.0, 0.0),
        })
        output = QImage(240, 240, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._render_scene(
            painter, QRectF(0, 0, 240, 240), None, True, camera,
            allow_transparent_background=True)
        painter.end()

        left = output.pixelColor(75, 120)
        right = output.pixelColor(185, 120)
        self.assertGreater(left.red(), 180)
        self.assertLess(left.blue(), 80)
        self.assertGreater(right.blue(), 180)
        self.assertLess(right.red(), 80)

    def test_backface_culling_is_applied_to_each_runtime_fan_triangle(self):
        viewport = AssetViewport()
        viewport._backface_cull = True
        viewport._mode = "solid"
        viewport._show_grid = False
        viewport._show_axes = False
        viewport._materials = [ViewMaterial("solid")]
        # The first runtime fan triangle (0,2,1) faces the camera; the second
        # (0,3,2) faces away.  Whole-polygon culling cannot represent this.
        viewport._faces = [ViewFace(
            [
                (-2.0 / 3.0, 2.0 / 3.0, 0.0),
                (-2.0 / 3.0, -2.0 / 3.0, 0.0),
                (2.0 / 3.0, -2.0 / 3.0, 0.0),
                (-2.0 / 3.0, -4.0 / 3.0, 0.0),
            ],
            [],
            0,
            poly_id=7,
        )]
        camera = viewport._camera_state()
        camera.update({
            "center": (0.0, 0.0, 0.0),
            "scale": 1.0,
            "yaw": 0.0,
            "pitch": 0.0,
            "zoom": 1.0,
            "pan": QPointF(0.0, 0.0),
        })
        output = QImage(200, 200, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._render_scene(
            painter, QRectF(0, 0, 200, 200), None, False, camera,
            allow_transparent_background=True)
        painter.end()
        self.assertEqual(len(viewport._pick_shapes), 1)
        self.assertEqual(viewport._pick_shapes[0].face.poly_id, 7)


if __name__ == "__main__":
    unittest.main()
