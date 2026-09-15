"""Optional real Blender regressions. Set ONUA3D_BLENDER to blender.exe.

Retail tests additionally use ONUA3D_HUBI2_PACKAGE and ONUA3D_RETAIL_ARCHIVE,
the same original-data fixtures as test_onua3d_retail. No game data in Git.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import zipfile

from assembly_window import AssemblyWindow
from asset_family import rebuild_materials
from onua3d_export import MANIFEST, build_scene, write_onua3d
from onua3d_identity import ATTRIBUTE, canonical_vertex_positions, vertex_identity_mapping
from tests import test_onua3d_export as fixtures
from tests import test_onua3d_identity as identity_fixtures


def triangles_and_copies(scene):
    doc, binary = fixtures.decode_glb(scene)
    triangles, slots, normals = [], {}, {}
    for mesh in doc["meshes"]:
        for p in mesh["primitives"]:
            attrs = p["attributes"]
            ids = fixtures.values(doc, binary, attrs[ATTRIBUTE])
            indices = fixtures.values(doc, binary, p["indices"])
            normal = fixtures.values(doc, binary, attrs["NORMAL"]) if "NORMAL" in attrs else None
            for i in set(indices):
                slots[(attrs[ATTRIBUTE], i)] = int(ids[i])
                if normal:
                    normals.setdefault(int(ids[i]), set()).add(normal[i])
            for start in range(0, len(indices), 3):
                triangle = tuple(int(ids[i]) for i in indices[start:start + 3])
                # Cyclic triangle rotations are equivalent; reversed winding is not.
                triangles.append(min(triangle, triangle[1:] + triangle[:1], triangle[2:] + triangle[:2]))
    return sorted(triangles), Counter(slots.values()), normals


def reorder_scene(scene):
    """Reindex nodes and reverse primitive/vertex arrays, preserving references."""
    from onua3d_export import _Glb
    doc, binary = fixtures.decode_glb(scene)
    g = _Glb(); g.doc = copy.deepcopy(doc); g.binary = bytearray(binary)
    remap = {i: len(doc["nodes"]) - 1 - i for i in range(len(doc["nodes"]))}
    g.doc["nodes"].reverse()
    for node in g.doc["nodes"]:
        if "children" in node: node["children"] = [remap[i] for i in node["children"]]
    for scene_row in g.doc["scenes"]:
        scene_row["nodes"] = [remap[i] for i in scene_row["nodes"]]
    for animation in g.doc.get("animations", []):
        for channel in animation["channels"]:
            channel["target"]["node"] = remap[channel["target"]["node"]]
    for mesh in g.doc["meshes"]:
        mesh["primitives"].reverse()
        for p in mesh["primitives"]:
            count = doc["accessors"][p["attributes"]["POSITION"]]["count"]
            for name, index in list(p["attributes"].items()):
                width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[doc["accessors"][index]["type"]]
                p["attributes"][name] = g.accessor(list(reversed(fixtures.values(doc, binary, index))), width)
            p["indices"] = g.accessor([count - 1 - i for i in fixtures.values(doc, binary, p["indices"])],
                                      1, indices=True)
    return g.encode()


class BlenderIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blender = os.environ.get("ONUA3D_BLENDER", "")
        if not Path(cls.blender).is_file():
            raise unittest.SkipTest("set ONUA3D_BLENDER to a real Blender executable")
        fixtures.Onua3dTests.setUpClass()

    def setUp(self):
        fixtures.Onua3dTests.setUp(self)

    def _blender(self, source, target, move_ids, *, object_move=False):
        job = target.with_suffix(".json")
        job.write_text(json.dumps({"input": str(source), "output": str(target),
                                   "move_ids": move_ids, "object_move": object_move}), encoding="utf-8")
        result = subprocess.run([self.blender, "--background", "--factory-startup",
            "--python-exit-code", "1", "--python",
            str(Path(__file__).with_name("blender_onua3d_identity.py")), "--", str(job)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return target.read_bytes()

    def _roundtrip(self, scene, manifest, label):
        source = self.root / (label + ".glb"); source.write_bytes(scene)
        base = canonical_vertex_positions(manifest, identity_fixtures.decoded_primitives(scene))
        triangles, _, _ = triangles_and_copies(scene)
        noop = self._blender(source, self.root / (label + "_noop.glb"), [])
        self.assertEqual(canonical_vertex_positions(manifest, identity_fixtures.decoded_primitives(noop)), base)
        actual_triangles, copies, normals = triangles_and_copies(noop)
        self.assertEqual(actual_triangles, triangles)
        split_ids = sorted(i for i, n in copies.items() if n > 1)
        self.assertTrue(split_ids, "fixture must exercise actual Blender vertex splits")
        self.assertTrue(any(len(normals[i]) > 1 for i in split_ids), "normal splits were not exercised")
        move_ids = split_ids[:2]
        moved = self._blender(source, self.root / (label + "_move.glb"), move_ids)
        expected = dict(base)
        for i in move_ids:
            x, y, z = base[i]; expected[i] = (x + 0.25, y, z)
        self.assertEqual(canonical_vertex_positions(manifest, identity_fixtures.decoded_primitives(moved)), expected)
        self.assertEqual(triangles_and_copies(moved)[0], triangles)
        reordered = reorder_scene(moved)
        self.assertEqual(canonical_vertex_positions(manifest, identity_fixtures.decoded_primitives(reordered)), expected)
        self.assertEqual(triangles_and_copies(reordered)[0], triangles)
        # The IDs still decode against the manifest, despite unchanged OLD numeric node mappings.
        print(f"{label}: {len(base)} logical IDs, {sum(copies.values())} Blender instances, "
              f"{len(split_ids)} split IDs; moved {move_ids}; no-op/move/reorder PASS")

    def test_synthetic_normal_uv_material_splits_and_reorder(self):
        obj = self.family.root_object
        obj.skeleton.points = [(0, 0, 0), (1, 0, 0), (1, 1, 0.25), (0, 1, 0)]
        obj.skeleton.polygons = [[0, 1, 2, 3], [0, 2, 3]]
        obj.base_object.ades[0].olpl[0] = [(0, 0), (255, 0), (255, 255), (0, 255)]
        block = copy.deepcopy(obj.base_object.ades[0])
        block.atts[0].poly_id = 1; block.olpl[0] = [(32, 64), (128, 64), (64, 128)]
        obj.base_object.ades.append(block)
        rebuild_materials(obj, self.family)
        kid = copy.deepcopy(obj); kid.owner_path = "root/kid[0]"; obj.kids.append(kid)
        scene, manifest = identity_fixtures.scene_identity(self.family)
        self.assertEqual(len(vertex_identity_mapping(manifest)), 14)
        self._roundtrip(scene, manifest, "synthetic")

    def test_retail_hubi2_legacy_regeneration_preserves_embedded_family(self):
        source = Path(os.environ.get("ONUA3D_HUBI2_PACKAGE", ""))
        if not source.is_file(): self.skipTest("set ONUA3D_HUBI2_PACKAGE")
        before = source.read_bytes()
        with zipfile.ZipFile(source) as old:
            old.extractall(self.root / "legacy")
            package = self.root / "updated.onua3d"
            write_onua3d(self.root / "legacy" / "ua_family", package)
            with zipfile.ZipFile(package) as new:
                for name in old.namelist():
                    if name.startswith("ua_family/"): self.assertEqual(old.read(name), new.read(name))
                self._roundtrip(new.read("scene.glb"), json.loads(new.read(MANIFEST)), "VP_HUBI2")
        self.assertEqual(source.read_bytes(), before)

    def test_retail_hubi4_ui_export(self):
        source = Path(os.environ.get("ONUA3D_RETAIL_ARCHIVE", ""))
        if not source.is_file(): self.skipTest("set ONUA3D_RETAIL_ARCHIVE")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        window = AssemblyWindow()
        try:
            window.open_setbas(str(source))
            self.assertIsNotNone(window._activate_setbas_base("VP_HUBI4.base"))
            target = self.root / "VP_HUBI4.onua3d"
            with patch("assembly_window.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                    patch("assembly_window.QMessageBox.critical") as errors:
                window._export_to_blender(); errors.assert_not_called()
            with zipfile.ZipFile(target) as archive:
                self._roundtrip(archive.read("scene.glb"), json.loads(archive.read(MANIFEST)), "VP_HUBI4")
        finally:
            window.close(); window.deleteLater()
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
