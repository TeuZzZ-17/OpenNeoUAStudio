import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from studio.ui.style import apply_studio_style, theme_accent_color
from tests.test_selection_runtime_v3 import _prepare_window


class OriginMarkerTests(unittest.TestCase):
    """The origin marker is a purely visual indicator: not interactive."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_marker_color_follows_the_active_theme_accent(self):
        window, _obj, _model = _prepare_window()
        try:
            apply_studio_style("ghorkov")
            self.assertEqual(theme_accent_color().name(), "#ef5a6e")
            apply_studio_style("sulgogar")
            self.assertEqual(theme_accent_color().name(), "#4cf0eb")
        finally:
            window.close()
            apply_studio_style("resistance")

    def test_set_origin_to_model_center_renamed(self):
        window, _obj, _model = _prepare_window()
        try:
            self.assertEqual(
                window.center_origin_button.text(), "Set Origin to Model Center")
            self.assertEqual(
                window.edit_center_action.text(), "Set Origin to Model Center")
            self.assertTrue(hasattr(window, "_set_origin_to_model_center"))
        finally:
            window.close()

    def test_marker_has_no_interactive_api(self):
        window, _obj, _model = _prepare_window()
        try:
            viewport = window.viewport
            for name in (
                    "_origin_marker_selected", "_origin_marker_hit",
                    "_origin_marker_triangle", "_origin_marker_screen_point",
                    "_active_gizmo_pivot"):
                self.assertFalse(
                    hasattr(viewport, name),
                    f"origin marker interactivity leftover: {name}")
            self.assertFalse(hasattr(viewport.edit_session, "begin_origin_modal"))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
