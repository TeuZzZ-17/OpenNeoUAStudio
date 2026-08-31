import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabWidget

from snapshot_studio import SnapshotStudioWindow


class SnapshotStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_bas_archive_uses_shared_action(self):
        window = SnapshotStudioWindow()
        try:
            labels = [
                action.text() for action in window.file_menu.actions()
                if not action.isSeparator()]
            self.assertIn("Close Current Resource", labels)
            self.assertFalse(window.close_bas_archive_action.isEnabled())
        finally:
            window.close()

    def test_workspace_has_only_snapshot_and_bas_manager_tabs(self):
        window = SnapshotStudioWindow()
        try:
            labels = [
                window._right_tabs.tabText(index)
                for index in range(window._right_tabs.count())
            ]
            self.assertEqual(labels, ["Snapshot", "BAS Manager"])
            self.assertIs(
                window._right_tabs.widget(0), window._snapshot_scroll)
            self.assertIs(window._right_tabs.widget(1), window._bas_panel)
            self.assertFalse(
                isinstance(window._right_tabs.widget(0), QTabWidget))
            self.assertFalse(
                isinstance(window._right_tabs.widget(1), QTabWidget))
        finally:
            window.close()

    def test_workspace_remains_snapshot_view_only_on_both_tabs(self):
        window = SnapshotStudioWindow()
        try:
            self.assertTrue(window._snapshot_mode_active)
            self.assertFalse(window.viewport.is_edit_mode)
            self.assertFalse(window.edit_toggle_action.isEnabled())
            self.assertFalse(window.edit_toggle_action.isVisible())
            self.assertEqual(window.edit_toggle_action.shortcuts(), [])
            self.assertFalse(window.edit_menu.menuAction().isVisible())

            window._right_tabs.setCurrentWidget(window._bas_panel)
            self.app.processEvents()
            self.assertTrue(window._snapshot_mode_active)
            self.assertFalse(window.viewport.is_edit_mode)
            self.assertFalse(window._editing_allowed())

            window.edit_toggle_action.setChecked(True)
            self.app.processEvents()
            self.assertFalse(window.edit_toggle_action.isChecked())
            self.assertFalse(window.viewport.is_edit_mode)
        finally:
            window.close()

    def test_viewport_context_menu_contains_no_edit_commands(self):
        window = SnapshotStudioWindow()
        try:
            menu = window._create_viewport_context_menu()
            self.assertEqual(
                [action.text() for action in menu.actions()],
                ["Reset camera"],
            )
        finally:
            window.close()

    def test_tools_menu_hides_other_workspace_launchers(self):
        window = SnapshotStudioWindow()
        try:
            self.assertFalse(window.wireframe_editor_action.isVisible())
            self.assertFalse(window.collision_editor_action.isVisible())
            self.assertFalse(window.map_editor_action.isVisible())
            self.assertFalse(window.mapping_repair_action.isVisible())
            self.assertFalse(window.integrated_editors_separator.isVisible())
        finally:
            window.close()

    def test_view_menu_reuses_shared_actions_with_snapshot_defaults(self):
        window = SnapshotStudioWindow()
        try:
            shared_actions = (
                window.sen_check, window.wire_check, window.cull_check,
                window.axes_check, window.grid_check, window.overlay_check,
                window.mapping_diag_check, window.retail_distance_fade_check,
            )
            self.assertTrue(all(action.isVisible() for action in shared_actions))
            self.assertTrue(window.cull_check.isChecked())
            for action in shared_actions:
                if action is not window.cull_check:
                    self.assertFalse(action.isChecked())
            self.assertTrue(window.reset_camera_action.isVisible())
        finally:
            window.close()

    def test_snapshot_controls_have_no_duplicate_guides_button(self):
        window = SnapshotStudioWindow()
        try:
            self.assertFalse(hasattr(window, "snapshot_guides_button"))
            self.assertEqual(window.snapshot_previous_frame_button.text(),
                             "<< Previous Frame")
            self.assertEqual(window.snapshot_next_frame_button.text(),
                             "Next Frame >>")
            self.assertTrue(hasattr(window, "snapshot_figurine_button"))
            self.assertTrue(hasattr(window, "snapshot_opacity_spin"))
        finally:
            window.close()

    def test_shared_view_actions_enable_snapshot_overlay_rendering(self):
        window = SnapshotStudioWindow()
        try:
            self.assertFalse(window.viewport._snapshot_show_guides)
            window.axes_check.setChecked(True)
            self.app.processEvents()
            self.assertTrue(window.viewport._snapshot_show_guides)
            self.assertTrue(window.viewport._show_axes)
            window.axes_check.setChecked(False)
            self.app.processEvents()
            self.assertFalse(window.viewport._snapshot_show_guides)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
