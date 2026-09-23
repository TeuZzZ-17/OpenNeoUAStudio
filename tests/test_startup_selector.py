import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QSizePolicy

from startup_selector import TOOL_OPTIONS, StartupToolSelector, _ToolCard


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

        dialog.tool_list.set_current_row(3)
        self.assertEqual(dialog.selected_tool(), "collision_editor")
        dialog.tool_list.set_current_row(4)
        self.assertEqual(dialog.selected_tool(), "wireframe_editor")

    def test_tool_card_is_clickable(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        cards = dialog.tool_list.findChildren(_ToolCard)
        self.assertEqual(len(cards), 5)
        QTest.mouseClick(cards[1], Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")

    def test_all_workspace_cards_fit_without_vertical_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        panel = dialog.tool_list
        cards = panel.findChildren(_ToolCard)
        # The panel height is fixed around its cards, so every workspace is
        # visible at once and can never overflow the available space.
        self.assertEqual(
            panel.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)
        self.assertGreaterEqual(
            panel.height(), sum(card.height() for card in cards))

    def test_workspace_list_ignores_wheel_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        # The panel has no scrolling machinery at all: no scroll area and no
        # scrollbar, so the wheel can never move the workspace cards.
        self.assertNotIsInstance(dialog.tool_list, QAbstractScrollArea)
        self.assertEqual(
            dialog.tool_list.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Fixed)


if __name__ == "__main__":
    unittest.main()
