import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from model_editor import ModelEditorWindow


class ModelEditorWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_editor_tab_precedes_resources_tab(self):
        window = ModelEditorWindow()
        try:
            labels = [
                window._right_tabs.tabText(index)
                for index in range(window._right_tabs.count())
            ]
            self.assertEqual(labels, ["Editor", "Resources"])
            self.assertIs(window._right_tabs.widget(0), window._editor_tabs)
            self.assertIs(window._right_tabs.widget(1), window._resources_tabs)
        finally:
            window.close()

    def test_tools_menu_keeps_mapping_repair_only_for_integrated_editors(self):
        window = ModelEditorWindow()
        try:
            self.assertFalse(window.wireframe_editor_action.isVisible())
            self.assertFalse(window.collision_editor_action.isVisible())
            self.assertFalse(window.map_editor_action.isVisible())
            self.assertTrue(window.mapping_repair_action.isVisible())
            self.assertTrue(window.integrated_editors_separator.isVisible())
        finally:
            window.close()

    def test_poly_id_uses_group_box_like_transform_and_keeps_controls_compact(self):
        window = ModelEditorWindow()
        try:
            self.assertEqual(window.poly_id_box.title(), "Poly ID")
            self.assertIs(window.poly_id_box.layout(), window.poly_id_row_layout)
            self.assertIs(
                window.poly_id_row_layout.itemAt(0).widget(),
                window.poly_id_spin)
            self.assertEqual(window.poly_id_row_layout.stretch(0), 0)
            self.assertEqual(window.poly_id_previous_button.width(), 36)
            self.assertEqual(window.poly_id_next_button.width(), 36)
            self.assertGreaterEqual(window.poly_id_spin.minimumWidth(), 72)
            self.assertLessEqual(window.poly_id_spin.maximumWidth(), 96)
            for button in (
                    window.poly_select_all_button,
                    window.poly_deselect_all_button):
                required = (
                    button.fontMetrics().horizontalAdvance(button.text()) + 18)
                self.assertGreaterEqual(button.minimumWidth(), required)
        finally:
            window.close()



if __name__ == "__main__":
    unittest.main()
