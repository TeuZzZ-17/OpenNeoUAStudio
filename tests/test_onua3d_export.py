from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from anm_parser import VanmData, VanmFrame
from assembly_viewer import _apply, _rotation_matrix
from assembly_window import AssemblyWindow
from asset_family import load_asset_family, rebuild_materials
from asset_family_package import (AssetFamilyPackageError, validate_family_package,
                                  MANIFEST_NAME, PackageEntry, write_package_manifest)
from base_parser import BaseTransform
from onua3d_export import MANIFEST, build_scene, convert_point, write_onua3d
from tests.test_asset_family_package import _make_package, _base_bytes, _chunk, _form


def _textured_base_bytes():
    # The older package fixture uses 0x08 (Retail NNN/black). A visual export
    # fixture needs the actual LINEAR texture dispatch 0x0a, plus nonzero scale.
    data = bytearray(_base_bytes().replace(
        struct.pack(">hHHBBBB", 1, 0, 0x08, 0, 255, 0, 0),
        struct.pack(">hHHBBBB", 1, 0, 0x0a, 0, 255, 0, 0)))
    from base_parser import parse_base_bytes
    parsed = parse_base_bytes(bytes(data))
    def set_scale(chunk):
        if chunk.form_type == "BASE":
            strc = next(c for c in chunk.children if c.tag == "STRC")
            struct.pack_into(">fff", data, strc.payload_offset + 26, 1, 1, 1)
        for child in chunk.children:
            set_scale(child)
    for root in parsed.tree.roots:
        set_scale(root)
    return bytes(data)


def decode_glb(data):
    magic, version, size = struct.unpack_from("<4sII", data)
    assert (magic, version, size) == (b"glTF", 2, len(data))
    length, tag = struct.unpack_from("<I4s", data, 12)
    assert tag == b"JSON"
    doc = json.loads(data[20:20 + length])
    binary_length, binary_tag = struct.unpack_from("<I4s", data, 20 + length)
    assert binary_tag == b"BIN\0"
    binary = data[28 + length:]
    assert len(binary) == binary_length
    return doc, binary


def values(doc, binary, index):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[accessor["type"]]
    fmt = {5125: "I", 5126: "f"}[accessor["componentType"]]
    flat = struct.unpack_from("<" + fmt * accessor["count"] * width,
                              binary, view.get("byteOffset", 0))
    return list(flat) if width == 1 else [tuple(flat[i:i + width])
                                         for i in range(0, len(flat), width)]


def source_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class Onua3dTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.family = _make_package(self.source)
        (self.source / "TEST.BASE").write_bytes(_textured_base_bytes())
        self.family = load_asset_family(self.source / "TEST.BASE", isolated_root=self.source)
        entries = json.loads((self.source / MANIFEST_NAME).read_text())["entries"]
        write_package_manifest(self.source, "TEST.BASE", self.family,
                               [PackageEntry(**row) for row in entries])

    def window(self):
        window = AssemblyWindow()
        window._set_family(self.family)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def export(self, window, target):
        with patch("assembly_window.QFileDialog.getSaveFileName",
                   return_value=(str(target), "")), patch.object(window, "_notify"), \
                patch("assembly_window.QMessageBox.critical") as error:
            window._export_to_blender()
            error.assert_not_called()
        self.assertTrue(target.is_file())

    def test_menu_root_export_deterministic_validated_and_read_only(self):
        window = self.window()
        self.assertIn(window.export_blender_action, window.file_export_menu.actions())
        self.assertEqual(window.export_blender_action.text(), "Export OpenNeoUA 3D...")
        self.assertTrue(window.export_blender_action.isEnabled())
        before = source_hashes(self.source)
        targets = [self.root / "a.onua3d", self.root / "b.onua3d"]
        for target in targets:
            self.export(window, target)
        self.assertEqual(targets[0].read_bytes(), targets[1].read_bytes())
        self.assertEqual(source_hashes(self.source), before)
        with zipfile.ZipFile(targets[0]) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            self.assertEqual((manifest["format"], manifest["version"]), ("OpenNeoUA ONUA3D", 1))
            for name, row in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), row["sha256"])
            extracted = self.root / "extracted"
            archive.extractall(extracted)
        validation = validate_family_package(extracted / "ua_family")
        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(manifest["semantic_sha256"], validation.manifest["semantic_sha256"])

    def test_geometry_uv_material_mapping_and_embedded_png(self):
        scene, pngs, mapping, _ = build_scene(self.family)
        doc, binary = decode_glb(scene)
        prim = doc["meshes"][0]["primitives"][0]
        self.assertEqual(values(doc, binary, prim["attributes"]["POSITION"]),
                         [(0, 0, 0), (1, 0, 0), (0, -1, 0)])
        self.assertEqual(values(doc, binary, prim["attributes"]["TEXCOORD_0"]),
                         [(0, 0), (255 / 256, 0), (0, 255 / 256)])
        self.assertEqual(values(doc, binary, prim["indices"]), [0, 1, 2])
        self.assertEqual(prim["material"], 0)
        self.assertEqual(mapping[0]["source_point_count"], 3)
        self.assertEqual(mapping[0]["source_polygon_count"], 1)
        self.assertEqual(mapping[0]["primitives"][0]["point_ids"], [0, 1, 2])
        self.assertEqual(mapping[0]["primitives"][0]["faces"][0]["poly_id"], 0)
        image = doc["images"][0]
        view = doc["bufferViews"][image["bufferView"]]
        png = binary[view["byteOffset"]:view["byteOffset"] + view["byteLength"]]
        self.assertIn(png, pngs.values())
        qimage = QImage.fromData(png, "PNG")
        self.assertEqual(qimage.pixelColor(0, 0).getRgb(), (8, 8, 8, 255))

    def test_recursive_kids_and_transform_match_studio_including_nonuniform_scale(self):
        root = self.family.root_object
        kid = copy.deepcopy(root)
        kid.owner_path = "root/kid[0]"
        grandkid = copy.deepcopy(kid)
        grandkid.owner_path += "/kid[0]"
        kid.kids.append(grandkid)
        root.kids.append(kid)
        root.base_object.transform = BaseTransform(position=(100, 200, 300))
        kid.base_object.transform = BaseTransform(position=(4, 5, 6), euler=(20, 30, 40), scale=(2, 3, 4))
        scene, _, mapping, _ = build_scene(self.family)
        doc, _ = decode_glb(scene)
        self.assertEqual([r["owner_path"] for r in mapping], ["root", "root/kid[0]", "root/kid[0]/kid[0]"])
        self.assertIn(mapping[1]["node"], doc["nodes"][mapping[0]["node"]]["children"])
        self.assertIn(mapping[2]["node"], doc["nodes"][mapping[1]["node"]]["children"])
        placement = doc["nodes"][mapping[1]["placement_node"]]
        matrix = doc["nodes"][mapping[1]["geometry_node"]]["matrix"]
        point = (1, 2, 3)
        converted = convert_point(point)
        actual = [placement["translation"][i] + placement["scale"][i] *
                  sum(matrix[j * 4 + i] * converted[j] for j in range(3)) for i in range(3)]
        transform = kid.base_object.transform
        expected = convert_point(_apply(_rotation_matrix(transform.euler, transform.scale), point, transform.position))
        for a, b in zip(actual, expected):
            self.assertAlmostEqual(a, b, places=6)

    def test_kids_survive_canonical_staging_when_child_selected(self):
        original = _textured_base_bytes()
        objt = original[12:]
        # Build through the same IFF fixture helpers; BASE wrapper size is
        # recalculated, and both nested BASEs retain their own source spans.
        from base_parser import parse_base_bytes
        parsed = parse_base_bytes(original)
        # Locate BASE via the existing IFF tree, not a second parser.
        def find_base(chunk):
            if chunk.form_type == "BASE":
                return chunk
            for child in chunk.children:
                found = find_base(child)
                if found is not None:
                    return found
            return None
        base = next(find_base(c) for c in parsed.tree.roots if find_base(c) is not None)
        payload = original[base.payload_offset + 4:base.payload_offset + base.size]
        changed = _form(b"MC2 ", _form(b"OBJT", _chunk(b"CLID", b"base.class\0") +
                        _form(b"BASE", payload + _form(b"KIDS", objt))))
        (self.source / "TEST.BASE").write_bytes(changed)
        self.family = load_asset_family(self.source / "TEST.BASE", isolated_root=self.source)
        self.assertEqual(len(self.family.root_object.kids), 1)
        window = self.window()
        window._selected_owner = "root/kid[0]"
        target = self.root / "kids.onua3d"
        self.export(window, target)
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read(MANIFEST))
            self.assertEqual([r["owner_path"] for r in manifest["nodes"]], ["root", "root/kid[0]"])

    def test_material_seams_keep_source_identity_and_polygons(self):
        obj = self.family.root_object
        obj.skeleton.points.append((1, 1, 0))
        obj.skeleton.polygons.append([1, 3, 2])
        block = copy.deepcopy(obj.base_object.ades[0])
        block.atts[0].poly_id = 1
        obj.base_object.ades.append(block)
        rebuild_materials(obj, self.family)
        scene, _, mapping, _ = build_scene(self.family)
        doc, _ = decode_glb(scene)
        self.assertEqual(len(doc["materials"]), 2)
        self.assertEqual([p["material"] for p in doc["meshes"][0]["primitives"]], [0, 1])
        self.assertEqual([p["block_index"] for p in mapping[0]["primitives"]], [0, 1])
        self.assertEqual(mapping[0]["source_point_count"], 4)

    def test_structural_base_container_with_only_kids_can_export(self):
        child = _textured_base_bytes()[12:]
        container = _form(b"MC2 ", _form(b"OBJT", _chunk(b"CLID", b"base.class\0") +
                          _form(b"BASE", _form(b"ROOT", _chunk(b"NAME", b"CONTAINER\0")) +
                                _form(b"KIDS", child))))
        (self.source / "TEST.BASE").write_bytes(container)
        self.family = load_asset_family(self.source / "TEST.BASE", isolated_root=self.source)
        window = self.window()
        target = self.root / "container.onua3d"
        self.export(window, target)
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read(MANIFEST))
        self.assertEqual([r["source_point_count"] for r in manifest["nodes"]], [0, 3])

    def test_short_static_uv_is_rejected(self):
        self.family.root_object.materials[0].faces[0][1].pop()
        with self.assertRaisesRegex(AssetFamilyPackageError, "2 UVs for 3 corners"):
            build_scene(self.family)

    def test_duplicate_casefold_texture_is_not_chosen_arbitrarily(self):
        self.family.textures["texture:static.ilb"] = copy.deepcopy(
            self.family.textures["Texture:STATIC.ILB"])
        with self.assertRaisesRegex(AssetFamilyPackageError, "ambiguous"):
            build_scene(self.family)

    def test_clear_tracy_uses_numeric_zero_and_resolved_set_palette(self):
        image = self.family.textures["Texture:STATIC.ILB"]
        image.width, image.height, image.pixels = 2, 1, b"\x00\x08"
        image.palette = [(255, 0, 255)] * 256  # Retail uses the SET profile.
        self.family.root_object.materials[0].block.polflags |= 0x40
        scene, pngs, _, _ = build_scene(self.family)
        doc, _ = decode_glb(scene)
        image = QImage.fromData(next(iter(pngs.values())), "PNG")
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(image.pixelColor(1, 0).getRgb(), (8, 8, 8, 255))
        self.assertEqual(doc["materials"][0]["alphaMode"], "MASK")

    def test_animation_uses_frame_bitmap_and_uv_not_skeletal_animation(self):
        obj = self.family.root_object
        group = obj.materials[0]
        group.kind, group.texture_name = "bmpanim", "TEST.ANM"
        self.family.animations["TEST.ANM"] = VanmData(
            bitmap_names=["Texture:STATIC.ILB"], texcoord_groups=[[(10, 20), (30, 40), (50, 60)]],
            frames=[VanmFrame(100, 0, 0)])
        scene, _, _, warnings = build_scene(self.family)
        doc, binary = decode_glb(scene)
        self.assertIn("animations", doc)
        self.assertTrue(all(c["target"]["path"] == "scale"
                            for c in doc["animations"][0]["channels"]))
        self.assertNotIn("skins", doc)
        prim = doc["meshes"][0]["primitives"][0]
        self.assertEqual(values(doc, binary, prim["attributes"]["TEXCOORD_0"]),
                         [(10/256, 20/256), (30/256, 40/256), (50/256, 60/256)])
        self.assertEqual(doc["animations"][0]["samplers"][0]["interpolation"], "STEP")

    def test_export_does_not_mark_edits_saved(self):
        window = self.window()
        obj = self.family.root_object
        obj.skeleton.points[0] = (0.123456789, 0.3, 0.5)
        window._geom_dirty["root"] = obj
        before = copy.deepcopy(window._bundle_base_snapshots)
        self.export(window, self.root / "edited.onua3d")
        self.assertIs(window._geom_dirty["root"], obj)
        self.assertEqual(window._bundle_base_snapshots, before)

    def test_missing_texture_and_invalid_uv_fail_without_replacing_destination(self):
        target = self.root / "old.onua3d"
        target.write_bytes(b"previous export")
        self.family.textures.clear()
        with self.assertRaises(AssetFamilyPackageError):
            build_scene(self.family)
        (self.source / "Texture" / "STATIC.ILB").unlink()
        with self.assertRaises(AssetFamilyPackageError):
            write_onua3d(self.source, target)
        self.assertEqual(target.read_bytes(), b"previous export")

    def test_encoding_failure_preserves_existing_output_and_editor_state(self):
        window = self.window()
        target = self.root / "old.onua3d"
        target.write_bytes(b"previous export")
        with patch("assembly_window.QFileDialog.getSaveFileName", return_value=(str(target), "")), \
                patch("onua3d_export.build_scene", side_effect=AssetFamilyPackageError("invalid UV")), \
                patch("assembly_window.QMessageBox.critical") as error:
            window._export_to_blender()
        error.assert_called_once()
        self.assertEqual(target.read_bytes(), b"previous export")


if __name__ == "__main__":
    unittest.main()
