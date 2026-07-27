import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QHeaderView, QMessageBox, QTreeWidgetItem,
)

import assembly_window as assembly_window_module
from assembly_viewer import VIEW_MODES
from assembly_window import AssemblyWindow
from vp_manager import VPManager, parse_visproto_text


class _FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback


class _FakeAction:
    def __init__(self, text="", separator=False):
        self._text = text
        self._separator = separator
        self.enabled = True
        self.triggered = _FakeSignal()

    def text(self):
        return self._text

    def isSeparator(self):
        return self._separator

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _FakeMenu:
    def __init__(self):
        self._actions = []

    def addAction(self, text, callback=None):
        action = _FakeAction(text)
        if callback is not None:
            action.triggered.connect(callback)
        self._actions.append(action)
        return action

    def addSeparator(self):
        self._actions.append(_FakeAction(separator=True))

    def actions(self):
        return list(self._actions)

    def exec(self, *_args, **_kwargs):
        return None


class WindowContractV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_compact_startup_ui_contract(self):
        window = AssemblyWindow()
        try:
            window.show()
            for _ in range(3):
                self.app.processEvents()
                QTest.qWait(5)

            self.assertFalse(window._diagnostics_dock.isVisible())
            self.assertEqual(
                [window._diagnostics_tabs.tabText(index)
                 for index in range(window._diagnostics_tabs.count())],
                ["Warnings", "Validation", "Log"])
            window._show_diagnostics(0)
            for _ in range(2):
                self.app.processEvents()
                QTest.qWait(5)
            self.assertTrue(window._diagnostics_dock.isVisible())
            diagnostic_height = window._diagnostics_splitter.sizes()[-1]
            self.assertGreaterEqual(diagnostic_height, 70)
            self.assertLessEqual(diagnostic_height, 90)
            window._hide_diagnostics()
            self.assertFalse(window._diagnostics_dock.isVisible())

            visible_modes = [
                window.mode_combo.itemText(index).casefold()
                for index in range(window.mode_combo.count())]
            self.assertNotIn("solid", visible_modes)
            self.assertIn("solid", VIEW_MODES)
            self.assertEqual(window.step_button.text(), ">>")
            self.assertTrue(window.auto_align_check.isChecked())
            self.assertEqual(window._resources_tabs.tabText(0), "Bas Manager")
            add_labels = [
                action.text() for action in window.add_menu.actions()]

            view_labels = [
                action.text() for action in window.view_menu.actions()
                if not action.isSeparator()]
            self.assertNotIn("Frame full family", view_labels)
            self.assertNotIn("Navigation help", view_labels)
            tool_labels = []
            for menu_action in window.menuBar().actions():
                menu = menu_action.menu()
                if menu is None:
                    continue
                tool_labels.extend(
                    child.text() for child in menu.actions()
                    if not child.isSeparator())
            self.assertNotIn("Go to polyID...", tool_labels)
            self.assertEqual(add_labels, [
                "Triangle", "Square", "Rectangle...", "Circle / Disc...",
                "Plane", "Cube / Box...", "Pyramid...", "Cylinder..."])
        finally:
            window.close()

    def test_resource_columns_are_user_resizable(self):
        window = AssemblyWindow()
        try:
            for tree, count in (
                    (window.setbas_tree, 3),
                    (window.asset_tree, 2),
                    (window.resolve_tree, 3)):
                header = tree.header()
                for column in range(count):
                    self.assertEqual(
                        header.sectionResizeMode(column),
                        QHeaderView.ResizeMode.Interactive)
                self.assertEqual(
                    tree.horizontalScrollBarPolicy(),
                    Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.assertGreater(
                window.setbas_tree.columnWidth(0),
                window.setbas_tree.columnWidth(2))
            self.assertGreater(
                window.asset_tree.columnWidth(0),
                window.asset_tree.columnWidth(1))
            self.assertEqual(window.setbas_tree.columnWidth(0), 285)
            self.assertEqual(window.setbas_tree.columnWidth(1), 105)
            self.assertEqual(window.setbas_tree.columnWidth(2), 55)
            self.assertEqual(window.asset_tree.columnWidth(0), 300)
            self.assertEqual(window.asset_tree.columnWidth(1), 105)
            self.assertEqual(window.resolve_tree.columnWidth(0), 260)
            self.assertEqual(window.resolve_tree.columnWidth(1), 70)
            self.assertEqual(window.resolve_tree.columnWidth(2), 165)
        finally:
            window.close()

    def test_viewport_context_menu_contains_reset_camera(self):
        window = AssemblyWindow()
        try:
            actions = [
                action for action in
                window._create_viewport_context_menu().actions()
                if not action.isSeparator()]
            labels = [action.text() for action in actions]
            self.assertIn("Reset camera", labels)
            reset_camera = next(
                action for action in actions
                if action.text() == "Reset camera")
            self.assertFalse(reset_camera.isEnabled())
            self.assertFalse(window.reset_camera_action.isEnabled())
            deselect = next(
                action for action in actions
                if action.text() == "Deselect")
            self.assertFalse(deselect.isEnabled())

            window._selected_polys = {7}
            selected_actions = [
                action for action in
                window._create_viewport_context_menu().actions()
                if not action.isSeparator()]
            selected_deselect = next(
                action for action in selected_actions
                if action.text() == "Deselect")
            self.assertTrue(selected_deselect.isEnabled())
        finally:
            window.close()

    def test_reset_camera_only_enables_for_loaded_displaced_view(self):
        window = AssemblyWindow()
        try:
            self.assertFalse(window.viewport.has_loaded_resource)
            self.assertTrue(window.viewport.camera_is_reset)
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            window.viewport._family_ref = object()
            window._update_reset_camera_action()
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())

            window.viewport._yaw += 5.0
            window._on_manual_camera_changed()
            self.assertTrue(window.viewport.can_reset_camera)
            self.assertTrue(window.reset_camera_action.isEnabled())

            actions = [
                action for action in
                window._create_viewport_context_menu().actions()
                if not action.isSeparator()]
            reset_camera = next(
                action for action in actions
                if action.text() == "Reset camera")
            self.assertTrue(reset_camera.isEnabled())

            window._reset_view_and_gizmo()
            self.assertTrue(window.viewport.camera_is_reset)
            self.assertFalse(window.viewport.can_reset_camera)
            self.assertFalse(window.reset_camera_action.isEnabled())
        finally:
            window.close()

    def test_setbas_animation_single_click_routes_to_preview(self):
        window = AssemblyWindow()
        try:
            resource = SimpleNamespace(
                class_id="bmpanim.class", resource_name="PROP1.ANM",
                error="")
            window._setbas = SimpleNamespace(resources=[resource])
            item = QTreeWidgetItem(["PROP1.ANM", "VANM", ""])
            item.setData(0, assembly_window_module._BAS_KIND_ROLE,
                         "bmpanim.class")
            item.setData(0, Qt.ItemDataRole.UserRole, 0)
            with patch.object(
                    window, "_preview_setbas_animation") as preview:
                window._on_setbas_item_selected(item)
            preview.assert_called_once_with(resource)
        finally:
            window.close()

    def test_setbas_animation_owner_matching_accepts_paths(self):
        window = AssemblyWindow()
        try:
            animation = SimpleNamespace(
                kind="bmpanim", name="Effects/PROP1.ANM")
            block = SimpleNamespace(texture=animation, tracy_texture=None)
            base_object = SimpleNamespace(ades=[block])
            owner = SimpleNamespace(
                owner_path="root/kid[2]", base_object=base_object)
            family = SimpleNamespace(all_objects=lambda: [owner])
            self.assertEqual(
                window._animation_owners(family, "PROP1.ANM"),
                ["root/kid[2]"])
        finally:
            window.close()

    def test_poly_id_enter_confirms_current_value(self):
        window = AssemblyWindow()
        try:
            window._mapping_index = SimpleNamespace(poly_count=4)
            window._sync_poly_id_control()
            selected = []
            window._on_polygon_picked = (
                lambda poly_id, additive=False:
                selected.append((poly_id, additive)))

            window.poly_id_spin.editingFinished.emit()
            self.assertEqual(selected, [(0, False)])
            window.poly_id_spin.setValue(2)
            self.assertEqual(selected[-1], (2, False))
            self.assertEqual(window.poly_id_spin.maximum(), 3)
        finally:
            window.close()

    def test_asset_texture_click_routes_only_on_double_click_and_menu_is_clean(
            self):
        window = AssemblyWindow()
        try:
            window._family = SimpleNamespace(
                texture_refs={}, textures={}, dependencies=[],
                animations={}, external_palette=None,
                setbas_archive=None)
            window._set_object_info = lambda _lines: None
            window._effective_status = lambda _name, status: status
            window._saved_choice_for = lambda _name: None
            item = QTreeWidgetItem(["TEST.ILBM", "found"])
            item.setData(
                0, Qt.ItemDataRole.UserRole, ("texture", "TEST.ILBM"))

            window._right_tabs.setCurrentWidget(window._resources_tabs)
            window._on_tree_node_selected(item)
            self.assertIs(
                window._right_tabs.currentWidget(), window._resources_tabs)
            window._on_tree_double_clicked(item)
            self.assertIs(
                window._right_tabs.currentWidget(), window._resources_tabs)

            window._prepare_context_item = (
                lambda _widget, _position: item)
            fake_menu = _FakeMenu()
            with patch.object(
                    assembly_window_module, "QMenu",
                    return_value=fake_menu):
                window._show_asset_context_menu(QPoint())
            captured = [
                action.text() for action in fake_menu.actions()
                if not action.isSeparator()]
            self.assertIn("Preview texture", captured)
            self.assertIn("Copy info", captured)
            self.assertNotIn("Copy item", captured)
            self.assertNotIn("Expand all", captured)
            self.assertNotIn("Collapse all", captured)
        finally:
            window.close()

    def test_texture_catalog_populates_the_real_visuals_widget(self):
        window = AssemblyWindow()
        try:
            reference = SimpleNamespace(
                status="found", path=None, source="fixture",
                found=True, display_path="fixture/SHIP.ILBM",
                candidates=[])
            family = SimpleNamespace(
                texture_refs={"SHIP.ILBM": reference},
                textures={},
                texture_tracy_usage={},
                external_palette=None,
                setbas_archive=None)
            window._fill_textures(family)
            self.assertEqual(window.texture_list.count(), 1)
            self.assertEqual(
                window.texture_list.item(0).data(
                    Qt.ItemDataRole.UserRole),
                "SHIP.ILBM")
            self.assertIn(
                "[FOUND] SHIP.ILBM",
                window.texture_list.item(0).text())
        finally:
            window.close()

    def test_archive_only_catalog_and_picker_exclude_missing_refs(self):
        window = AssemblyWindow()
        try:
            resource = SimpleNamespace(
                class_id="ilbm.class", resource_name="ARCHIVE.ILBM",
                decodable=True, error="", display_payload="ILBM")
            window._setbas = SimpleNamespace(resources=[resource])
            window._fill_textures(None)
            self.assertEqual(window.texture_list.count(), 1)
            self.assertEqual(
                window.texture_list.item(0).data(
                    Qt.ItemDataRole.UserRole),
                "ARCHIVE.ILBM")

            missing = SimpleNamespace(
                status="missing", path=None, source="", found=False,
                display_path="", candidates=[])
            loaded = SimpleNamespace(
                status="found", path=None, source="fixture", found=True,
                display_path="LOADED.ILBM", candidates=[])
            window._family = SimpleNamespace(
                texture_refs={
                    "MISSING.ILBM": missing,
                    "LOADED.ILBM": loaded,
                },
                textures={"LOADED.ILBM": object()},
                setbas_archive=window._setbas)
            self.assertEqual(
                window._available_model_textures(),
                ["ARCHIVE.ILBM", "LOADED.ILBM"])
        finally:
            window.close()

    def test_child_metadata_and_texture_owner_selection_are_preserved(self):
        window = AssemblyWindow()
        try:
            child = SimpleNamespace(
                owner_path="root/kid[0]",
                base_object=SimpleNamespace(skeleton_class="sklt.class"))
            child_item = QTreeWidgetItem(["Child BASE"])
            child_item.setData(
                0, Qt.ItemDataRole.UserRole, ("child", child))
            metadata = window._asset_item_search_metadata(child_item)
            self.assertIn("base.class", metadata)
            self.assertIn("sklt.class", metadata)

            texture_item = QTreeWidgetItem(["STONE.ILBM"])
            texture_item.setData(
                0, Qt.ItemDataRole.UserRole,
                ("texture", "STONE.ILBM"))
            child_item.addChild(texture_item)
            window._family = SimpleNamespace(
                texture_refs={}, textures={}, dependencies=[],
                animations={}, external_palette=None)
            window._set_object_info = lambda _lines: None
            window._effective_status = lambda _name, status: status
            window._saved_choice_for = lambda _name: None
            with patch.object(window, "_select_owner") as select_owner:
                window._on_tree_node_selected(texture_item)
            select_owner.assert_called_once_with(
                "root/kid[0]", preserve_asset_selection=True)
        finally:
            window.close()

    def test_load_texture_is_rejected_in_view_mode(self):
        window = AssemblyWindow()
        try:
            window._mapping_index = SimpleNamespace(poly_count=2)
            window._workbench_obj = object()
            window._selected_polys = {0, 1}
            window._selected_poly = 0
            with patch(
                    "assembly_window.classify_texture_assignment") as classify:
                window._load_model_texture()
            classify.assert_not_called()
            self.assertFalse(window._editing_allowed())
        finally:
            window.close()

    def test_unexported_vp_assignments_require_confirmation(self):
        window = AssemblyWindow()
        try:
            window._vp_manager = VPManager(parse_visproto_text(
                "ONE.base\nTWO.base\n>"))
            window._vp_manager.swap(0, 1)
            self.assertTrue(window._has_unsaved_vp_changes())
            window._skip_model_switch_warning = True
            with patch.object(
                    QMessageBox, "exec",
                    return_value=QMessageBox.StandardButton.No):
                self.assertFalse(window._confirm_discard_geometry())
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No):
                self.assertFalse(window._confirm_discard_vp_changes(
                    "Discard?"))
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes):
                self.assertTrue(window._confirm_discard_vp_changes(
                    "Discard?"))
            window._vp_manager.undo()
            self.assertFalse(window._has_unsaved_vp_changes())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
