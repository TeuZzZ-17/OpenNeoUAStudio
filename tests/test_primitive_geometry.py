import math
import unittest
from types import SimpleNamespace

from base_parser import AmeshBlock, AttsEntry, TextureRef
from geometry_editor import (
    GeometryClipboard,
    GeometryClipboardError,
    append_geometry_clipboard,
)
from primitive_geometry import (
    DEFAULT_SEGMENTS,
    MAX_SEGMENTS,
    MIN_SEGMENTS,
    build_primitive_clipboard,
)
from sklt_parser import SkltModel


def _fixture(*, class_id="amesh.class", texture_kind="ilbm"):
    model = SkltModel(
        source_name="PRIMITIVE.SKLT",
        points=[
            (0.0, 0.0, 0.0),
            (100.0, 0.0, 0.0),
            (0.0, 100.0, 0.0),
        ],
        polygons=[[0, 1, 2]],
        parsed_polygon_count=1,
    )
    block = AmeshBlock(
        class_id=class_id,
        texture=TextureRef(kind=texture_kind, name="STATIC.ILB"),
        atts=[AttsEntry(0, 11, 22, 33, 44)],
        olpl=[[(0, 0), (255, 0), (0, 255)]],
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[block]),
        owner_path="root",
    )
    return obj, model, block


class PrimitiveGeometryTests(unittest.TestCase):
    @staticmethod
    def _newell_normal(clipboard, polygon):
        points = [
            clipboard.points[index] for index in polygon.local_indices]
        nx = ny = nz = 0.0
        for index, (x0, y0, z0) in enumerate(points):
            x1, y1, z1 = points[(index + 1) % len(points)]
            nx += (y0 - y1) * (z0 + z1)
            ny += (z0 - z1) * (x0 + x1)
            nz += (x0 - x1) * (y0 + y1)
        return (nx, ny, nz), points

    def test_all_v1_shapes_are_normal_geometry_clipboards(self):
        obj, model, _block = _fixture()
        expected = {
            "triangle": (3, 1),
            "square": (4, 1),
            "rectangle": (4, 1),
            "circle": (DEFAULT_SEGMENTS, 1),
            "plane": (4, 1),
            "cube": (8, 6),
            "pyramid": (5, 5),
            "cylinder": (DEFAULT_SEGMENTS * 2, DEFAULT_SEGMENTS + 2),
        }
        original_points = list(model.points)
        original_polygons = [list(polygon) for polygon in model.polygons]

        for kind, (point_count, polygon_count) in expected.items():
            with self.subTest(kind=kind):
                clipboard = build_primitive_clipboard(
                    obj, "root", 0, 0, kind)
                self.assertIsInstance(clipboard, GeometryClipboard)
                self.assertEqual(len(clipboard.points), point_count)
                self.assertEqual(len(clipboard.polygons), polygon_count)
                self.assertEqual(clipboard.owner, "root")
                self.assertEqual(clipboard.model_identity, id(model))
                self.assertEqual(clipboard.source_name, model.source_name)
                self.assertEqual(clipboard.original_to_local, ())
                self.assertEqual(len(clipboard.local_points), point_count)
                for polygon in clipboard.polygons:
                    self.assertEqual(polygon.source_poly_id, 0)
                    self.assertEqual(polygon.block_index, 0)
                    self.assertEqual(
                        (polygon.color_val, polygon.shade_val,
                         polygon.tracy_val, polygon.pad),
                        (11, 22, 33, 44))
                    self.assertEqual(polygon.block_class_id, "amesh.class")
                    self.assertFalse(polygon.atts_only_mapping)
                    self.assertEqual(
                        len(polygon.uvs), len(polygon.local_indices))
                    self.assertEqual(
                        len(set(polygon.local_indices)),
                        len(polygon.local_indices))
                    self.assertTrue(all(
                        0 <= u <= 255 and 0 <= v <= 255
                        for u, v in polygon.uvs))

        self.assertEqual(model.points, original_points)
        self.assertEqual(model.polygons, original_polygons)

    def test_aliases_match_their_canonical_shapes(self):
        obj, _model, _block = _fixture()
        disc = build_primitive_clipboard(
            obj, "root", 0, 0, "disc", segments=7, radius=25)
        circle = build_primitive_clipboard(
            obj, "root", 0, 0, "circle", segments=7, radius=25)
        box = build_primitive_clipboard(
            obj, "root", 0, 0, "box", size=30)
        cube = build_primitive_clipboard(
            obj, "root", 0, 0, "cube", size=30)
        self.assertEqual(disc.points, circle.points)
        self.assertEqual(disc.polygons, circle.polygons)
        self.assertEqual(box.points, cube.points)
        self.assertEqual(box.polygons, cube.polygons)

    def test_dimensions_are_applied_and_finite(self):
        obj, _model, _block = _fixture()
        rectangle = build_primitive_clipboard(
            obj, "root", 0, 0, "rectangle", width=240, height=80)
        xs = [point[0] for point in rectangle.points]
        ys = [point[1] for point in rectangle.points]
        self.assertEqual(max(xs) - min(xs), 240.0)
        self.assertEqual(max(ys) - min(ys), 80.0)

        pyramid = build_primitive_clipboard(
            obj, "root", 0, 0, "pyramid", base=180, height=300)
        xs = [point[0] for point in pyramid.points]
        ys = [point[1] for point in pyramid.points]
        zs = [point[2] for point in pyramid.points]
        self.assertEqual(max(xs) - min(xs), 180.0)
        self.assertEqual(max(ys) - min(ys), 300.0)
        self.assertEqual(max(zs) - min(zs), 180.0)
        for value in pyramid.pivot:
            self.assertAlmostEqual(value, 0.0)
        self.assertTrue(all(
            math.isfinite(value)
            for point in pyramid.points for value in point))

    def test_cube_has_full_independent_mapping_on_every_face(self):
        obj, _model, _block = _fixture()
        cube = build_primitive_clipboard(
            obj, "root", 0, 0, "cube")
        expected_corners = {
            (0, 0), (255, 0), (255, 255), (0, 255)}
        self.assertEqual(len(cube.polygons), 6)
        for polygon in cube.polygons:
            self.assertEqual(set(polygon.uvs), expected_corners)

    def test_closed_solids_have_outward_non_degenerate_winding(self):
        obj, _model, _block = _fixture()
        for kind in ("cube", "pyramid", "cylinder"):
            clipboard = build_primitive_clipboard(
                obj, "root", 0, 0, kind)
            with self.subTest(kind=kind):
                for polygon in clipboard.polygons:
                    normal, points = self._newell_normal(
                        clipboard, polygon)
                    face_center = tuple(
                        sum(point[axis] for point in points) / len(points)
                        for axis in range(3))
                    self.assertGreater(
                        sum(normal[axis] * face_center[axis]
                            for axis in range(3)),
                        0.0)

    def test_segment_defaults_limits_and_custom_counts(self):
        obj, _model, _block = _fixture()
        circle = build_primitive_clipboard(
            obj, "root", 0, 0, "circle")
        self.assertEqual(len(circle.points), DEFAULT_SEGMENTS)

        for segments in (MIN_SEGMENTS, MAX_SEGMENTS):
            with self.subTest(segments=segments):
                circle = build_primitive_clipboard(
                    obj, "root", 0, 0, "circle", segments=segments)
                cylinder = build_primitive_clipboard(
                    obj, "root", 0, 0, "cylinder", segments=segments)
                self.assertEqual(len(circle.points), segments)
                self.assertEqual(len(circle.polygons[0].uvs), segments)
                self.assertEqual(len(cylinder.points), segments * 2)
                self.assertEqual(len(cylinder.polygons), segments + 2)

        for invalid in (
                MIN_SEGMENTS - 1, MAX_SEGMENTS + 1, 12.0, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                        GeometryClipboardError, "segments"):
                    build_primitive_clipboard(
                        obj, "root", 0, 0, "circle", segments=invalid)

    def test_append_is_compatible_and_reuses_selected_atts_material(self):
        obj, model, block = _fixture()
        clipboard = build_primitive_clipboard(
            obj, "root", 0, 0, "cube", size=40)
        before_points = len(model.points)
        before_polygons = len(model.polygons)
        result = append_geometry_clipboard(
            model, [block], clipboard, (10.0, 20.0, 30.0))

        self.assertEqual(len(result.point_indices), 8)
        self.assertEqual(len(result.polygon_indices), 6)
        self.assertEqual(len(model.points), before_points + 8)
        self.assertEqual(len(model.polygons), before_polygons + 6)
        self.assertEqual(len(block.atts), 7)
        self.assertEqual(len(block.olpl), 7)
        self.assertEqual(
            model.points[result.point_indices[0]],
            tuple(value + delta for value, delta in zip(
                clipboard.points[0], (10.0, 20.0, 30.0))))
        self.assertEqual(
            [entry.poly_id for entry in block.atts[1:]],
            list(result.polygon_indices))
        self.assertTrue(all(
            (entry.color_val, entry.shade_val,
             entry.tracy_val, entry.pad) == (11, 22, 33, 44)
            for entry in block.atts[1:]))

    def test_invalid_material_context_is_rejected_without_mutation(self):
        cases = (
            {"class_id": "area.class", "message": "amesh"},
            {"texture_kind": "bmpanim", "message": "static ILBM"},
            {"texture_kind": "unknown.class", "message": "static ILBM"},
        )
        for settings in cases:
            message = settings.pop("message")
            with self.subTest(settings=settings):
                obj, model, block = _fixture(**settings)
                before = (
                    list(model.points),
                    [list(polygon) for polygon in model.polygons],
                    list(block.atts),
                    [list(group) for group in block.olpl],
                )
                with self.assertRaisesRegex(
                        GeometryClipboardError, message):
                    build_primitive_clipboard(
                        obj, "root", 0, 0, "triangle")
                self.assertEqual(before, (
                    model.points, model.polygons, block.atts, block.olpl))

    def test_invalid_mapping_dimensions_kind_and_parameters_are_rejected(self):
        obj, _model, block = _fixture()
        block.olpl.clear()
        with self.assertRaisesRegex(GeometryClipboardError, "ATTS/OLPL"):
            build_primitive_clipboard(
                obj, "root", 0, 0, "triangle")

        obj, _model, _block = _fixture()
        invalid_calls = (
            ("rectangle", {"width": 0, "height": 10}, "width"),
            ("square", {"size": float("nan")}, "finite"),
            ("circle", {"radius": float("inf")}, "finite"),
            ("pyramid", {"base": 10, "base_size": 10}, "either"),
            ("triangle", {"unexpected": 1}, "unsupported"),
            ("sphere", {}, "unsupported primitive"),
        )
        for kind, params, message in invalid_calls:
            with self.subTest(kind=kind, params=params):
                with self.assertRaisesRegex(
                        GeometryClipboardError, message):
                    build_primitive_clipboard(
                        obj, "root", 0, 0, kind, **params)


if __name__ == "__main__":
    unittest.main()
