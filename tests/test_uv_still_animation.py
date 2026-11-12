"""Add UV Phase / Paste UV Position on a still polygon create a 2-phase animation (new ANM + animated block)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from studio.assets.animated_polygons import convert_polygon_to_animation, new_animation_name
from studio.assets.mapping import MappingEditError
from studio.formats.animation import export_anm_bytes
from studio.ui.uv_editor import UVEditorWidget
from tests.test_uv_multiloop_phase3 import _open_memory_window, _two_polygon_family


def _bounds(group):
    us, vs = zip(*group)
    return min(us), min(vs), max(us), max(vs)


class ConvertPolygonToAnimationTests(unittest.TestCase):
    def test_second_phase_is_a_copy_on_a_free_area(self):
        family, obj = _two_polygon_family()
        first = [tuple(uv) for uv in obj.base_object.ades[0].olpl[0]]
        result = convert_polygon_to_animation(family, obj, 0, "UVA001.ANM")
        blocks = obj.base_object.ades
        self.assertEqual(len(blocks[0].atts), 1)          # polygon 1 stays on its still block
        self.assertEqual(blocks[0].atts[0].poly_id, 1)
        animated = blocks[result.block_index]
        self.assertEqual((animated.class_id, animated.texture.kind, animated.texture.name),
                         ("area.class", "bmpanim", "UVA001.ANM"))
        self.assertEqual(animated.ade_poly_id, 0)
        animation = family.animations["UVA001.ANM"]
        self.assertTrue(animation.new_file)
        self.assertEqual(len(animation.frames), 2)
        self.assertEqual(animation.texcoord_groups[0], first)
        a, b = _bounds(animation.texcoord_groups[0]), _bounds(animation.texcoord_groups[1])
        self.assertTrue(a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1], (a, b))   # not overlapping
        self.assertEqual((b[2] - b[0], b[3] - b[1]), (a[2] - a[0], a[3] - a[1]))              # same shape
        export_anm_bytes(animation)                                                          # writable

    def test_pasted_position_becomes_the_second_phase_and_emptied_block_goes(self):
        family, obj = _two_polygon_family()
        target = [(130, 140), (190, 140), (190, 200), (130, 200)]
        convert_polygon_to_animation(family, obj, 0, "UVA001.ANM", second_uvs=target)
        convert_polygon_to_animation(family, obj, 1, "UVA002.ANM", second_uvs=[(0, 0), (9, 0), (9, 9), (0, 9)])
        self.assertEqual([b.class_id for b in obj.base_object.ades], ["area.class", "area.class"])
        self.assertEqual(family.animations["UVA001.ANM"].texcoord_groups[1], target)

    def test_refusals(self):
        family, obj = _two_polygon_family()
        with self.assertRaises(MappingEditError):
            convert_polygon_to_animation(family, obj, 2, "UVA001.ANM")      # unmapped polygon
        with self.assertRaises(MappingEditError):
            convert_polygon_to_animation(family, obj, 0, "TOO_LONG_NAME.ANM")
        with self.assertRaises(MappingEditError):
            convert_polygon_to_animation(family, obj, 0, "UVA001.ANM", second_uvs=[(0, 0)])
        convert_polygon_to_animation(family, obj, 0, "UVA001.ANM")
        with self.assertRaises(MappingEditError):
            convert_polygon_to_animation(family, obj, 1, "uva001.anm")      # name already used
        with self.assertRaises(MappingEditError):
            convert_polygon_to_animation(family, obj, 0, "UVA002.ANM")      # already animated

    def test_only_animated_segments_get_yellow_vertices(self):
        from types import SimpleNamespace
        from studio.rendering.asset_viewport_paint import animated_vertices
        family, obj = _two_polygon_family()
        session = SimpleNamespace(model=obj.skeleton, fam_obj=obj)
        self.assertEqual(animated_vertices(session), set())
        convert_polygon_to_animation(family, obj, 0, "UVA001.ANM")
        self.assertEqual(animated_vertices(session), {0, 1, 2, 3})

    def test_name_generator_skips_taken_names(self):
        self.assertEqual(new_animation_name(lambda name: name == "UVA001.ANM"), "UVA002.ANM")


class StillPolygonWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_add_uv_phase_shows_a_grey_preview_then_lmb_places_it(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            self.assertTrue(window.uv_add_phase_button.isEnabled(), window._uv_still_animation_reason())
            window.uv_add_phase_button.click()
            editor = window.uv_editor
            self.assertTrue(editor.placement_active)
            self.assertFalse(family.animations)                       # nothing is created yet
            editor._move_placement(editor._uv_to_screen((200, 200)))   # carry it to the lower right
            self.assertGreaterEqual(min(u for u, _ in editor.placement_uvs()), 140)
            placed = editor.placement_uvs()
            QTest.mouseClick(editor, Qt.MouseButton.LeftButton, pos=editor._uv_to_screen((200, 200)).toPoint())
            self.assertFalse(editor.placement_active)
            self.assertEqual(family.animations["UVA001.ANM"].texcoord_groups[1], placed)
            self.assertIsNotNone(window._uv_timeline)
            self.assertEqual(window.uv_phase_combo.currentIndex(), 0)  # still "All UV phases": phase 1 stays visible
            self.assertEqual(window.uv_editor.loop_count(), 2)
            self.assertEqual(len({key for key, _ in window.uv_editor.selected_handles()}), 1)  # new phase highlighted
            window._undo_edit()
            self.assertNotIn("UVA001.ANM", family.animations)
            self.assertEqual(len(obj.base_object.ades[0].atts), 2)
            window._redo_edit()
            self.assertIn("UVA001.ANM", family.animations)
        finally:
            window.close()

    def test_rmb_and_esc_cancel_the_preview(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            editor = window.uv_editor
            window.uv_add_phase_button.click()
            QTest.mouseClick(editor, Qt.MouseButton.RightButton, pos=editor._uv_to_screen((100, 100)).toPoint())
            self.assertFalse(editor.placement_active)
            window.uv_add_phase_button.click()
            QTest.keyClick(editor, Qt.Key.Key_Escape)
            self.assertFalse(editor.placement_active)
            self.assertFalse(family.animations)
        finally:
            window.close()

    def test_preview_stays_inside_the_texture(self):
        editor = UVEditorWidget()
        editor.resize(400, 400)
        editor.begin_placement([(10, 10), (60, 10), (60, 60), (10, 60)])
        editor._move_placement(editor._uv_to_screen((255, 255)))
        us = [u for u, _ in editor.placement_uvs()]
        self.assertEqual((min(us), max(us)), (205, 255))
        editor._move_placement(editor._uv_to_screen((0, 0)))
        self.assertEqual(min(u for u, _ in editor.placement_uvs()), 0)

    def test_copy_uv_position_carries_a_preview_and_places_it_as_the_second_phase(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            editor = window.uv_editor
            window._copy_uv_position(("root", 0, 0))
            copied = list(window._uv_position_clipboard)
            self.assertTrue(editor.placement_active)
            self.assertEqual(editor.placement_uvs(), copied)          # starts on the original spot
            editor.cancel_placement()
            self.assertFalse(family.animations)
            window._on_polygon_picked(1)
            window._paste_uv_position()                                # paste = the same preview
            self.assertTrue(editor.placement_active)
            editor.finish_placement()
            animation = family.animations["UVA001.ANM"]
            self.assertEqual(animation.texcoord_groups[1], copied)
            self.assertEqual(len(animation.frames), 2)
        finally:
            window.close()

    def test_speed_control_sets_the_duration_of_the_selected_phase_with_undo(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            self.assertFalse(window.uv_speed_spin.isEnabled())            # no animation yet
            window._on_polygon_picked(0)
            window.uv_add_phase_button.click()
            window.uv_editor.finish_placement()
            animation = family.animations["UVA001.ANM"]
            self.assertTrue(window.uv_speed_spin.isEnabled())
            self.assertEqual(window.uv_speed_spin.value(), 250)
            window._select_uv_phase(2)                                     # isolate phase 2
            window.uv_speed_spin.setValue(900)
            animation = family.animations["UVA001.ANM"]
            self.assertEqual([round(f.duration_ms) for f in animation.frames], [250, 900])
            self.assertTrue(animation.new_file)                             # still written on in-place save
            window._undo_edit()
            self.assertEqual([round(f.duration_ms) for f in family.animations["UVA001.ANM"].frames], [250, 250])
            window._redo_edit()
            window._select_uv_phase(0)                                     # no phase: every frame
            window.uv_speed_spin.setValue(500)
            self.assertEqual([round(f.duration_ms) for f in family.animations["UVA001.ANM"].frames], [500, 500])
        finally:
            window.close()

    def test_play_stays_on_through_edits_until_pause_is_pressed(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            window.uv_add_phase_button.click()
            window.uv_editor.finish_placement()
            window.play_button.setChecked(True)
            viewport = window.viewport
            self.assertTrue(viewport._anim_playing and viewport._anim_timer.isActive())
            window.uv_speed_spin.setValue(900)                          # an edit reloads the family
            self.assertTrue(viewport._anim_playing and viewport._anim_timer.isActive())
            self.assertTrue(window.play_button.isChecked())
            window._undo_edit()
            window._redo_edit()
            self.assertTrue(viewport._anim_playing and viewport._anim_timer.isActive())
            window.play_button.setChecked(False)                         # only Pause stops it
            window.uv_speed_spin.setValue(300)
            self.assertFalse(viewport._anim_playing or viewport._anim_timer.isActive())
        finally:
            window.close()

    def test_add_uv_phase_on_a_playing_animation_also_uses_the_preview(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            window.uv_add_phase_button.click()
            window.uv_editor.finish_placement()
            self.assertEqual(len(family.animations["UVA001.ANM"].frames), 2)
            window.uv_add_phase_button.click()                         # the animated polygon now has a timeline
            self.assertTrue(window.uv_editor.placement_active)
            window.uv_editor.cancel_placement()
            self.assertEqual(len(family.animations["UVA001.ANM"].frames), 2)
            window.uv_add_phase_button.click()
            window.uv_editor.finish_placement()
            self.assertEqual(len(family.animations["UVA001.ANM"].frames), 3)
        finally:
            window.close()


class OverwriteWritesTheAnmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_overwrite_is_enabled_by_an_animation_edit_and_writes_the_anm_beside_the_base(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox
        from studio.formats.animation import parse_anm_file
        from studio.formats.base_format import parse_base_file
        from studio.workbench.window import AssemblyWindow
        from tests.test_fx_clipboard_v3 import _writable_family

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            family, obj = _writable_family(root, "STONE")
            obj.base_object.ades[0].olpl[0] = [(10, 10), (60, 10), (60, 60), (10, 60)]
            window = AssemblyWindow()
            try:
                window._set_family(family)
                window._show_model_editor()
                window._on_polygon_picked(0)
                window.uv_add_phase_button.click()
                window.uv_editor.finish_placement()
                self.assertTrue(window.model_save_button.isEnabled(), "Overwrite must be available")
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                        patch.object(QMessageBox, "critical", side_effect=lambda *a: self.fail(a[1:])), \
                        patch.object(QMessageBox, "warning", side_effect=lambda *a: self.fail(a[1:])):
                    window._overwrite_model()
                anm = root / "UVA001.ANM"
                self.assertTrue(anm.is_file())
                self.assertEqual(len(parse_anm_file(anm).frames), 2)
                texture = parse_base_file(root / "FXTEST.BASE").root.ades[0].texture
                self.assertEqual((texture.kind, texture.name), ("bmpanim", "UVA001.ANM"))
                # Saved: nothing left to write.
                self.assertFalse(family.animations["UVA001.ANM"].new_file)
                self.assertFalse(family.animations["UVA001.ANM"].timeline_dirty)
                # A later speed edit writes the ANM again.
                window._on_polygon_picked(0)
                self.assertTrue(window.uv_speed_spin.isEnabled())
                window.uv_speed_spin.setValue(800)
                # the selected phase (the new phase 2) is the one that changes
                self.assertEqual([round(f.duration_ms) for f in family.animations["UVA001.ANM"].frames], [250, 800])
                self.assertTrue(window.model_save_button.isEnabled())
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                        patch.object(QMessageBox, "critical", side_effect=lambda *a: self.fail(a[1:])), \
                        patch.object(QMessageBox, "warning", side_effect=lambda *a: self.fail(a[1:])):
                    window._overwrite_model()
                self.assertEqual([round(f.duration_ms) for f in parse_anm_file(anm).frames], [250, 800])
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
