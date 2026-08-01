import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

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


if __name__ == "__main__":
    unittest.main()
