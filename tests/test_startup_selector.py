import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QSizePolicy

from startup_selector import TOOL_OPTIONS, StartupToolSelector, _ToolCard


class StartupToolSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Resource validation has its own tests; these check tool navigation.
        validation = patch("studio_resources.ResourcesPanel.apply", return_value=object())
        validation.start()
        self.addCleanup(validation.stop)

    def test_selector_exposes_the_six_startup_workspaces(self):
        self.assertEqual(
            [option.key for option in TOOL_OPTIONS],
            [
                "model_editor",
                "snapshot_studio",
                "map_editor",
                "entity_designer",
                "wireframe_editor",
                "sector_editor",
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

        self.assertEqual(dialog.tool_list.count(), 6)
        self.assertEqual(dialog.selected_tool(), "model_editor")
        self.assertEqual(dialog.windowTitle(), "OpenNeoUA Studio - Select Tool")

    def test_selection_returns_the_requested_tool_key(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        dialog.tool_list.set_current_row(3)
        self.assertEqual(dialog.selected_tool(), "entity_designer")
        dialog.tool_list.set_current_row(4)
        self.assertEqual(dialog.selected_tool(), "wireframe_editor")

    def test_tool_card_is_clickable(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        cards = dialog.tool_list.findChildren(_ToolCard)
        self.assertEqual(len(cards), 6)
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

    def test_wheel_over_card_and_panel_moves_selection_without_scrolling(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        self.assertNotIsInstance(dialog.tool_list, QAbstractScrollArea)
        self.assertEqual(
            dialog.tool_list.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Fixed)

        cards = dialog.tool_list.findChildren(_ToolCard)
        self._send_wheel(cards[0], -120)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")
        self._send_wheel(dialog.tool_list, 120)
        self.assertEqual(dialog.selected_tool(), "model_editor")
        self._send_wheel(dialog.tool_list, 120)
        self.assertEqual(dialog.selected_tool(), "model_editor")
        self._send_wheel(dialog.tool_list, -120)
        for _ in range(dialog.tool_list.count()):
            self._send_wheel(dialog.tool_list, -120)
        self.assertEqual(dialog.selected_tool(), "sector_editor")

    def test_w_s_navigate_with_focus_on_cards_and_buttons(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        cards = dialog.tool_list.findChildren(_ToolCard)
        cards[0].setFocus()
        QTest.keyClick(cards[0], Qt.Key.Key_S)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")

        dialog.open_button.setFocus()
        QTest.keyClick(dialog.open_button, Qt.Key.Key_S)
        self.assertEqual(dialog.selected_tool(), "map_editor")
        QTest.keyClick(dialog.open_button, Qt.Key.Key_W)
        self.assertEqual(dialog.selected_tool(), "snapshot_studio")

    def test_enter_opens_the_wheel_selected_workspace_not_the_focused_card(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()

        cards = dialog.tool_list.findChildren(_ToolCard)
        cards[0].setFocus()
        for _ in range(3):
            self._send_wheel(cards[0], -120)
        self.assertEqual(dialog.selected_tool(), "entity_designer")

        QTest.keyClick(cards[0], Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        self.assertEqual(dialog.selected_tool(), "entity_designer")

    def test_embedded_resources_box_has_the_reset_button(self):
        dialog = StartupToolSelector()
        self.addCleanup(dialog.close)

        self.assertEqual(dialog.resources_panel.reset_button.text(), "Reset Configuration")
        self.assertEqual(
            sorted(dialog.resources_panel.edits),
            sorted(["briefings", "skies", "music", "movies", "sets",
                    "scripts", "levels", "models", "interface"]))

    @staticmethod
    def _send_wheel(widget, delta):
        position = QPointF(widget.rect().center())
        global_position = QPointF(widget.mapToGlobal(widget.rect().center()))
        event = QWheelEvent(
            position, global_position, QPoint(), QPoint(0, delta),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(widget, event)


if __name__ == "__main__":
    unittest.main()
