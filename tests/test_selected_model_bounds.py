import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport, ViewFace, ViewMaterial
from assembly_window import AssemblyWindow


class SelectedModelBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _render(viewport):
        image = QImage(160, 120, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        viewport._render_scene(
            painter, QRectF(0, 0, 160, 120), QColor(0, 0, 0), False,
            viewport._camera_state())
        painter.end()

    def _viewport_with_selected_owner(self):
        viewport = AssetViewport()
        viewport.resize(160, 120)
        viewport._backface_cull = False
        viewport._materials = [ViewMaterial("test")]
        viewport._faces = [ViewFace(
            vertices=[(-1.0, 0.0, 0.0), (0.0, -1.0, 0.0),
                      (1.0, 0.0, 0.0)],
            uvs=[], material=0, poly_id=0, owner="root")]
        viewport._owner_bounds["root"] = (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
        viewport._selected_owner = "root"
        return viewport

    def test_menu_action_is_disabled_by_default(self):
        window = AssemblyWindow()
        self.assertTrue(window.owner_bounds_check.isCheckable())
        self.assertFalse(window.owner_bounds_check.isChecked())
        self.assertIs(window.copy_geometry_action.parent(), window.viewport)
        self.assertEqual(
            window.copy_geometry_action.shortcutContext(),
            Qt.ShortcutContext.WidgetShortcut)
        self.assertIs(window.paste_geometry_action.parent(), window.viewport)
        self.assertEqual(
            window.paste_geometry_action.shortcutContext(),
            Qt.ShortcutContext.WidgetShortcut)
        window.close()

    def test_view_action_order_and_overlay_defaults_are_exact(self):
        window = AssemblyWindow()
        labels = [
            action.text() for action in window.view_menu.actions()
            if not action.isSeparator()
        ][:8]
        self.assertEqual(labels, [
            "SEN2 volume",
            "Selected Model Bounds",
            "Wire overlay",
            "Backface cull",
            "Axes",
            "Grid",
            "Preview issues overlay",
            "Mapping diagnostics",
        ])
        self.assertFalse(window.sen_check.isChecked())
        self.assertFalse(window.owner_bounds_check.isChecked())
        self.assertFalse(window.wire_check.isChecked())
        self.assertTrue(window.cull_check.isChecked())
        self.assertTrue(window.axes_check.isChecked())
        self.assertTrue(window.grid_check.isChecked())
        self.assertTrue(window.overlay_check.isChecked())
        self.assertTrue(window.mapping_diag_check.isChecked())
        self.assertFalse(window.viewport._show_sen)
        self.assertFalse(window.viewport._show_owner_bbox)
        self.assertFalse(window.viewport._show_wire_overlay)
        self.assertTrue(window.viewport._backface_cull)
        self.assertTrue(window.viewport._show_axes)
        self.assertTrue(window.viewport._show_grid)
        self.assertTrue(window.viewport._show_diag_overlay)
        self.assertTrue(window.viewport._mapping_diagnostics)
        window.close()

    def test_snapshot_saves_hides_and_restores_three_new_view_states(self):
        viewport = self._viewport_with_selected_owner()
        viewport.set_show_sen(True)
        viewport.set_show_owner_bbox(False)
        viewport.set_wire_overlay(True)
        viewport.begin_snapshot_mode()
        self.assertFalse(viewport._show_sen)
        self.assertFalse(viewport._show_owner_bbox)
        self.assertFalse(viewport._show_wire_overlay)
        viewport.set_snapshot_guides_visible(True)
        self.assertTrue(viewport._show_sen)
        self.assertFalse(viewport._show_owner_bbox)
        self.assertTrue(viewport._show_wire_overlay)
        viewport.set_snapshot_guides_visible(False)
        self.assertFalse(viewport._show_sen)
        self.assertFalse(viewport._show_wire_overlay)
        viewport.end_snapshot_mode()
        self.assertTrue(viewport._show_sen)
        self.assertFalse(viewport._show_owner_bbox)
        self.assertTrue(viewport._show_wire_overlay)

    def test_existing_additive_multi_selection_semantics_are_unchanged(self):
        window = AssemblyWindow()
        window._on_polygon_picked(1, False)
        window._on_polygon_picked(2, True)
        self.assertEqual(window._selected_polys, {1, 2})
        window._on_polygon_picked(1, True)
        self.assertEqual(window._selected_polys, {2})
        window.close()

    def test_toggle_controls_only_owner_bbox_draw_call(self):
        viewport = self._viewport_with_selected_owner()
        calls = []
        viewport._draw_owner_bbox = lambda *args: calls.append(args)
        self._render(viewport)
        self.assertEqual(len(calls), 0)
        pick_count = len(viewport._pick_shapes)

        viewport.set_show_owner_bbox(False)
        self._render(viewport)
        self.assertEqual(len(calls), 0)
        self.assertEqual(len(viewport._pick_shapes), pick_count)

        viewport.set_show_owner_bbox(True)
        self._render(viewport)
        self.assertEqual(len(calls), 1)

    def test_snapshot_guides_respect_and_restore_preference(self):
        viewport = self._viewport_with_selected_owner()
        viewport.set_show_owner_bbox(False)
        viewport.begin_snapshot_mode()
        viewport.set_snapshot_guides_visible(True)
        self.assertFalse(viewport._show_owner_bbox)
        viewport.end_snapshot_mode()
        self.assertFalse(viewport._show_owner_bbox)

        viewport.set_show_owner_bbox(True)
        viewport.begin_snapshot_mode()
        self.assertFalse(viewport._show_owner_bbox)
        viewport.set_snapshot_guides_visible(True)
        self.assertTrue(viewport._show_owner_bbox)
        viewport.end_snapshot_mode()
        self.assertTrue(viewport._show_owner_bbox)


if __name__ == "__main__":
    unittest.main()
