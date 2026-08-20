import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from startup_selector import TOOL_OPTIONS, StartupToolSelector


class StartupToolSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_selector_exposes_the_five_startup_workspaces(self):
        self.assertEqual(
            [option.key for option in TOOL_OPTIONS],
            [
                "model_editor",
                "snapshot_studio",
                "map_editor",
                "collision_editor",
                "wireframe_editor",
            ],
        )
        self.assertNotIn(
            "mapping_repair",
            [option.key for option in TOOL_OPTIONS],
        )
        self.assertTrue(all(option.description for option in TOOL_OPTIONS))

    def test_model_editor_is_selected_by_default(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.tool_list.count(), 5)
        self.assertEqual(dialog.selected_tool(), "model_editor")
        self.assertEqual(dialog.windowTitle(), "OpenNeoUA Studio - Select Tool")

    def test_selection_returns_the_requested_tool_key(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        dialog.tool_list.setCurrentRow(3)
        self.assertEqual(dialog.selected_tool(), "collision_editor")
        dialog.tool_list.setCurrentRow(4)
        self.assertEqual(dialog.selected_tool(), "wireframe_editor")

    def test_tool_card_is_clickable(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        item = dialog.tool_list.item(1)
        card = dialog.tool_list.itemWidget(item)
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")

    def test_all_workspace_cards_fit_without_vertical_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        self.assertEqual(dialog.tool_list.verticalScrollBar().maximum(), 0)
        self.assertEqual(
            dialog.tool_list.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_workspace_list_ignores_wheel_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        class FakeWheelEvent:
            def __init__(self):
                self.accepted = False

            def accept(self):
                self.accepted = True

        event = FakeWheelEvent()
        dialog.tool_list.wheelEvent(event)
        self.assertTrue(event.accepted)


if __name__ == "__main__":
    unittest.main()
