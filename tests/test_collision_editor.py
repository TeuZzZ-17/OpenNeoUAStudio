import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHeaderView, QMessageBox, QSizePolicy,
    QPushButton, QToolBar, QToolButton,
)

from asset_family import AssetFamily, FamilyObject
from base_parser import BaseObject
import collision_editor.editor as editor_module
from collision_editor import (
    ApplyScriptDialog,
    CollisionEditorWindow,
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    CollisionViewport,
    GunPoint,
    OpenScriptObjectDialog,
    LEGACY,
    VEHICLE,
    WEAPON,
    TYPE_COLORS,
    FIRE_POINT_COLOR,
    GUN_POINT_COLOR,
    VIEW_PRESETS,
    create_backup,
    effective_runtime_radius,
    export_collision_text,
    fire_point_positions,
    find_script_blocks,
    import_collision_block,
    import_cockpit_camera_block,
    import_fire_points_block,
    import_gun_points_block,
    import_overeof_block,
    plan_script_update,
    read_script_file,
    runtime_vp_table,
    script_model_references,
    validate_project,
    vehicle_model_references,
    weapon_model_references,
    write_script_file,
)
from sklt_parser import SkltModel


def _family():
    root_base = BaseObject(name="Root", skeleton_name="models/root.sklt")
    kid_base = BaseObject(name="Kid", skeleton_name="models/kid.sklt")
    root = FamilyObject(
        root_base,
        skeleton=SkltModel(
            source_name="root.sklt",
            points=[(-2, -1, -1), (2, -1, -1), (0, 2, 1)],
            polygons=[[0, 1, 2]],
        ),
        owner_path="root",
    )
    kid = FamilyObject(
        kid_base,
        skeleton=SkltModel(
            source_name="kid.sklt",
            points=[(-1, -1, 0), (1, -1, 0), (0, 1, 0)],
            polygons=[[0, 1, 2]],
        ),
        owner_path="root/kid[0]",
    )
    root.kids.append(kid)
    return AssetFamily(root_object=root, textures={"dummy.ilbm": object()})


def _mixed_project():
    return CollisionProject(
        name="Wasp",
        source_model="wasp.sklt",
        source_base="user.base",
        legacy=CollisionSphere(LEGACY, radius=35),
        compound=[
            CollisionSphere(VEHICLE, 0, -50, 120, 85),
            CollisionSphere(WEAPON, 80, -15, 10, 45),
        ],
    )


class CollisionEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        window = CollisionEditorWindow()
        self.addCleanup(self._close_window, window)
        return window

    @staticmethod
    def _close_window(window):
        window._modified = False
        window.close()

    def test_01_base_lists_every_resolved_model(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        self.assertEqual(window.model_tree.topLevelItemCount(), 2)
        self.assertEqual(
            window.model_tree.topLevelItem(0).text(0), "models/root.sklt")
        self.assertEqual(window.model_tree.topLevelItem(0).text(1), "0")
        self.assertEqual(window.model_tree.topLevelItem(1).text(1), "1")

    def test_02_model_search_filters_names_and_restores_list(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        self.assertEqual(
            window.model_search.placeholderText(), "Search model names...")
        window.model_search.setText("Kid")
        self.assertTrue(window.model_tree.topLevelItem(0).isHidden())
        self.assertFalse(window.model_tree.topLevelItem(1).isHidden())
        window.model_search.setText("root/kid[0]")
        self.assertTrue(window.model_tree.topLevelItem(0).isHidden())
        self.assertFalse(window.model_tree.topLevelItem(1).isHidden())
        window.model_search.clear()
        self.assertFalse(window.model_tree.topLevelItem(0).isHidden())
        self.assertFalse(window.model_tree.topLevelItem(1).isHidden())

    def test_02_switch_model_without_closing_editor(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        second = window.model_tree.topLevelItem(1)
        window.model_tree.setCurrentItem(second)
        self.assertEqual(window.project.source_model, "kid.sklt")
        self.assertEqual(window._current_owner, "root/kid[0]")

    def test_03_base_texture_family_reaches_existing_viewport(self):
        family = _family()
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=family):
            window.open_base("sample.base")
        self.assertIs(window.viewport._family_ref, family)
        self.assertIn("dummy.ilbm", window.viewport._family_ref.textures)
        self.assertEqual(window.viewport._mode, "textured")

    def test_04_external_sklt_uses_manual_family_loader(self):
        family = _family()
        family.root_object.kids.clear()
        window = self._window()
        with patch("collision_editor.load_manual_family",
                   return_value=family) as loader:
            window.open_sklt("loose.sklt")
        loader.assert_called_once_with("loose.sklt", [], [])
        self.assertEqual(window.model_tree.topLevelItemCount(), 1)

    def test_05_f10_geometry_has_three_twelve_segment_rings(self):
        viewport = CollisionViewport()
        sphere = CollisionSphere(VEHICLE, radius=1)
        self.assertEqual(viewport.RING_SEGMENTS, 12)
        self.assertEqual(
            [len(viewport._ring_points(sphere, axis)) for axis in range(3)],
            [13, 13, 13],
        )

    def test_06_f10_colors_match_openua(self):
        self.assertEqual(TYPE_COLORS[LEGACY], QColor(220, 60, 60))
        self.assertEqual(TYPE_COLORS[VEHICLE], QColor(60, 220, 60))
        self.assertEqual(TYPE_COLORS[WEAPON], QColor(60, 130, 235))

    def test_07_add_single_legacy_radius_at_runtime_origin(self):
        window = self._window()
        window.add_legacy()
        window.add_legacy()
        self.assertEqual(len(window.project.spheres()), 1)
        self.assertEqual(window.project.legacy.center, (0.0, 0.0, 0.0))
        self.assertFalse(window.gizmo.isEnabled())
        self.assertFalse(hasattr(window, "x_spin"))

    def test_08_add_vehicle_and_weapon_categories(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.add_compound(WEAPON)
        self.assertEqual(
            [sphere.category for sphere in window.project.compound],
            [VEHICLE, WEAPON],
        )

    def test_09_move_gizmo_updates_compound_coordinates(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.move_strength_slider.setValue(3)
        window._gizmo_nudge((1, 0, 0))
        window._gizmo_nudge((0, -1, 0))
        self.assertEqual(
            window.project.compound[0].center, (3.0, -3.0, 0.0))

    def test_10_move_does_not_invent_legacy_offsets(self):
        window = self._window()
        window.add_legacy()
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.legacy.center, (0.0, 0.0, 0.0))

    def test_11_radius_slider_scales_one_effective_radius(self):
        window = self._window()
        window.add_compound(VEHICLE)
        slider_value = window._radius_to_slider(85.0)
        expected = round(window._slider_to_radius(slider_value))
        window._begin_radius_slider()
        window.radius_slider.setValue(slider_value)
        window._finish_radius_slider()
        self.assertAlmostEqual(
            window.project.compound[0].radius, expected, places=8)
        self.assertAlmostEqual(window.radius_spin.value(), expected, places=4)

    def test_12_duplicate_and_delete_keep_dense_order(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.duplicate_sphere()
        window.add_compound(WEAPON)
        window._selected = 1
        window.delete_sphere()
        output = export_collision_text(window.project)
        self.assertIn("coll_num = 2", output)
        self.assertEqual(output.count("coll_act = 0"), 1)
        self.assertEqual(output.count("coll_act = 1"), 1)
        self.assertNotIn("coll_act = 2", output)

    def test_13_export_without_radius(self):
        project = _mixed_project()
        project.legacy = None
        output = export_collision_text(project)
        self.assertNotIn("\nradius =", output)
        self.assertIn("coll_num = 2", output)

    def test_14_export_without_compound(self):
        project = _mixed_project()
        project.compound.clear()
        output = export_collision_text(project)
        self.assertIn("radius = 35", output)
        self.assertNotIn("coll_num", output)
        self.assertNotIn("coll_act", output)

    def test_15_green_and_blue_export_identically(self):
        green = CollisionProject(
            name="A", source_model="a.sklt",
            compound=[CollisionSphere(VEHICLE, 1, 2, 3, 4)])
        blue = CollisionProject(
            name="A", source_model="a.sklt",
            compound=[CollisionSphere(WEAPON, 1, 2, 3, 4)])
        green_data = export_collision_text(green).split("coll_num", 1)[1]
        blue_data = export_collision_text(blue).split("coll_num", 1)[1]
        self.assertEqual(green_data, blue_data)
        self.assertNotIn("green", green_data.lower())
        self.assertNotIn("blue", blue_data.lower())

    def test_16_import_new_vehicle(self):
        text = (
            "new_vehicle 5\n name = Wasp\n radius = 35\n coll_num = 1\n"
            " coll_act = 0\n coll_x = 1\n coll_y = 2\n coll_z = 3\n"
            " coll_radius = 4\nend\n")
        block = find_script_blocks(text)[0]
        legacy, compound, warnings = import_collision_block(
            text, block, VEHICLE)
        self.assertEqual(legacy.radius, 35)
        self.assertEqual(compound[0].center, (1, 2, 3))
        self.assertEqual(compound[0].category, VEHICLE)
        self.assertFalse(warnings)

    def test_17_import_new_weapon_is_editor_blue_only(self):
        text = (
            "new_weapon 9\n coll_num = 1\n coll_act = 0\n"
            " coll_x = 0\n coll_y = 0\n coll_z = 0\n"
            " coll_radius = 8\nend\n")
        block = find_script_blocks(text)[0]
        _legacy, compound, _warnings = import_collision_block(
            text, block, WEAPON)
        self.assertEqual(compound[0].category, WEAPON)

    def test_18_nested_begin_end_does_not_truncate_target(self):
        text = (
            "modify_vehicle 3\n begin_chain_fx hit\n"
            "  radius = 999\n end\n radius = 7\nend\n")
        blocks = find_script_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].complete)
        self.assertEqual(blocks[0].end_line, 5)
        updated, _preview, _name = plan_script_update(
            text, "modify_vehicle", 3, _mixed_project())
        self.assertIn("  radius = 999", updated)
        self.assertIn(" radius = 35", updated)

    def test_19_script_update_changes_only_target_collision_keys(self):
        text = (
            "; header\nnew_vehicle 1\n mass = 11\n radius = 2\nend\n\n"
            "new_vehicle 2\n ; keep this\n mass = 22\n radius = 3\nend\n")
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 2, _mixed_project())
        self.assertIn("new_vehicle 1\n mass = 11\n radius = 2\nend", updated)
        self.assertIn("; keep this", updated)
        self.assertIn("mass = 22", updated)
        self.assertIn("radius = 35", updated)

    def test_20_missing_collision_data_is_commented_not_deleted(self):
        text = (
            "modify_weapon 4\n radius = 9\n coll_num = 1\n"
            " coll_act = 0\n coll_x = 1\n coll_y = 2\n coll_z = 3\n"
            " coll_radius = 4\nend\n")
        project = CollisionProject(name="Empty", source_model="x.sklt")
        updated, _preview, _name = plan_script_update(
            text, "modify_weapon", 4, project, comment_missing=True)
        self.assertIn("; Collision Editor disabled: legacy radius", updated)
        self.assertIn("; radius = 9", updated)
        self.assertIn("; coll_radius = 4", updated)

    def test_21_missing_target_and_ambiguous_id_are_rejected(self):
        project = _mixed_project()
        with self.assertRaises(CollisionScriptError):
            plan_script_update(
                "new_vehicle 1\nend\n", "new_vehicle", 2, project)
        duplicate = "new_vehicle 1\nend\nnew_vehicle 1\nend\n"
        with self.assertRaises(CollisionScriptError):
            plan_script_update(
                duplicate, "new_vehicle", 1, project)

    def test_22_incomplete_and_nonnumeric_parse_are_rejected(self):
        incomplete = "new_weapon 2\n radius = 3\n"
        block = find_script_blocks(incomplete)[0]
        with self.assertRaises(CollisionScriptError):
            import_collision_block(incomplete, block, WEAPON)
        bad = "new_weapon 2\n radius = nope\nend\n"
        with self.assertRaises(CollisionScriptError):
            import_collision_block(
                bad, find_script_blocks(bad)[0], WEAPON)

    def test_23_backup_is_created_before_script_write(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Vehicles.txt"
            path.write_text("original", encoding="utf-8")
            backup = create_backup(path)
            path.write_text("changed", encoding="utf-8")
            self.assertEqual(backup.read_text(encoding="utf-8"), "original")

    def test_24_script_encoding_and_bom_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Weapons.txt"
            path.write_bytes(b"\xef\xbb\xbfnew_weapon 1\r\nend\r\n")
            text, encoding, bom = read_script_file(path)
            self.assertTrue(bom)
            write_script_file(path, text, encoding, bom)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", path.read_bytes())

    def test_25_preview_cancel_does_not_write_or_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Vehicles.txt"
            original = "modify_vehicle 1\n radius = 2\nend\n"
            path.write_text(original, encoding="utf-8")
            dialog = ApplyScriptDialog(None, _mixed_project())
            dialog.path_edit.setText(str(path))
            dialog.id_spin.setValue(1)
            dialog.refresh_preview()
            self.assertTrue(dialog.preview.toPlainText())
            dialog.reject()
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(Path(str(path) + ".bak").exists())

    def test_25b_apply_dialog_searches_and_writes_when_apply_is_clicked(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Vehicles.txt"
            path.write_text(
                "new_vehicle 1\n name = Wasp\n radius = 2\nend\n"
                "modify_vehicle 14\n name = Dragonfly\n radius = 3\nend\n",
                encoding="utf-8")
            dialog = ApplyScriptDialog(
                None, _mixed_project(), initial_path=path)
            self.addCleanup(dialog.close)
            dialog.search_edit.setText("dragonfly")
            self.assertEqual(dialog.block_combo.count(), 1)
            self.assertEqual(dialog.id_spin.value(), 14)
            self.assertEqual(dialog.detected_name.text(), "Dragonfly")
            self.assertTrue(dialog.apply_button.isEnabled())
            dialog.apply_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertTrue(Path(str(path) + ".bak").is_file())
            self.assertIn("coll_num", path.read_text(encoding="utf-8"))

    def test_26_validation_reports_missing_name_model_and_bad_radius(self):
        project = CollisionProject(
            legacy=CollisionSphere(LEGACY, radius=0))
        errors, warnings = validate_project(project, None)
        self.assertTrue(any("Name" in error for error in errors))
        self.assertTrue(any("modello" in error for error in errors))
        self.assertTrue(any("raggio" in error for error in errors))
        self.assertTrue(warnings)

    def test_27_validation_warns_for_distant_sphere(self):
        project = CollisionProject(
            name="Far", source_model="far.sklt",
            compound=[CollisionSphere(VEHICLE, 1000, 0, 0, 1)])
        _errors, warnings = validate_project(
            project, (-1, -1, -1, 1, 1, 1))
        self.assertTrue(any("distante" in warning for warning in warnings))

    def test_28_tools_menu_exposes_collision_editor(self):
        from assembly_window import AssemblyWindow
        window = AssemblyWindow()
        self.addCleanup(window.close)
        labels = []
        for menu_action in window.menuBar().actions():
            if menu_action.text().replace("&", "") == "Tools":
                labels = [
                    action.text() for action in menu_action.menu().actions()]
                break
        self.assertIn("Collision Editor", labels)

    def test_29_window_matches_wireframe_size_position_contract(self):
        from wireframe_editor.window import WireframeEditorWindow
        collision = self._window()
        wireframe = WireframeEditorWindow()
        self.addCleanup(wireframe.close)
        collision.show()
        wireframe.show()
        QApplication.processEvents()
        self.assertEqual(collision.size(), wireframe.size())
        self.assertEqual(collision.pos(), wireframe.pos())
        self.assertIsNone(collision.parent())

    def test_30_top_menu_order_and_information_menus(self):
        window = self._window()
        self.assertEqual([
            action.text().replace("&", "")
            for action in window.menuBar().actions()
        ], ["File", "Edit", "Viewpoint", "Project Summary"])
        self.assertTrue(all(
            action.isCheckable()
            for action in window.viewpoint_menu.actions()))
        window.project.name = "Wasp"
        window.project.source_model = "wasp.sklt"
        window.add_compound(VEHICLE)
        summary = [
            action.text() for action in window.project_summary_menu.actions()]
        self.assertIn("Project: Wasp", summary)
        self.assertIn("Total Compound Spheres: 1", summary)
        self.assertTrue(all(
            not action.isEnabled()
            for action in window.project_summary_menu.actions()))

    def test_31_toolbar_is_simplified_and_has_view_preset(self):
        window = self._window()
        toolbar = window.findChild(QToolBar, "collisionTools")
        labels = [action.text() for action in toolbar.actions()]
        for removed in (
                "Select", "Move Sphere", "Scale Sphere", "Frame Model"):
            self.assertNotIn(removed, labels)
        self.assertIn("Add Legacy Radius", labels)
        self.assertEqual(
            window.toolbar_view_preset_combo.count(), len(VIEW_PRESETS))
        self.assertIs(
            window.toolbar_view_preset_combo.parentWidget(),
            window.model_preview_scale_toolbar)

    def test_32_model_list_has_only_internal_path_and_vp(self):
        window = self._window()
        self.assertEqual(window.model_tree.columnCount(), 2)
        self.assertEqual([
            window.model_tree.headerItem().text(index)
            for index in range(2)
        ], ["Internal path", "VP"])
        file_labels = [
            action.text() for action in window.file_import_menu.actions()]
        self.assertIn("Import BAS Archive", file_labels)
        self.assertNotIn("Open BASE...", file_labels)

    def test_33_sphere_list_selects_named_sphere_and_shows_radius(self):
        window = self._window()
        window.add_legacy()
        window.add_compound(VEHICLE)
        window.add_compound(WEAPON)
        self.assertEqual(window.sphere_tree.topLevelItemCount(), 3)
        self.assertEqual(
            window.sphere_tree.topLevelItem(0).text(0), "Legacy Radius")
        self.assertEqual(
            window.sphere_tree.topLevelItem(1).text(0),
            "Vehicle Collision")
        self.assertEqual(window.sphere_tree.topLevelItem(1).text(2), "Visible")
        self.assertEqual(window.sphere_tree.topLevelItem(1).text(3), "0")
        third = window.sphere_tree.topLevelItem(2)
        window.sphere_tree.setCurrentItem(third)
        self.assertEqual(window._selected, 2)
        self.assertEqual(window.type_value.text(), "Weapon Collision")
        self.assertEqual(third.text(2), "Visible")
        self.assertEqual(third.text(3), "1")
        self.assertEqual(
            float(third.text(1)), window.project.compound[1].radius)
        window.sphere_tree.setCurrentItem(
            window.sphere_tree.topLevelItem(1))
        window.visible_check.setChecked(False)
        self.assertEqual(window.sphere_tree.topLevelItem(1).text(2), "")
        window.visible_check.setChecked(True)
        self.assertEqual(
            window.sphere_tree.topLevelItem(1).text(2), "Visible")

    def test_34_move_gizmo_is_compact_but_readable_and_tracks_camera(self):
        window = self._window()
        self.assertGreaterEqual(window.gizmo.minimumWidth(), 300)
        self.assertGreaterEqual(window.gizmo.minimumHeight(), 150)
        self.assertEqual(window.transform_box.minimumHeight(), 248)
        self.assertEqual(window.transform_box.maximumHeight(), 248)
        self.assertEqual(
            window.gizmo.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)
        self.assertGreater(window.gizmo._handle_scale, 1.0)
        self.assertEqual(window.gizmo.directions, (
            (0, 0, 1), (0, 1, 0), (1, 0, 0),
            (0, 0, -1), (0, -1, 0), (-1, 0, 0),
        ))
        window.viewport._yaw = 72.0
        window.viewport._pitch = -18.0
        window.viewport.manualCameraChanged.emit()
        self.assertEqual(window.gizmo._yaw, 72.0)
        self.assertEqual(window.gizmo._pitch, -18.0)

    def test_35_models_stay_view_only_while_spheres_remain_editable(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        key = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Tab,
            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(window.viewport, key)
        self.assertFalse(window.viewport.is_edit_mode)
        window.add_compound(VEHICLE)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.compound[0].x, 1.0)
        window.viewport._mode_label_rect = object()
        window.viewport._draw_mode_label(None, None, False)
        self.assertIsNone(window.viewport._mode_label_rect)

    def test_36_view_presets_match_existing_viewport_functions(self):
        window = self._window()
        window.toolbar_view_preset_combo.setCurrentText("Front")
        self.assertEqual(window.viewport._yaw, 0.0)
        self.assertEqual(window.viewport._pitch, 0.0)
        self.assertEqual(window.gizmo._yaw, 0.0)
        window.viewport._yaw = 33.0
        window.viewport.manualCameraChanged.emit()
        self.assertEqual(
            window.toolbar_view_preset_combo.currentText(), "Current View")

    def test_37_selected_sphere_has_visible_white_outline(self):
        viewport = CollisionViewport()
        viewport.resize(420, 260)
        viewport.set_collision_spheres([
            CollisionSphere(VEHICLE, radius=0.7)], -1)
        viewport.show()
        QApplication.processEvents()
        plain = viewport.grab().toImage()
        viewport.set_collision_spheres(viewport._collision_spheres, 0)
        QApplication.processEvents()
        selected = viewport.grab().toImage()

        def white_pixels(image):
            count = 0
            for y in range(image.height()):
                for x in range(image.width()):
                    color = image.pixelColor(x, y)
                    if color.red() > 245 and color.green() > 245 \
                            and color.blue() > 245:
                        count += 1
            return count

        self.assertGreater(white_pixels(selected), white_pixels(plain) + 20)
        viewport.close()

    def test_38_tools_menu_places_mapping_repair_after_map_editor(self):
        from assembly_window import AssemblyWindow
        window = AssemblyWindow()
        self.addCleanup(window.close)
        labels = []
        for menu_action in window.menuBar().actions():
            if menu_action.text().replace("&", "") == "Tools":
                labels = [
                    action.text() for action in menu_action.menu().actions()]
                break
        self.assertLess(
            labels.index("Map Editor"), labels.index("Mapping Repair..."))

    def test_39_toolbar_hides_file_import_actions_but_keeps_undo_redo(self):
        window = self._window()
        toolbar = window.findChild(QToolBar, "collisionTools")
        labels = [action.text() for action in toolbar.actions()]
        self.assertNotIn("Import BAS Archive", labels)
        self.assertNotIn("Import SKLT", labels)
        self.assertNotIn("Script Object", labels)
        file_labels = [
            action.text() for action in window.file_import_menu.actions()]
        self.assertIn("Import BAS Archive", file_labels)
        self.assertIn("Import SKLT", file_labels)
        self.assertLess(labels.index("Reset View"), labels.index("Undo"))
        self.assertLess(labels.index("Undo"), labels.index("Redo"))
        self.assertEqual(window.undo_action.iconText(), "< Undo")
        self.assertEqual(window.redo_action.iconText(), "Redo >")
        self.assertIn("Reset Collisions", labels)

    def test_40_radius_list_and_export_are_whole_numbers(self):
        project = CollisionProject(
            name="Rounded", source_model="rounded.sklt",
            legacy=CollisionSphere(LEGACY, radius=89.1251),
            compound=[
                CollisionSphere(VEHICLE, radius=78.379051),
            ])
        output = export_collision_text(project)
        self.assertIn("radius = 89", output)
        self.assertIn("coll_radius = 78", output)
        self.assertNotIn("89.1251", output)
        self.assertNotIn("78.379051", output)
        window = self._window()
        window.project = project
        window._selected = 1
        window._sync_all()
        self.assertEqual(window.sphere_tree.topLevelItem(0).text(1), "89")
        self.assertEqual(window.sphere_tree.topLevelItem(1).text(1), "78")
        self.assertEqual(window.radius_spin.decimals(), 0)

    def test_41_reset_collisions_is_undoable_and_in_edit_menu(self):
        window = self._window()
        window.add_legacy()
        window.add_compound(VEHICLE)
        with patch(
                "collision_editor.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes):
            window.reset_collisions()
        self.assertFalse(window.project.spheres())
        window.undo()
        self.assertEqual(len(window.project.spheres()), 2)
        edit_menu_action = window.menuBar().actions()[1]
        edit_labels = [
            action.text() for action in edit_menu_action.menu().actions()]
        for label in (
                "Undo", "Redo", "Duplicate Sphere", "Delete Sphere",
                "Reset Collisions", "Add Vehicle Collision"):
            self.assertIn(label, edit_labels)

    def test_42_context_menu_reuses_full_toolbar_action_set(self):
        window = self._window()
        window.add_compound(VEHICLE)
        labels = [
            action.text()
            for action in window._create_sphere_context_menu(0).actions()]
        for label in (
                "Undo", "Redo", "Add Legacy Radius",
                "Add Vehicle Collision", "Add Weapon Collision",
                "Duplicate Sphere", "Delete Sphere", "Reset Collisions",
                "Reset View", "Import BAS Archive", "Import SKLT",
                "View Preset"):
            self.assertIn(label, labels)

    def test_43_mouse_drag_on_sphere_does_not_move_collision(self):
        viewport = CollisionViewport()
        viewport.resize(500, 350)
        sphere = CollisionSphere(VEHICLE, radius=0.7)
        viewport.set_collision_spheres([sphere], 0)
        press_point = viewport._ring_points(sphere, 0)[0]
        original = sphere.center
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            press_point, press_point, press_point,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        viewport.mousePressEvent(press)
        target = press_point + QPointF(30, 15)
        move = QMouseEvent(
            QEvent.Type.MouseMove, target, target, target,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        viewport.mouseMoveEvent(move)
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease, target, target, target,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)
        viewport.mouseReleaseEvent(release)
        self.assertEqual(sphere.center, original)
        self.assertFalse(hasattr(viewport, "sphereDragStarted"))

    def test_44_sphere_labels_are_removed_and_move_uses_slider(self):
        window = self._window()
        self.assertNotIn("Show Sphere Labels", window.viewpoint_actions)
        self.assertFalse(hasattr(window, "step_spin"))
        window.add_compound(VEHICLE)
        window.move_strength_slider.setValue(140)
        window._gizmo_nudge((0, 0, 1))
        self.assertEqual(window.project.compound[0].z, 140.0)
        self.assertEqual(window.move_strength_value.text(), "140")

    def test_45_center_handle_still_selects_without_direct_drag(self):
        viewport = CollisionViewport()
        viewport.resize(500, 350)
        sphere = CollisionSphere(VEHICLE, radius=0.7)
        viewport.set_collision_spheres([sphere], -1)
        selected = []
        viewport.spherePicked.connect(selected.append)
        center = viewport._project(viewport._camera_vertex(sphere.center))
        press = QMouseEvent(
            QEvent.Type.MouseButtonPress, center, center, center,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        viewport.mousePressEvent(press)
        target = center + QPointF(10, 5)
        move = QMouseEvent(
            QEvent.Type.MouseMove, target, target, target,
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        viewport.mouseMoveEvent(move)
        self.assertEqual(selected, [0])
        self.assertEqual(sphere.center, (0.0, 0.0, 0.0))

    def test_46_double_click_empty_space_deselects_every_sphere(self):
        window = self._window()
        window.add_compound(VEHICLE)
        self.assertEqual(window._selected, 0)
        empty = QPointF(3.0, 3.0)
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            empty, empty, empty,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        window.viewport.mouseDoubleClickEvent(event)
        self.assertEqual(window._selected, -1)
        self.assertEqual(window.type_value.text(), "None")
        self.assertIsNone(window.sphere_tree.currentItem())

    def test_47_sphere_list_uses_all_available_vertical_space(self):
        window = self._window()
        self.assertGreaterEqual(window.sphere_tree.minimumHeight(), 150)
        self.assertLessEqual(window.sphere_tree.minimumHeight(), 160)
        self.assertGreater(window.sphere_tree.maximumHeight(), 10000)
        self.assertEqual(
            window.sphere_tree.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)
        self.assertEqual(
            window.spheres_box.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)

    def test_selected_element_panel_is_bottom_right_viewport_overlay(self):
        window = self._window()
        self.assertIs(window.selected_box.parentWidget(), window.viewport_panel)
        index = window.viewport_layout.indexOf(window.selected_box)
        self.assertGreaterEqual(index, 0)
        alignment = window.viewport_layout.itemAt(index).alignment()
        self.assertTrue(alignment & Qt.AlignmentFlag.AlignBottom)
        self.assertTrue(alignment & Qt.AlignmentFlag.AlignRight)
        self.assertFalse(alignment & Qt.AlignmentFlag.AlignTop)
        self.assertFalse(alignment & Qt.AlignmentFlag.AlignLeft)

    def test_48_identical_spheres_keep_distinct_dense_indices(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.duplicate_sphere()
        window.duplicate_sphere()
        indices = [
            window.sphere_tree.topLevelItem(index).text(3)
            for index in range(window.sphere_tree.topLevelItemCount())]
        self.assertEqual(indices, ["0", "1", "2"])
        self.assertTrue(all(
            window.sphere_tree.topLevelItem(index).text(0)
            == "Vehicle Collision"
            for index in range(window.sphere_tree.topLevelItemCount())))
        window._selected = 1
        window.delete_sphere()
        self.assertEqual(len(window.project.compound), 2)

    def test_49_source_panel_and_vp_column_are_readable(self):
        window = self._window()
        window.show()
        QApplication.processEvents()
        source_panel = window.main_splitter.widget(0)
        self.assertGreaterEqual(source_panel.minimumWidth(), 230)
        self.assertGreaterEqual(window.main_splitter.sizes()[0], 230)
        self.assertEqual(
            window.model_tree.header().sectionResizeMode(0),
            QHeaderView.ResizeMode.Stretch)
        self.assertEqual(
            window.model_tree.header().sectionResizeMode(1),
            QHeaderView.ResizeMode.ResizeToContents)
        self.assertEqual(
            window.model_tree.textElideMode(), Qt.TextElideMode.ElideNone)

    def test_50_project_name_uses_neutral_example_placeholder(self):
        window = self._window()
        self.assertEqual(window.name_edit.placeholderText(), "Example")

    def test_51_overlapping_spheres_cycle_on_repeated_clicks(self):
        viewport = CollisionViewport()
        viewport.resize(500, 350)
        spheres = [
            CollisionSphere(VEHICLE, radius=0.7),
            CollisionSphere(VEHICLE, radius=0.7),
        ]
        viewport.set_collision_spheres(spheres, -1)
        point = viewport._ring_points(spheres[0], 0)[0]
        self.assertEqual(viewport._hit_sphere(point), 0)
        self.assertEqual(viewport._hit_sphere(point), 1)
        self.assertEqual(viewport._hit_sphere(point), 0)

    def test_52_collision_editor_is_a_dedicated_package(self):
        import collision_editor
        from collision_editor import editor
        self.assertTrue(collision_editor.__file__.endswith("__init__.py"))
        self.assertTrue(editor.__file__.endswith("collision_editor/editor.py"))
        self.assertFalse(
            (Path(editor.__file__).parents[1] / "collision_editor.py").exists())

    def test_53_radius_can_be_edited_from_sphere_table(self):
        window = self._window()
        window.add_compound(VEHICLE)
        self.assertTrue(
            window.sphere_tree.editTriggers()
            & QAbstractItemView.EditTrigger.DoubleClicked)
        item = window.sphere_tree.topLevelItem(0)
        item.setText(1, "144")
        self.assertEqual(window.project.compound[0].radius, 144.0)
        self.assertEqual(window.radius_spin.value(), 144.0)

    def test_54_move_strength_accepts_manual_values(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.move_strength_spin.setValue(1200)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.compound[0].x, 1200.0)
        self.assertEqual(window.move_strength_slider.maximum(), 1200)
        window.move_strength_spin.setValue(12)
        self.assertEqual(window.move_strength_slider.maximum(), 500)

    def test_55_compound_disables_legacy_preview_and_f10_red_sphere(self):
        project = CollisionProject(
            name="Weasel", source_model="weasel.sklt",
            legacy=CollisionSphere(LEGACY, radius=90),
            compound=[
                CollisionSphere(
                    VEHICLE, 2.459262, -3.46734, 57.795378, 59),
                CollisionSphere(
                    VEHICLE, -0.805237, -4.454739, -3.599648, 59),
                CollisionSphere(VEHICLE, 0, -9.5, 11.5, 90),
            ],
        )
        self.assertAlmostEqual(effective_runtime_radius(project), 116.95, 1)
        window = self._window()
        window.project = project
        window._selected = 0
        window._sync_all()
        legacy_preview = window.viewport._collision_spheres[0]
        self.assertEqual(legacy_preview.radius, 90)
        self.assertFalse(legacy_preview.visible)
        self.assertEqual(window.sphere_tree.topLevelItem(0).text(1), "90")
        self.assertIn("disabled", window.runtime_radius_value.text().lower())
        self.assertNotIn("117", window.runtime_radius_value.text())

    def test_55b_compound_broad_extent_ignores_larger_legacy_radius(self):
        project = CollisionProject(
            legacy=CollisionSphere(LEGACY, radius=200),
            compound=[CollisionSphere(VEHICLE, 0, 0, 0, 10)],
        )
        self.assertEqual(effective_runtime_radius(project), 10)
        project.compound.clear()
        self.assertEqual(effective_runtime_radius(project), 200)

    def test_56_selected_panel_is_compact_and_index_is_in_table(self):
        window = self._window()
        window.add_compound(VEHICLE)
        self.assertTrue(window.type_value.isHidden())
        self.assertTrue(window.index_value.isHidden())
        self.assertEqual(window.sphere_tree.columnCount(), 4)
        self.assertEqual(window.sphere_tree.headerItem().text(2), "Visible")
        self.assertEqual(window.sphere_tree.headerItem().text(3), "Index")

    def test_57_vp_values_are_right_aligned_without_widening_panel(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        item = window.model_tree.topLevelItem(0)
        alignment = item.textAlignment(1)
        self.assertTrue(alignment & int(Qt.AlignmentFlag.AlignRight))
        self.assertGreaterEqual(window.main_splitter.widget(0).minimumWidth(), 230)
        self.assertLessEqual(window.main_splitter.widget(0).maximumWidth(), 440)
        self.assertFalse(window.model_tree.header().stretchLastSection())
        self.assertEqual(item.toolTip(0), "models/root.sklt")

    def test_58_vanilla_collision_notice_is_visible_and_precise(self):
        window = self._window()
        notice = window.vanilla_collision_notice
        self.assertTrue(notice.isVisibleTo(window) or not window.isVisible())
        self.assertIn("Vanilla Urban Assault", notice.text())
        self.assertIn("Legacy Radius only", notice.text())
        self.assertIn("OpenUA", notice.text())

    def test_59_model_preview_scale_defaults_to_identity(self):
        window = self._window()
        self.assertEqual(
            (window.project.model_scale_x, window.project.model_scale_y,
             window.project.model_scale_z),
            (1.0, 1.0, 1.0))
        self.assertEqual(window.viewport.model_preview_scale, (1.0, 1.0, 1.0))
        self.assertEqual(window.model_scale_x_spin.value(), 1.0)
        self.assertEqual(window.model_scale_y_spin.value(), 1.0)
        self.assertEqual(window.model_scale_z_spin.value(), 1.0)

    def test_60_model_preview_scale_changes_model_not_collision_spheres(self):
        family = _family()
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=family):
            window.open_base("sample.base")
        window.add_compound(VEHICLE)
        sphere_before = window.project.compound[0].clone()
        source_points = list(family.root_object.skeleton.points)
        window.model_scale_x_spin.setValue(2.0)
        window.model_scale_y_spin.setValue(1.5)
        window.model_scale_z_spin.setValue(0.5)
        self.assertEqual(window.viewport.model_preview_scale, (2.0, 1.5, 0.5))
        self.assertEqual(window.project.compound[0].center, sphere_before.center)
        self.assertEqual(window.project.compound[0].radius, sphere_before.radius)
        self.assertEqual(family.root_object.skeleton.points, source_points)
        bounds = window._model_bounds()
        self.assertEqual(bounds, (-4.0, -1.5, -0.5, 4.0, 3.0, 0.5))

    def test_61_suggested_sphere_uses_scaled_model_bounds(self):
        family = _family()
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=family):
            window.open_base("sample.base")
        window.model_scale_x_spin.setValue(3.0)
        window.model_scale_y_spin.setValue(1.0)
        window.model_scale_z_spin.setValue(1.0)
        window.create_suggested()
        sphere = window.project.compound[0]
        self.assertEqual(sphere.center, (0.0, 0.5, 0.0))
        self.assertEqual(sphere.radius, 6.0)

    def test_62_model_preview_scale_is_undoable_and_resettable(self):
        window = self._window()
        window.model_scale_x_spin.setValue(2.0)
        self.assertEqual(window.project.model_scale_x, 2.0)
        window.undo()
        self.assertEqual(window.project.model_scale_x, 1.0)
        window.redo()
        self.assertEqual(window.project.model_scale_x, 2.0)
        window._reset_model_preview_scale()
        self.assertEqual(
            (window.project.model_scale_x, window.project.model_scale_y,
             window.project.model_scale_z),
            (1.0, 1.0, 1.0))

    def test_63_collision_export_does_not_emit_preview_scale(self):
        project = _mixed_project()
        project.model_scale_x = 2.0
        project.model_scale_y = 1.5
        project.model_scale_z = 0.5
        output = export_collision_text(project)
        self.assertNotIn("vp_scale", output)
        self.assertNotIn("model_scale", output)
        self.assertIn("coll_num = 2", output)

    def test_64_project_summary_reports_visual_preview_scale(self):
        window = self._window()
        window.model_scale_x_spin.setValue(2.0)
        window.model_scale_y_spin.setValue(1.5)
        labels = [
            action.text() for action in window.project_summary_menu.actions()]
        self.assertIn("Model preview scale: X 2  Y 1.5  Z 1", labels)

    def test_65_change_sphere_type_uses_explicit_target_menu(self):
        window = self._window()
        window.add_legacy()
        window.add_compound(VEHICLE)
        window.change_sphere_type(WEAPON)
        self.assertEqual(window.project.compound[0].category, WEAPON)
        window.change_sphere_type(LEGACY)
        self.assertEqual(window.project.compound[0].category, WEAPON)
        self.assertIsNotNone(window.project.legacy)
        window._selected = 0
        window.change_sphere_type(VEHICLE)
        self.assertIsNone(window.project.legacy)
        self.assertEqual(window.project.compound[0].category, VEHICLE)
        window.change_sphere_type(WEAPON)
        self.assertEqual(window.project.compound[0].category, WEAPON)

    def test_66_radius_spin_updates_preview_while_value_changes(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.radius_spin.setValue(123)
        self.assertEqual(window.project.compound[0].radius, 123.0)
        self.assertEqual(window.viewport._collision_spheres[0].radius, 123.0)
        self.assertEqual(window.sphere_tree.topLevelItem(0).text(1), "123")
        window._finish_radius_spin_edit()
        window.undo()
        self.assertNotEqual(window.project.compound[0].radius, 123.0)

    def test_67_arrow_keys_request_nudges_at_current_strength(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.move_strength_spin.setValue(7)
        for key in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            event = QKeyEvent(
                QEvent.Type.KeyPress, key,
                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(window.viewport, event)
        self.assertEqual(window.project.compound[0].center, (7.0, 0.0, 7.0))

    def test_68_model_preview_scale_uses_full_width_toolbar(self):
        window = self._window()
        toolbar = window.findChild(QToolBar, "modelPreviewScaleTools")
        self.assertIsNotNone(toolbar)
        self.assertEqual(window.reset_model_scale_button.text(), "Reset")
        for spin in (
                window.model_scale_x_spin, window.model_scale_y_spin,
                window.model_scale_z_spin):
            self.assertGreaterEqual(spin.minimumWidth(), 92)
            self.assertEqual(spin.decimals(), 3)
        window.model_scale_x_spin.setValue(1.2)
        self.assertNotIn("00", window.model_scale_x_spin.text())
        self.assertIs(window.model_preview_scale_box, toolbar)

    def test_69_mirror_selected_sphere_supports_all_three_axes(self):
        window = self._window()
        window.project.compound = [
            CollisionSphere(VEHICLE, 2.5, -3.0, 4.5, 10)]
        window._selected = 0
        window._sync_all()
        window.mirror_selected_sphere("x")
        self.assertEqual(window.project.compound[1].center, (-2.5, -3.0, 4.5))
        window.mirror_selected_sphere("y")
        self.assertEqual(window.project.compound[2].center, (-2.5, 3.0, 4.5))
        window.mirror_selected_sphere("z")
        self.assertEqual(window.project.compound[3].center, (-2.5, 3.0, -4.5))

    def test_70_change_type_and_mirror_use_explicit_submenus(self):
        window = self._window()
        window.add_compound(VEHICLE)
        toolbar = window.findChild(QToolBar, "collisionTools")
        buttons = {
            button.text(): button for button in toolbar.findChildren(QToolButton)
        }
        self.assertIn("Change Sphere Type", buttons)
        self.assertIn("Mirror Selected Sphere", buttons)
        change_labels = [
            action.text() for action in
            buttons["Change Sphere Type"].menu().actions()]
        self.assertEqual(change_labels, [
            "Change to Legacy Radius",
            "Change to Vehicle Collision",
            "Change to Weapon Collision",
        ])
        context = window._create_sphere_context_menu(0)
        submenus = {
            action.text(): action.menu() for action in context.actions()
            if action.menu() is not None
        }
        self.assertIn("Change Sphere Type", submenus)
        self.assertIn("Mirror Selected Sphere", submenus)
        self.assertEqual(
            [action.text() for action in
             submenus["Change Sphere Type"].actions()], change_labels)

    def test_71_selected_sphere_keeps_thick_color_inside_white_halo(self):
        self.assertGreater(
            CollisionViewport.SELECTED_COLOR_WIDTH, 1.0)
        self.assertGreater(
            CollisionViewport.SELECTED_HALO_WIDTH,
            CollisionViewport.SELECTED_COLOR_WIDTH)
        self.assertGreaterEqual(
            CollisionViewport.SELECTED_HALO_WIDTH
            - CollisionViewport.SELECTED_COLOR_WIDTH, 2.0)


    def test_72_overeof_defaults_to_unmanaged_vehicle_ground_preview(self):
        window = self._window()
        self.assertFalse(window.project.overeof_enabled)
        self.assertEqual(window.project.overeof, 0.0)
        self.assertEqual(window.viewport.overeof_preview_offset, 0.0)
        self.assertEqual(
            window.ground_alignment_box.title(), "Ground Alignment")
        self.assertEqual(window.ground_alignment_notice.text(), "")
        self.assertTrue(window.ground_alignment_notice.isHidden())
        self.assertIn("Show Ground Simulation", window.viewpoint_actions)
        self.assertIn("Show Overeof", window.viewpoint_actions)
        self.assertTrue(window.viewpoint_actions["Show Overeof"].isChecked())

    def test_73_vehicle_export_includes_overeof_only_when_enabled(self):
        project = CollisionProject(
            name="Tank", source_model="tank.sklt",
            target_category=VEHICLE,
            overeof_enabled=True, overeof=12.5,
            compound=[CollisionSphere(VEHICLE, 0, 0, 0, 20)],
        )
        output = export_collision_text(project)
        self.assertIn("overeof = 12.5", output)
        project.overeof_enabled = False
        self.assertNotIn("overeof", export_collision_text(project))
        project.overeof_enabled = True
        project.target_category = WEAPON
        self.assertNotIn("overeof", export_collision_text(project))

    def test_74_import_overeof_ignores_commented_values(self):
        text = (
            "new_vehicle 8\n ; overeof = 99\n overeof = 12.5\n"
            " coll_num = 0\nend\n")
        block = find_script_blocks(text)[0]
        enabled, value = import_overeof_block(text, block)
        self.assertTrue(enabled)
        self.assertEqual(value, 12.5)
        commented = "new_vehicle 8\n ; overeof = 99\nend\n"
        enabled, value = import_overeof_block(
            commented, find_script_blocks(commented)[0])
        self.assertFalse(enabled)
        self.assertEqual(value, 0.0)

    def test_75_script_update_manages_overeof_only_when_explicit(self):
        text = (
            "new_vehicle 3\n radius = 35\n overeof = 7\n"
            " coll_num = 0\nend\n")
        project = CollisionProject(
            name="Unit", source_model="unit.sklt",
            target_category=VEHICLE,
            overeof_enabled=True, overeof=14,
        )
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 3, project)
        self.assertIn("overeof = 14", updated)
        project.overeof_enabled = False
        preserved, _preview, _name = plan_script_update(
            text, "new_vehicle", 3, project)
        self.assertIn("overeof = 7", preserved)
        self.assertNotIn("disabled: overeof", preserved)

    def test_76_overeof_moves_preview_model_and_spheres_not_authored_data(self):
        family = _family()
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=family):
            window.open_base("sample.base")
        window.project.compound = [
            CollisionSphere(VEHICLE, 1.0, 2.0, 3.0, 4.0)]
        window.project.overeof_enabled = True
        window.project.overeof = 12.0
        source_vertex_y = (
            window.viewport._model_preview_base_faces[0][0][1])
        window._sync_all()
        self.assertEqual(window.viewport.overeof_preview_offset, -12.0)
        self.assertEqual(
            window.viewport._faces[0].vertices[0][1], source_vertex_y - 12.0)
        self.assertEqual(window.project.compound[0].y, 2.0)
        self.assertEqual(window.viewport._collision_spheres[0].y, -10.0)
        self.assertEqual(
            window._model_bounds(),
            (-2.0, -1.0, -1.0, 2.0, 2.0, 1.0))

    def test_77_suggest_overeof_uses_scaled_lowest_model_point(self):
        family = _family()
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=family):
            window.open_base("sample.base")
        window.model_scale_y_spin.setValue(1.5)
        window._suggest_overeof_from_bounds()
        self.assertTrue(window.project.overeof_enabled)
        self.assertEqual(window.project.overeof, 3.0)
        self.assertEqual(window.viewport.overeof_preview_offset, -3.0)
        window.undo()
        self.assertFalse(window.project.overeof_enabled)
        self.assertEqual(window.project.overeof, 0.0)

    def test_78_overeof_numeric_edit_is_live_undoable_and_resettable(self):
        window = self._window()
        window.overeof_check.setChecked(True)
        window.overeof_spin.setValue(18.5)
        self.assertTrue(window.project.overeof_enabled)
        self.assertEqual(window.project.overeof, 18.5)
        self.assertEqual(window.viewport.overeof_preview_offset, -18.5)
        window._finish_overeof_edit()
        window.undo()
        self.assertNotEqual(window.project.overeof, 18.5)
        window._reset_overeof()
        self.assertFalse(window.project.overeof_enabled)
        self.assertEqual(window.project.overeof, 0.0)

    def test_79_weapon_target_hides_ground_editor_and_ground_plane(self):
        window = self._window()
        window.project.target_category = WEAPON
        window.project.overeof_enabled = True
        window.project.overeof = 20.0
        window._sync_all()
        self.assertTrue(window.ground_alignment_box.isHidden())
        self.assertEqual(window.viewport.overeof_preview_offset, 0.0)
        self.assertFalse(window.viewport._ground_alignment_available)

    def test_80_compound_vehicle_without_overeof_gets_safety_warning(self):
        project = CollisionProject(
            name="Unit", source_model="unit.sklt",
            target_category=VEHICLE,
            compound=[CollisionSphere(VEHICLE, radius=10)],
        )
        _errors, warnings = validate_project(project, None)
        self.assertTrue(any("Overeof" in warning for warning in warnings))
        project.overeof_enabled = True
        _errors, warnings = validate_project(project, None)
        self.assertFalse(any("Overeof" in warning for warning in warnings))

    def test_81_project_summary_reports_ground_alignment(self):
        window = self._window()
        window.project.overeof_enabled = True
        window.project.overeof = 12.5
        window._sync_all()
        labels = [
            action.text() for action in window.project_summary_menu.actions()]
        self.assertIn("Ground alignment: Overeof 12.5", labels)

    def test_82_overeof_spin_uses_one_visible_decimal(self):
        window = self._window()
        self.assertEqual(window.overeof_spin.decimals(), 1)
        window.overeof_check.setChecked(True)
        window.overeof_spin.setValue(20.0)
        self.assertNotIn("0000", window.overeof_spin.text())

    def test_83_output_actions_follow_scale_reset_on_toolbar(self):
        window = self._window()
        toolbar = window.model_preview_scale_toolbar
        actions = toolbar.actions()
        reset_index = next(
            index for index, action in enumerate(actions)
            if toolbar.widgetForAction(action)
            is window.reset_model_scale_button)
        output_actions = (
            window.create_suggested_action,
            window.export_action,
            window.copy_output_action,
            window.import_action,
            window.apply_script_action,
        )
        for action in output_actions:
            self.assertIn(action, actions)
            self.assertGreater(actions.index(action), reset_index)
        central_button_texts = {
            button.text() for button in
            window.centralWidget().findChildren(QPushButton)}
        self.assertNotIn("Create Suggested Sphere", central_button_texts)
        self.assertNotIn("Export Collision Text", central_button_texts)
        self.assertNotIn("Copy Output to Clipboard", central_button_texts)
        self.assertNotIn("Import Collision Text", central_button_texts)
        self.assertNotIn("Apply to Existing Script", central_button_texts)

    def test_82_ground_simulation_is_hidden_until_source_is_loaded(self):
        viewport = CollisionViewport()
        self.assertFalse(viewport._ground_alignment_source_loaded)
        viewport.clear()
        self.assertFalse(viewport._ground_alignment_source_loaded)

    def test_83_ground_simulation_is_enabled_after_family_load(self):
        source = Path(editor_module.__file__).read_text(encoding="utf-8")
        self.assertIn("self._ground_alignment_source_loaded = True", source)
        self.assertIn("and self._ground_alignment_source_loaded", source)

    def test_84_fire_point_distribution_matches_vanilla_runtime(self):
        project = CollisionProject(
            fire_points_enabled=True,
            fire_x=30.0, fire_y=-10.0, fire_z=50.0,
            num_weapons=4,
        )
        self.assertEqual(fire_point_positions(project), [
            (-30.0, -10.0, 50.0),
            (-10.0, -10.0, 50.0),
            (10.0, -10.0, 50.0),
            (30.0, -10.0, 50.0),
        ])
        project.num_weapons = 1
        project.fire_x = -12.0
        self.assertEqual(
            fire_point_positions(project), [(-12.0, -10.0, 50.0)])

    def test_85_export_fire_points_are_separate_from_coll_num(self):
        project = CollisionProject(
            name="Wasp", source_model="wasp.sklt",
            target_category=VEHICLE,
            fire_points_enabled=True,
            fire_x=25.0, fire_y=-8.0, fire_z=40.0,
            num_weapons=2,
            compound=[CollisionSphere(VEHICLE, radius=20.0)],
        )
        output = export_collision_text(project)
        self.assertIn("fire_x = 25", output)
        self.assertIn("fire_y = -8", output)
        self.assertIn("fire_z = 40", output)
        self.assertIn("num_weapons = 2", output)
        self.assertNotIn("death_damage", output)
        self.assertIn("coll_num = 1", output)
        self.assertEqual(output.count("coll_act ="), 1)

    def test_86_import_fire_parameters_ignores_comments(self):
        text = (
            "new_vehicle 8\n"
            " ; fire_x = 999\n fire_x = 30\n fire_y = -5\n"
            " fire_z = 44\n num_weapons = 3\nend\n"
        )
        block = find_script_blocks(text)[0]
        self.assertEqual(
            import_fire_points_block(text, block),
            (True, 30.0, -5.0, 44.0, 3),
        )






    def test_87_script_update_preserves_unmanaged_obsolete_data(self):
        text = (
            "new_vehicle 3\n fire_x = 12\n fire_y = 2\n fire_z = 4\n"
            " num_weapons = 2\n death_damage = 1000\n"
            " death_damage_radius = 90\nend\n"
        )
        project = CollisionProject(
            name="Unit", source_model="unit.sklt",
            target_category=VEHICLE,
        )
        preserved, _preview, _name = plan_script_update(
            text, "new_vehicle", 3, project)
        self.assertIn("fire_x = 12", preserved)
        self.assertIn("death_damage_radius = 90", preserved)

        project.fire_points_enabled = True
        project.fire_x = 30
        project.fire_y = -6
        project.fire_z = 50
        project.num_weapons = 4
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 3, project)
        self.assertIn("fire_x = 30", updated)
        self.assertIn("num_weapons = 4", updated)
        self.assertIn("death_damage_radius = 90", updated)

    def test_88_vehicle_spatial_controls_live_in_right_column(self):
        window = self._window()
        self.assertIsNone(
            window.findChild(QToolBar, "vehicleSpatialPreviewTools"))
        self.assertEqual(
            window.fire_points_box.title(), "Fire Points (Vanilla)")
        self.assertEqual(
            window.add_fire_point_button.text(), "Add Fire Point")
        self.assertEqual(window.remove_fire_point_button.text(), "Remove")
        self.assertIsNone(getattr(window, "fire_points_check", None))
        self.assertIn("Show Fire Points", window.viewpoint_actions)
        self.assertNotIn("Show Death Damage AoE", window.viewpoint_actions)
        self.assertFalse(window.fire_points_box.isHidden())
        window.project.target_category = WEAPON
        window._sync_all()
        self.assertTrue(window.fire_points_box.isHidden())


    def test_90_vehicle_model_reference_reads_vp_and_preview_scale(self):
        text = (
            "new_vehicle 17\n"
            " name = Wasp\n"
            " vp_normal = 42\n"
            " vp_wait = 43\n"
            " vp_scale_x = 2\n"
            " vp_scale_y = 1.5\n"
            " vp_scale_z = 0.75\n"
            "end\n"
            "modify_vehicle 17\n vp_normal = 99\nend\n"
        )
        references = vehicle_model_references(text)
        self.assertEqual(len(references), 1)
        reference = references[0]
        self.assertEqual(reference.block.object_id, 17)
        self.assertEqual(reference.vp_normal, 42)
        self.assertEqual(reference.vp_wait, 43)
        self.assertEqual(
            (reference.scale_x, reference.scale_y, reference.scale_z),
            (2.0, 1.5, 0.75),
        )

    def test_90b_script_model_references_include_weapons_and_scales(self):
        text = (
            "new_vehicle 1\n name = Wasp\n vp_normal = 30\nend\n"
            "new_weapon 1\n name = Rocket\n vp_normal = 91\n"
            " vp_scale_x = 1.25\n vp_scale_y = 0.5\n"
            " vp_scale_z = 3\nend\n"
            "modify_weapon 1\n vp_normal = 999\nend\n"
        )
        references = script_model_references(text)
        self.assertEqual(
            [(item.block.kind, item.block.object_id, item.vp_normal)
             for item in references],
            [("new_vehicle", 1, 30), ("new_weapon", 1, 91)],
        )
        weapons = weapon_model_references(text)
        self.assertEqual(len(weapons), 1)
        self.assertEqual(
            (weapons[0].scale_x, weapons[0].scale_y, weapons[0].scale_z),
            (1.25, 0.5, 3.0),
        )

    def test_90c_tutorial_labels_are_removed(self):
        window = self._window()
        self.assertTrue(window.ground_alignment_notice.isHidden())
        self.assertTrue(window.fire_point_notice.isHidden())

    def test_90d_file_action_names_are_compact(self):
        window = self._window()
        self.assertEqual(window.open_base_action.text(), "Import BAS Archive")
        self.assertEqual(window.open_sklt_action.text(), "Import SKLT")
        self.assertEqual(
            window.open_vehicle_script_action.text(),
            "Import Vehicle / Weapon from Script")
        self.assertEqual(window.import_action.text(), "Import Collision Text")
        self.assertEqual(window.export_action.text(), "Export Collision Text")
        self.assertEqual(
            window.apply_script_action.text(), "Apply to Existing Script")
        self.assertEqual(window.save_loaded_script_action.text(), "Overwrite")

    def test_90e_script_dialog_filters_by_exact_id_or_name(self):
        text = (
            "new_vehicle 1\n name = Wasp\n vp_normal = 30\nend\n"
            "new_vehicle 10\n name = Firefly\n vp_normal = 48\nend\n"
            "new_weapon 1\n name = Rocket\n vp_normal = 91\nend\n"
        )
        dialog = OpenScriptObjectDialog(
            None, Path("objects.txt"), script_model_references(text))
        self.addCleanup(dialog.close)
        self.assertEqual(
            dialog.windowTitle(), "Import Vehicle / Weapon from Script")
        self.assertEqual(dialog.open_button.text(), "Import")
        dialog.search_edit.setText("rocket")
        self.assertEqual(dialog.block_combo.count(), 1)
        self.assertEqual(
            dialog.block_combo.currentData().block.kind, "new_weapon")
        dialog.search_edit.setText("1")
        self.assertEqual(dialog.block_combo.count(), 2)
        self.assertTrue(all(
            dialog.block_combo.itemData(index).block.object_id == 1
            for index in range(dialog.block_combo.count())))

    def test_90f_selected_multi_fire_rack_draws_white_halo_on_every_point(self):
        viewport = CollisionViewport()
        viewport._ground_alignment_source_loaded = True
        viewport.set_fire_points([(0, 0, 0), (10, 0, 0)], selected=0)
        painter = MagicMock()
        screens = [QPointF(20, 20), QPointF(60, 20)]
        with patch.object(
                viewport, "_fire_point_screen_positions",
                return_value=screens), patch.object(
                viewport, "_draw_fire_marker") as draw_marker:
            viewport._draw_fire_points_overlay(painter)
        self.assertEqual(draw_marker.call_count, 4)
        white_calls = [
            call for call in draw_marker.call_args_list
            if call.args[2].color() == QColor(255, 255, 255)
        ]
        self.assertEqual(len(white_calls), 2)

    def test_91_canonical_save_replaces_all_active_managed_parameters(self):
        text = (
            "new_vehicle 17\n"
            " name = Wasp\n"
            " vp_normal = 42\n"
            " radius = 10\n"
            " radius = 20\n"
            " fire_x = 999\n"
            " num_mguns = 8\n"
            " coll_num = 1\n"
            " coll_act = 0\n"
            " coll_x = 3\n"
            " coll_y = 4\n"
            " coll_z = 5\n"
            " coll_radius = 6\n"
            " ; radius = 777\n"
            " mass = 300\n"
            "end\n"
        )
        project = CollisionProject(
            name="Wasp", source_model="wasp.sklt",
            target_category=VEHICLE,
            overeof_enabled=True, overeof=12,
            fire_points_enabled=True,
            fire_x=30, fire_y=-5, fire_z=44, num_weapons=2,
            compound=[CollisionSphere(VEHICLE, 1, 2, 3, 40)],
        )
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 17, project,
            replace_all_managed=True,
        )
        self.assertNotIn("radius = 10", updated)
        self.assertNotIn("radius = 20", updated)
        self.assertNotIn("fire_x = 999", updated)
        self.assertIn("num_mguns = 8", updated)
        self.assertEqual(updated.count("coll_num ="), 1)
        self.assertEqual(updated.count("coll_act ="), 1)
        self.assertIn("fire_x = 30", updated)
        self.assertIn("mass = 300", updated)
        self.assertIn("vp_normal = 42", updated)
        self.assertIn("; radius = 777", updated)

    def test_92_script_object_actions_remain_in_file_menu(self):
        window = self._window()
        self.assertEqual(
            window.open_vehicle_script_action.text(),
            "Import Vehicle / Weapon from Script")
        self.assertEqual(window.save_loaded_script_action.text(), "Overwrite")
        self.assertIn(
            window.open_vehicle_script_action,
            window.file_import_menu.actions())
        self.assertIn(
            window.save_loaded_script_action,
            window.file_script_menu.actions())
        self.assertFalse(window.save_loaded_script_action.isEnabled())

    def test_93_runtime_vp_table_prefers_loose_visproto(self):
        from vp_manager import EmbeddedVPEntry, EmbeddedVPSet

        with tempfile.TemporaryDirectory() as tmp:
            set_root = Path(tmp) / "Set1"
            objects = set_root / "OBJECTS"
            loose = set_root / "Loose"
            objects.mkdir(parents=True)
            loose.mkdir()
            set_bas = objects / "SET.BAS"
            set_bas.write_bytes(b"placeholder")
            (loose / "VISPROTO.LST").write_text(
                "Custom.base\n>\n", encoding="utf-8")
            embedded = EmbeddedVPSet(
                source_path=set_bas,
                container_source_offset=0,
                entries=(EmbeddedVPEntry(
                    0, "Embedded.base", "Skeleton/Embedded.sklt", 1),),
            )
            table, source, warnings = runtime_vp_table(set_bas, embedded)
            self.assertEqual(table.entry(0).base_name, "Custom.base")
            self.assertEqual(source, "Loose VISPROTO.LST")
            self.assertEqual(warnings, [])


    def test_94_view_preset_precedes_model_scale_on_second_toolbar(self):
        window = self._window()
        toolbar = window.model_preview_scale_toolbar
        widgets = [
            toolbar.widgetForAction(action) for action in toolbar.actions()]
        preset_index = widgets.index(window.toolbar_view_preset_combo)
        scale_index = widgets.index(window.model_scale_x_spin)
        self.assertLess(preset_index, scale_index)
        collision_toolbar = window.findChild(QToolBar, "collisionTools")
        self.assertNotIn(
            window.toolbar_view_preset_combo,
            collision_toolbar.findChildren(type(window.toolbar_view_preset_combo)))

    def test_95_fire_points_are_listed_and_selectable(self):
        window = self._window()
        window.project.fire_points_enabled = True
        window.project.fire_x = 30.0
        window.project.fire_y = -6.0
        window.project.fire_z = 35.0
        window.project.num_weapons = 2
        window._sync_all()
        self.assertEqual(window.fire_point_tree.topLevelItemCount(), 2)
        second = window.fire_point_tree.topLevelItem(1)
        window.fire_point_tree.setCurrentItem(second)
        self.assertEqual(window._selected_fire_point, 1)
        self.assertEqual(window._selected, -1)
        self.assertTrue(window.gizmo.isEnabled())
        self.assertEqual(window.transform_box.title(), "Move Fire Point")
        self.assertEqual(window.viewport._fire_point_selected, 1)

    def test_95b_add_remove_fire_points_use_vanilla_num_weapons_rack(self):
        window = self._window()
        self.assertFalse(window.project.fire_points_enabled)
        self.assertFalse(window.fire_x_spin.isEnabled())
        self.assertFalse(window.remove_fire_point_button.isEnabled())

        window.add_fire_point()
        self.assertTrue(window.project.fire_points_enabled)
        self.assertEqual(window.project.num_weapons, 1)
        self.assertEqual(window.fire_point_tree.topLevelItemCount(), 1)
        self.assertEqual(window._selected_fire_point, 0)
        self.assertTrue(window.fire_x_spin.isEnabled())
        self.assertTrue(window.remove_fire_point_button.isEnabled())

        window.project.fire_x = 30.0
        window.add_fire_point()
        self.assertEqual(window.project.num_weapons, 2)
        self.assertEqual(fire_point_positions(window.project), [
            (-30.0, 0.0, 0.0),
            (30.0, 0.0, 0.0),
        ])
        self.assertEqual(window._selected_fire_point, 1)

        window.remove_fire_point()
        self.assertTrue(window.project.fire_points_enabled)
        self.assertEqual(window.project.num_weapons, 1)
        self.assertEqual(window.fire_point_tree.topLevelItemCount(), 1)

        window.remove_fire_point()
        self.assertFalse(window.project.fire_points_enabled)
        self.assertEqual(window.fire_point_tree.topLevelItemCount(), 0)
        self.assertEqual(window._selected_fire_point, -1)
        self.assertFalse(window.fire_x_spin.isEnabled())
        self.assertFalse(window.remove_fire_point_button.isEnabled())

    def test_95c_add_fire_point_after_disable_starts_with_one_point(self):
        window = self._window()
        window.project.fire_points_enabled = False
        window.project.num_weapons = 8
        window.project.fire_x = 25.0
        window._sync_all()
        window.add_fire_point()
        self.assertEqual(window.project.num_weapons, 1)
        self.assertEqual(fire_point_positions(window.project), [
            (25.0, 0.0, 0.0),
        ])

    def test_96_gizmo_moves_vanilla_fire_rack_without_inventing_offsets(self):
        window = self._window()
        window.project.fire_points_enabled = True
        window.project.fire_x = 30.0
        window.project.fire_y = -6.0
        window.project.fire_z = 35.0
        window.project.num_weapons = 2
        window._select_fire_point(1)
        window.move_strength_spin.setValue(5)
        window._gizmo_nudge((1, 0, 0))
        window._gizmo_nudge((0, -1, 0))
        window._gizmo_nudge((0, 0, 1))
        self.assertEqual(window.project.fire_x, 35.0)
        self.assertEqual(window.project.fire_y, -11.0)
        self.assertEqual(window.project.fire_z, 40.0)
        self.assertEqual(fire_point_positions(window.project), [
            (-35.0, -11.0, 40.0),
            (35.0, -11.0, 40.0),
        ])

    def test_97_center_fire_point_cannot_break_vanilla_symmetry(self):
        window = self._window()
        window.project.fire_points_enabled = True
        window.project.fire_x = 30.0
        window.project.num_weapons = 3
        window._select_fire_point(1)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.fire_x, 30.0)
        self.assertEqual(fire_point_positions(window.project)[1][0], 0.0)

    def test_98_selected_fire_point_uses_white_outline_state(self):
        viewport = CollisionViewport()
        viewport.set_fire_points([(1.0, 2.0, 3.0)], selected=0)
        self.assertEqual(viewport._fire_point_selected, 0)
        viewport.set_fire_points([(1.0, 2.0, 3.0)], selected=-1)
        self.assertEqual(viewport._fire_point_selected, -1)



    def test_99_point_colors_and_near_plane_rejection(self):
        self.assertEqual(FIRE_POINT_COLOR, QColor(235, 60, 60))
        self.assertTrue(
            CollisionViewport._camera_point_is_projectable((0, 0, 3.7)))
        self.assertFalse(
            CollisionViewport._camera_point_is_projectable((0, 0, 3.9)))
        self.assertFalse(
            CollisionViewport._camera_point_is_projectable((0, 0, 5.0)))

    def test_100_selecting_a_sphere_clears_fire_point_selection(self):
        window = self._window()
        window.project.fire_points_enabled = True
        window.project.fire_x = 30.0
        window.project.num_weapons = 2
        window._select_fire_point(1)
        self.assertEqual(window._selected_fire_point, 1)
        window.add_compound(VEHICLE)
        self.assertEqual(window._selected_fire_point, -1)
        self.assertGreaterEqual(window._selected, 0)

    def test_101_overlay_projection_rejects_far_offscreen_points(self):
        viewport = CollisionViewport()
        viewport.resize(640, 480)
        self.assertIsNone(
            viewport._project_visible_world((1_000_000.0, 0.0, 0.0)))

    def test_102_imports_legacy_and_generic_gun_points(self):
        text = (
            "new_vehicle 7\n"
            " name = Carrier\n"
            " robo_num_guns = 1\n"
            " robo_act_gun = 0\n"
            " robo_gun_pos_x = 10\n"
            " robo_gun_pos_y = -2\n"
            " robo_gun_pos_z = 30\n"
            " robo_gun_dir_z = 1\n"
            " robo_gun_type = 22\n"
            " robo_gun_name = Flak\n"
            " unit_num_guns = 2\n"
            " unit_gun_icon = default_icon.bmp\n"
            " unit_act_gun = 0\n"
            " unit_gun_pos_x = -15\n"
            " unit_gun_type = 40\n"
            " unit_act_gun = 1\n"
            " unit_gun_pos_x = 15\n"
            " unit_gun_dir_y = 1\n"
            " unit_gun_type = 41\n"
            " unit_gun_name = SideGun\n"
            " unit_gun_icon = side.bmp\n"
            "end\n"
        )
        block = find_script_blocks(text)[0]
        enabled, points, default_icon = import_gun_points_block(text, block)
        self.assertTrue(enabled)
        self.assertEqual(default_icon, "default_icon.bmp")
        self.assertEqual([point.scheme for point in points], [
            "robo", "unit", "unit"])
        self.assertEqual(points[0].position, (10.0, -2.0, 30.0))
        self.assertEqual(points[0].gun_type, 22)
        self.assertEqual(points[0].name, "Flak")
        self.assertEqual(points[2].direction, (0.0, 1.0, 0.0))
        self.assertEqual(points[2].icon, "side.bmp")

    def test_103_canonical_gun_save_preserves_num_mguns(self):
        text = (
            "new_vehicle 17\n"
            " name = Carrier\n"
            " vp_normal = 42\n"
            " num_mguns = 8\n"
            " unit_num_guns = 1\n"
            " unit_act_gun = 0\n"
            " unit_gun_pos_x = 999\n"
            " unit_gun_type = 3\n"
            "end\n"
        )
        project = CollisionProject(
            name="Carrier", source_model="carrier.sklt",
            target_category=VEHICLE, gun_points_enabled=True,
            gun_points=[GunPoint(
                scheme="unit", x=12, y=3, z=-5, dir_z=1,
                gun_type=44, name="Flak")],
        )
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 17, project, replace_all_managed=True)
        self.assertIn("num_mguns = 8", updated)
        self.assertNotIn("unit_gun_pos_x = 999", updated)
        self.assertIn("unit_num_guns = 1", updated)
        self.assertIn("unit_gun_pos_x = 12", updated)
        self.assertIn("unit_gun_dir_z = 1", updated)
        self.assertIn("unit_gun_type = 44", updated)
        self.assertIn("unit_gun_name = Flak", updated)

    def test_104_gun_points_are_purple_listed_and_selectable(self):
        self.assertEqual(GUN_POINT_COLOR, QColor(178, 78, 238))
        window = self._window()
        window.project.gun_points_enabled = True
        window.project.gun_points = [
            GunPoint(scheme="unit", x=4, y=5, z=6, dir_z=1, gun_type=9)]
        window._sync_all()
        self.assertFalse(window.gun_points_box.isHidden())
        self.assertEqual(window.gun_point_tree.topLevelItemCount(), 1)
        item = window.gun_point_tree.topLevelItem(0)
        self.assertEqual(window.gun_point_tree.headerItem().text(1), "Family")
        self.assertEqual(item.text(1), "OpenUA")
        window.gun_point_tree.setCurrentItem(item)
        self.assertEqual(window._selected_gun_point, 0)
        self.assertEqual(window._selected, -1)
        self.assertEqual(window.transform_box.title(), "Move Gun Point")
        self.assertEqual(window.viewport._gun_point_selected, 0)

    def test_105_gizmo_moves_selected_gun_point_directly(self):
        window = self._window()
        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(scheme="unit", x=1, y=2, z=3)]
        window._select_gun_point(0)
        window.move_strength_spin.setValue(5)
        window._gizmo_nudge((1, -1, 1))
        point = window.project.gun_points[0]
        self.assertEqual(point.position, (6.0, -3.0, 8.0))

    def test_106_new_gun_point_defaults_to_generic_unit_family(self):
        window = self._window()
        window.add_gun_point()
        self.assertTrue(window.project.gun_points_enabled)
        self.assertEqual(len(window.project.gun_points), 1)
        self.assertEqual(window.project.gun_points[0].scheme, "unit")
        self.assertEqual(window._selected_gun_point, 0)

        window.gun_point_type_combo.setCurrentIndex(
            window.gun_point_type_combo.findData("robo"))
        window.add_gun_point()
        self.assertEqual(window.project.gun_points[-1].scheme, "robo")

    def test_107_weapon_target_hides_gun_point_editor(self):
        window = self._window()
        window.project.target_category = WEAPON
        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(scheme="unit")]
        window._sync_all()
        self.assertFalse(window.gun_points_box.isVisible())
        self.assertEqual(window.viewport._gun_points, [])

    def test_108_gizmo_lives_below_tabs_and_gun_fields_share_columns(self):
        window = self._window()
        self.assertIs(window.main_splitter.widget(1), window.viewport_panel)
        self.assertIs(
            window.transform_box.parentWidget(), window.properties_scroll.widget())
        self.assertLess(window.properties_scroll.widget().layout().indexOf(
            window.properties_tabs),
            window.properties_scroll.widget().layout().indexOf(
                window.transform_box))
        self.assertIs(window.selected_box.parentWidget(), window.viewport_panel)
        selected_index = window.viewport_layout.indexOf(window.selected_box)
        self.assertGreaterEqual(selected_index, 0)
        selected_alignment = window.viewport_layout.itemAt(selected_index).alignment()
        self.assertTrue(selected_alignment & Qt.AlignmentFlag.AlignBottom)
        self.assertTrue(selected_alignment & Qt.AlignmentFlag.AlignLeft)
        self.assertFalse(selected_alignment & Qt.AlignmentFlag.AlignRight)
        self.assertEqual(window.transform_box.minimumHeight(), 248)
        self.assertEqual(window.transform_box.maximumHeight(), 248)
        self.assertGreaterEqual(window.gizmo.minimumHeight(), 150)
        self.assertEqual(
            window.transform_box.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding)

        grid = window.gun_fields_grid
        expected = {
            window.gun_point_spins["x"]: (0, 1),
            window.gun_point_spins["y"]: (0, 3),
            window.gun_point_spins["z"]: (0, 5),
            window.gun_dir_spins["x"]: (1, 1),
            window.gun_dir_spins["z"]: (1, 3),
            window.gun_type_spin: (2, 1),
        }
        for widget, position in expected.items():
            index = grid.indexOf(widget)
            self.assertGreaterEqual(index, 0)
            row, column, _row_span, _column_span = grid.getItemPosition(index)
            self.assertEqual((row, column), position)

    def test_109_gun_direction_controls_are_canonical_and_name_is_script_name(self):
        window = self._window()
        self.assertEqual(window.gun_points_box.title(),
                         "Gun Points (Vanilla/OpenUA)")
        for spin in window.gun_dir_spins.values():
            self.assertEqual(spin.minimum(), -1.0)
            self.assertEqual(spin.maximum(), 1.0)
            self.assertEqual(spin.decimals(), 0)
            self.assertEqual(spin.singleStep(), 1.0)

        text = (
            "new_vehicle 56\n"
            " robo_num_guns = 1\n"
            " robo_act_gun = 0\n"
            " robo_gun_dir_z = 1\n"
            " robo_gun_type = 90\n"
            " robo_gun_name = FLAK1\n"
            "end\n"
        )
        block = find_script_blocks(text)[0]
        _enabled, points, _icon = import_gun_points_block(text, block)
        window.project.target_category = VEHICLE
        window.project.gun_points_enabled = True
        window.project.gun_points = points
        window._sync_all()
        self.assertEqual(window.gun_point_tree.topLevelItem(0).text(0),
                         "G1 — FLAK1")
        self.assertEqual(window.gun_point_tree.topLevelItem(0).text(1),
                         "Vanilla")

    def test_110_sphere_radius_controls_live_inside_spheres_panel(self):
        window = self._window()
        self.assertIs(window.radius_title.parentWidget(), window.spheres_box)
        self.assertTrue(window.radius_title.isHidden())
        self.assertIs(window.radius_slider.parentWidget(), window.spheres_box)
        self.assertIs(window.radius_spin.parentWidget(), window.spheres_box)
        self.assertLessEqual(window.radius_spin.maximumWidth(), 86)
        self.assertGreaterEqual(window.sphere_tree.minimumHeight(), 80)
        self.assertEqual(
            window.spheres_box.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)

    def test_111_right_properties_are_split_into_domain_tabs(self):
        window = self._window()
        self.assertEqual(
            [window.properties_tabs.tabText(index)
             for index in range(window.properties_tabs.count())],
            ["Collision", "Fire Points", "Gun Points", "Cockpit View"])
        self.assertIs(
            window.ground_alignment_box.parentWidget(), window.collision_tab)
        self.assertIs(window.spheres_box.parentWidget(), window.collision_tab)
        self.assertIs(
            window.fire_points_box.parentWidget(), window.fire_points_tab)
        self.assertIs(
            window.gun_points_box.parentWidget(), window.gun_points_tab)
        self.assertIs(window.cockpit_box.parentWidget(), window.cockpit_tab)
        self.assertEqual(window.cockpit_model_state_combo.currentData(), "normal")
        self.assertLessEqual(window.sphere_tree.minimumHeight(), 90)
        self.assertLessEqual(window.fire_point_tree.minimumHeight(), 90)
        self.assertLessEqual(window.gun_point_tree.minimumHeight(), 90)

    def test_112_properties_column_is_stable_wide_and_outer_scroll_free(self):
        window = self._window()
        self.assertEqual(
            window.properties_scroll.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(
            window.properties_scroll.verticalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertGreaterEqual(window.properties_scroll.minimumWidth(), 420)
        self.assertLessEqual(window.properties_scroll.minimumWidth(), 430)
        bar = window.properties_tabs.tabBar()
        self.assertTrue(bar.expanding())
        self.assertEqual(bar.elideMode(), Qt.TextElideMode.ElideNone)
        self.assertFalse(bar.usesScrollButtons())
        self.assertFalse(bar.isMovable())
        self.assertEqual(
            [window.properties_tabs.tabText(index)
             for index in range(window.properties_tabs.count())],
            ["Collision", "Fire Points", "Gun Points", "Cockpit View"])

    def test_113_gun_point_type_selector_is_explicit_and_non_converting(self):
        window = self._window()
        combo = window.gun_point_type_combo
        self.assertEqual(combo.itemData(0), "robo")
        self.assertEqual(combo.itemData(1), "unit")
        self.assertIn("Robo only", combo.itemText(0))
        self.assertIn("All vehicle classes", combo.itemText(1))

        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(scheme="robo", name="FLAK1")]
        window._select_gun_point(0)
        self.assertEqual(window._new_gun_point_scheme, "robo")
        self.assertEqual(window.project.gun_points[0].scheme, "robo")


    def test_114_cockpit_parser_reads_partial_axes_with_zero_fallback(self):
        text = (
            "new_vehicle 7\n"
            " cockpit_camera_offset_x = 12.5\n"
            " cockpit_camera_offset_z = -40\n"
            "end\n"
        )
        block = find_script_blocks(text)[0]
        enabled, x, y, z = import_cockpit_camera_block(text, block)
        self.assertTrue(enabled)
        self.assertEqual((x, y, z), (12.5, 0.0, -40.0))

    def test_115_cockpit_output_is_openua_vehicle_only_and_opt_in(self):
        project = CollisionProject(
            name="Wasp", source_model="wasp.sklt",
            target_category=VEHICLE, cockpit_camera_enabled=True,
            cockpit_camera_offset_x=10, cockpit_camera_offset_y=-5,
            cockpit_camera_offset_z=30)
        output = export_collision_text(project)
        self.assertIn("cockpit_camera_offset_x = 10", output)
        self.assertIn("cockpit_camera_offset_y = -5", output)
        self.assertIn("cockpit_camera_offset_z = 30", output)

        project.cockpit_camera_enabled = False
        self.assertNotIn("cockpit_camera_offset", export_collision_text(project))
        project.cockpit_camera_enabled = True
        project.target_category = WEAPON
        self.assertNotIn("cockpit_camera_offset", export_collision_text(project))

    def test_116_overwrite_replaces_only_managed_cockpit_parameters(self):
        text = (
            "new_vehicle 17\n"
            " name = Wasp\n"
            " cockpit_camera_offset_x = 999\n"
            " cockpit_camera_offset_y = 888\n"
            " cockpit_camera_offset_z = 777\n"
            " num_mguns = 2\n"
            "end\n"
        )
        project = CollisionProject(
            name="Wasp", source_model="wasp.sklt",
            target_category=VEHICLE, cockpit_camera_enabled=True,
            cockpit_camera_offset_x=1, cockpit_camera_offset_y=-2,
            cockpit_camera_offset_z=3)
        updated, _preview, _name = plan_script_update(
            text, "new_vehicle", 17, project, replace_all_managed=True)
        self.assertIn("cockpit_camera_offset_x = 1", updated)
        self.assertIn("cockpit_camera_offset_y = -2", updated)
        self.assertIn("cockpit_camera_offset_z = 3", updated)
        self.assertNotIn("cockpit_camera_offset_x = 999", updated)
        self.assertIn("num_mguns = 2", updated)

        project.cockpit_camera_enabled = False
        removed, _preview, _name = plan_script_update(
            text, "new_vehicle", 17, project, replace_all_managed=True)
        self.assertNotIn("cockpit_camera_offset", removed)
        self.assertIn("num_mguns = 2", removed)

    def test_117_cockpit_tab_enters_fixed_preview_and_restores_orbit_state(self):
        window = self._window()
        window.viewport._yaw = 23.0
        window.viewport._pitch = -17.0
        window.viewport._zoom = 2.5
        before = (
            window.viewport._yaw, window.viewport._pitch,
            window.viewport._zoom, QPointF(window.viewport._pan))

        window.properties_tabs.setCurrentIndex(window.cockpit_tab_index)
        self.assertTrue(window.viewport.cockpit_preview_active)
        self.assertEqual(window.transform_box.title(), "Move Cockpit Camera")
        self.assertFalse(window.toolbar_view_preset_combo.isEnabled())
        self.assertTrue(window.selected_box.isHidden())

        window.properties_tabs.setCurrentIndex(window.collision_tab_index)
        self.assertFalse(window.viewport.cockpit_preview_active)
        after = (
            window.viewport._yaw, window.viewport._pitch,
            window.viewport._zoom, QPointF(window.viewport._pan))
        self.assertEqual(after, before)
        self.assertTrue(window.toolbar_view_preset_combo.isEnabled())

    def test_118_cockpit_gizmo_moves_xyz_and_auto_enables_output(self):
        window = self._window()
        window.properties_tabs.setCurrentIndex(window.cockpit_tab_index)
        window.move_strength_spin.setValue(5)
        window._gizmo_nudge((1, -1, 1))
        self.assertTrue(window.project.cockpit_camera_enabled)
        self.assertEqual(
            (window.project.cockpit_camera_offset_x,
             window.project.cockpit_camera_offset_y,
             window.project.cockpit_camera_offset_z),
            (5.0, -5.0, 5.0))
        output = window.cockpit_output.toPlainText()
        self.assertIn("cockpit_camera_offset_x = 5", output)
        self.assertIn("cockpit_camera_offset_y = -5", output)
        self.assertIn("cockpit_camera_offset_z = 5", output)

    def test_119_cockpit_projection_uses_local_plus_z_forward(self):
        viewport = CollisionViewport()
        self.addCleanup(viewport.close)
        # Cockpit math uses authored UA units, not the normal editor auto-fit
        # scale. This is what keeps the real one-unit near plane meaningful.
        viewport._scale = 0.01
        viewport.set_cockpit_camera_offset(10.0, -4.0, 20.0)
        viewport.set_cockpit_preview_active(True)
        # Ten units in front on vehicle-local +Z lands ten perspective units
        # ahead of the camera; X/Y are measured relative to the authored eye.
        self.assertEqual(
            viewport._camera_vertex((10.0, -4.0, 30.0)),
            (0.0, 0.0, -6.0))
        self.assertEqual(
            viewport._camera_vertex((12.0, -6.0, 30.0)),
            (2.0, 2.0, -6.0))
        self.assertEqual(viewport._camera_state()["near_distance"], 1.0)

    def test_119b_cockpit_preview_uses_runtime_aspect_correction(self):
        viewport = CollisionViewport()
        self.addCleanup(viewport.close)
        viewport.resize(1000, 1000)
        viewport.set_cockpit_preview_active(True)
        viewport.set_cockpit_preview_aspect(16.0 / 10.0)
        target = viewport._cockpit_render_rect()
        self.assertAlmostEqual(target.width(), 1000.0)
        self.assertAlmostEqual(target.height(), 625.0)

        center = target.center()
        projected = viewport._project((10.0, 0.0, -6.0), target)
        half = (target.width() + target.height()) * 0.5
        corr_w = half * 1.1429 / target.width()
        expected_x = center.x() + 10.0 * (target.width() * 0.5 * corr_w) / 10.0
        self.assertAlmostEqual(projected.x(), expected_x, places=5)
        self.assertAlmostEqual(projected.y(), center.y(), places=5)

    def test_119b2_cockpit_culling_matches_opengl_window_winding(self):
        viewport = CollisionViewport()
        self.addCleanup(viewport.close)
        viewport.resize(1000, 625)
        viewport.set_cockpit_camera_offset(0.0, -40.0, 0.0)
        viewport.set_cockpit_preview_active(True)
        target = QRectF(0.0, 0.0, 1000.0, 625.0)

        # Eisenhans VP_PANZ1 polygon 0, runtime fan triangle (0, 2, 1).
        # OpenUA/OpenGL sees it as GL_CCW/front-facing. Converted to Qt
        # top-left coordinates its signed area is negative. The old
        # Cockpit View culled it and removed the lower nose panel.
        points = (
            (27.0, -30.0, 10.0),
            (-27.0, -3.0, 46.0),
            (-27.0, -30.0, 10.0),
        )
        screen = [
            viewport._project(viewport._camera_vertex(point), target)
            for point in points
        ]
        area = sum(
            screen[index].x() * screen[(index + 1) % 3].y()
            - screen[(index + 1) % 3].x() * screen[index].y()
            for index in range(3)
        )
        self.assertLess(area, 0.0)
        self.assertTrue(viewport._front_facing_from_screen_area(area))

        viewport.set_cockpit_preview_active(False)
        self.assertFalse(viewport._front_facing_from_screen_area(area))

    def test_119c_window_title_reports_script_vehicle_base_and_archive(self):
        window = self._window()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            window.project.name = "Eisenhans"
            window._active_script_path = root / "Vehicles.txt"
            window._active_base_path = root / "SET.BAS"
            window._active_model_reference = MagicMock(vp_normal=18)
            window._vp_table = MagicMock()
            window._vp_table.entries = [None] * 19
            window._vp_table.entry.return_value.base_name = "VP_PANZ1.base"
            window._set_modified(False)
            title = window.windowTitle()
            self.assertIn("Eisenhans", title)
            self.assertIn("VP_PANZ1.base", title)
            self.assertIn("SET.BAS", title)
            self.assertIn(str((root / "SET.BAS").resolve()), title)

    def test_119d_model_bounds_stay_on_authored_owner_during_vp_preview(self):
        window = self._window()
        window._current_owner_base_bounds = (-2.0, -3.0, -4.0, 5.0, 6.0, 7.0)
        window.project.model_scale_x = 2.0
        window.project.model_scale_y = 3.0
        window.project.model_scale_z = 4.0
        self.assertEqual(
            window._model_bounds(),
            (-4.0, -9.0, -16.0, 10.0, 18.0, 28.0))

    def test_120_weapon_mode_disables_vehicle_only_tabs(self):
        window = self._window()
        window.project.target_category = WEAPON
        window.properties_tabs.setCurrentIndex(window.cockpit_tab_index)
        window._sync_all()
        self.assertEqual(
            window.properties_tabs.currentIndex(), window.collision_tab_index)
        self.assertFalse(
            window.properties_tabs.isTabEnabled(window.fire_points_tab_index))
        self.assertFalse(
            window.properties_tabs.isTabEnabled(window.gun_points_tab_index))
        self.assertFalse(
            window.properties_tabs.isTabEnabled(window.cockpit_tab_index))
        self.assertFalse(window.viewport.cockpit_preview_active)

    def test_121_fire_point_reset_creates_origin_and_resets_existing_rack(self):
        window = self._window()
        window.project.fire_points_enabled = False
        window.project.fire_x = 12.0
        window.project.fire_y = -3.0
        window.project.fire_z = 7.0
        window.project.num_weapons = 0
        window._reset_fire_point_position()
        self.assertTrue(window.project.fire_points_enabled)
        self.assertEqual(window.project.num_weapons, 1)
        self.assertEqual(
            (window.project.fire_x, window.project.fire_y,
             window.project.fire_z),
            (0.0, 0.0, 0.0))
        self.assertEqual(window._selected_fire_point, 0)

        window.project.fire_x = 50.0
        window.project.fire_y = 10.0
        window.project.fire_z = -20.0
        window.project.num_weapons = 3
        window._reset_fire_point_position()
        self.assertEqual(window.project.num_weapons, 3)
        self.assertEqual(
            (window.project.fire_x, window.project.fire_y,
             window.project.fire_z),
            (0.0, 0.0, 0.0))

    def test_122_gun_point_reset_changes_position_only(self):
        window = self._window()
        point = GunPoint(
            scheme="unit", x=12, y=-4, z=9,
            dir_x=1, dir_y=0, dir_z=-1,
            gun_type=90, name="FLAK1")
        window.project.gun_points_enabled = True
        window.project.gun_points = [point]
        window._select_gun_point(0)
        window._reset_gun_point_position()
        self.assertEqual(point.position, (0.0, 0.0, 0.0))
        self.assertEqual((point.dir_x, point.dir_y, point.dir_z), (1, 0, -1))
        self.assertEqual(point.gun_type, 90)
        self.assertEqual(point.name, "FLAK1")
        self.assertEqual(point.scheme, "unit")

    def test_123_shared_gizmo_moves_only_active_tab_domain(self):
        window = self._window()
        window.project.compound = [
            CollisionSphere(VEHICLE, x=1, y=2, z=3, radius=10)]
        window.project.fire_points_enabled = True
        window.project.fire_x = 4
        window.project.fire_y = 5
        window.project.fire_z = 6
        window.project.num_weapons = 1
        window.project.gun_points_enabled = True
        window.project.gun_points = [GunPoint(scheme="unit", x=7, y=8, z=9)]
        window._selected = 0
        window._selected_fire_point = 0
        window._selected_gun_point = 0
        window.move_strength_spin.setValue(2)

        window.properties_tabs.setCurrentIndex(window.fire_points_tab_index)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.fire_x, 6.0)
        self.assertEqual(window.project.compound[0].x, 1)
        self.assertEqual(window.project.gun_points[0].x, 7)

        window.properties_tabs.setCurrentIndex(window.gun_points_tab_index)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.gun_points[0].x, 9.0)
        self.assertEqual(window.project.compound[0].x, 1)

        window.properties_tabs.setCurrentIndex(window.collision_tab_index)
        window._gizmo_nudge((1, 0, 0))
        self.assertEqual(window.project.compound[0].x, 3)

    def test_124_cockpit_gizmo_is_oblique_and_user_orbit_is_independent(self):
        window = self._window()
        window.properties_tabs.setCurrentIndex(window.cockpit_tab_index)
        window._sync_gizmo_camera()
        self.assertEqual(window.gizmo._anchor_yaw, -35.0)
        self.assertEqual(window.gizmo._anchor_pitch, 20.0)

        original_viewport = (window.viewport._yaw, window.viewport._pitch)
        window.gizmo._view_yaw_offset = 25.0
        window.gizmo._view_pitch_offset = -10.0
        window.gizmo._apply_gizmo_view_orientation()
        self.assertEqual(
            (window.viewport._yaw, window.viewport._pitch), original_viewport)
        self.assertEqual(window.gizmo._yaw, -10.0)
        self.assertEqual(window.gizmo._pitch, 10.0)
        window.gizmo.reset_view_orientation()
        self.assertEqual(window.gizmo._yaw, -35.0)
        self.assertEqual(window.gizmo._pitch, 20.0)


if __name__ == "__main__":
    unittest.main()
