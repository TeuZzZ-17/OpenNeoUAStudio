import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from studio.rendering.asset_viewport import AssetViewport
from studio.workbench.window import AssemblyWindow


class SelectedModelBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_selected_model_bounds_api_and_menu_action_are_removed(self):
        viewport = AssetViewport()
        window = AssemblyWindow()
        try:
            self.assertFalse(hasattr(viewport, "_show_owner_bbox"))
            self.assertFalse(hasattr(viewport, "set_show_owner_bbox"))
            self.assertFalse(hasattr(viewport, "_draw_owner_bbox"))
            self.assertFalse(hasattr(window, "owner_bounds_check"))
            labels = [action.text() for action in window.view_menu.actions()]
            self.assertNotIn("Selected Model Bounds", labels)
        finally:
            window.close()
            viewport.close()

    def test_view_menu_keeps_current_overlay_actions_and_defaults(self):
        window = AssemblyWindow()
        try:
            labels = [
                action.text() for action in window.view_menu.actions()
                if not action.isSeparator()
            ][:8]
            self.assertEqual(labels, [
                "SEN2 volume",
                "Wire overlay",
                "Backface cull",
                "Axes",
                "Grid",
                "Preview issues overlay",
                "Mapping diagnostics",
                "Vanilla distance fade",
            ])
            self.assertFalse(window.sen_check.isChecked())
            self.assertTrue(window.wire_check.isChecked())
            self.assertFalse(window.viewport._show_sen)
            self.assertTrue(window.viewport._show_wire_overlay)
        finally:
            window.close()

    def test_snapshot_hides_and_restores_sen2_and_wire_overlay(self):
        viewport = AssetViewport()
        viewport.set_show_sen(True)
        viewport.set_wire_overlay(True)

        viewport.begin_snapshot_mode()
        state = viewport._snapshot_saved_state
        self.assertIsNotNone(state)
        self.assertNotIn("show_owner_bbox", state)
        self.assertFalse(viewport._show_sen)
        self.assertFalse(viewport._show_wire_overlay)

        viewport.set_snapshot_guides_visible(True)
        self.assertTrue(viewport._show_sen)
        self.assertTrue(viewport._show_wire_overlay)
        viewport.set_snapshot_guides_visible(False)
        self.assertFalse(viewport._show_sen)
        self.assertFalse(viewport._show_wire_overlay)

        viewport.end_snapshot_mode()
        self.assertTrue(viewport._show_sen)
        self.assertTrue(viewport._show_wire_overlay)
        viewport.close()

    def test_snapshot_saved_state_contains_no_owner_bounds_setting(self):
        viewport = AssetViewport()
        viewport.begin_snapshot_mode()
        try:
            state = viewport._snapshot_saved_state
            self.assertIsNotNone(state)
            self.assertEqual(set(state), {
                "camera", "mode", "show_sen", "show_axes", "show_grid",
                "show_wire_overlay", "mapping_diagnostics",
                "show_diag_overlay",
            })
        finally:
            viewport.end_snapshot_mode()
            viewport.close()

    def test_snapshot_guides_restore_sen_and_wire_preferences(self):
        viewport = AssetViewport()
        viewport.set_show_sen(False)
        viewport.set_wire_overlay(True)
        viewport.begin_snapshot_mode()
        try:
            viewport.set_snapshot_guides_visible(True)
            self.assertFalse(viewport._show_sen)
            self.assertTrue(viewport._show_wire_overlay)
            viewport.set_snapshot_guides_visible(False)
            self.assertFalse(viewport._show_sen)
            self.assertFalse(viewport._show_wire_overlay)
        finally:
            viewport.end_snapshot_mode()
            self.assertFalse(viewport._show_sen)
            self.assertTrue(viewport._show_wire_overlay)
            viewport.close()

    def test_existing_additive_multi_selection_semantics_are_unchanged(self):
        window = AssemblyWindow()
        try:
            window._on_polygon_picked(1, False)
            window._on_polygon_picked(2, True)
            self.assertEqual(window._selected_polys, {1, 2})
            window._on_polygon_picked(1, True)
            self.assertEqual(window._selected_polys, {2})
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
