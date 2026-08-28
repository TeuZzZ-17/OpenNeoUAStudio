from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from assembly_window import AssemblyWindow
from base_mapping_editor import (
    MappingEditError,
    build_material_block_clipboard,
    delete_material_block,
    paste_material_block,
    rewrite_model_base_structure,
    verify_model_base_structure,
)
from ilbm_parser import IlbmImage
from tests.test_area_fx_structural import _family as _area_family
from tests.test_asset_family_package import _make_package


class MaterialBlockEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_amesh_copy_pastes_empty_slot_and_writer_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            family = _make_package(root)
            obj = family.root_object
            original = family.base_path.read_bytes()

            clipboard = build_material_block_clipboard(family, obj, 0)
            result = paste_material_block(family, obj, clipboard)

            self.assertEqual(result.block_index, 1)
            self.assertIsNone(result.assigned_poly_id)
            self.assertEqual(len(obj.base_object.ades[0].atts), 1)
            self.assertEqual(obj.base_object.ades[1].atts, [])
            self.assertEqual(obj.base_object.ades[1].olpl, [])

            window = AssemblyWindow()
            try:
                window._family = family
                states = window._bundle_topology_states(original, obj)
            finally:
                window.close()
            edited = rewrite_model_base_structure(original, states)
            notes = verify_model_base_structure(original, edited, states)
            self.assertTrue(notes)

    def test_area_paste_requires_explicit_unmapped_compatible_polygon(self):
        with tempfile.TemporaryDirectory() as tmp:
            family, obj, original = _area_family(Path(tmp))
            family.textures["STONE.ILBM"] = IlbmImage(
                source_name="STONE.ILBM", kind="ILBM", width=1, height=1,
                palette=[(index, index, index) for index in range(256)],
                pixels=b"\x08")
            clipboard = build_material_block_clipboard(family, obj, 1)

            with self.assertRaisesRegex(MappingEditError, "explicit unmapped"):
                paste_material_block(family, obj, clipboard)
            with self.assertRaisesRegex(MappingEditError, "already mapped"):
                paste_material_block(
                    family, obj, clipboard, target_poly_id=16)

            result = paste_material_block(
                family, obj, clipboard, target_poly_id=0)
            block = obj.base_object.ades[result.block_index]
            self.assertEqual(result.assigned_poly_id, 0)
            self.assertEqual(block.ade_poly_id, 0)
            self.assertEqual(block.atts[0].poly_id, 0)
            self.assertEqual(len(block.olpl[0]), 4)

            window = AssemblyWindow()
            try:
                window._family = family
                states = window._bundle_topology_states(original, obj)
            finally:
                window.close()
            edited = rewrite_model_base_structure(original, states)
            verify_model_base_structure(original, edited, states)

            removed = delete_material_block(obj, result.block_index)
            self.assertEqual(removed.ade_poly_id, 0)

    def test_cross_family_dependency_transfer_is_collision_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source_root = parent / "source"
            target_root = parent / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = _make_package(source_root)
            target = _make_package(target_root)
            clipboard = build_material_block_clipboard(
                source, source.root_object, 0)

            target.textures.clear()
            target.texture_refs.clear()
            result = paste_material_block(
                target, target.root_object, clipboard)
            self.assertEqual(
                result.imported_resources, (("texture", "Texture:STATIC.ILB"),))
            self.assertIn("Texture:STATIC.ILB", target.textures)

            collision_root = parent / "collision"
            collision_root.mkdir()
            collision = _make_package(collision_root)
            image = copy.deepcopy(collision.textures["Texture:STATIC.ILB"])
            image.pixels = b"\x09"
            collision.textures["Texture:STATIC.ILB"] = image
            before = len(collision.root_object.base_object.ades)
            with self.assertRaisesRegex(MappingEditError, "content differs"):
                paste_material_block(
                    collision, collision.root_object, clipboard)
            self.assertEqual(
                len(collision.root_object.base_object.ades), before)

    def test_cross_family_different_set_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source_root = parent / "source"
            target_root = parent / "target"
            source_root.mkdir()
            target_root.mkdir()
            source = _make_package(source_root)
            target = _make_package(target_root)
            clipboard = build_material_block_clipboard(
                source, source.root_object, 0)
            shader = target.indexed_profile_refs["shader"].path
            shader.write_bytes(shader.read_bytes() + b"different SET")

            with self.assertRaisesRegex(MappingEditError, "profiles differ"):
                paste_material_block(
                    target, target.root_object, clipboard)

    def test_window_material_slot_participates_in_global_undo_redo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            family = _make_package(root)
            obj = family.root_object
            window = AssemblyWindow()
            try:
                window._set_family(family)
                window._show_model_editor()
                window.edit_toggle_action.setChecked(True)
                self.app.processEvents()
                self.assertTrue(window._editing_allowed())
                window.blocks_list.setCurrentRow(0)

                window._add_material_slot()
                self.assertEqual(len(obj.base_object.ades), 2)
                self.assertEqual(
                    window._edit_undo_stack[-1]["kind"],
                    "material_topology")
                window._undo_edit()
                self.assertEqual(len(obj.base_object.ades), 1)
                window._redo_edit()
                self.assertEqual(len(obj.base_object.ades), 2)
                self.assertEqual(obj.base_object.ades[1].atts, [])
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
