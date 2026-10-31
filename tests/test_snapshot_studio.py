import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from studio.ui.cursor_themes import cursor_kind
from PySide6.QtWidgets import QApplication, QTabWidget

from snapshot_studio import SnapshotStudioWindow


class SnapshotStudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_custom_background_is_opaque_and_snapshot_cursor_is_a_hand(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor
        from PySide6.QtTest import QTest

        window = SnapshotStudioWindow()
        try:
            window._snapshot_custom_color = QColor(100, 20, 30, 74)
            self.assertEqual(window._snapshot_background().alpha(), 255)
            window.viewport.begin_snapshot_mode(window._snapshot_background())
            self.assertEqual(cursor_kind(window.viewport.cursor()), "open")
            QTest.mousePress(window.viewport, Qt.MouseButton.LeftButton)
            self.assertEqual(cursor_kind(window.viewport.cursor()), "closed")
            QTest.mouseRelease(window.viewport, Qt.MouseButton.LeftButton)
            self.assertEqual(cursor_kind(window.viewport.cursor()), "open")
            window._clear_snapshot_color()
            self.assertIsNone(window._snapshot_background())
        finally:
            window.close()

    def test_compatibility_dialog_clears_menu_selection(self):
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtGui import QCursor
        from PySide6.QtTest import QTest
        from studio.ui import shell

        window = SnapshotStudioWindow()
        try:
            shell.install_workspace_shell(window, "snapshot_studio")
            # The checker is the last menu: keep the bar wide enough to show it.
            window.resize(1800, 700)
            window.show()
            self.app.processEvents()
            menu = window.menuBar()
            action = window.studio_compat_action
            opened = []

            def close_dialog():
                dialog = self.app.activeModalWidget()
                opened.append(isinstance(dialog, shell.CompatibilityDialog))
                QCursor.setPos(window.viewport.mapToGlobal(window.viewport.rect().center()))
                QTest.mouseRelease(dialog, Qt.MouseButton.LeftButton)
                dialog.close()

            QTimer.singleShot(50, close_dialog)
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                             pos=menu.actionGeometry(action).center())
            self.assertEqual(opened, [])
            self.app.processEvents()
            self.assertEqual(opened, [True])
            self.assertIsNone(menu.activeAction())
            self.assertFalse(menu.hasFocus())
        finally:
            window.close()

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

    def test_workspace_has_snapshot_bas_manager_and_entity_list_tabs(self):
        window = SnapshotStudioWindow()
        try:
            labels = [
                window._right_tabs.tabText(index)
                for index in range(window._right_tabs.count())
            ]
            self.assertEqual(labels, ["Snapshot", "BAS Manager", "Entity List"])
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
                ["Reset Camera"],
            )
        finally:
            window.close()

    def test_tools_menu_hides_other_workspace_launchers(self):
        window = SnapshotStudioWindow()
        try:
            self.assertFalse(window.wireframe_editor_action.isVisible())
            self.assertFalse(window.entity_designer_action.isVisible())
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
            self.assertFalse(hasattr(window, "snapshot_opacity_spin"))
            self.assertEqual(window.snapshot_clear_color_button.text(), "Remove Color")
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
