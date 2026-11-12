"""Guided model import: requirement probing and the import dialog rows."""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from studio.assets.family import (
    collect_family_requirements,
    load_asset_family,
    probe_family_requirements,
)
from studio.exchange.portable import DirectoryIndex
from studio.formats.base_format import parse_base_file
from studio.formats.setbas import read_setbas
from studio.workbench.model_import import ModelImportDialog


def _chunk(tag: bytes, payload: bytes) -> bytes:
    encoded = tag + struct.pack(">I", len(payload)) + payload
    return encoded + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes = b"") -> bytes:
    return _chunk(b"FORM", form_type + children)


def _emrs(class_name: str, resource_name: str, payload: bytes) -> bytes:
    record = (class_name.encode("latin-1") + b"\0"
              + resource_name.encode("latin-1") + b"\0")
    return _chunk(b"EMRS", record) + payload


def _base_bytes(*, embedded: bytes = b"") -> bytes:
    transform = bytearray(62)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"Skeleton/HERO.SKLT\0")))
    texture = _form(
        b"OBJT",
        _chunk(b"CLID", b"ilbm.class\0")
        + _form(
            b"CIBO",
            _chunk(b"NAM2", b"MTL.ILBM\0")
            + _chunk(b"OTL2", bytes((0, 0, 255, 0, 0, 255)))))
    area = _form(
        b"AREA",
        _chunk(b"STRC", struct.pack(">hHHBBBB", 1, 0, 0x08, 0, 255, 0, 0))
        + texture)
    amesh = _form(
        b"OBJT",
        _chunk(b"CLID", b"amesh.class\0")
        + _form(
            b"AMSH",
            area
            + _chunk(b"ATTS", struct.pack(">hBBBB", 0, 1, 2, 3, 0))
            + _chunk(
                b"OLPL",
                struct.pack(">hBBBBBB", 3, 0, 0, 255, 0, 0, 255))))
    bani = _form(
        b"OBJT",
        _chunk(b"CLID", b"bmpanim.class\0")
        + _form(
            b"BANI",
            _chunk(
                b"STRC",
                struct.pack(">hhh", 1, 6, 0) + b"FLASH.ANM\0")))
    animation = _form(
        b"OBJT",
        _chunk(b"CLID", b"area.class\0")
        + _form(
            b"AREA",
            _chunk(
                b"STRC",
                struct.pack(">hHHBBBB", 1, 0, 0x08, 0, 255, 0, 0))
            + bani))
    base = _form(
        b"BASE",
        _form(b"ROOT", _chunk(b"NAME", b"TEST\0"))
        # The embedded-resources OBJT must come before STRC (base.cpp).
        + (_form(
            b"OBJT",
            _chunk(b"CLID", b"embed.class\0") + _form(b"EMBD", embedded))
           if embedded else b"")
        + _chunk(b"STRC", bytes(transform))
        + skeleton
        + _form(b"ADES", amesh + animation))
    return _form(
        b"MC2 ",
        _form(b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


def _sklt() -> bytes:
    return _form(
        b"SKLT",
        _chunk(b"POO2", struct.pack(">fff", 0.0, 0.0, 0.0))
        + _chunk(b"POL2", b""))


def _vbmp() -> bytes:
    return _form(
        b"VBMP",
        _chunk(b"HEAD", struct.pack(">HHH", 1, 1, 0))
        + _chunk(b"BODY", b"\x01"))


def _vanm() -> bytes:
    bitmap_class = b"ilbm.class\0"
    bitmap_names = b"MTL.ILBM\0"
    stream = (
        struct.pack(">h", len(bitmap_class)) + bitmap_class
        + struct.pack(">h", len(bitmap_names)) + bitmap_names
        + struct.pack(">hh", 0, 0)
    )
    return _form(b"VANM", _chunk(b"DATA", stream))


def _setbas_bytes() -> bytes:
    return _form(
        b"MC2 ",
        _emrs("sklt.class", "Skeleton/HERO.SKLT", _sklt())
        + _emrs("ilbm.class", "MTL.ILBM", _vbmp())
        + _emrs("bmpanim.class", "FLASH.ANM", _vanm()))


class ModelImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _fixture(self, root):
        base = root / "MODEL.BASE"
        base.write_bytes(_base_bytes())
        setbas_path = root / "SET.BAS"
        setbas_path.write_bytes(_setbas_bytes())
        return base, setbas_path, read_setbas(setbas_path)

    def test_deleted_loose_resources_fall_back_without_restarting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model"
            model.mkdir()
            base, _path, archive = self._fixture(model)
            installed = root / "set" / "PALETTE"
            installed.mkdir(parents=True)
            local = model / "PALETTE"
            local.mkdir()
            palette = _form(b"ILBM", _chunk(b"CMAP", bytes(range(256)) * 3))
            (installed / "STANDARD.PAL").write_bytes(palette)
            (local / "STANDARD.PAL").write_bytes(palette)
            texture = model / "MTL.ILBM"
            texture.write_bytes(_vbmp())
            first = load_asset_family(base, extra_roots=[installed.parent], setbas=archive)
            self.assertEqual(first.external_palette_path, local / "STANDARD.PAL")
            self.assertEqual(first.texture_refs["MTL.ILBM"].path, texture)
            texture.unlink()
            (local / "STANDARD.PAL").unlink()
            second = load_asset_family(base, extra_roots=[installed.parent], setbas=archive)
            self.assertEqual(second.texture_refs["MTL.ILBM"].status, "setbas")
            self.assertIn("MTL.ILBM", second.textures)
            self.assertEqual(second.external_palette_path, installed / "STANDARD.PAL")
            self.assertFalse(second.load_errors)

    def test_probe_includes_animation_frame_textures(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The probe also searches the parent folder: keep it private so no stray FX2.ILBM of the
            # shared temp folder can make the name ambiguous.
            root = Path(tmp) / "family"
            root.mkdir()
            base, _path, archive = self._fixture(root)
            (root / "FLASH.ANM").write_bytes(_vanm().replace(b"MTL.ILBM", b"FX2.ILBM"))
            _asset, probes = probe_family_requirements(base, setbas=archive)
            frames = [p for p in probes if p.requirement.logical_name == "FX2.ILBM"]
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].status, "missing")

    def test_probe_animation_frame_texture_uses_set_or_manual_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _path, archive = self._fixture(root)
            # Reuse the embedded MTL bitmap solely as an indirect VANM dependency.
            base.write_bytes(_base_bytes().replace(b"MTL.ILBM", b"DIR.ILBM"))
            _asset, probes = probe_family_requirements(base, setbas=archive)
            frames = [p for p in probes if p.requirement.logical_name == "MTL.ILBM"]
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0].status, "setbas")
            chosen = root / "CHOSEN.ILBM"
            chosen.write_bytes(_vbmp())
            _asset, probes = probe_family_requirements(
                base, setbas=archive, overrides={"MTL.ILBM": chosen})
            frame = next(p for p in probes if p.requirement.logical_name == "MTL.ILBM")
            self.assertEqual(frame.status, "manual")
            self.assertEqual(frame.path, chosen)

    def test_cached_index_excludes_deleted_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "Skeleton"
            nested.mkdir()
            skeleton = nested / "HERO.SKLT"
            skeleton.write_bytes(_sklt())
            index = DirectoryIndex.get(root)
            self.assertEqual(index.find_relative("Skeleton/HERO.SKLT"), [skeleton])
            skeleton.unlink()
            self.assertEqual(index.find_relative("Skeleton/HERO.SKLT"), [])
            self.assertEqual(index.find_name("HERO.SKLT"), [])

    def test_requirements_cover_skeleton_textures_and_animations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "MODEL.BASE"
            base.write_bytes(_base_bytes())
            asset = parse_base_file(base)
        requirements = collect_family_requirements(asset)
        self.assertEqual(
            [(item.kind, item.logical_name) for item in requirements],
            [("skeleton", "Skeleton/HERO.SKLT"),
             ("texture", "MTL.ILBM"),
             ("animation", "FLASH.ANM")])

    def test_probe_resolves_from_setbas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _path, archive = self._fixture(Path(tmp))
            _asset, probes = probe_family_requirements(
                base, setbas=archive)
        self.assertEqual(
            [probe.status for probe in probes],
            ["setbas", "setbas", "setbas"])
        self.assertEqual(
            [probe.path for probe in probes], [None, None, None])
        self.assertTrue(all(probe.embedded_names for probe in probes))

    def test_probe_prefers_loose_files_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _path, archive = self._fixture(root)
            (root / "Skeleton").mkdir()
            (root / "Skeleton" / "HERO.SKLT").write_bytes(_sklt())
            (root / "MTL.ILBM").write_bytes(_vbmp())
            _asset, probes = probe_family_requirements(
                base, setbas=archive)
            self.assertEqual(
                [probe.status for probe in probes],
                ["loose", "loose", "setbas"])
            self.assertEqual(probes[0].path, root / "Skeleton" / "HERO.SKLT")
            self.assertEqual(probes[1].path, root / "MTL.ILBM")

            (root / "MTL.ILBM").unlink()
            (root / "Skeleton" / "HERO.SKLT").unlink()
            # The resolver caches its directory scan per root.
            DirectoryIndex.clear_cache()
            _asset, probes = probe_family_requirements(base)
        self.assertEqual(
            [probe.status for probe in probes],
            ["missing", "missing", "missing"])

    def test_dialog_shows_setbas_rows_and_keeps_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, _path, archive = self._fixture(Path(tmp))
            dialog = ModelImportDialog(
                initial_base=base, initial_archive=archive)
            try:
                self.assertEqual(dialog.tree.topLevelItemCount(), 3)
                self.assertEqual(
                    [dialog.tree.topLevelItem(index).text(2)
                     for index in range(3)],
                    ["Ready (SET.BAS)"] * 3)
                selection = dialog.selection()
                self.assertIs(selection.archive, archive)
                self.assertEqual(selection.overrides, {})
            finally:
                dialog.deleteLater()

    def test_dialog_manual_choice_becomes_an_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _path, archive = self._fixture(root)
            dialog = ModelImportDialog(
                initial_base=base, initial_archive=archive)
            try:
                texture = root / "OTHER.PNG"
                texture.write_bytes(b"not decoded by the probe")
                dialog._set_file_choice(dialog._probes[1], texture)
                dialog._reprobe()
                self.assertEqual(
                    dialog.tree.topLevelItem(1).text(2), "Ready (chosen)")
                self.assertEqual(
                    dialog.selection().overrides,
                    {"mtl.ilbm": str(texture)})
            finally:
                dialog.deleteLater()

    def test_dialog_setbas_choice_keeps_loose_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _path, archive = self._fixture(root)
            (root / "Skeleton").mkdir()
            (root / "Skeleton" / "HERO.SKLT").write_bytes(_sklt())
            # Two same-named loose textures in the same priority root make the
            # reference ambiguous; the archive has a unique match for it.
            (root / "a").mkdir()
            (root / "a" / "MTL.ILBM").write_bytes(_vbmp())
            (root / "b").mkdir()
            (root / "b" / "MTL.ILBM").write_bytes(_vbmp())
            dialog = ModelImportDialog(
                initial_base=base, initial_archive=archive)
            try:
                self.assertEqual(
                    [dialog.tree.topLevelItem(index).text(2)
                     for index in range(3)],
                    ["Ready (loose)", "Ambiguous", "Ready (SET.BAS)"])
                dialog._choices["mtl.ilbm"] = "setbas"
                dialog._reprobe()
                self.assertEqual(
                    [dialog.tree.topLevelItem(index).text(2)
                     for index in range(3)],
                    ["Ready (loose)", "Ready (SET.BAS)", "Ready (SET.BAS)"])
                overrides = dialog.selection().overrides
                self.assertEqual(
                    {name: value for name, value in overrides.items()
                     if value.startswith("setbas:")},
                    {"mtl.ilbm": "setbas:mtl.ilbm"})
            finally:
                dialog.deleteLater()

    def test_probe_uses_the_base_own_embedded_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "MODEL.BASE"
            base.write_bytes(_base_bytes(
                embedded=_emrs("ilbm.class", "MTL.ILBM", _vbmp())))
            _asset, probes = probe_family_requirements(base)
        self.assertEqual(
            [probe.status for probe in probes],
            ["missing", "setbas", "missing"])
        self.assertEqual(probes[1].embedded_names, ("MODEL.BASE:MTL.ILBM",))


if __name__ == "__main__":
    unittest.main()
