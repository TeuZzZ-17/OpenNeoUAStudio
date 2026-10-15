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

    def test_resources_pages_are_flat_palette_buttons(self):
        window = ModelEditorWindow()
        try:
            window.resize(1400, 900)
            window.show()
            strip = window._right_tabs._palette_strip
            strip.refresh()
            self.app.processEvents()
            # One flat level of palette buttons: no sub-tab bars.
            self.assertEqual(
                [button.text() for button in strip.buttons()],
                ["Model and Texture Editor", "Bas Manager", "Entity List",
                 "Asset Dependencies", "Asset Textures"])
            self.assertTrue(window._right_tabs.tabBar().isHidden())
            self.assertTrue(window._resources_tabs.tabBar().isHidden())
            strip.buttons()[2].click()
            self.assertIs(window._right_tabs.currentWidget(), window._resources_tabs)
            self.assertEqual(
                window._resources_tabs.tabText(
                    window._resources_tabs.currentIndex()), "Entity List")
        finally:
            window.close()

    def test_set_is_chosen_from_the_menu_not_from_a_visible_selector(self):
        window = ModelEditorWindow()
        try:
            window.resize(1400, 900)
            window.show()
            window._right_tabs.setCurrentIndex(1)
            self.app.processEvents()
            # The combo stays as the hidden holder of the current set.
            self.assertIs(window._set_combo, window.entity_browser.set_combo)
            self.assertFalse(window._set_combo.isVisible())
            self.assertFalse(hasattr(window._resources_tabs, "set_combo"))
        finally:
            window.close()

    def test_tools_menu_keeps_mapping_repair_only_for_integrated_editors(self):
        window = ModelEditorWindow()
        try:
            self.assertFalse(window.wireframe_editor_action.isVisible())
            self.assertFalse(window.entity_designer_action.isVisible())
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
            layout = window.poly_id_row_layout
            # Centered row: stretch, "<", value, ">", stretch.
            self.assertIs(layout.itemAt(1).widget(),
                          window.poly_id_previous_button)
            self.assertIs(layout.itemAt(2).widget(), window.poly_id_spin)
            self.assertIs(layout.itemAt(3).widget(),
                          window.poly_id_next_button)
            self.assertIsNone(layout.itemAt(0).widget())
            self.assertIsNone(layout.itemAt(4).widget())
            self.assertEqual(layout.stretch(1), 0)
            self.assertEqual(window.poly_id_previous_button.width(), 36)
            self.assertEqual(window.poly_id_next_button.width(), 36)
            self.assertGreaterEqual(window.poly_id_spin.minimumWidth(), 72)
            self.assertLessEqual(window.poly_id_spin.maximumWidth(), 96)
        finally:
            window.close()



if __name__ == "__main__":
    unittest.main()
