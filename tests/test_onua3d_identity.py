from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch
import zipfile

from asset_family import rebuild_materials
from asset_family_package import family_semantic_snapshot, validate_family_package
from onua3d_export import MANIFEST, build_scene, write_onua3d
from onua3d_identity import (
    ATTRIBUTE, IDENTITY_CONTRACT, VertexIdentityError,
    canonical_vertex_positions, vertex_identity_mapping,
)
from tests import test_onua3d_export as fixtures
from tests import test_onua3d_fidelity as fidelity_fixtures


def decoded_primitives(scene):
    doc, binary = fixtures.decode_glb(scene)
    return [{name: fixtures.values(doc, binary, accessor)
             for name, accessor in p["attributes"].items()}
            for mesh in doc["meshes"] for p in mesh["primitives"]]


def scene_identity(family):
    scene, _, nodes, _ = build_scene(family)
    return scene, {"vertex_identity": dict(IDENTITY_CONTRACT), "nodes": nodes}


class VertexIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.Onua3dTests.setUpClass()

    def setUp(self):
        fixtures.Onua3dTests.setUp(self)
        self.scene, self.manifest = scene_identity(self.family)
        self.primitives = decoded_primitives(self.scene)

    def test_unique_id_has_exact_native_origin_and_float_scalar_accessor(self):
        origins = vertex_identity_mapping(self.manifest)
        self.assertEqual(origins, {i+1: {"owner_path": "root", "point_id": i,
            "polygon_id": 0, "corner": i, "block_index": 0} for i in range(3)})
        doc, _ = fixtures.decode_glb(self.scene)
        accessor = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"][ATTRIBUTE]]
        self.assertEqual((accessor["type"], accessor["componentType"]), ("SCALAR", 5126))
        self.assertEqual(canonical_vertex_positions(self.manifest, self.primitives),
                         {1: (0, 0, 0), 2: (1, 0, 0), 3: (0, -1, 0)})

    def test_normal_split_equal_duplicates_and_reordering_have_one_canonical_position(self):
        expected = canonical_vertex_positions(self.manifest, self.primitives)
        duplicates = copy.deepcopy(self.primitives)
        duplicates[0]["NORMAL"] = [(0, 0, -1)] * 3
        duplicates[0]["TEXCOORD_0"] = [(0.25, 0.25)] * 3
        combined = self.primitives + duplicates
        self.assertEqual(canonical_vertex_positions(self.manifest, combined), expected)
        reordered = [{k: list(reversed(v)) for k, v in p.items()} for p in reversed(combined)]
        self.assertEqual(canonical_vertex_positions(self.manifest, reordered), expected)

    def test_conflicting_copy_fails_in_both_orders_without_modifying_inputs(self):
        duplicate = copy.deepcopy(self.primitives[0])
        duplicate["POSITION"][0] = (0.25, 0, 0)
        before = copy.deepcopy(self.primitives)
        for items in (self.primitives + [duplicate], [duplicate] + self.primitives):
            with self.assertRaisesRegex(VertexIdentityError, "conflicting POSITION"):
                canonical_vertex_positions(self.manifest, items)
        self.assertEqual(self.primitives, before)

    def test_missing_unknown_corrupt_ids_and_positions_fail_closed(self):
        cases = []
        missing = copy.deepcopy(self.primitives); del missing[0][ATTRIBUTE]; cases.append(missing)
        missing = copy.deepcopy(self.primitives)
        for values in missing[0].values(): values.pop()
        cases.append(missing)
        for value in (0, -1, 1.5, float("nan"), float("inf"), True, "1", 4, 16777217):
            bad = copy.deepcopy(self.primitives); bad[0][ATTRIBUTE][0] = value; cases.append(bad)
        for value in ((float("nan"), 0, 0), (0, float("inf"), 0), (0, 0), (True, 0, 0)):
            bad = copy.deepcopy(self.primitives); bad[0]["POSITION"][0] = value; cases.append(bad)
        bad = copy.deepcopy(self.primitives); bad[0][ATTRIBUTE].pop(); cases.append(bad)
        for i, items in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(VertexIdentityError):
                canonical_vertex_positions(self.manifest, items)

    def test_ambiguous_manifest_id_and_invalid_origin_fail_closed(self):
        for edit in ("duplicate", "point", "missing", "version"):
            manifest = copy.deepcopy(self.manifest)
            if edit == "duplicate":
                kid = copy.deepcopy(manifest["nodes"][0]); kid["owner_path"] = "root/kid[0]"
                manifest["nodes"].append(kid)
            elif edit == "point": manifest["nodes"][0]["vertex_instances"]["1"]["point_id"] = -1
            elif edit == "missing": del manifest["nodes"][0]["vertex_instances"]
            else: manifest["vertex_identity"]["version"] = 2
            with self.subTest(edit=edit), self.assertRaises(VertexIdentityError):
                canonical_vertex_positions(manifest, self.primitives)

    def test_seams_distinct_ids_can_share_one_sklt_point_without_welding(self):
        obj = self.family.root_object
        obj.skeleton.points.append((1, 1, 0))
        obj.skeleton.polygons.append([1, 3, 2])
        block = copy.deepcopy(obj.base_object.ades[0]); block.atts[0].poly_id = 1
        block.olpl[0] = [(32, 64), (128, 64), (64, 128)]
        obj.base_object.ades.append(block)
        rebuild_materials(obj, self.family)
        scene, manifest = scene_identity(self.family)
        origins = vertex_identity_mapping(manifest)
        self.assertEqual(len(origins), 6)
        shared = [i for i, o in origins.items() if o["point_id"] == 1]
        self.assertEqual(len(shared), 2)
        self.assertEqual({origins[i]["block_index"] for i in shared}, {0, 1})
        canonical = canonical_vertex_positions(manifest, decoded_primitives(scene))
        self.assertEqual(canonical[shared[0]], canonical[shared[1]])
        self.assertEqual(len(canonical), 6)

    def test_kids_and_vanm_have_global_unique_ids_and_native_state_mapping(self):
        fidelity_fixtures.Onua3dFidelityTests._animated_fixture(self)
        kid = copy.deepcopy(self.family.root_object); kid.owner_path = "root/kid[0]"
        self.family.root_object.kids.append(kid)
        before = family_semantic_snapshot(self.family)
        scene, manifest = scene_identity(self.family)
        origins = vertex_identity_mapping(manifest)
        self.assertEqual(len(origins), 48)
        self.assertEqual({o["owner_path"] for o in origins.values()}, {"root", "root/kid[0]"})
        self.assertEqual({o["vanm_frame"] for o in origins.values()}, set(range(8)))
        self.assertEqual(len(canonical_vertex_positions(manifest, decoded_primitives(scene))), 48)
        self.assertEqual(before, family_semantic_snapshot(self.family))

    def test_package_is_deterministic_and_embedded_family_is_byte_identical(self):
        before = fixtures.source_hashes(self.source)
        target = self.root / "new.onua3d"
        manifest = write_onua3d(self.source, target)
        first = target.read_bytes(); write_onua3d(self.source, target)
        self.assertEqual(first, target.read_bytes())
        self.assertEqual(manifest["version"], 1)
        with zipfile.ZipFile(target) as archive:
            stored = json.loads(archive.read(MANIFEST))
            canonical_vertex_positions(stored, decoded_primitives(archive.read("scene.glb")))
            for name in archive.namelist():
                if name.startswith("ua_family/"):
                    self.assertEqual(archive.read(name), (self.source / name[len("ua_family/"):]).read_bytes())
        self.assertEqual(before, fixtures.source_hashes(self.source))

    def test_legacy_without_identity_remains_a_valid_family_but_not_editable(self):
        legacy = copy.deepcopy(self.manifest); del legacy["vertex_identity"]
        for row in legacy["nodes"]: del row["vertex_instances"]
        with self.assertRaisesRegex(VertexIdentityError, "regenerate"):
            canonical_vertex_positions(legacy, self.primitives)
        validation = validate_family_package(self.source)
        self.assertTrue(validation.valid, validation.errors)

    def test_float_id_limit_fails_without_replacing_destination(self):
        target = self.root / "keep.onua3d"; target.write_bytes(b"old output")
        with patch("onua3d_export.MAX_VERTEX_ID", 2):
            with self.assertRaisesRegex(Exception, "ID limit exceeded"):
                write_onua3d(self.source, target)
        self.assertEqual(target.read_bytes(), b"old output")


if __name__ == "__main__":
    unittest.main()
