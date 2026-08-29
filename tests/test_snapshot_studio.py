import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabWidget

from snapshot_studio import SnapshotStudioWindow


class SnapshotStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_workspace_has_only_snapshot_and_bas_manager_tabs(self):
        window = SnapshotStudioWindow()
        try:
            labels = [
                window._right_tabs.tabText(index)
                for index in range(window._right_tabs.count())
            ]
            self.assertEqual(labels, ["Snapshot", "BAS Manager"])
            self.assertIs(
                window._right_tabs.widget(0), window._snapshot_panel)
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

    def test_view_menu_hides_unavailable_snapshot_filters(self):
        window = SnapshotStudioWindow()
        try:
            for action in (
                    window.sen_check,
                    window.wire_check,
                    window.axes_check,
                    window.grid_check,
                    window.overlay_check,
                    window.mapping_diag_check):
                self.assertFalse(action.isVisible())
            self.assertTrue(window.cull_check.isVisible())
            self.assertTrue(window.reset_camera_action.isVisible())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
