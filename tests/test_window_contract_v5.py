import os
import unittest
from pathlib import Path
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
from vp_manager import parse_visproto_text


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
                ["Log", "Warnings", "Validation"])
            diagnostic_menu = next(
                action.menu() for action in window.menuBar().actions()
                if action.text().replace("&", "") == "Diagnostics")
            diagnostic_labels = [
                action.text() for action in diagnostic_menu.actions()
                if not action.isSeparator()]
            self.assertEqual(
                diagnostic_labels, ["Show Diagnostic Panel", "Clear Log"])
            window._show_diagnostics(0)
            self.assertEqual(
                window.diagnostics_toggle_action.text(),
                "Hide Diagnostic Panel")
            for _ in range(2):
                self.app.processEvents()
                QTest.qWait(5)
            self.assertTrue(window._diagnostics_dock.isVisible())
            diagnostic_height = window._diagnostics_splitter.sizes()[-1]
            self.assertGreaterEqual(diagnostic_height, 70)
            self.assertLessEqual(diagnostic_height, 90)
            window._hide_diagnostics()
            self.assertFalse(window._diagnostics_dock.isVisible())
            self.assertEqual(
                window.diagnostics_toggle_action.text(),
                "Show Diagnostic Panel")

            visible_modes = [
                window.mode_combo.itemText(index).casefold()
                for index in range(window.mode_combo.count())]
            self.assertNotIn("solid", visible_modes)
            self.assertIn("solid", VIEW_MODES)
            self.assertEqual(window.step_button.text(), ">>")
            self.assertTrue(window.auto_align_check.isChecked())
            self.assertEqual(window._resources_tabs.tabText(0), "Bas Manager")
            self.assertEqual(
                window._resources_tabs.tabText(1), "Asset Dependencies")
            self.assertFalse(hasattr(window, "global_edit_button"))
            toolbar_widgets = [
                action.defaultWidget()
                for action in window.animation_toolbar.actions()]
            speed_index = toolbar_widgets.index(window.speed_spin)
            self.assertEqual(
                toolbar_widgets[speed_index + 1:speed_index + 3],
                [window.global_undo_button, window.global_redo_button])
            self.assertEqual(
                [window._editor_tabs.tabText(index)
                 for index in range(window._editor_tabs.count())],
                ["Model and Texture Editor"])
            self.assertEqual(window.mapping_repair_action.text(),
                             "Mapping Repair...")
            self.assertNotIn(
                "Add",
                [action.text().replace("&", "")
                 for action in window.menuBar().actions()])

            view_labels = [
                action.text() for action in window.view_menu.actions()
                if not action.isSeparator()]
            self.assertNotIn("Frame full family", view_labels)
            self.assertNotIn("Navigation help", view_labels)
            self.assertIn("Vanilla distance fade", view_labels)
            self.assertFalse(window.retail_distance_fade_check.isChecked())
            tool_labels = []
            for menu_action in window.menuBar().actions():
                menu = menu_action.menu()
                if menu is None:
                    continue
                tool_labels.extend(
                    child.text() for child in menu.actions()
                    if not child.isSeparator())
            self.assertNotIn("Go to polyID...", tool_labels)
            self.assertEqual(window.global_undo_button.styleSheet(), "")
            self.assertEqual(window.global_redo_button.styleSheet(), "")
            self.assertFalse(hasattr(window, "mirror_x_check"))
            self.assertFalse(hasattr(window, "mirror_y_check"))
            self.assertFalse(hasattr(window, "mirror_z_check"))
            self.assertFalse(hasattr(window, "mirror_axis_checks"))
            self.assertEqual(window._mirror_axes(), (0, 1, 2))
            self.assertEqual(
                window.mirror_select_check.text(), "Mirror Select")
            self.assertFalse(hasattr(window, "mirror_copy_check"))
            self.assertFalse(hasattr(window, "mirror_delete_check"))
            self.assertFalse(hasattr(window, "completeness_label"))
            self.assertTrue(window.loaded_resource_label.font().bold())
            self.assertGreaterEqual(
                window.loaded_resource_label.font().pointSize(), 13)
            self.assertEqual(
                window.loaded_resource_label.text(), "No Resource Loaded")
            self.assertFalse(hasattr(window, "setbas_preview_button"))
            self.assertEqual(
                window.setbas_runtime_loose_button.text(),
                "Export Runtime Loose SET")
            self.assertEqual(
                window.export_runtime_loose_action.text(),
                "Export Runtime Loose SET")
            self.assertFalse(window.setbas_runtime_loose_button.isEnabled())
            self.assertFalse(window.export_runtime_loose_action.isEnabled())
            source = (Path(__file__).resolve().parents[1] /
                      "assembly_window.py").read_text(encoding="utf-8")
            self.assertNotIn("Source (always read-only):", source)
            self.assertNotIn("Dry run / validate only (write no files)", source)
            self.assertNotIn(
                "Replace different files at the exact managed output paths",
                source)
            self.assertIn(
                "Select the destination SetN folder. Loose/ is created ",
                source)
            self.assertIn("target_edit.setMinimumWidth(500)", source)
            self.assertNotIn("Please wait until completion.", source)
            self.assertNotIn("runtimeLooseWaitDialog", source)
            self.assertNotIn("QProgressDialog", source)
            self.assertFalse(hasattr(window, "import_vp_package_action"))
            self.assertEqual(
                window.save_asset_family_action.text(), "Export Asset Family")
            for removed_vp_control in (
                    "vp_import_button", "vp_new_spin", "vp_assign_button",
                    "vp_undo_button", "vp_redo_button", "vp_clear_button",
                    "vp_append_button", "vp_duplicate_button",
                    "vp_copy_button", "vp_paste_button",
                    "vp_remove_trailing_button", "vp_export_button"):
                self.assertFalse(hasattr(window, removed_vp_control))
            self.assertEqual(window.vp_selected_base_label.text(), "-")
            self.assertEqual(window.vp_current_label.text(), "-")
            self.assertEqual(
                window.model_save_as_button.text(),
                "Export Asset Family")
            self.assertEqual(window.material_copy_button.text(),
                             "Copy Material")
            self.assertEqual(window.material_paste_button.text(),
                             "Paste Material")
            self.assertEqual(window.material_add_button.text(),
                             "Add Compatible Slot")
            self.assertEqual(window.material_delete_button.text(),
                             "Delete Material")
            editor_tab_center = window._right_tabs.tabBar().mapToGlobal(
                window._right_tabs.tabBar().tabRect(1).center()).x()
            status_center = window.editor_status_panel.mapToGlobal(
                window.editor_status_panel.rect().center()).x()
            self.assertLessEqual(abs(editor_tab_center - status_center), 2)
            self.assertGreater(
                window.editor_status_panel.width(),
                window._right_tabs.tabBar().tabRect(1).width() * 2)
            window._right_tabs.setCurrentWidget(window._editor_tabs)
            self.app.processEvents()
        finally:
            window.close()

    def test_file_menu_contains_only_the_asset_workbench_actions(self):
        window = AssemblyWindow()
        try:
            labels = [
                action.text() for action in window.file_menu.actions()
                if not action.isSeparator()]
            self.assertEqual(
                labels, ["Import", "Export", "Close BAS Archive", "Exit"])
            self.assertFalse(window.close_bas_archive_action.isEnabled())
            self.assertEqual(
                [action.text() for action in window.file_import_menu.actions()],
                [
                    "Import BAS Archive", "Import BASE", "Import SKLT",
                    "Import ILBM",
                    "Import Asset Family",
                ],
            )
            self.assertEqual(
                [action.text() for action in window.file_export_menu.actions()],
                [
                    "Export Runtime Loose SET", "Export BASE",
                    "Export SKLT", "Export ILBM", "Export Asset Family",
                    "Overwrite",
                ],
            )
            self.assertEqual(window.open_base_action.shortcut().toString(), "")
            toolbar_texts = [
                action.text() for toolbar in window.findChildren(
                    assembly_window_module.QToolBar)
                for action in toolbar.actions() if action.text()]
            self.assertNotIn("Import BAS Archive", toolbar_texts)
            self.assertNotIn("Import Asset Family", toolbar_texts)
            self.assertFalse(any(
                "extra asset root" in label.casefold()
                or "report" in label.casefold()
                or label == "Reload"
                for label in labels))
        finally:
            window.close()

    def test_standalone_sklt_enables_only_relevant_exports(self):
        window = AssemblyWindow()
        try:
            model = SimpleNamespace(original_data=b"FORM")
            ref = SimpleNamespace(
                path=Path("C:/UA/complete.sklt"),
                status="manual", source="manual")
            obj = SimpleNamespace(skeleton=model, skeleton_ref=ref)
            family = SimpleNamespace(base_asset=None, textures={})
            window._family = family
            window._selected_owner = "root"
            window._owner_to_obj = {"root": obj}
            with patch.object(
                    window, "can_export_sklt", return_value=True), patch.object(
                    window, "_standalone_sklt_source", return_value=ref.path):
                window._sync_geometry_save_controls()

            self.assertTrue(window.save_sklt_action.isEnabled())
            self.assertTrue(window.overwrite_action.isEnabled())
            self.assertFalse(window.save_base_action.isEnabled())
            self.assertFalse(window.save_ilbm_action.isEnabled())
        finally:
            window.close()

    def test_import_asset_family_reuses_selected_directory_between_steps(self):
        window = AssemblyWindow()
        try:
            window._last_directory = Path("C:/start")
            selected_dir = Path("C:/UA/Assets/Family")
            single_results = [
                (str(selected_dir / "MODEL.BASE"), "BASE"),
                (str(selected_dir / "MODEL.SKLT"), "Skeletons"),
            ]
            multi_results = [
                ([str(selected_dir / "MODEL.ILBM")], "Textures"),
                ([str(selected_dir / "MODEL.ANM")], "Animations"),
            ]
            with patch.object(
                    assembly_window_module.QFileDialog, "getOpenFileName",
                    side_effect=single_results) as single_dialog, patch.object(
                    assembly_window_module.QFileDialog, "getOpenFileNames",
                    side_effect=multi_results) as multi_dialog, patch.object(
                    window, "_open_manual_asset_family") as open_family:
                window.open_family_dialog()

            self.assertEqual(single_dialog.call_args_list[0].args[2],
                             str(Path("C:/start")))
            self.assertEqual(single_dialog.call_args_list[1].args[2],
                             str(selected_dir))
            self.assertEqual(multi_dialog.call_args_list[0].args[2],
                             str(selected_dir))
            self.assertEqual(multi_dialog.call_args_list[1].args[2],
                             str(selected_dir))
            open_family.assert_called_once_with(
                str(selected_dir / "MODEL.SKLT"),
                str(selected_dir / "MODEL.BASE"),
                [str(selected_dir / "MODEL.ILBM")],
                [str(selected_dir / "MODEL.ANM")])
        finally:
            window.close()

    def test_import_bas_archive_routes_setbas_to_archive_loader(self):
        window = AssemblyWindow()
        try:
            path = Path("C:/UA/Data/Objects/SET.BAS")
            with patch.object(
                    assembly_window_module.QFileDialog, "getOpenFileName",
                    return_value=(str(path), "BAS archive")), patch.object(
                    window, "open_setbas") as open_setbas, patch.object(
                    window, "open_base") as open_base:
                window.open_bas_archive_dialog()
            open_setbas.assert_called_once_with(path)
            open_base.assert_not_called()
        finally:
            window.close()

    def test_import_base_routes_standalone_base_normally(self):
        window = AssemblyWindow()
        try:
            path = Path("C:/UA/Data/Objects/ASKY2.BAS")
            with patch.object(
                    assembly_window_module.QFileDialog, "getOpenFileName",
                    return_value=(str(path), "BAS archive")), patch.object(
                    window, "open_setbas") as open_setbas, patch.object(
                    window, "open_base") as open_base:
                window.open_base_dialog()
            open_base.assert_called_once_with(path)
            open_setbas.assert_not_called()
        finally:
            window.close()

    def test_editor_status_shows_only_resource_and_all_unsaved_edits(self):
        window = AssemblyWindow()
        try:
            window._family = object()
            window._selected_owner = "root"
            window._owner_to_obj = {
                "root": SimpleNamespace(display_name="VP_HUBI1.sklt")}
            window._geom_dirty = {"root": object()}
            window._uv_original = {}
            window._vanm_uv_original = {}
            window._texture_original = {("root", 1): object()}

            window._update_editor_status()

            self.assertEqual(
                window.loaded_resource_label.text(), "VP_HUBI1.sklt")
            self.assertEqual(
                window.unsaved_edits_label.text(), "Unsave edits: 2")
            self.assertIn(
                "color: #ffffff",
                window.unsaved_edits_label.styleSheet())
            self.assertFalse(window.unsaved_edits_label.isHidden())
            self.assertEqual(
                window.loaded_resource_label.geometry().center().y(),
                window.unsaved_edits_label.geometry().center().y())
            visible_text = (
                window.loaded_resource_label.text() + " "
                + window.unsaved_edits_label.text())
            for removed in (
                    "Complete textured preview", "selected + children",
                    "large family", "TEXTURE PREVIEW"):
                self.assertNotIn(removed, visible_text)
        finally:
            window.close()

    def test_resource_columns_are_user_resizable(self):
        window = AssemblyWindow()
        try:
            for tree, count in (
                    (window.setbas_tree, 3),
                    (window.asset_tree, 3)):
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
            self.assertEqual(window.asset_tree.columnWidth(2), 190)
            self.assertFalse(hasattr(window, "resolve_tree"))
        finally:
            window.close()

    def test_save_model_as_asks_for_folder_and_keeps_bundle_layout(
            self):
        window = AssemblyWindow()
        try:
            family = SimpleNamespace()
            fam_obj = SimpleNamespace(
                base_object=SimpleNamespace(name="OriginalModel"))
            context = ("root", family, fam_obj, object(), object())
            selected = Path("C:/chosen")
            expected_base = selected / "OriginalModel.BASE"
            expected_skeleton = (
                selected / "Skeleton" / "ORIGINAL.sklt")
            with patch.object(
                    window, "_export_owner_for_selection",
                    return_value="root"), patch.object(
                    window, "_model_save_context",
                    return_value=context), patch.object(
                    window, "_bundle_skeleton_relative_path",
                    return_value=Path("Skeleton/ORIGINAL.sklt")), patch.object(
                    window, "_owner_vanm_uv_keys",
                    return_value=set()), patch.object(
                    assembly_window_module.QFileDialog,
                    "getExistingDirectory",
                    return_value=str(selected)), patch.object(
                    window, "_write_model_files",
                    return_value=True) as write, patch.object(
                    window, "_sync_geometry_save_controls"), patch.object(
                    window, "_notify"):
                window._save_model_as()
            write.assert_called_once_with(
                "root", family, fam_obj,
                expected_skeleton, expected_base,
                ask_replace=True)
            self.assertEqual(
                window._bundle_targets["root"],
                (expected_skeleton, expected_base))
            self.assertEqual(window._last_directory, selected)
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

    def test_setbas_context_menu_does_not_change_current_row(self):
        window = AssemblyWindow()
        try:
            resources = [
                SimpleNamespace(
                    class_id="sklt.class", resource_name="A.SKLT",
                    error=""),
                SimpleNamespace(
                    class_id="ilbm.class", resource_name="B.ILBM",
                    error=""),
            ]
            window._setbas = SimpleNamespace(resources=resources)
            first = QTreeWidgetItem(["A.SKLT", "SKLT", ""])
            second = QTreeWidgetItem(["B.ILBM", "ILBM", ""])
            for index, (item, kind) in enumerate((
                    (first, "sklt.class"), (second, "ilbm.class"))):
                item.setData(
                    0, assembly_window_module._BAS_KIND_ROLE, kind)
                item.setData(0, Qt.ItemDataRole.UserRole, index)
            window.setbas_tree.blockSignals(True)
            window.setbas_tree.addTopLevelItems([first, second])
            window.setbas_tree.setCurrentItem(first)
            window.setbas_tree.blockSignals(False)

            fake_menu = _FakeMenu()
            with patch.object(
                    window.setbas_tree, "itemAt", return_value=second), \
                    patch.object(
                        assembly_window_module, "QMenu",
                        return_value=fake_menu):
                window._show_setbas_context_menu(QPoint())
            self.assertIs(window.setbas_tree.currentItem(), first)
            labels = [
                action.text() for action in fake_menu.actions()
                if not action.isSeparator()]
            self.assertNotIn("Expand group", labels)
            self.assertNotIn("Collapse group", labels)
            self.assertIn("Preview", labels)
        finally:
            window.close()

    def test_setbas_base_context_copies_the_base_name(self):
        window = AssemblyWindow()
        try:
            window._setbas = SimpleNamespace(resources=[])
            item = QTreeWidgetItem(["MODEL.BASE", "", ""])
            item.setData(
                0, assembly_window_module._BAS_KIND_ROLE, "base")
            item.setData(
                0, assembly_window_module._BAS_NAME_ROLE,
                "Objects/MODEL.BASE")
            fake_menu = _FakeMenu()
            with patch.object(
                    window.setbas_tree, "itemAt", return_value=item), \
                    patch.object(
                        assembly_window_module, "QMenu",
                        return_value=fake_menu), \
                    patch.object(window, "_copy_text") as copied:
                window._show_setbas_context_menu(QPoint())
                action = next(
                    candidate for candidate in fake_menu.actions()
                    if candidate.text() == "Copy BASE name")
                action.triggered.callback()
                labels = [
                    candidate.text() for candidate in fake_menu.actions()
                    if not candidate.isSeparator()]
            copied.assert_called_once_with(
                "Objects/MODEL.BASE", "BASE name copied successfully.")
            self.assertIn("Show Dependencies", labels)
            self.assertIn("Edit BASE Dependencies", labels)
        finally:
            window.close()

    def test_setbas_right_click_never_selects_or_previews_any_resource_type(
            self):
        window = AssemblyWindow()
        try:
            kinds = (
                ("base", "MODEL.BASE"),
                ("bmpanim.class", "EFFECT.ANM"),
                ("ilbm.class", "BODY.ILBM"),
                ("sklt.class", "MODEL.SKLT"),
                ("particle.class", "SMOKE.PARTICLE"),
            )
            resources = [
                SimpleNamespace(
                    class_id=kind, resource_name=name, error="")
                for kind, name in kinds
            ]
            window._setbas = SimpleNamespace(resources=resources)
            items = []
            for index, (kind, name) in enumerate(kinds):
                item = QTreeWidgetItem([name, kind, ""])
                item.setData(
                    0, assembly_window_module._BAS_KIND_ROLE, kind)
                item.setData(0, Qt.ItemDataRole.UserRole, index)
                items.append(item)
            window.setbas_tree.blockSignals(True)
            window.setbas_tree.addTopLevelItems(items)
            window.setbas_tree.setCurrentItem(items[0])
            window.setbas_tree.blockSignals(False)
            window.setbas_tree.resize(520, 260)
            window.setbas_tree.show()
            QApplication.processEvents()

            with patch.object(
                    window, "_preview_setbas_resource") as preview, \
                    patch.object(
                        assembly_window_module, "QMenu",
                        return_value=_FakeMenu()):
                for item in items[1:]:
                    window.setbas_tree.scrollToItem(item)
                    QApplication.processEvents()
                    position = window.setbas_tree.visualItemRect(item).center()
                    QTest.mouseClick(
                        window.setbas_tree.viewport(),
                        Qt.MouseButton.RightButton,
                        Qt.KeyboardModifier.NoModifier,
                        position)
                    QApplication.processEvents()
                    self.assertIs(
                        window.setbas_tree.currentItem(), items[0])
            preview.assert_not_called()
        finally:
            window.close()

    def test_setbas_left_click_selects_and_previews_every_supported_type(self):
        window = AssemblyWindow()
        try:
            kinds = (
                ("base", "MODEL.BASE"),
                ("bmpanim.class", "EFFECT.ANM"),
                ("ilbm.class", "BODY.ILBM"),
                ("sklt.class", "MODEL.SKLT"),
            )
            resources = [
                SimpleNamespace(
                    class_id=kind, resource_name=name, error="")
                for kind, name in kinds
            ]
            window._setbas = SimpleNamespace(resources=resources)
            items = []
            for index, (kind, name) in enumerate(kinds):
                item = QTreeWidgetItem([name, kind, ""])
                item.setData(
                    0, assembly_window_module._BAS_KIND_ROLE, kind)
                item.setData(0, Qt.ItemDataRole.UserRole, index)
                items.append(item)
            window.setbas_tree.blockSignals(True)
            window.setbas_tree.addTopLevelItems(items)
            window.setbas_tree.blockSignals(False)
            window.setbas_tree.resize(520, 240)
            window.setbas_tree.show()
            QApplication.processEvents()

            with patch.object(
                    window, "_preview_setbas_resource") as preview:
                for item in items:
                    window.setbas_tree.scrollToItem(item)
                    QApplication.processEvents()
                    position = window.setbas_tree.visualItemRect(item).center()
                    QTest.mouseClick(
                        window.setbas_tree.viewport(),
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier,
                        position)
                    QApplication.processEvents()
                    self.assertIs(window.setbas_tree.currentItem(), item)
                    preview.assert_called_with(item)
        finally:
            window.close()

    def test_setbas_arrow_navigation_refreshes_supported_preview(self):
        window = AssemblyWindow()
        try:
            resources = [
                SimpleNamespace(
                    class_id="base.class", resource_name="MODEL.BASE",
                    error=""),
                SimpleNamespace(
                    class_id="ilbm.class", resource_name="BODY.ILBM",
                    error=""),
            ]
            window._setbas = SimpleNamespace(resources=resources)
            first = QTreeWidgetItem(["MODEL.BASE", "BASE", ""])
            second = QTreeWidgetItem(["BODY.ILBM", "ILBM", ""])
            first.setData(0, assembly_window_module._BAS_KIND_ROLE, "base")
            second.setData(
                0, assembly_window_module._BAS_KIND_ROLE, "ilbm.class")
            first.setData(0, Qt.ItemDataRole.UserRole, 0)
            second.setData(0, Qt.ItemDataRole.UserRole, 1)
            window.setbas_tree.blockSignals(True)
            window.setbas_tree.addTopLevelItems([first, second])
            window.setbas_tree.setCurrentItem(first)
            window.setbas_tree.blockSignals(False)
            window.setbas_tree.show()
            window.setbas_tree.setFocus()

            with patch.object(
                    window, "_preview_setbas_resource") as preview:
                QTest.keyClick(window.setbas_tree, Qt.Key.Key_Down)
                QApplication.processEvents()
            self.assertIs(window.setbas_tree.currentItem(), second)
            preview.assert_called_once_with(second)
        finally:
            window.close()

    def test_mapping_repair_is_one_standalone_shared_state_tool(self):
        window = AssemblyWindow()
        try:
            panel = window._mapping_panel
            window._show_mapping_repair()
            first_dialog = window._mapping_dialog
            self.assertIsNotNone(first_dialog)
            self.assertIs(panel.parentWidget(), first_dialog)
            first_dialog.close()
            window._show_mapping_repair()
            self.assertIs(window._mapping_dialog, first_dialog)
            self.assertIs(window._mapping_panel, panel)
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
            self.assertIn("Select Resource...", captured)
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

    def test_setbas_vp_table_uses_embedded_source_only(self):
        window = AssemblyWindow()
        try:
            embedded_table = parse_visproto_text("EMBEDDED.base\n>")
            embedded = SimpleNamespace(
                entries=[], as_table=lambda: embedded_table)
            archive = SimpleNamespace(path=Path("/tmp/Set1/SET.BAS"))
            with patch(
                    "assembly_window.reconstruct_embedded_vps",
                    return_value=embedded):
                window._load_vp_table(archive)
            self.assertEqual(window._vp_source, "embedded visproto.base")
            self.assertEqual(
                window._vp_table.entry(0).base_name,
                "EMBEDDED.base")
            self.assertEqual(window._vp_source_path, archive.path)
        finally:
            window.close()

    def test_embedded_setbas_model_keeps_full_in_memory_edit_mode(self):
        window = AssemblyWindow()
        try:
            owner = "root"
            archive = SimpleNamespace(path=Path("C:/UA/Set1/SET.BAS"))
            window._setbas = archive
            window._family = SimpleNamespace(base_path=archive.path)
            window._selected_owner = owner
            window._owner_to_obj = {
                owner: SimpleNamespace(
                    skeleton=object(),
                    skeleton_ref=SimpleNamespace(
                        path=None, status="setbas", source="SET.BAS"),
                )
            }
            self.assertTrue(window._selected_model_is_archive_read_only())
            self.assertTrue(window._has_editable_model())
            window._sync_edit_action_states()
            editor_index = window._right_tabs.indexOf(window._editor_tabs)
            self.assertTrue(window._right_tabs.isTabEnabled(editor_index))
            self.assertIn(
                "never overwritten", window._right_tabs.tabToolTip(editor_index))
            self.assertTrue(window.edit_menu.isEnabled())
            self.assertTrue(window.mapping_repair_action.isEnabled())
            self.assertTrue(window.edit_toggle_action.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
