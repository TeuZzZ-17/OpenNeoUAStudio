import copy
import unittest
from types import SimpleNamespace

from base_mapping_editor import MappingIndex
from base_parser import AmeshBlock, AttsEntry
from geometry_editor import (
    GeometryClipboardError,
    apply_delete_geometry,
    plan_delete_geometry,
)
from sklt_parser import SkltModel


def _fixture():
    model = SkltModel(
        source_name="DELETE.SKLT",
        points=[
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
            (0.0, 1.0, 0.0),
            (99.0, 99.0, 99.0),
        ],
        polygons=[[0, 1, 2], [0, 2, 3]],
        parsed_polygon_count=2,
        poo2_payload_offset=12,
        poo2_payload_size=60,
        pol2_payload_offset=80,
        pol2_payload_size=18,
    )
    block = AmeshBlock(
        class_id="amesh.class",
        atts_chunk_offset=100,
        olpl_chunk_offset=120,
        atts=[
            AttsEntry(0, 10, 20, 30, 40),
            AttsEntry(1, 11, 21, 31, 41),
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
    return obj, model, block


class GeometryDeleteV2Tests(unittest.TestCase):
    def test_delete_compacts_points_polygons_atts_and_matching_olpl(self):
        obj, model, block = _fixture()
        surviving_uvs = copy.deepcopy(block.olpl[1])
        surviving_atts = copy.deepcopy(block.atts[1])
        plan = plan_delete_geometry(obj, {0}, MappingIndex(obj))

        self.assertEqual(plan.old_to_new_polygon, ((1, 0),))
        self.assertEqual(plan.old_to_new_point, ((0, 0), (2, 1), (3, 2)))
        self.assertEqual(plan.polygons, ((0, 1, 2),))
        self.assertEqual(plan.points, (
            (0.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
            (0.0, 1.0, 0.0),
        ))

        result = apply_delete_geometry(
            model, obj.base_object.ades, plan)
        self.assertEqual(result.deleted_polygon_indices, (0,))
        self.assertEqual(model.parsed_polygon_count, 1)
        self.assertEqual(model.polygons, [[0, 1, 2]])
        self.assertEqual(len(block.atts), 1)
        self.assertEqual(block.atts[0], AttsEntry(
            0, surviving_atts.color_val, surviving_atts.shade_val,
            surviving_atts.tracy_val, surviving_atts.pad))
        self.assertEqual(block.olpl, [surviving_uvs])

    def test_shared_vertices_survive_and_exclusive_vertices_are_removed(self):
        obj, model, _block = _fixture()
        plan = plan_delete_geometry(obj, {1}, MappingIndex(obj))
        apply_delete_geometry(model, obj.base_object.ades, plan)
        self.assertEqual(model.points, [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (1.0, 1.0, 0.0),
        ])
        self.assertEqual(model.polygons, [[0, 1, 2]])

    def test_validation_failures_leave_every_list_unchanged(self):
        obj, model, block = _fixture()
        original = (
            copy.deepcopy(model.points),
            copy.deepcopy(model.polygons),
            copy.deepcopy(block.atts),
            copy.deepcopy(block.olpl),
        )
        block.atts.append(copy.deepcopy(block.atts[0]))
        block.olpl.append(copy.deepcopy(block.olpl[0]))
        with self.assertRaisesRegex(
                GeometryClipboardError, "duplicate polygon mappings"):
            plan_delete_geometry(obj, {0}, MappingIndex(obj))
        block.atts.pop()
        block.olpl.pop()
        with self.assertRaisesRegex(
                GeometryClipboardError, "entire model"):
            plan_delete_geometry(obj, {0, 1}, MappingIndex(obj))
        self.assertEqual(model.points, original[0])
        self.assertEqual(model.polygons, original[1])
        self.assertEqual(block.atts, original[2])
        self.assertEqual(block.olpl, original[3])

    def test_invalid_mapping_and_partial_fx_are_rejected(self):
        obj, _model, block = _fixture()
        block.atts[1].poly_id = 99
        with self.assertRaisesRegex(
                GeometryClipboardError, "invalid ATTS"):
            plan_delete_geometry(obj, {0}, MappingIndex(obj))
        block.atts[1].poly_id = 1
        with self.assertRaisesRegex(
                GeometryClipboardError, "FX geometry"):
            plan_delete_geometry(
                obj, {0}, MappingIndex(obj), unsafe_fx_poly_ids={0})

    def test_multiple_polygon_delete_is_one_complete_plan(self):
        obj, model, block = _fixture()
        model.points.extend([
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        ])
        model.polygons.append([5, 6, 7])
        model.parsed_polygon_count = 3
        block.atts.append(AttsEntry(2, 12, 22, 32, 42))
        block.olpl.append([(7, 8), (9, 10), (11, 12)])
        plan = plan_delete_geometry(obj, {0, 1}, MappingIndex(obj))
        apply_delete_geometry(model, obj.base_object.ades, plan)
        self.assertEqual(model.points, [
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        ])
        self.assertEqual(model.polygons, [[0, 1, 2]])
        self.assertEqual(block.atts, [AttsEntry(0, 12, 22, 32, 42)])
        self.assertEqual(block.olpl, [[(7, 8), (9, 10), (11, 12)]])


if __name__ == "__main__":
    unittest.main()
