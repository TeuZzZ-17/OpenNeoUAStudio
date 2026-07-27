import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF
from PySide6.QtWidgets import QApplication

from assembly_viewer import (
    AssetViewport,
    PickShape,
    ViewFace,
    ViewMaterial,
    resolve_pick_shape,
    resolve_visible_edit_vertices,
)
from geometry_editor import GeometryEditSession
from sklt_parser import SkltModel


def _shape(poly_id, screen, depths, draw_order, *, owner="root",
           front_facing=True):
    face = ViewFace(
        vertices=[(0.0, 0.0, depth) for depth in depths],
        uvs=[],
        material=0,
        poly_id=poly_id,
        owner=owner,
    )
    return PickShape(
        QPolygonF([QPointF(x, y) for x, y in screen]),
        face,
        tuple((0.0, 0.0, depth) for depth in depths),
        draw_order,
        front_facing,
    )


class PickingDepthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_opaque_in_front_of_later_additive(self):
        screen = [(0, 0), (0, 100), (100, 100), (100, 0)]
        opaque = _shape(1, screen, [1.0] * 4, 0)
        additive = _shape(2, screen, [-1.0] * 4, 1)
        self.assertIs(resolve_pick_shape(
            [opaque, additive], QPointF(50, 50)), opaque)

    def test_additive_really_in_front(self):
        screen = [(0, 0), (0, 100), (100, 100), (100, 0)]
        opaque = _shape(1, screen, [0.0] * 4, 0)
        additive = _shape(2, screen, [1.5] * 4, 1)
        self.assertIs(resolve_pick_shape(
            [opaque, additive], QPointF(50, 50)), additive)

    def test_local_depth_beats_face_average_on_inclined_polygon(self):
        screen = [(0, 0), (0, 100), (100, 100), (100, 0)]
        inclined = _shape(1, screen, [1.8, 1.8, -1.8, -1.8], 0)
        flat = _shape(2, screen, [0.2] * 4, 1)
        self.assertIs(resolve_pick_shape(
            [inclined, flat], QPointF(8, 50)), inclined)
        self.assertIs(resolve_pick_shape(
            [inclined, flat], QPointF(92, 50)), flat)

    def test_nearly_coincident_faces_use_draw_order_only_as_tie_breaker(self):
        screen = [(0, 0), (0, 100), (100, 100), (100, 0)]
        first = _shape(1, screen, [0.0] * 4, 0)
        last = _shape(2, screen, [0.0] * 4, 1)
        self.assertIs(resolve_pick_shape(
            [first, last], QPointF(50, 50)), last)

    def test_partially_overlapping_polygons(self):
        left = _shape(
            1, [(0, 0), (0, 80), (80, 80), (80, 0)], [0.0] * 4, 0)
        right = _shape(
            2, [(40, 0), (40, 80), (120, 80), (120, 0)], [1.0] * 4, 1)
        self.assertIs(resolve_pick_shape(
            [left, right], QPointF(20, 40)), left)
        self.assertIs(resolve_pick_shape(
            [left, right], QPointF(60, 40)), right)

    def test_pick_and_deselect_share_the_same_resolver(self):
        viewport = AssetViewport()
        screen = [(0, 0), (0, 100), (100, 100), (100, 0)]
        near = _shape(7, screen, [1.0] * 4, 0)
        far = _shape(8, screen, [-1.0] * 4, 1)
        viewport._pick_shapes = [near, far]
        deselected = []
        viewport.polygonDeselected.connect(deselected.append)
        self.assertEqual(viewport.pick_at(QPoint(50, 50), True), 7)
        self.assertTrue(viewport.deselect_at(QPoint(50, 50)))
        self.assertEqual(deselected, [7])

    def test_ctrl_additive_flag_is_preserved(self):
        viewport = AssetViewport()
        shape = _shape(
            4, [(0, 0), (0, 100), (100, 100), (100, 0)], [0.0] * 4, 0)
        viewport._pick_shapes = [shape]
        picked = []
        viewport.polygonPickedDetailed.connect(
            lambda poly_id, additive: picked.append((poly_id, additive)))
        viewport.pick_at(QPoint(50, 50), True)
        self.assertEqual(picked, [(4, True)])

    def test_only_registered_rendered_shapes_are_pickable(self):
        visible = _shape(
            1, [(0, 0), (0, 100), (100, 100), (100, 0)], [0.0] * 4, 0)
        hidden_or_culled = _shape(
            2, [(0, 0), (0, 100), (100, 100), (100, 0)], [2.0] * 4, 1,
            owner="hidden")
        self.assertIs(resolve_pick_shape(
            [visible], QPointF(50, 50)), visible)
        self.assertIsNot(resolve_pick_shape(
            [visible], QPointF(50, 50)), hidden_or_culled)

    def test_backface_culling_excludes_face_from_pick_registry(self):
        viewport = AssetViewport()
        viewport.resize(200, 200)
        viewport._yaw = 0.0
        viewport._pitch = 0.0
        viewport._center = (0.0, 0.0, 0.0)
        viewport._scale = 1.0
        viewport._materials = [ViewMaterial("test")]
        viewport._faces = [ViewFace(
            vertices=[(-1.0, 0.0, 0.0), (0.0, -1.0, 0.0),
                      (1.0, 0.0, 0.0)],
            uvs=[], material=0, poly_id=3)]
        image = QImage(200, 200, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        viewport._render_scene(
            painter, QRectF(0, 0, 200, 200), QColor(0, 0, 0), False,
            viewport._camera_state())
        painter.end()
        self.assertEqual(viewport._pick_shapes, [])

        viewport._faces[0].vertices.reverse()
        painter = QPainter(image)
        viewport._render_scene(
            painter, QRectF(0, 0, 200, 200), QColor(0, 0, 0), False,
            viewport._camera_state())
        painter.end()
        self.assertEqual(len(viewport._pick_shapes), 1)

    def test_vertex_overlay_click_and_box_share_front_depth_visibility(self):
        screen = [(40, 40), (40, 120), (120, 120)]
        near = _shape(0, screen, [1.0] * 3, 1)
        far = _shape(1, screen, [-1.0] * 3, 0)
        backface = _shape(
            2, [(180, 40), (180, 120), (260, 120)],
            [1.0] * 3, 2, front_facing=False)
        points = [QPointF(x, y) for x, y in (
            *screen, *screen, (180, 40), (180, 120), (260, 120))]
        polygons = [[0, 1, 2], [3, 4, 5], [6, 7, 8]]

        visible = resolve_visible_edit_vertices(
            [far, near, backface], points, polygons, "root",
            target=QRectF(0, 0, 300, 180))
        self.assertEqual(visible, {0, 1, 2})
        self.assertEqual(resolve_visible_edit_vertices(
            [far, near, backface], points, polygons, "root",
            selected={3}, target=QRectF(0, 0, 300, 180)),
            {0, 1, 2, 3})

        model = SkltModel(
            source_name="VISIBLE.SKLT",
            points=[(0.0, 0.0, 0.0)] * len(points),
            polygons=polygons,
            parsed_polygon_count=3,
        )
        viewport = AssetViewport()
        viewport.resize(300, 180)
        viewport._edit_session = GeometryEditSession(
            SimpleNamespace(
                skeleton=model,
                base_object=SimpleNamespace(ades=[])),
            [[1.0, 0.0, 0.0],
             [0.0, 1.0, 0.0],
             [0.0, 0.0, 1.0]],
            (0.0, 0.0, 0.0),
        )
        viewport._edit_owner = "root"
        viewport._pick_shapes = [far, near, backface]
        viewport._edit_visibility_ready = False
        with patch.object(
                viewport, "_edit_screen_points", return_value=points):
            self.assertEqual(viewport._nearest_edit_vertex(QPoint(40, 40)), 0)
            viewport.edit_session.selection = {3}
            self.assertEqual(viewport._nearest_edit_vertex(QPoint(40, 40)), 3)
            viewport.edit_session.selection.clear()
            viewport._box_rect = QRect(35, 35, 90, 90)
            viewport._apply_box_select(False)
        self.assertEqual(viewport.edit_session.selection, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
