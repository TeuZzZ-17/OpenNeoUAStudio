import unittest
from types import SimpleNamespace

from base_mapping_editor import MappingIndex
from base_parser import AmeshBlock, AttsEntry, TextureRef
from geometry_editor import (
    GeometryClipboardError,
    append_geometry_clipboard,
    build_geometry_clipboard,
)
from sklt_parser import SkltModel


def _fixture(polygons=None, *, texture_kind="ilbm"):
    model = SkltModel(
        source_name="TEST.SKLT",
        points=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)],
        polygons=polygons or [[0, 1, 2], [0, 2, 3]],
        parsed_polygon_count=2,
    )
    block = AmeshBlock(
        class_id="amesh.class",
        texture=TextureRef(kind=texture_kind, name="TEST.ILB"),
        atts=[
            AttsEntry(0, 1, 2, 3, 4),
            AttsEntry(1, 5, 6, 7, 8),
        ],
        olpl=[
            [(0, 0), (255, 0), (255, 255)],
            [(0, 0), (255, 255), (0, 255)],
        ],
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[block]),
    )
    return obj, model, block, MappingIndex(obj)


class GeometryClipboardTests(unittest.TestCase):
    def test_multi_polygon_payload_deduplicates_shared_vertices(self):
        obj, model, _block, mapping = _fixture()
        payload = build_geometry_clipboard(
            obj, "root", {0, 1}, mapping)
        self.assertEqual(len(payload.points), 4)
        self.assertEqual(payload.original_to_local,
                         ((0, 0), (1, 1), (2, 2), (3, 3)))
        self.assertEqual(payload.polygons[0].local_indices, (0, 1, 2))
        self.assertEqual(payload.polygons[1].local_indices, (0, 2, 3))
        self.assertEqual(payload.polygons[1].uvs,
                         ((0, 0), (255, 255), (0, 255)))
        self.assertEqual(payload.pivot, (0.5, 0.5, 0.0))
        self.assertEqual(len(model.points), 4)

    def test_quad_order_and_uv_order_are_preserved(self):
        obj, model, block, _mapping = _fixture()
        model.polygons[:] = [[0, 1, 2, 3]]
        model.parsed_polygon_count = 1
        block.atts[:] = [AttsEntry(0, 9, 8, 7, 6)]
        block.olpl[:] = [[(1, 2), (3, 4), (5, 6), (7, 8)]]
        payload = build_geometry_clipboard(
            obj, "root", {0}, MappingIndex(obj))
        self.assertEqual(payload.polygons[0].local_indices, (0, 1, 2, 3))
        self.assertEqual(
            payload.polygons[0].uvs,
            ((1, 2), (3, 4), (5, 6), (7, 8)))

    def test_append_is_independent_and_append_only(self):
        obj, model, block, mapping = _fixture()
        old_points = list(model.points)
        old_polygons = [list(poly) for poly in model.polygons]
        old_atts = list(block.atts)
        payload = build_geometry_clipboard(
            obj, "root", {0, 1}, mapping)
        result = append_geometry_clipboard(
            model, [block], payload, (10.0, -2.0, 3.0))
        self.assertEqual(result.point_indices, (4, 5, 6, 7))
        self.assertEqual(result.polygon_indices, (2, 3))
        self.assertEqual(model.points[:4], old_points)
        self.assertEqual(model.polygons[:2], old_polygons)
        self.assertEqual(block.atts[:2], old_atts)
        self.assertEqual(model.polygons[2:], [[4, 5, 6], [4, 6, 7]])
        self.assertEqual(model.points[4], (10.0, -2.0, 3.0))

    def test_invalid_sources_are_rejected_without_changes(self):
        obj, model, block, mapping = _fixture(texture_kind="bmpanim")
        before = (list(model.points), [list(poly) for poly in model.polygons],
                  list(block.atts), [list(group) for group in block.olpl])
        with self.assertRaisesRegex(GeometryClipboardError, "animated"):
            build_geometry_clipboard(obj, "root", {0}, mapping)
        after = (list(model.points), [list(poly) for poly in model.polygons],
                 list(block.atts), [list(group) for group in block.olpl])
        self.assertEqual(after, before)

    def test_unmapped_duplicate_missing_uv_and_degenerate_rejected(self):
        obj, model, block, mapping = _fixture()
        mapping.refs.pop(0)
        with self.assertRaisesRegex(GeometryClipboardError, "unmapped"):
            build_geometry_clipboard(obj, "root", {0}, mapping)

        obj, model, block, mapping = _fixture()
        mapping.refs[0].append(mapping.refs[0][0])
        with self.assertRaisesRegex(GeometryClipboardError, "duplicate"):
            build_geometry_clipboard(obj, "root", {0}, mapping)

        obj, model, block, mapping = _fixture()
        block.olpl.clear()
        with self.assertRaisesRegex(GeometryClipboardError, "OLPL"):
            build_geometry_clipboard(obj, "root", {0}, mapping)

        obj, model, block, mapping = _fixture(
            polygons=[[0, 1, 1], [0, 2, 3]])
        with self.assertRaisesRegex(GeometryClipboardError, "repeats"):
            build_geometry_clipboard(obj, "root", {0}, mapping)


if __name__ == "__main__":
    unittest.main()
