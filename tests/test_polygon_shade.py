from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from studio.assets.shade import apply_polygon_shade, auto_shade_value, outward_normal
from studio.workbench.window import AssemblyWindow
from tests.test_asset_family_package import _make_package


def _plain_shade(obj) -> int:
    return obj.base_object.ades[0].atts[0].shade_val


class PolygonShadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_formula_matches_the_original_vehicles(self):
        # y points DOWN: a face looking up is lit, one looking down is the dark belly.
        top = [(0, 0, 0), (0, 0, 1), (1, 0, 1), (1, 0, 0)]
        self.assertEqual(outward_normal(top), (0.0, -1.0, 0.0))
        self.assertEqual(auto_shade_value(top), 5)
        self.assertEqual(auto_shade_value(list(reversed(top))), 95)
        self.assertIsNone(auto_shade_value([(0, 0, 0), (1, 0, 0), (2, 0, 0)]))
        self.assertEqual(auto_shade_value(top, offset=-100), 0)
        self.assertEqual(auto_shade_value(list(reversed(top)), offset=500), 250)

    def test_set_and_auto_shade_only_touch_plain_textured_polygons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            family = _make_package(root)
            obj = family.root_object
            result = apply_polygon_shade(obj, [0], value=77)
            self.assertEqual(result["shaded"], 1)
            self.assertEqual(_plain_shade(obj), 77)
            result = apply_polygon_shade(obj)
            self.assertEqual(result["shaded"], 1)
            self.assertNotEqual(_plain_shade(obj), 77)
            with self.assertRaises(ValueError):
                apply_polygon_shade(obj, [0], value=251)
            obj.base_object.ades[0].polflags |= 0x40       # clear transparency: keeps its own shade
            before = _plain_shade(obj)
            self.assertEqual(apply_polygon_shade(obj, [0], value=9)["shaded"], 0)
            self.assertEqual(_plain_shade(obj), before)

    def _window(self, family):
        window = AssemblyWindow()
        window._set_family(family)
        window._show_model_editor()
        window.edit_toggle_action.setChecked(True)
        self.app.processEvents()
        window._selected_polys = {0}
        window._selected_poly = 0
        return window

    def test_auto_shade_is_an_off_by_default_helper_that_locks_the_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            family = _make_package(root)
            obj = family.root_object
            window = self._window(family)
            try:
                window._update_material_buttons()
                self.assertFalse(window.shade_auto_button.isChecked())
                self.assertTrue(window.shade_slider.isEnabled())
                self.assertTrue(window.shade_spin.isEnabled())
                apply_polygon_shade(obj, [0], value=120)
                window.shade_auto_button.setChecked(True)      # shades by direction, level 0
                self.assertEqual(_plain_shade(obj), 70)
                self.assertEqual(window.shade_spin.value(), 0)
                self.assertFalse(window.shade_slider.isEnabled())
                self.assertFalse(window.shade_spin.isEnabled())
                window._on_polygon_picked(0)                   # a new selection is manual again
                self.assertFalse(window.shade_auto_button.isChecked())
                self.assertTrue(window.shade_slider.isEnabled())
            finally:
                window.close()

    def test_slider_and_number_are_linked_and_shade_with_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "package"
            root.mkdir()
            family = _make_package(root)
            obj = family.root_object
            window = self._window(family)
            try:
                apply_polygon_shade(obj, [0], value=70)       # the automatic shade of this triangle
                window._refresh_shade_readout()
                self.assertEqual(window.shade_spin.value(), 0)
                window.shade_slider.setValue(30)
                self.assertEqual(_plain_shade(obj), 100)
                self.assertEqual(window.shade_spin.value(), 30)
                self.assertEqual(window.shade_slider.value(), 30)
                window.shade_slider.setValue(-20)
                self.assertEqual(_plain_shade(obj), 50)
                self.assertEqual(window.shade_spin.value(), -20)
                window.shade_spin.setValue(18)                # the number drives the slider
                self.assertEqual(window.shade_slider.value(), 18)
                self.assertEqual(_plain_shade(obj), 88)
                window.shade_spin.setValue(100)
                self.assertEqual(_plain_shade(obj), 170)
                window._undo_edit()
                self.assertEqual(_plain_shade(obj), 88)
                window._redo_edit()
                self.assertEqual(_plain_shade(obj), 170)
            finally:
                window.close()

    def test_level_is_read_back_from_the_saved_model(self):
        from unittest.mock import patch

        from studio.assets.family import load_asset_family
        from studio.assets.shade import shade_offset

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / "source"
            source.mkdir()
            family = _make_package(source)
            obj = family.root_object
            window = self._window(family)
            try:
                window.shade_slider.setValue(-37)         # manual mode: applied
                self.assertEqual(window.shade_spin.value(), -37)
                output = parent / "exported"
                with patch.object(window, "_notify"),                         patch("studio.workbench.window.QMessageBox.critical"):
                    self.assertTrue(window._write_model_files(
                        "root", family, obj, output / "Skeleton" / "TEST.SKLT",
                        output / "TEST.BASE", ask_replace=False))
                reopened = load_asset_family(output / "TEST.BASE", isolated_root=output)
                self.assertEqual(shade_offset(reopened.root_object), -37)
                self.assertEqual(_plain_shade(reopened.root_object), 33)
            finally:
                window.close()

    def test_shade_reaches_the_exported_base_file(self):
        from unittest.mock import patch

        from studio.formats.base_format import parse_base_bytes

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            source = parent / "source"
            source.mkdir()
            family = _make_package(source)
            obj = family.root_object
            window = self._window(family)
            try:
                apply_polygon_shade(obj, [0], value=133)
                output = parent / "exported"
                with patch.object(window, "_notify"),                         patch("studio.workbench.window.QMessageBox.critical") as critical:
                    exported = window._write_model_files(
                        "root", family, obj, output / "Skeleton" / "TEST.SKLT",
                        output / "TEST.BASE", ask_replace=False)
                self.assertTrue(exported, critical.call_args_list)
                saved = parse_base_bytes((output / "TEST.BASE").read_bytes(), "t")
                self.assertEqual(saved.root.ades[0].atts[0].shade_val, 133)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
