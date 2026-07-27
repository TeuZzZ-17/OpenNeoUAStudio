import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asset_family import AssetFamily, FamilyObject, _load_family_object
from asset_resolver import AssetResolver, ResolvedFile
from base_dependency_resolver import collect_dependencies
from base_parser import parse_base_bytes


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _area_stage(texture_name, animated):
    if animated:
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"bmpanim.class\0")
            + _form(
                b"BANI",
                _chunk(
                    b"STRC",
                    struct.pack(">hhh", 1, 6, 1)
                    + texture_name.encode("ascii") + b"\0")))
    else:
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"ilbm.class\0")
            + _form(
                b"CIBO",
                _chunk(b"NAM2", texture_name.encode("ascii") + b"\0")
                + _chunk(b"OTL2", bytes((0, 0, 255, 0, 255, 255, 0, 255)))))
    area = _form(
        b"AREA",
        _form(
            b"ADE ",
            _chunk(b"STRC", struct.pack(">hbbhhh", 1, 0, 0, -1, -1, 0)))
        + _chunk(
            b"STRC",
            struct.pack(">hHHBBBB", 1, 0, 0x88, 0, 255, 0, 255))
        + texture)
    return _form(
        b"OBJT", _chunk(b"CLID", b"area.class\0") + area)


def _particle_base_bytes():
    particle_atts = struct.pack(
        ">h12f11i",
        1,
        *([0.0] * 12),
        0, 50, 1, 1000, 0, 1000, 10, 3000, 30, 30, 0)
    particle = _form(
        b"OBJT",
        _chunk(b"CLID", b"particle.class\0")
        + _form(
            b"PTCL",
            _form(
                b"ADE ",
                _chunk(
                    b"STRC",
                    struct.pack(">hbbhhh", 1, 0, 0, -1, -1, 0)))
            + _chunk(b"ATTS", particle_atts)
            + _area_stage("SMOKE.ANM", True)
            + _area_stage("SPARK.ILBM", False)))
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62))
        + _form(b"ADES", particle))
    return _form(
        b"MC2 ",
        _form(
            b"OBJT",
            _chunk(b"CLID", b"base.class\0") + base))


class ParticleDependencyTests(unittest.TestCase):
    def setUp(self):
        self.asset = parse_base_bytes(_particle_base_bytes(), "particle.base")
        self.block = self.asset.root.ades[0]

    def test_particle_attributes_and_nested_life_stages_are_typed(self):
        self.assertEqual(self.block.class_id, "particle.class")
        self.assertEqual(self.block.particle_attributes.version, 1)
        self.assertEqual(self.block.particle_attributes.start_speed, 50)
        self.assertEqual(len(self.block.particle_stages), 2)
        self.assertEqual(
            [stage.texture.name for stage in self.block.particle_stages],
            ["SMOKE.ANM", "SPARK.ILBM"])
        self.assertEqual(
            self.asset.referenced_animations(), ["SMOKE.ANM"])
        self.assertEqual(
            self.asset.referenced_textures(),
            ["SMOKE.ANM", "SPARK.ILBM"])

    def test_nested_resources_are_loaded_and_reported_as_real_dependencies(self):
        family = AssetFamily()
        resolver = AssetResolver([])
        with patch("asset_family._load_animation") as load_animation, \
                patch("asset_family._load_texture") as load_texture:
            _load_family_object(self.asset.root, family, resolver)
        load_animation.assert_called_once_with(
            family, resolver, "SMOKE.ANM")
        load_texture.assert_called_once_with(
            family, resolver, "SPARK.ILBM")

        obj = FamilyObject(
            base_object=self.asset.root, owner_path="root")
        family.root_object = obj
        family.animation_refs["SMOKE.ANM"] = ResolvedFile(
            "SMOKE.ANM", status="found", path=Path("SMOKE.ANM"))
        family.texture_refs["SPARK.ILBM"] = ResolvedFile(
            "SPARK.ILBM", status="found", path=Path("SPARK.ILBM"))
        family.animations["SMOKE.ANM"] = SimpleNamespace(bitmap_names=[])
        family.textures["SPARK.ILBM"] = object()
        dependencies = collect_dependencies(family)
        self.assertEqual(
            [(dep.kind, dep.raw_ref) for dep in dependencies],
            [("animation", "SMOKE.ANM"), ("texture", "SPARK.ILBM")])
        self.assertTrue(all(
            "PTCL stage #" in dep.source for dep in dependencies))
        self.assertNotIn(
            "particle", {dep.kind for dep in dependencies})
        self.assertNotIn(
            "unsupported_loader", {dep.status for dep in dependencies})


if __name__ == "__main__":
    unittest.main()
