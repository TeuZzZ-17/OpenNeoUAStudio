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
from base_parser import parse_base_bytes
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
    atts = struct.pack(">hBBBB", 0, 1, 2, 3, 0)
    olpl = struct.pack(">hBBBBBB", 3, 0, 0, 255, 0, 0, 255)
    texture = _form(
        b"OBJT",
        _chunk(b"CLID", b"ilbm.class\0")
        + _form(
            b"CIBO",
            _chunk(b"NAM2", b"STATIC.ILB\0")
            + _chunk(b"OTL2", bytes((0, 0, 255, 0, 0, 255)))))
    area = _form(
        b"AREA",
        _chunk(
            b"STRC",
            struct.pack(">hHHBBBB", 1, 0, 0x88, 0, 255, 0, 255))
        + texture)
    amesh = _form(
        b"AMSH",
        area + _chunk(b"ATTS", atts) + _chunk(b"OLPL", olpl))
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"TEST.SKLT\0")))
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62)) + skeleton
        + _form(b"ADES", material))
    return _form(
        b"MC2 ", _form(
            b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


class GeometryPasteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sklt_path = root / "TEST.SKLT"
        base_path = root / "TEST.BASE"
        initial = create_minimal_sklt_model()
        save_sklt_with_poo2_pol2_structure(
            initial,
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
             (0.0, 1.0, 0.0)],
            [[0, 1, 2]], sklt_path)
        base_path.write_bytes(_base_bytes())
        self.model = parse_sklt_file(sklt_path)
        base_asset = parse_base_bytes(base_path.read_bytes())
        base_object = base_asset.root
        self.block = base_object.ades[0]
        self.obj = FamilyObject(
            base_object=base_object,
            skeleton_ref=SimpleNamespace(
                path=sklt_path, status="found", source="loose"),
            skeleton=self.model,
            materials=[MaterialGroup(
                "material", block=self.block,
                faces=[(0, list(self.block.olpl[0]), 2)])],
            owner_path="root",
        )
        self.family = AssetFamily(
            base_path=base_path,
            base_asset=base_asset,
            root_object=self.obj,
        )
        self.window = AssemblyWindow()
        self.window._family = self.family
        self.window._owner_to_obj = {"root": self.obj}
        self.window._selected_owner = "root"
        self.window._workbench_obj = self.obj
        self.window._mapping_index = MappingIndex(self.obj)
        self.window._selected_polys = {0}
        self.window._selected_poly = 0
        self.window.viewport.load_family(
            self.family, primary_owner="root")
        self.window.viewport.set_selected_owner("root")
        self.window.edit_toggle_action.setChecked(True)
        self.window.viewport.enter_edit_mode_with_vertices(
            "root", [0, 1, 2], pick_polygons=True)
        self.window._geometry_clipboard = build_geometry_clipboard(
            self.obj, "root", {0}, MappingIndex(self.obj))

    def tearDown(self):
        self.window.close()
        self.temp.cleanup()

    def _confirm_at_source_pivot(self):
        clipboard = self.window._geometry_clipboard
        position = self.window.viewport._project(
            self.window.viewport._camera_vertex(
                clipboard.pivot)).toPoint()
        self.assertTrue(self.window.viewport.begin_paste_preview(
            clipboard, position))
        self.assertFalse(self.window.file_menu.isEnabled())
        self.assertFalse(self.window.edit_toggle_action.isEnabled())
        self.assertFalse(self.window.edit_undo_action.isEnabled())
        self.assertTrue(self.window.model_gizmo.isEnabled())
        self.window._confirm_paste_geometry()
        self.assertTrue(self.window.file_menu.isEnabled())

    def test_paste_is_one_topological_undo_and_redo(self):
        self._confirm_at_source_pivot()
        self.assertEqual((len(self.model.points), len(self.model.polygons)),
                         (6, 2))
        self.assertEqual(len(self.block.atts), 2)
        self.assertEqual(
            {face.poly_id for face in self.window.viewport._faces}, {0, 1})
        self.assertEqual(len(self.window._edit_undo_stack), 1)
        self.assertEqual(self.window._selected_polys, {1})

        self.window._undo_edit()
        self.assertEqual((len(self.model.points), len(self.model.polygons)),
                         (3, 1))
        self.assertEqual(len(self.block.atts), 1)
        self.window._redo_edit()
        self.assertEqual((len(self.model.points), len(self.model.polygons)),
                         (6, 2))
        self.assertEqual(len(self.block.olpl), 2)

    def test_post_append_failure_rolls_back_without_undo_ghost(self):
        clipboard = self.window._geometry_clipboard
        position = self.window.viewport._project(
            self.window.viewport._camera_vertex(
                clipboard.pivot)).toPoint()
        self.assertTrue(self.window.viewport.begin_paste_preview(
            clipboard, position))
        before = self.window._capture_topology_state("root")

        with patch.object(
                self.window, "_rebuild_workbench",
                side_effect=RuntimeError("simulated refresh failure")):
            self.window._confirm_paste_geometry()

        after = self.window._capture_topology_state("root")
        self.assertEqual(
            self.window._topology_content(after),
            self.window._topology_content(before))
        self.assertEqual(self.window._edit_undo_stack, [])
        self.assertEqual(self.window._edit_redo_stack, [])
        self.assertFalse(self.window.viewport.paste_preview_active)
        self.assertNotIn("root", self.window._topology_original)

    def test_transform_after_paste_undoes_before_topology(self):
        self._confirm_at_source_pivot()
        pasted_points = list(self.model.points)
        self.assertTrue(
            self.window.viewport.nudge_edit_selection(12.0, -4.0))
        self.assertEqual(len(self.window._edit_undo_stack), 2)
        self.assertNotEqual(self.model.points, pasted_points)

        self.window._undo_edit()
        self.assertEqual(self.model.points, pasted_points)
        self.assertEqual(len(self.model.polygons), 2)
        self.window._undo_edit()
        self.assertEqual(len(self.model.polygons), 1)

    def test_reset_model_restores_pre_paste_topology(self):
        self._confirm_at_source_pivot()
        with patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.Yes):
            self.window._reset_model()
        self.assertEqual((len(self.model.points), len(self.model.polygons)),
                         (3, 1))
        self.assertEqual((len(self.block.atts), len(self.block.olpl)), (1, 1))

    def test_copy_starts_preview_and_paste_confirms_then_reuses_clipboard(self):
        self.window._geometry_clipboard = None
        points = list(self.model.points)
        polygons = [list(poly) for poly in self.model.polygons]
        self.window._copy_geometry()
        clipboard = self.window._geometry_clipboard
        self.assertIsNotNone(clipboard)
        self.assertTrue(self.window.viewport.paste_preview_active)
        self.assertTrue(self.window.paste_geometry_action.isEnabled())
        self.assertEqual(self.model.points, points)
        self.assertEqual(self.model.polygons, polygons)
        self.assertNotIn("root", self.window._geom_dirty)

        self.window.paste_geometry_action.trigger()
        self.assertFalse(self.window.viewport.paste_preview_active)
        self.assertEqual(self.window._selected_polys, {1})
        self.assertIs(self.window._geometry_clipboard, clipboard)
        self.window.paste_geometry_action.trigger()
        self.assertTrue(self.window.viewport.paste_preview_active)
        self.window.viewport.cancel_paste_preview()

    def test_only_geometry_copy_paste_cut_delete_are_visible(self):
        labels = [action.text() for action in self.window.edit_menu.actions()]
        self.assertNotIn("Copy Selected Vertex Positions", labels)
        self.assertNotIn("Paste Vertex Positions", labels)
        self.assertEqual(
            [label for label in labels
             if label in ("Copy", "Paste", "Cut", "Delete")],
            ["Copy", "Paste", "Cut", "Delete"])
        context_labels = [
            action.text()
            for action in self.window._create_viewport_context_menu().actions()
        ]
        self.assertNotIn("Copy Selected Vertex Positions", context_labels)
        self.assertNotIn("Paste Vertex Positions", context_labels)
        self.assertEqual(
            [label for label in context_labels
             if label in ("Copy", "Paste", "Cut", "Delete")],
            ["Copy", "Paste", "Cut", "Delete"])

    def test_preview_and_commit_do_not_depend_on_loose_paths(self):
        self.obj.skeleton_ref.path = None
        self.obj.skeleton_ref.status = "setbas"
        self.obj.skeleton_ref.source = "SET.BAS"
        self.family.base_path = None
        self.assertTrue(self.window.can_preview_geometry())
        self.assertTrue(self.window.can_commit_geometry())
        self.assertTrue(self.window.can_save_as_geometry())
        self.assertFalse(self.window.can_overwrite_geometry())
        self.window._geometry_clipboard = None
        self.window._copy_geometry()
        self.assertTrue(self.window.viewport.paste_preview_active)
        self.window.viewport.cancel_paste_preview()

    def test_viewport_ctrl_copy_and_ctrl_paste_use_geometry_actions(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.window._geometry_clipboard = None
        self.window.show()
        self.window.viewport.setFocus()
        QApplication.processEvents()
        QTest.keyClick(
            self.window.viewport, Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
        self.assertIsNotNone(self.window._geometry_clipboard)
        self.assertTrue(self.window.viewport.paste_preview_active)
        QTest.keyClick(
            self.window.viewport, Qt.Key.Key_V,
            Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
        self.assertFalse(self.window.viewport.paste_preview_active)
        self.assertEqual(len(self.model.polygons), 2)
        self.window._geom_dirty.clear()
        self.window.hide()

    def test_invalid_decoded_writer_refuses_only_commit_with_reason(self):
        self.model.original_data = b""
        self.window._geometry_writer_capability_cache.clear()
        self.assertTrue(self.window.can_preview_geometry())
        self.assertFalse(self.window.can_commit_geometry())
        self.assertIn(
            "decoded SKLT source bytes",
            self.window._commit_geometry_reason())
        self.window._sync_edit_action_states()
        self.assertTrue(self.window.paste_geometry_action.isEnabled())
        self.assertIn(
            "confirmation will be refused",
            self.window.paste_geometry_action.toolTip())

    def test_text_field_ctrl_copy_is_not_intercepted_by_viewport(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLineEdit

        self.window._geometry_clipboard = None
        editor = QLineEdit(self.window)
        editor.setText("field text")
        editor.selectAll()
        editor.show()
        self.window.show()
        editor.setFocus()
        QApplication.processEvents()
        QTest.keyClick(
            editor, Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
        self.assertIsNone(self.window._geometry_clipboard)
        self.assertFalse(self.window.viewport.paste_preview_active)
        editor.deleteLater()
        self.window.hide()


if __name__ == "__main__":
    unittest.main()
