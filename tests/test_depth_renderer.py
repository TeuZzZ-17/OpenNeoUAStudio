import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QApplication

from asset_family import AssetFamily, FamilyObject
from assembly_viewer import (
    AssetViewport,
    ViewFace,
    ViewMaterial,
    _projective_image_transform_is_bounded,
    resolve_visible_edit_vertices,
)
from base_parser import BaseObject
from depth_renderer import (
    CameraPolygon,
    clip_camera_polygon_near,
    order_camera_polygons,
    order_camera_polygons_fast,
    projective_texture_coefficients,
)
from sklt_parser import SkltModel


def _solid_image(color):
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(color)
    return image


class DepthRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _editable_triangle_viewport():
        model = SkltModel(
            points=[(-1.0, 1.0, 0.0), (-1.0, -1.0, 0.0),
                    (1.0, -1.0, 0.0)],
            polygons=[[0, 2, 1]],
        )
        obj = FamilyObject(BaseObject(), skeleton=model)
        family = AssetFamily(root_object=obj)
        viewport = AssetViewport()
        viewport._show_grid = False
        viewport._show_axes = False
        viewport.load_family(family, primary_owner="root")
        return viewport

    @staticmethod
    def _render(viewport):
        output = QImage(160, 160, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._render_scene(
            painter, QRectF(0, 0, 160, 160), None, False,
            viewport._camera_state(), allow_transparent_background=True)
        painter.end()
        return output

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

    def test_fast_order_preserves_polygons_without_bsp_splitting(self):
        polygons = [
            CameraPolygon(
                ((0.0, 0.0, depth), (1.0, 0.0, depth),
                 (0.0, 1.0, depth)),
                ((0.0, 0.0),) * 3, str(depth), index)
            for index, depth in enumerate((1.0, -1.0, 0.0))
        ]
        ordered = order_camera_polygons_fast(polygons)
        self.assertEqual([polygon.mean_z() for polygon in ordered],
                         [-1.0, 0.0, 1.0])
        self.assertEqual(len(ordered), len(polygons))

    def test_play_and_edit_use_fast_order_while_static_view_stays_exact(self):
        viewport = self._editable_triangle_viewport()
        try:
            with patch(
                    "assembly_viewer.order_camera_polygons",
                    wraps=order_camera_polygons) as exact, patch(
                    "assembly_viewer.order_camera_polygons_fast",
                    wraps=order_camera_polygons_fast) as fast:
                self._render(viewport)
                self.assertEqual(exact.call_count, 1)
                self.assertEqual(fast.call_count, 0)

                viewport._anim_playing = True
                self._render(viewport)
                self.assertEqual(fast.call_count, 1)

                viewport._anim_playing = False
                self.assertTrue(viewport.enter_edit_mode("root"))
                self._render(viewport)
                self.assertEqual(fast.call_count, 2)
                self.assertEqual(exact.call_count, 1)
        finally:
            viewport.close()

    def test_camera_preview_restores_full_render_hints_at_rest(self):
        viewport = self._editable_triangle_viewport()
        output = QImage(160, 160, QImage.Format.Format_ARGB32)
        painter = QPainter(output)
        try:
            viewport._camera_interacting = True
            viewport._render_scene(
                painter, QRectF(0, 0, 160, 160), None, False,
                viewport._camera_state(),
                allow_transparent_background=True)
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.Antialiasing))
            self.assertFalse(painter.testRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform))

            viewport._camera_interacting = False
            viewport._render_scene(
                painter, QRectF(0, 0, 160, 160), None, False,
                viewport._camera_state(),
                allow_transparent_background=True)
            self.assertTrue(painter.testRenderHint(
                QPainter.RenderHint.Antialiasing))
            self.assertTrue(painter.testRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform))
        finally:
            painter.end()
            viewport.close()

    def test_edit_visibility_is_reused_until_camera_or_committed_geometry_changes(
            self):
        viewport = self._editable_triangle_viewport()
        try:
            self.assertTrue(viewport.enter_edit_mode("root"))
            with patch(
                    "assembly_viewer.resolve_visible_edit_vertices",
                    wraps=resolve_visible_edit_vertices) as resolve:
                self._render(viewport)
                self._render(viewport)
                self.assertEqual(resolve.call_count, 1)

                viewport._yaw += 1.0
                self._render(viewport)
                self.assertEqual(resolve.call_count, 2)

                viewport._refresh_edit_faces(
                    invalidate_visibility=False)
                self._render(viewport)
                self.assertEqual(resolve.call_count, 2)

                viewport._refresh_edit_faces()
                self._render(viewport)
                self.assertEqual(resolve.call_count, 3)
        finally:
            viewport.close()

    def test_near_plane_clips_vertices_and_interpolates_uvs(self):
        polygon = CameraPolygon(
            ((-1.0, 0.0, 3.0), (1.0, 0.0, 3.0),
             (0.0, 1.0, 4.2)),
            ((0.0, 0.0), (100.0, 0.0), (50.0, 100.0)),
            "near",
        )
        clipped = clip_camera_polygon_near(polygon)
        self.assertIsNotNone(clipped)
        self.assertTrue(all(vertex[2] <= 3.8 + 1e-7
                            for vertex in clipped.vertices))
        self.assertEqual(len(clipped.vertices), len(clipped.attributes))

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

    def test_unbounded_projective_image_uses_bounded_fallback_predicate(self):
        self.assertFalse(_projective_image_transform_is_bounded(
            (1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, -50.0),
            100, 100))
        self.assertTrue(_projective_image_transform_is_bounded(
            (1.0, 0.0, 0.01, 0.0, 1.0, 0.01, 0.0, 0.0, 5.0),
            100, 100))

    def test_unbounded_projective_map_always_uses_bounded_affine_path(self):
        viewport = AssetViewport()
        output = QImage(120, 120, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        screen = [
            QPointF(10, 10), QPointF(100, 20), QPointF(20, 100)]
        uvs = [(0, 0), (255, 0), (0, 255)]
        camera = [(-1.0, 1.0, 0.0),
                  (1.0, 1.0, 1.0),
                  (-1.0, -1.0, 2.0)]
        unbounded = (
            1.0, 0.0, 1.0,
            0.0, 1.0, 0.0,
            0.0, 0.0, -2.0,
        )
        try:
            with patch(
                    "assembly_viewer.projective_texture_coefficients",
                    return_value=unbounded), patch.object(
                    viewport, "_draw_degenerate_uv_triangle") as fallback:
                viewport._camera_interacting = True
                viewport._draw_textured(
                    painter, screen, uvs,
                    _solid_image(QColor(80, 140, 220)), False, camera)
                fallback.assert_not_called()

                viewport._camera_interacting = False
                viewport._draw_textured(
                    painter, screen, uvs,
                    _solid_image(QColor(80, 140, 220)), False, camera)
                fallback.assert_not_called()
        finally:
            painter.end()
            viewport.close()

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

    def test_wire_overlay_is_occluded_by_nearer_bsp_surface(self):
        viewport = AssetViewport()
        viewport.resize(240, 240)
        viewport._yaw = 0.0
        viewport._pitch = 0.0
        viewport._center = (0.0, 0.0, 0.0)
        viewport._scale = 1.0
        viewport._zoom = 1.0
        viewport._pan = QPointF()
        viewport._show_grid = False
        viewport._show_axes = False
        viewport._show_wire_overlay = True
        viewport._backface_cull = False
        viewport._mode = "materials"
        viewport._materials = [
            ViewMaterial("far", color=QColor(230, 40, 40)),
            ViewMaterial("near", color=QColor(40, 80, 230)),
        ]
        far = list(reversed([
            (-0.22, 0.95, -1.0), (-0.22, -0.95, -1.0),
            (0.22, -0.95, -1.0), (0.22, 0.95, -1.0),
        ]))
        near = list(reversed([
            (-0.85, 0.8, 1.0), (-0.85, -0.8, 1.0),
            (0.85, -0.8, 1.0), (0.85, 0.8, 1.0),
        ]))
        viewport._faces = [
            ViewFace(far, [], 0, poly_id=0),
            ViewFace(near, [], 1, poly_id=1),
        ]
        camera = viewport._camera_state()
        output = QImage(240, 240, QImage.Format.Format_ARGB32)
        output.fill(QColor(0, 0, 0, 0))
        painter = QPainter(output)
        viewport._render_scene(
            painter, QRectF(0, 0, 240, 240), None, False, camera,
            allow_transparent_background=True)
        painter.end()

        hidden_edge = viewport._project(
            viewport._camera_vertex((-0.22, 0.0, -1.0), camera),
            QRectF(0, 0, 240, 240), camera)
        sample = output.pixelColor(
            round(hidden_edge.x()), round(hidden_edge.y()))
        self.assertGreater(sample.blue(), 180)
        self.assertLess(sample.red(), 80)

    def test_wire_overlay_ignores_rendered_backface_when_culling_is_off(self):
        viewport = AssetViewport()
        viewport.resize(180, 180)
        viewport._yaw = 0.0
        viewport._pitch = 0.0
        viewport._center = (0.0, 0.0, 0.0)
        viewport._scale = 1.0
        viewport._zoom = 1.0
        viewport._pan = QPointF()
        viewport._show_grid = False
        viewport._show_axes = False
        viewport._backface_cull = False
        viewport._mode = "materials"
        viewport._materials = [
            ViewMaterial("back", color=QColor(180, 90, 40))]
        viewport._faces = [ViewFace([
            (-0.8, 0.8, 0.0), (-0.8, -0.8, 0.0),
            (0.8, -0.8, 0.0), (0.8, 0.8, 0.0),
        ], [], 0, poly_id=0)]
        camera = viewport._camera_state()

        def render(wire):
            viewport._show_wire_overlay = wire
            image = QImage(180, 180, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0))
            painter = QPainter(image)
            viewport._render_scene(
                painter, QRectF(0, 0, 180, 180), None, False, camera,
                allow_transparent_background=True)
            painter.end()
            return image

        plain = render(False)
        overlay = render(True)
        self.assertEqual(
            [plain.pixel(x, y)
             for y in range(plain.height()) for x in range(plain.width())],
            [overlay.pixel(x, y)
             for y in range(overlay.height()) for x in range(overlay.width())],
        )


if __name__ == "__main__":
    unittest.main()
