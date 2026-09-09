import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from assembly_window import AssemblyWindow, AddFxElementDialog
from base_mapping_editor import MappingIndex, MappingEditError, rewrite_model_base_structure
from base_parser import parse_base_bytes
from fx_authoring import prepare_fx_clipboard, stage_fx_clipboard
from fx_element_editor import detect_fx_elements, build_fx_element_clipboard
from tests.test_fx_clipboard_v3 import _writable_family, _image


class FxAuthoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self, family, obj):
        window = AssemblyWindow()
        window._set_family(family)
        window._show_model_editor()
        return window

    def test_fx3_and_non_atlas_animation_are_discovered_and_copyable(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, animated in (("FX3", False), ("CUSTOM", True)):
                family, obj = _writable_family(Path(directory), name, vanm=animated)
                element = detect_fx_elements(obj, family.animations)[0]
                self.assertEqual(element.fx_name, "VANM" if animated else "FX3")
                build_fx_element_clipboard(obj, element, family.animations)

    def test_mouse_pick_move_cancel_and_delete_use_complete_bilateral_fx(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "FX1", bilateral=True)
            # The byte-level fixture deliberately uses an all-zero BASE STRC;
            # give its renderer a non-degenerate object transform for picking.
            obj.base_object.transform.scale = (1., 1., 1.)
            window = AssemblyWindow()
            try:
                window._set_family(family)
                window._show_model_editor()
                window.show()
                self.app.processEvents()
                view = window.viewport
                QTest.qWait(100)
                view.repaint()
                self.app.processEvents()
                self.assertTrue(view._pick_shapes, (view.isVisible(), len(view._faces), view.size(), view._snapshot_active, view._last_render_error))
                position = view._pick_shapes[0].polygon.boundingRect().center().toPoint()
                QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=position)
                self.assertEqual(window._selected_polys, {0, 1})
                self.assertTrue(window.copy_geometry_action.isEnabled())
                self.assertTrue(window.delete_fx_button.isEnabled())
                before = list(obj.skeleton.points)
                window.edit_move_action.trigger()
                self.assertTrue(view.paste_preview_active)
                QTest.mouseMove(view, position + QPoint(25, 15))
                self.assertEqual(obj.skeleton.points, before)
                QTest.keyClick(view, Qt.Key.Key_Escape)
                self.assertFalse(view.paste_preview_active)
                self.assertEqual(obj.skeleton.points, before)
                self.assertEqual(len(window._edit_undo_stack), 0)
                window.fx_combo.setCurrentIndex(0)
                self.assertEqual(window._selected_polys, set())
                self.assertFalse(window.delete_fx_button.isEnabled())
                self.app.processEvents()
                view.repaint()
                position = view._pick_shapes[0].polygon.boundingRect().center().toPoint()
                QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=position)
                self.assertEqual(window._selected_polys, {0, 1})
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    QTest.keyClick(view, Qt.Key.Key_Delete)
                self.assertEqual(obj.skeleton.polygons, [])
                window._undo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 2)
            finally:
                window.close()

    def test_new_fx_on_model_without_fx_and_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "STONE")
            family.textures["FX3.ILBM"] = _image("FX3.ILBM")
            original = copy.deepcopy(obj.skeleton.points)
            source_family, source = _writable_family(Path(directory), "FX3", bilateral=True)
            element = detect_fx_elements(source, source_family.animations)[0]
            clipboard = prepare_fx_clipboard(
                obj, source_family, source, element)
            staged_family, staged, result = stage_fx_clipboard(family, obj, clipboard, (2., 3., 4.))
            self.assertEqual(obj.skeleton.points, original)
            self.assertEqual(result.polygon_indices, (1, 2))
            self.assertEqual(staged.skeleton.points[4], tuple(source.skeleton.points[0][i] + (2., 3., 4.)[i] for i in range(3)))
            self.assertEqual(detect_fx_elements(staged, staged_family.animations)[0].poly_ids, (1, 2))
            window = self.window(family, obj)
            try:
                self.assertTrue(window._can_add_fx_element())
                states = window._bundle_topology_states(family.base_path.read_bytes(), staged)
                saved = rewrite_model_base_structure(family.base_path.read_bytes(), states)
                reloaded = parse_base_bytes(saved).root
                self.assertEqual(len(reloaded.ades), 2)
                self.assertEqual(reloaded.ades[1].texture.name, "FX3.ILBM")
                self.assertEqual([e.poly_id for e in reloaded.ades[1].atts], [1, 2])
            finally:
                window.close()

    def test_import_commit_cancel_undo_redo_and_resource_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family, obj = _writable_family(root, "STONE")
            source_family, source = _writable_family(root, "FX2", vanm=True)
            element = detect_fx_elements(source, source_family.animations)[0]
            clipboard = prepare_fx_clipboard(obj, source_family, source, element)
            window = self.window(family, obj)
            try:
                before = window._capture_topology_state("root")
                window._geometry_clipboard = clipboard
                self.assertTrue(window.viewport.begin_paste_preview(clipboard, window.viewport.rect().center()))
                window.viewport.cancel_paste_preview()
                self.assertEqual(window._capture_topology_state("root"), before)
                self.assertNotIn("GLOW.ANM", family.animations)
                window.viewport.begin_paste_preview(clipboard, window.viewport.rect().center())
                self.assertEqual(window._commit_geometry_reason(), "")
                window._confirm_paste_geometry()
                self.assertEqual(len(obj.skeleton.polygons), 2)
                self.assertIn("GLOW.ANM", family.animations)
                self.assertEqual(len(window._edit_undo_stack), 1)
                window._undo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 1)
                self.assertNotIn("GLOW.ANM", family.animations)
                window._redo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 2)
                self.assertIn("GLOW.ANM", family.animations)
            finally:
                window.close()
            family.textures["FX2.ILBM"].pixels = b"different"
            with self.assertRaises(MappingEditError):
                stage_fx_clipboard(family, obj, clipboard)

    def test_uv_edit_delete_undo_redo_preserves_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "FX1", bilateral=True)
            window = self.window(family, obj)
            try:
                window.fx_combo.setCurrentIndex(1)
                key = window.uv_editor.loop_keys()[0]
                old = list(obj.base_object.ades[0].olpl[0])
                edited = list(old)
                edited[0] = (20, 20)
                window._on_uv_changed({key: edited})
                window._on_uv_edit_finished()
                self.assertTrue(window.can_delete_geometry(), window._delete_geometry_reason())
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    window._delete_selected_fx_element()
                self.assertEqual(obj.skeleton.polygons, [])
                self.assertFalse(window._uv_original)
                window._undo_edit()
                self.assertEqual(obj.base_object.ades[0].olpl[0], edited)
                self.assertTrue(window._uv_original)
                window._undo_edit()
                self.assertEqual(obj.base_object.ades[0].olpl[0], old)
                window._redo_edit()
                window._redo_edit()
                self.assertEqual(obj.skeleton.polygons, [])
            finally:
                window.close()

    def test_all_animation_groups_stay_visible_during_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "FX2", vanm=True)
            window = self.window(family, obj)
            try:
                window.fx_combo.setCurrentIndex(1)
                keys = window.uv_editor.loop_keys()
                self.assertEqual(len(keys), 1)
                window.uv_editor.select_handle(keys[0], 0)
                selected = window.uv_editor.selected_handles()
                window._on_animation_frame_changed()
                self.assertEqual(window.uv_editor.loop_keys(), keys)
                self.assertEqual(window.uv_editor.selected_handles(), selected)
                original = copy.deepcopy(family.animations['GLOW.ANM'].texcoord_groups)
                window.uv_editor.nudge_selected(5, 5)
                groups = family.animations['GLOW.ANM'].texcoord_groups
                self.assertEqual(groups[1], list(reversed(groups[0])))
                self.assertNotEqual(groups, original)
                window._revert_selected_uv()
                self.assertEqual(family.animations['GLOW.ANM'].texcoord_groups, original)
            finally:
                window.close()

    def test_import_unsupported_flags_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "STONE")
            source_family, source = _writable_family(Path(directory), "FX3")
            source.base_object.ades[0].polflags = 0x88
            element = detect_fx_elements(source, source_family.animations)[0]
            clipboard = prepare_fx_clipboard(obj, source_family, source, element)
            before = copy.deepcopy(obj.skeleton.points)
            from geometry_editor import GeometryClipboardError
            with self.assertRaisesRegex(GeometryClipboardError, "0x88"):
                stage_fx_clipboard(family, obj, clipboard)
            self.assertEqual(obj.skeleton.points, before)

    def test_last_fx_can_be_deleted_and_added_back(self):
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), "FX1", bilateral=True)
            window = self.window(family, obj)
            try:
                element = detect_fx_elements(obj, family.animations)[0]
                clipboard = prepare_fx_clipboard(obj, family, obj, element)
                window.fx_combo.setCurrentIndex(1)
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    window._delete_selected_fx_element()
                self.assertEqual(obj.skeleton.polygons, [])
                self.assertTrue(window._can_add_fx_element())
                self.assertEqual(window._geometry_writer_reason("root", obj), "")
                window._geometry_clipboard = clipboard
                self.assertTrue(window.viewport.begin_paste_preview(clipboard, window.viewport.rect().center()))
                window._confirm_paste_geometry()
                self.assertEqual(len(obj.skeleton.polygons), 2)
                self.assertEqual(len(window._edit_undo_stack), 2)
            finally:
                window.close()

    def test_area_uv_edit_roundtrip_and_delete_remapping(self):
        from tests.test_area_fx_structural import _family
        with tempfile.TemporaryDirectory() as directory:
            family, obj, original = _family(Path(directory))
            window = self.window(family, obj)
            try:
                element = next(e for e in window._fx_elements if e.bilateral)
                window.fx_combo.setCurrentIndex(window._fx_combo_index(element.identity))
                key = window.uv_editor.loop_keys()[0]
                self.assertTrue(window.uv_editor.loop_editable(key))
                updated = window.uv_editor.loop_uvs()[key]
                updated[0] = (15, 25)
                window._on_uv_changed({key: updated})
                window._on_uv_edit_finished()
                states = window._bundle_topology_states(original, obj)
                saved = rewrite_model_base_structure(original, states)
                self.assertEqual(parse_base_bytes(saved).root.ades[key[1]].olpl[0], updated)
                # Delete an earlier AREA: the edited FX's block/ATTS address moves.
                before = window._capture_topology_state('root')
                from geometry_editor import plan_delete_geometry
                plan = plan_delete_geometry(obj, {15}, MappingIndex(obj))
                window._apply_delete_plan('root', obj, plan, label='Delete earlier AREA')
                self.assertTrue(window._uv_original)
                new_key = next(iter(window._uv_original))
                self.assertEqual(window._uv_storage_points(window._uv_storage(new_key)), updated)
                window._undo_edit()
                self.assertEqual(window._uv_original, before['uv_original'])
            finally:
                window.close()

    def test_presets_transform_selection_with_single_undo_and_alias_is_internal(self):
        from PySide6.QtWidgets import QInputDialog
        with tempfile.TemporaryDirectory() as directory:
            family, obj = _writable_family(Path(directory), 'FX1', bilateral=True)
            window = self.window(family, obj)
            try:
                window.fx_combo.setCurrentIndex(1)
                original = list(obj.skeleton.points)
                window._set_transform_mode('scale')
                window._apply_transform_preset(0)  # 50% of the original size
                self.assertAlmostEqual(obj.skeleton.points[0][0], original[0][0] * .5)
                self.assertEqual(len(window._edit_undo_stack), 1)
                window._undo_edit()
                self.assertEqual(obj.skeleton.points, original)
                window._set_transform_mode('rotate')
                window.transform_preset_axis.setCurrentText('Z')
                window._apply_transform_preset(3)  # +90 degrees
                self.assertNotEqual(obj.skeleton.points, original)
                window._undo_edit()
                for actual, expected in zip(obj.skeleton.points, original):
                    for a, b in zip(actual, expected):
                        self.assertAlmostEqual(a, b)
                with patch.object(QInputDialog, 'getText', return_value=('Main rotor', True)):
                    window._rename_selected_fx()
                self.assertEqual(window.fx_combo.currentText(), 'Main rotor')
                self.assertEqual(obj.base_object.ades[0].texture.name, 'FX1.ILBM')
                menu = window._create_viewport_context_menu()
                labels = [a.text() for a in menu.actions()]
                self.assertIn('Clone FX Element', labels)
                self.assertIn('Rename FX...', labels)
                self.assertNotIn('Copy FX Element', labels)
                self.assertNotIn('Replace FX Element...', labels)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
