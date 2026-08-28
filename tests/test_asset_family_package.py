from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from assembly_window import AssemblyWindow
from asset_family import load_asset_family
from asset_family_package import (
    MANIFEST_NAME,
    AssetFamilyPackageError,
    PackageEntry,
    package_relative_path,
    validate_family_package,
    write_package_manifest,
)
from sklt_parser import (
    create_minimal_sklt_model,
    save_sklt_with_poo2_pol2_structure,
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    encoded = tag + struct.pack(">I", len(payload)) + payload
    return encoded + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes) -> bytes:
    return _chunk(b"FORM", form_type + children)


def _base_bytes() -> bytes:
    atts = struct.pack(">hBBBB", 0, 1, 2, 3, 0)
    olpl = struct.pack(">hBBBBBB", 3, 0, 0, 255, 0, 0, 255)
    texture = _form(
        b"OBJT",
        _chunk(b"CLID", b"ilbm.class\0")
        + _form(
            b"CIBO",
            _chunk(b"NAM2", b"Texture:STATIC.ILB\0")
            + _chunk(b"OTL2", bytes((0, 0, 255, 0, 0, 255)))))
    area = _form(
        b"AREA",
        _chunk(
            b"STRC",
            struct.pack(">hHHBBBB", 1, 0, 0x08, 0, 255, 0, 0))
        + texture)
    amesh = _form(
        b"AMSH", area + _chunk(b"ATTS", atts) + _chunk(b"OLPL", olpl))
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"Skeleton:TEST.SKLT\0")))
    transform = bytearray(62)
    struct.pack_into(">ii", transform, 54, 1400, 255)
    base = _form(
        b"BASE",
        _form(b"ROOT", _chunk(b"NAME", b"TEST\0"))
        + _chunk(b"STRC", bytes(transform)) + skeleton
        + _form(b"ADES", material))
    return _form(
        b"MC2 ", _form(
            b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


def _vbmp(width: int, height: int, pixels: bytes) -> bytes:
    return _form(
        b"VBMP",
        _chunk(b"HEAD", struct.pack(">HHH", width, height, 0))
        + _chunk(b"BODY", pixels))


def _palette_bytes() -> bytes:
    cmap = bytes(channel for index in range(256)
                 for channel in (index, index, index))
    return _form(b"ILBM", _chunk(b"CMAP", cmap))


def _shader_bytes() -> bytes:
    data = bytearray(bytes(range(256)) * 256)
    for shade in range(256):
        data[shade * 256] = 0
    data[6] = 0
    data[255 * 256:] = bytes(256)
    return _vbmp(256, 256, bytes(data))


def _tracy_bytes() -> bytes:
    data = bytearray()
    for background in range(256):
        data.extend(bytes((background,)) * 256)
    for source in (8, 10, 11, 12, 14, 15):
        for background in range(256):
            data[background * 256 + source] = source
    for background in range(256):
        data[background * 256] = 0 if background == 13 else background
    for source in range(256):
        data[13 * 256 + source] = source
    for (background, source), output in {
        (31, 220): 212,
        (220, 31): 200,
        (13, 255): 255,
        (255, 13): 177,
        (156, 185): 54,
        (159, 185): 106,
        (0, 156): 223,
        (0, 159): 255,
        (223, 159): 207,
        (255, 156): 191,
    }.items():
        data[background * 256 + source] = output
    return _vbmp(256, 256, bytes(data))


def _make_package(root: Path):
    (root / "Skeleton").mkdir(parents=True)
    (root / "Texture").mkdir(parents=True)
    (root / "PALETTE").mkdir(parents=True)
    (root / "REMAP").mkdir(parents=True)
    base = root / "TEST.BASE"
    base.write_bytes(_base_bytes())
    model = create_minimal_sklt_model()
    save_sklt_with_poo2_pol2_structure(
        model,
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
         (0.0, 1.0, 0.0)],
        [[0, 1, 2]], root / "Skeleton" / "TEST.SKLT")
    (root / "Texture" / "STATIC.ILB").write_bytes(
        _vbmp(1, 1, b"\x08"))
    (root / "PALETTE" / "STANDARD.PAL").write_bytes(_palette_bytes())
    (root / "REMAP" / "SHADERMP.ILB").write_bytes(_shader_bytes())
    (root / "REMAP" / "TRACYRMP.ILB").write_bytes(_tracy_bytes())
    family = load_asset_family(base, isolated_root=root)
    entries = [
        PackageEntry(
            "TEST.BASE", "base.class", "fixture", "TEST.BASE",
            "entry point"),
        PackageEntry(
            "Skeleton:TEST.SKLT", "sklt.class", "fixture",
            "Skeleton/TEST.SKLT", "OBJT sklt.class NAME"),
        PackageEntry(
            "Texture:STATIC.ILB", "ilbm.class", "fixture",
            "Texture/STATIC.ILB", "ADES texture"),
        PackageEntry(
            "SET palette", "palette.class", "fixture",
            "PALETTE/STANDARD.PAL", "Retail indexed palette"),
        PackageEntry(
            "SHADERMP", "shadermap.class", "fixture",
            "REMAP/SHADERMP.ILB", "Retail shade lookup"),
        PackageEntry(
            "TRACYRMP", "tracymap.class", "fixture",
            "REMAP/TRACYRMP.ILB", "Retail transparency lookup"),
    ]
    write_package_manifest(root, base, family, entries)
    return family


class CompleteAssetFamilyPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_logical_paths_preserve_case_aliases_and_reject_traversal(self):
        self.assertEqual(
            package_relative_path(
                "Skeleton:Mixed/Ship.SKL", default_name="MODEL.SKLT",
                extensions=(".SKLT", ".SKL")),
            Path("Skeleton/Mixed/Ship.SKL"))
        with self.assertRaises(AssetFamilyPackageError):
            package_relative_path(
                "../escape.SKLT", default_name="MODEL.SKLT",
                extensions=(".SKLT", ".SKL"))

    def test_manifest_hashes_and_isolated_semantic_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            _make_package(root)

            validation = validate_family_package(root)

            self.assertTrue(validation.valid, validation.errors)
            self.assertIsNotNone(validation.family.root_object.skeleton)
            self.assertEqual(
                validation.family.root_object.base_object.transform.vis_limit,
                1400)
            manifest = json.loads(
                (root / MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertTrue(manifest["semantic_sha256"])
            self.assertEqual(len(manifest["entries"]), 6)
            self.assertTrue(all(
                entry["sha256"] for entry in manifest["entries"]))

    def test_tamper_and_sibling_dependency_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "package"
            root.mkdir()
            _make_package(root)
            # A sibling with the same basename must never rescue a missing
            # package-local dependency during isolated validation.
            sibling = parent / "poison"
            sibling.mkdir()
            (sibling / "TEST.SKLT").write_bytes(b"not a skeleton")
            (root / "Skeleton" / "TEST.SKLT").unlink()

            validation = validate_family_package(root)

            self.assertFalse(validation.valid)
            self.assertTrue(any(
                "missing exported file" in error
                for error in validation.errors))

    def test_manifest_must_hash_and_identify_the_entry_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            _make_package(root)
            manifest_path = root / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["entries"] = [
                item for item in manifest["entries"]
                if item["asset_class"] != "base.class"]
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

            validation = validate_family_package(root)

            self.assertFalse(validation.valid)
            self.assertTrue(any(
                "exactly one exported base.class" in error
                for error in validation.errors))

    def test_window_export_builds_the_validated_portable_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / "source"
            source.mkdir()
            family = _make_package(source)
            source_bytes = (source / "TEST.BASE").read_bytes()
            output = parent / "exported"
            window = AssemblyWindow()
            try:
                window._set_family(family)
                with patch.object(window, "_notify"), \
                        patch("assembly_window.QMessageBox.critical") as critical:
                    exported = window._write_model_files(
                        "root", family, family.root_object,
                        output / "Skeleton" / "TEST.SKLT",
                        output / "TEST.BASE", ask_replace=False)
                self.assertTrue(exported)
                critical.assert_not_called()
                validation = validate_family_package(output)
                self.assertTrue(validation.valid, validation.errors)
                self.assertEqual(
                    (source / "TEST.BASE").read_bytes(), source_bytes)
            finally:
                window.close()

    def test_window_export_normalizes_edited_geometry_to_sklt_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / "source"
            source.mkdir()
            family = _make_package(source)
            obj = family.root_object
            obj.skeleton.points[0] = (
                0.123456789, -0.987654321, 0.333333333)
            output = parent / "edited"
            window = AssemblyWindow()
            try:
                window._set_family(family)
                with patch.object(window, "_notify"), \
                        patch("assembly_window.QMessageBox.critical") as critical:
                    exported = window._write_model_files(
                        "root", family, obj,
                        output / "Skeleton" / "TEST.SKLT",
                        output / "TEST.BASE", ask_replace=False)
                self.assertTrue(exported)
                critical.assert_not_called()
                validation = validate_family_package(output)
                self.assertTrue(validation.valid, validation.errors)
                self.assertAlmostEqual(
                    validation.family.root_object.skeleton.points[0][0],
                    0.123456789, places=6)
            finally:
                window.close()
                window.deleteLater()

    def test_window_export_final_reload_failure_restores_previous_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / "source"
            source.mkdir()
            family = _make_package(source)
            output = parent / "exported"
            output.mkdir()
            previous_base = output / "TEST.BASE"
            previous_base.write_bytes(b"previous user BASE")
            real_validate = validate_family_package

            def fail_destination(root, **kwargs):
                validation = real_validate(root, **kwargs)
                if Path(root).resolve() == output.resolve():
                    validation.valid = False
                    validation.errors.append(
                        "simulated final destination failure")
                return validation

            window = AssemblyWindow()
            try:
                window._set_family(family)
                with patch.object(window, "_notify"), patch(
                        "assembly_window.QMessageBox.critical") as critical, \
                        patch(
                            "assembly_window.validate_family_package",
                            side_effect=fail_destination):
                    exported = window._write_model_files(
                        "root", family, family.root_object,
                        output / "Skeleton" / "TEST.SKLT",
                        previous_base, ask_replace=False)
                self.assertFalse(exported)
                critical.assert_called_once()
                self.assertEqual(
                    previous_base.read_bytes(), b"previous user BASE")
                self.assertEqual(
                    sorted(path.relative_to(output).as_posix()
                           for path in output.rglob("*") if path.is_file()),
                    ["TEST.BASE"])
            finally:
                window.close()
                window.deleteLater()


if __name__ == "__main__":
    unittest.main()
