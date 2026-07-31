import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QMessageBox, QSizePolicy,
    QToolBar, QToolButton,
)

from asset_family import AssetFamily, FamilyObject
from base_parser import BaseObject
from collision_editor import (
    ApplyScriptDialog,
    CollisionEditorWindow,
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    CollisionViewport,
    LEGACY,
    VEHICLE,
    WEAPON,
    TYPE_COLORS,
    VIEW_PRESETS,
    create_backup,
    effective_runtime_radius,
    export_collision_text,
    find_script_blocks,
    import_collision_block,
    plan_script_update,
    read_script_file,
    validate_project,
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

    def test_32_model_list_has_only_internal_path_and_vp(self):
        window = self._window()
        self.assertEqual(window.model_tree.columnCount(), 2)
        self.assertEqual([
            window.model_tree.headerItem().text(index)
            for index in range(2)
        ], ["Internal path", "VP"])
        file_labels = [
            action.text() for action in window.file_menu.actions()]
        self.assertIn("Open BAS Archive...", file_labels)
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
        self.assertEqual(window.sphere_tree.topLevelItem(1).text(2), "0")
        third = window.sphere_tree.topLevelItem(2)
        window.sphere_tree.setCurrentItem(third)
        self.assertEqual(window._selected, 2)
        self.assertEqual(window.type_value.text(), "Weapon Collision")
        self.assertEqual(third.text(2), "1")
        self.assertEqual(
            float(third.text(1)), window.project.compound[1].radius)

    def test_34_move_gizmo_is_large_six_axis_and_tracks_camera(self):
        window = self._window()
        self.assertGreaterEqual(window.gizmo.minimumWidth(), 285)
        self.assertGreaterEqual(window.gizmo.minimumHeight(), 220)
        self.assertGreater(window.gizmo.maximumHeight(), 10000)
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

    def test_39_toolbar_open_undo_redo_follow_view_preset(self):
        window = self._window()
        toolbar = window.findChild(QToolBar, "collisionTools")
        labels = [action.text() for action in toolbar.actions()]
        self.assertLess(
            labels.index("Open BAS Archive..."),
            labels.index("Open SKLT..."))
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
                "Reset View", "Open BAS Archive...", "Open SKLT...",
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
        self.assertGreaterEqual(window.sphere_tree.minimumHeight(), 180)
        self.assertGreater(window.sphere_tree.maximumHeight(), 10000)
        self.assertEqual(
            window.sphere_tree.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)
        self.assertEqual(
            window.spheres_box.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Expanding)

    def test_48_identical_spheres_keep_distinct_dense_indices(self):
        window = self._window()
        window.add_compound(VEHICLE)
        window.duplicate_sphere()
        window.duplicate_sphere()
        indices = [
            window.sphere_tree.topLevelItem(index).text(2)
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
        self.assertGreaterEqual(source_panel.minimumWidth(), 260)
        self.assertGreaterEqual(window.main_splitter.sizes()[0], 260)
        self.assertEqual(
            window.model_tree.header().sectionResizeMode(0),
            QHeaderView.ResizeMode.Stretch)
        self.assertEqual(
            window.model_tree.header().sectionResizeMode(1),
            QHeaderView.ResizeMode.ResizeToContents)

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
        self.assertEqual(window.sphere_tree.columnCount(), 3)
        self.assertEqual(window.sphere_tree.headerItem().text(2), "Index")

    def test_57_vp_values_are_right_aligned_without_widening_panel(self):
        window = self._window()
        with patch("collision_editor.load_asset_family", return_value=_family()):
            window.open_base("sample.base")
        item = window.model_tree.topLevelItem(0)
        alignment = item.textAlignment(1)
        self.assertTrue(alignment & int(Qt.AlignmentFlag.AlignRight))
        self.assertGreaterEqual(window.main_splitter.widget(0).minimumWidth(), 260)
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


if __name__ == "__main__":
    unittest.main()
