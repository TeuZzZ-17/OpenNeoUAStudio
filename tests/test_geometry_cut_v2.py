import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_mapping_editor import MappingIndex
from base_parser import AttsEntry, parse_base_bytes
from geometry_editor import build_geometry_clipboard
from sklt_parser import (
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_poo2_pol2_structure,
)


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _base_bytes():
    atts = (
        struct.pack(">hBBBB", 0, 1, 2, 3, 4)
        + struct.pack(">hBBBB", 1, 5, 6, 7, 8))
    olpl = (
        struct.pack(">hBBBBBB", 3, 0, 0, 255, 0, 0, 255)
        + struct.pack(">hBBBBBB", 3, 1, 2, 3, 4, 5, 6))
    amesh = _form(b"AMSH", _chunk(b"ATTS", atts) + _chunk(b"OLPL", olpl))
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"CUT.SKLT\0")))
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62)) + skeleton
        + _form(b"ADES", material))
    return _form(
        b"MC2 ", _form(
            b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


class GeometryCutV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sklt_path = root / "CUT.SKLT"
        initial = create_minimal_sklt_model()
        save_sklt_with_poo2_pol2_structure(
            initial,
            [
                (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0), (2.0, 0.0, 0.0),
                (3.0, 0.0, 0.0), (2.0, 1.0, 0.0),
            ],
            [[0, 1, 2], [3, 4, 5]], sklt_path)
        self.model = parse_sklt_file(sklt_path)
        base_asset = parse_base_bytes(_base_bytes())
        base_object = base_asset.root
        self.block = base_object.ades[0]
        self.obj = FamilyObject(
            base_object=base_object,
            skeleton_ref=SimpleNamespace(
                path=sklt_path, status="found", source="loose"),
            skeleton=self.model,
            materials=[MaterialGroup(
                "material", block=self.block,
                faces=[
                    (0, list(self.block.olpl[0]), 2),
                    (1, list(self.block.olpl[1]), 6),
                ])],
            owner_path="root",
        )
        self.family = AssetFamily(
            base_path=root / "CUT.BASE",
            base_asset=base_asset,
            root_object=self.obj,
        )
        self.window = AssemblyWindow()
        self.window._family = self.family
        self.window._owner_to_obj = {"root": self.obj}
        self.window._selected_owner = "root"
        self.window._workbench_obj = self.obj
        self.window._mapping_index = MappingIndex(self.obj)
        self.window._selected_polys = {1}
        self.window._selected_poly = 1
        self.window.viewport.load_family(
            self.family, primary_owner="root")
        self.window.viewport.set_selected_owner("root")
        self.window.viewport.enter_edit_mode_with_vertices(
            "root", [3, 4, 5], pick_polygons=True)

    def tearDown(self):
        self.window.close()
        self.temp.cleanup()

    def test_cut_is_one_topology_command_and_clipboard_remains_pasteable(self):
        source_uvs = list(self.block.olpl[1])
        self.window._cut_geometry()
        clipboard = self.window._geometry_clipboard
        self.assertIsNotNone(clipboard)
        self.assertEqual(len(clipboard.polygons), 1)
        self.assertEqual(clipboard.polygons[0].uvs, tuple(source_uvs))
        self.assertEqual(len(self.model.polygons), 1)
        self.assertEqual(len(self.window._edit_undo_stack), 1)
        self.assertFalse(self.window.viewport.paste_preview_active)
        self.assertTrue(self.window.paste_geometry_action.isEnabled())

        self.window._paste_geometry()
        self.assertTrue(self.window.viewport.paste_preview_active)
        points_before_confirm = list(self.model.points)
        self.window._confirm_paste_geometry()
        self.assertFalse(self.window.viewport.paste_preview_active)
        self.assertEqual(len(self.model.polygons), 2)
        self.assertEqual(len(self.window._edit_undo_stack), 2)
        self.assertEqual(self.block.olpl[-1], source_uvs)
        self.assertNotEqual(self.model.points, points_before_confirm)

        self.window._undo_edit()
        self.assertEqual(len(self.model.polygons), 1)
        self.window._undo_edit()
        self.assertEqual(len(self.model.polygons), 2)
        self.window._redo_edit()
        self.assertEqual(len(self.model.polygons), 1)

    def test_failed_cut_keeps_previous_clipboard_and_geometry(self):
        previous = build_geometry_clipboard(
            self.obj, "root", {0}, MappingIndex(self.obj))
        self.window._geometry_clipboard = previous
        self.block.atts.append(AttsEntry(1, 9, 9, 9, 9))
        self.block.olpl.append([(0, 0), (1, 0), (0, 1)])
        before = (list(self.model.points),
                  [list(poly) for poly in self.model.polygons])
        self.window._cut_geometry()
        self.assertIs(self.window._geometry_clipboard, previous)
        self.assertEqual(self.model.points, before[0])
        self.assertEqual(self.model.polygons, before[1])
        self.assertEqual(self.window._edit_undo_stack, [])

    def test_delete_confirmation_cancel_undo_redo_and_reset(self):
        before_points = list(self.model.points)
        before_polygons = [list(poly) for poly in self.model.polygons]
        with patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.StandardButton.No):
            self.window._delete_geometry()
        self.assertEqual(self.model.points, before_points)
        self.assertEqual(self.model.polygons, before_polygons)
        self.assertEqual(self.window._edit_undo_stack, [])

        with patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.StandardButton.Yes):
            self.window._delete_geometry()
        self.assertEqual(len(self.model.polygons), 1)
        self.assertEqual(len(self.window._edit_undo_stack), 1)
        self.assertEqual(self.window._selected_polys, set())
        self.window._undo_edit()
        self.assertEqual(self.model.polygons, before_polygons)
        self.window._redo_edit()
        self.assertEqual(len(self.model.polygons), 1)
        with patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.Yes):
            self.window._reset_model()
        self.assertEqual(self.model.points, before_points)
        self.assertEqual(self.model.polygons, before_polygons)


if __name__ == "__main__":
    unittest.main()
