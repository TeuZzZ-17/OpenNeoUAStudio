import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from base_parser import parse_base_file
from setbas_export import (
    SetBasExportError,
    export_runtime_loose,
    validate_runtime_loose_layout,
)
from setbas_reader import read_setbas


def _chunk(tag: bytes, payload: bytes) -> bytes:
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes = b"") -> bytes:
    return _chunk(b"FORM", form_type + children)


def _root_name(name: str) -> bytes:
    if not name:
        return b""
    return _form(b"ROOT", _chunk(b"NAME", name.encode("latin-1") + b"\0"))


def _base_object(name: str, *, kids: bytes = b"",
                 embedded: bytes = b"") -> bytes:
    base_children = _root_name(name)
    if embedded:
        embed_object = _form(
            b"OBJT",
            _chunk(b"CLID", b"embed.class\0")
            + _form(b"EMBD", embedded),
        )
        base_children += embed_object
    base_children += _chunk(b"STRC", bytes(62))
    if kids:
        base_children += _form(b"KIDS", kids)
    return _form(
        b"OBJT",
        _chunk(b"CLID", b"base.class\0")
        + _form(b"BASE", base_children),
    )


def _emrs(class_name: str, resource_name: str, payload: bytes) -> bytes:
    record = (class_name.encode("latin-1") + b"\0"
              + resource_name.encode("latin-1") + b"\0")
    return _chunk(b"EMRS", record) + payload


def _sklt(x: float = 0.0) -> bytes:
    return _form(
        b"SKLT",
        _chunk(b"POO2", struct.pack(">fff", x, 0.0, 0.0))
        + _chunk(b"POL2", b""),
    )


def _vbmp(pixel: int = 1) -> bytes:
    return _form(
        b"VBMP",
        _chunk(b"HEAD", struct.pack(">HHH", 1, 1, 0))
        + _chunk(b"BODY", bytes([pixel])),
    )


def _vanm() -> bytes:
    bitmap_class = b"ilbm.class\0"
    bitmap_names = b"MTL.ILBM\0"
    stream = (
        struct.pack(">h", len(bitmap_class)) + bitmap_class
        + struct.pack(">h", len(bitmap_names)) + bitmap_names
        + struct.pack(">hh", 0, 0)
    )
    return _form(b"VANM", _chunk(b"DATA", stream))


def _setbas_bytes(*, duplicate_sklt: bool = False,
                  invalid_duplicate_sklt: bool = False,
                  duplicate_base: bool = False) -> bytes:
    embedded = (
        _emrs("sklt.class", "Skeleton/HERO.sklt", _sklt())
        + _emrs("ilbm.class", "MTL.ILBM", _vbmp())
        + _emrs("bmpanim.class", "FLASH.ANM", _vanm())
        + _emrs("sound.class", "BOOM.SND", _form(b"SND "))
    )
    if duplicate_sklt:
        embedded += _emrs(
            "sklt.class", "skeleton/hero.SKLT", _sklt(4.0))
    if invalid_duplicate_sklt:
        embedded += _emrs(
            "sklt.class", "SKELETON/HERO.sklt", _form(b"BAD "))
    kids = _base_object("VP_HERO")
    if duplicate_base:
        kids += _base_object("vp_hero")
    return _form(
        b"MC2 ", _base_object("", kids=kids, embedded=embedded))


class RuntimeLooseExportTests(unittest.TestCase):
    def _fixture(self, root: Path, **kwargs):
        set_root = root / "Data" / "Set1"
        objects = set_root / "Objects"
        objects.mkdir(parents=True)
        setbas = objects / "SET.BAS"
        setbas.write_bytes(_setbas_bytes(**kwargs))
        return set_root, setbas, read_setbas(setbas)

    def test_exports_only_runtime_layout_and_never_changes_setbas(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, setbas, archive = self._fixture(Path(tmp))
            source_before = setbas.read_bytes()

            summary = export_runtime_loose(archive, set_root)
            loose = set_root / "Loose"

            self.assertTrue(summary["validation"]["valid"])
            self.assertEqual(summary["exported"], 4)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(setbas.read_bytes(), source_before)
            self.assertEqual(
                (loose / "SKLT" / "HERO.sklt").read_bytes(), _sklt())
            self.assertEqual(
                (loose / "ILBM" / "MTL.ILBM").read_bytes(), _vbmp())
            self.assertEqual((loose / "ANM" / "FLASH.ANM").read_bytes(), _vanm())
            self.assertFalse((loose / "raw").exists())
            self.assertFalse((loose / "textures_ilbm").exists())
            self.assertEqual(
                {path.name for path in loose.iterdir() if path.is_dir()},
                {"ILBM", "ANM", "SKLT", "BASE"})

            base_path = loose / "BASE" / "VP_HERO.BASE"
            self.assertEqual(parse_base_file(base_path).root.name, "VP_HERO")
            manifest = summary["manifest"]
            self.assertEqual(manifest["source"]["name"], "SET.BAS")
            self.assertEqual(manifest["layout"]["logical_root"],
                             "Data/Set1/Loose")
            for row in manifest["resources"]:
                self.assertTrue({
                    "source_name", "class", "logical_name", "output_path",
                    "hash", "status", "reason",
                }.issubset(row))
            unsupported = next(
                row for row in manifest["resources"]
                if row["source_name"] == "BOOM.SND")
            self.assertEqual(unsupported["status"], "skipped_unsupported")
            self.assertIn("SET.BAS fallback", unsupported["reason"])

    def test_dry_run_writes_nothing_and_skips_ambiguous_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, _setbas, archive = self._fixture(
                Path(tmp), duplicate_sklt=True, duplicate_base=True)

            summary = export_runtime_loose(
                archive, set_root / "Loose", dry_run=True)

            self.assertFalse((set_root / "Loose").exists())
            self.assertTrue(summary["validation"]["valid"])
            self.assertGreater(summary["planned"], 0)
            rows = summary["manifest"]["resources"]
            skeleton_rows = [
                row for row in rows
                if row["class"] == "sklt.class"]
            base_rows = [
                row for row in rows
                if row["class"] == "base.class"
                and row["logical_name"].casefold() == "vp_hero"]
            self.assertEqual(len(skeleton_rows), 2)
            self.assertEqual(len(base_rows), 2)
            self.assertTrue(all(
                row["status"] == "skipped_ambiguous"
                for row in skeleton_rows + base_rows))
            self.assertTrue(all(
                "no candidate selected" in row["reason"]
                for row in skeleton_rows + base_rows))

    def test_existing_file_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, _setbas, archive = self._fixture(Path(tmp))
            target = set_root / "Loose" / "SKLT" / "HERO.sklt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"user data")

            protected = export_runtime_loose(archive, set_root)
            self.assertEqual(target.read_bytes(), b"user data")
            self.assertFalse(protected["validation"]["valid"])
            row = next(
                row for row in protected["manifest"]["resources"]
                if row["source_name"] == "Skeleton/HERO.sklt")
            self.assertEqual(row["status"], "skipped_conflict")

            replaced = export_runtime_loose(
                archive, set_root, overwrite=True)
            self.assertEqual(target.read_bytes(), _sklt())
            self.assertTrue(replaced["validation"]["valid"])

    def test_invalid_duplicate_also_blocks_the_valid_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, _setbas, archive = self._fixture(
                Path(tmp), invalid_duplicate_sklt=True)

            summary = export_runtime_loose(archive, set_root)

            rows = [
                row for row in summary["manifest"]["resources"]
                if row["class"] == "sklt.class"]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(
                row["status"] == "skipped_ambiguous" for row in rows))
            self.assertFalse(
                (set_root / "Loose" / "SKLT" / "HERO.sklt").exists())
            self.assertTrue(summary["validation"]["valid"])

    def test_higher_priority_png_is_reported_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, _setbas, archive = self._fixture(Path(tmp))
            png = set_root / "Loose" / "ILBM" / "MTL.PNG"
            png.parent.mkdir(parents=True)
            png.write_bytes(b"existing png")

            summary = export_runtime_loose(
                archive, set_root, overwrite=True)

            self.assertEqual(png.read_bytes(), b"existing png")
            self.assertFalse((png.parent / "MTL.ILBM").exists())
            self.assertFalse(summary["validation"]["valid"])
            row = next(
                row for row in summary["manifest"]["resources"]
                if row["source_name"] == "MTL.ILBM")
            self.assertEqual(row["status"], "skipped_conflict")
            self.assertIn("higher-priority runtime PNG", row["reason"])

    def test_validation_detects_a_tampered_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, _setbas, archive = self._fixture(Path(tmp))
            summary = export_runtime_loose(archive, set_root)
            loose = set_root / "Loose"
            (loose / "ANM" / "FLASH.ANM").write_bytes(b"tampered")

            validation = validate_runtime_loose_layout(
                loose, summary["manifest"])

            self.assertFalse(validation["valid"])
            self.assertTrue(any(
                "SHA-256 mismatch" in issue
                for issue in validation["issues"]))

    def test_coordinated_verification_failure_restores_previous_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_root, setbas, archive = self._fixture(Path(tmp))
            loose = set_root / "Loose"
            target = loose / "SKLT" / "HERO.sklt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"user skeleton")
            source_before = setbas.read_bytes()

            with patch(
                    "setbas_export._resolver_check",
                    return_value="simulated final resolver failure"):
                with self.assertRaises(SetBasExportError):
                    export_runtime_loose(
                        archive, set_root, overwrite=True)

            self.assertEqual(target.read_bytes(), b"user skeleton")
            self.assertFalse((loose / "runtime_loose_manifest.json").exists())
            self.assertFalse((loose / "ILBM" / "MTL.ILBM").exists())
            self.assertFalse((loose / "ANM" / "FLASH.ANM").exists())
            self.assertFalse((loose / "BASE" / "VP_HERO.BASE").exists())
            self.assertEqual(setbas.read_bytes(), source_before)

    def test_cross_set_target_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            _set_root, _setbas, archive = self._fixture(Path(tmp))
            wrong_target = Path(tmp) / "Data" / "Set2"

            with self.assertRaises(SetBasExportError):
                export_runtime_loose(archive, wrong_target)
            self.assertFalse((wrong_target / "Loose").exists())


if __name__ == "__main__":
    unittest.main()
