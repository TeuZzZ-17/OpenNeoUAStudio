import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from assembly_viewer import AssetViewport
from assembly_window import AssemblyWindow
from asset_family import (
    AssetFamily, FamilyObject, MaterialGroup, rebuild_materials)
from base_mapping_editor import MappingIndex
from base_parser import AmeshBlock, AttsEntry, BaseObject, TextureRef
from geometry_editor import GeometryEditSession
from sklt_parser import SkltModel


def _selection_family():
    model = SkltModel(
        source_name="SELECT.SKLT",
        points=[
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.0),
        ],
        polygons=[[0, 1, 2], [1, 3, 2]],
        parsed_polygon_count=2,
        poo2_payload_offset=1,
        poo2_payload_size=48,
        pol2_payload_offset=2,
        pol2_payload_size=16,
    )
    block = AmeshBlock(
        class_id="amesh.class",
        texture=TextureRef(
            class_id="ilbm.class", kind="ilbm", name="STONE.ILBM"),
        atts=[
            AttsEntry(0, 1, 2, 3, 4),
            AttsEntry(1, 5, 6, 7, 8),
        ],
        olpl=[
            [(0, 0), (255, 0), (0, 255)],
            [(255, 0), (255, 255), (0, 255)],
        ],
        atts_chunk_offset=1,
        olpl_chunk_offset=2,
    )
    base_object = BaseObject(
        name="SELECT", skeleton_name="SELECT.SKLT", ades=[block])
    obj = FamilyObject(
        base_object=base_object,
        skeleton_ref=SimpleNamespace(path=None, status="memory",
                                     source="memory"),
        skeleton=model,
        materials=[MaterialGroup(
            "STONE.ILBM", texture_name="STONE.ILBM", kind="ilbm",
            block=block,
            faces=[
                (0, list(block.olpl[0]), 2),
                (1, list(block.olpl[1]), 6),
            ],
        )],
        owner_path="root",
    )
    family = AssetFamily(root_object=obj)
    return family, obj, model


def _prepare_window():
    family, obj, model = _selection_family()
    window = AssemblyWindow()
    window._family = family
    window._owner_to_obj = {"root": obj}
    window._selected_owner = "root"
    window._workbench_obj = obj
    window._mapping_index = MappingIndex(obj)
    window.viewport.load_family(family, primary_owner="root")
    window.viewport.set_selected_owner("root")
    window.viewport.enter_edit_mode("root")
    window.viewport.configure_edit_interaction(
        selected_only=False, pick_polygons=True)
    window._sync_edit_action_states()
    return window, obj, model


class SelectionRuntimeV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_complete_vertex_selection_enables_original_delete_and_undo(self):
        window, _obj, model = _prepare_window()
        try:
            session = window.viewport.edit_session
            session.selection = {0, 1}
            window._on_edit_selection_details_changed({0, 1}, "vertex")
            self.assertEqual(window._resolved_geometry_polys(), set())
            self.assertFalse(window.delete_geometry_action.isEnabled())

            session.selection = {0, 1, 2}
            window._on_edit_selection_details_changed({0, 1, 2}, "vertex")
            self.assertEqual(window._resolved_geometry_polys(), {0})
            self.assertTrue(window.copy_geometry_action.isEnabled())
            self.assertTrue(window.cut_geometry_action.isEnabled())
            self.assertTrue(window.delete_geometry_action.isEnabled())

            original = [list(polygon) for polygon in model.polygons]
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes):
                window._delete_geometry()
            self.assertEqual(len(model.polygons), 1)
            self.assertEqual(
                [model.points[index] for index in model.polygons[0]],
                [(1.0, 0.0, 0.0),
                 (1.0, 1.0, 0.0),
                 (0.0, 1.0, 0.0)])
            self.assertEqual(window._selected_polys, set())
            self.assertEqual(window.viewport.edit_session.selection, set())
            window._undo_edit()
            self.assertEqual(model.polygons, original)
            window._redo_edit()
            self.assertEqual(len(model.polygons), 1)
        finally:
            window.close()

    def test_mouse_to_resolver_to_delete_action_to_undo_redo(self):
        window, _obj, model = _prepare_window()
        window.viewport.resize(400, 300)
        window.viewport.show()
        QApplication.processEvents()
        try:
            def vertex_at(point):
                if point.x() < 70:
                    return 0
                if point.x() < 100:
                    return 1
                return 2

            with patch.object(
                    window.viewport, "_nearest_edit_vertex",
                    side_effect=vertex_at):
                QTest.mouseClick(
                    window.viewport, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier, QPoint(50, 80))
                QTest.mouseClick(
                    window.viewport, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier, QPoint(80, 80))
                QTest.mouseClick(
                    window.viewport, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier, QPoint(110, 80))
            self.assertEqual(
                window.viewport.edit_session.selection, {0, 1, 2})
            self.assertEqual(window._resolved_geometry_polys(), {0})
            self.assertTrue(window.delete_geometry_action.isEnabled())
            original = [list(polygon) for polygon in model.polygons]
            with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes):
                window.delete_geometry_action.trigger()
            self.assertEqual(len(model.polygons), 1)
            window._undo_edit()
            self.assertEqual(model.polygons, original)
            window._redo_edit()
            self.assertEqual(len(model.polygons), 1)
        finally:
            window.close()

    def test_shared_vertices_do_not_select_an_extra_polygon(self):
        window, _obj, _model = _prepare_window()
        try:
            session = window.viewport.edit_session
            session.selection = {0, 1, 2}
            window._on_edit_selection_details_changed(
                session.selection, "vertex")
            self.assertEqual(window._resolved_geometry_polys(), {0})
            session.selection = {1, 2, 3}
            window._on_edit_selection_details_changed(
                session.selection, "vertex")
            self.assertEqual(window._resolved_geometry_polys(), {1})
            session.selection = {0, 1, 2, 3}
            window._on_edit_selection_details_changed(
                session.selection, "vertex")
            self.assertEqual(window._resolved_geometry_polys(), {0, 1})
        finally:
            window.close()

    def test_explicit_polygon_pick_does_not_expand_from_shared_vertices(self):
        window, _obj, _model = _prepare_window()
        try:
            # Simulate two overlapping POL2 faces which reuse the exact same
            # POO2 vertices.  A direct click must remain one polygon even
            # though the edit session necessarily selects those shared points.
            window._workbench_obj.skeleton.polygons[1] = [0, 1, 2]
            window._on_polygon_picked(0, False)
            window.viewport.edit_session.selection = {0, 1, 2}
            self.assertEqual(window._selected_polys, {0})
            self.assertEqual(window._resolved_geometry_polys(), {0})
        finally:
            window.close()

    def test_visible_owner_rebuild_preserves_edit_and_view_modes(self):
        family, _obj, _model = _selection_family()
        viewport = AssetViewport()
        try:
            viewport.load_family(family, primary_owner="root")
            self.assertFalse(viewport.is_edit_mode)
            viewport.set_visible_owners({"root"})
            self.assertFalse(viewport.is_edit_mode)

            self.assertTrue(viewport.enter_edit_mode("root"))
            viewport.set_visible_owners({"root"})
            self.assertTrue(viewport.is_edit_mode)
            self.assertEqual(viewport.edit_owner, "root")
        finally:
            viewport.close()

    def test_family_reload_follows_active_main_tab_edit_contract(self):
        family, _obj, _model = _selection_family()
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window.edit_toggle_action.setChecked(True)
            self.assertTrue(window.edit_toggle_action.isChecked())
            self.assertTrue(window.viewport.is_edit_mode)

            window._set_family(family)
            self.assertIs(
                window._right_tabs.currentWidget(), window._resources_tabs)
            self.assertFalse(window.edit_toggle_action.isChecked())
            self.assertFalse(window.viewport.is_edit_mode)

            window._show_model_editor()
            self.assertTrue(window.edit_toggle_action.isChecked())
            self.assertTrue(window.viewport.is_edit_mode)

            window.edit_toggle_action.setChecked(False)
            self.assertFalse(window.viewport.is_edit_mode)
            window._set_family(family)
            self.assertFalse(window.edit_toggle_action.isChecked())
            self.assertFalse(window.viewport.is_edit_mode)
        finally:
            window.close()

    def test_editor_tab_auto_mode_preserves_selection_and_history(self):
        family, _obj, _model = _selection_family()
        window = AssemblyWindow()
        try:
            window._set_family(family)
            self.assertFalse(hasattr(window, "global_edit_button"))
            self.assertTrue(window.edit_toggle_action.isCheckable())
            self.assertEqual(
                window.edit_toggle_action.shortcut().toString(), "Tab")

            window._show_model_editor()
            self.assertTrue(window.edit_toggle_action.isChecked())
            self.assertTrue(window.viewport.is_edit_mode)
            window.viewport.edit_session.selection = {0, 1}
            marker = {"kind": "test-marker"}
            window._edit_undo_stack.append(marker)

            window._right_tabs.setCurrentWidget(window._resources_tabs)
            self.assertFalse(window.edit_toggle_action.isChecked())
            self.assertFalse(window.viewport.is_edit_mode)
            self.assertEqual(window._edit_undo_stack, [marker])

            window._show_model_editor()
            self.assertTrue(window.edit_toggle_action.isChecked())
            self.assertTrue(window.viewport.is_edit_mode)
            self.assertEqual(window.viewport.edit_session.selection, {0, 1})
            self.assertEqual(window._edit_undo_stack, [marker])

            window.viewport.set_edit_read_only_vertices({3})
            window.poly_select_all_button.click()
            self.assertEqual(
                window.viewport.edit_session.selection, {0, 1, 2})
            window.poly_deselect_all_button.click()
            self.assertEqual(window.viewport.edit_session.selection, set())
            window._right_tabs.setCurrentWidget(window._resources_tabs)
            window._show_model_editor()
            self.assertEqual(window.viewport.edit_session.selection, set())
        finally:
            window.close()

    def test_load_texture_updates_only_the_selected_material_block(self):
        family, obj, _model = _selection_family()
        first = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm",
                name="STONE.ILBM"),
            atts=[AttsEntry(0, 1, 2, 3, 4)],
            olpl=[[(0, 0), (255, 0), (0, 255)]],
        )
        second = AmeshBlock(
            class_id="amesh.class",
            texture=TextureRef(
                class_id="ilbm.class", kind="ilbm",
                name="OTHER.ILBM"),
            atts=[AttsEntry(1, 5, 6, 7, 8)],
            olpl=[[(255, 0), (255, 255), (0, 255)]],
        )
        obj.base_object.ades[:] = [first, second]
        rebuild_materials(obj, family)
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window._show_model_editor()
            window._on_polygon_picked(0)
            self.assertFalse(window._apply_texture_history({
                ("root", 0, "texture"): "BROKEN.ILBM",
                ("root", 99, "texture"): "BROKEN.ILBM",
            }))
            self.assertEqual(
                [block.texture.name for block in obj.base_object.ades],
                ["STONE.ILBM", "OTHER.ILBM"])
            with patch.object(
                    window, "_available_model_textures",
                    return_value=["MARBLE.ILBM"]), \
                    patch.object(
                        window, "_choose_model_texture",
                        return_value="MARBLE.ILBM"), \
                    patch.object(
                        window, "_ensure_model_texture_loaded",
                        return_value="MARBLE.ILBM"):
                window._load_model_texture()

            self.assertEqual(
                [block.texture.name for block in obj.base_object.ades],
                ["MARBLE.ILBM", "OTHER.ILBM"])
            command = window._edit_undo_stack[-1]
            self.assertEqual(command["kind"], "texture")
            self.assertEqual(len(command["before"]), 1)
            window._undo_edit()
            self.assertEqual(
                [block.texture.name for block in obj.base_object.ades],
                ["STONE.ILBM", "OTHER.ILBM"])
            window._redo_edit()
            self.assertEqual(
                [block.texture.name for block in obj.base_object.ades],
                ["MARBLE.ILBM", "OTHER.ILBM"])
        finally:
            window.close()

    def test_load_texture_updates_every_selected_material_block(self):
        family, obj, _model = _selection_family()
        obj.base_object.ades[:] = [
            AmeshBlock(
                class_id="amesh.class",
                texture=TextureRef(
                    class_id="ilbm.class", kind="ilbm",
                    name=name),
                atts=[AttsEntry(poly_id, 1, 2, 3, 4)],
                olpl=[[(0, 0), (255, 0), (0, 255)]],
            )
            for poly_id, name in (
                (0, "STONE.ILBM"), (1, "OTHER.ILBM"))
        ]
        rebuild_materials(obj, family)
        window = AssemblyWindow()
        try:
            window._set_family(family)
            window._show_model_editor()
            window._selected_polys = {0, 1}
            window._selected_poly = 0
            window.viewport.set_highlight_polys({0, 1})
            with patch.object(
                    window, "_available_model_textures",
                    return_value=["MARBLE.ILBM"]), \
                    patch.object(
                        window, "_choose_model_texture",
                        return_value="MARBLE.ILBM"), \
                    patch.object(
                        window, "_ensure_model_texture_loaded",
                        return_value="MARBLE.ILBM"):
                window._load_model_texture()
            self.assertEqual(
                [block.texture.name for block in obj.base_object.ades],
                ["MARBLE.ILBM", "MARBLE.ILBM"])
            self.assertEqual(
                len(window._edit_undo_stack[-1]["before"]), 2)
        finally:
            window.close()

    def test_load_texture_warns_when_selected_segment_has_no_binding(self):
        window, obj, _model = _prepare_window()
        try:
            obj.base_object.ades.clear()
            rebuild_materials(obj, window._family)
            window._mapping_index = MappingIndex(obj)
            window._on_polygon_picked(0)

            self.assertTrue(window.load_texture_button.isEnabled())
            with patch.object(QMessageBox, "warning") as warning:
                window._load_model_texture()

            warning.assert_called_once()
            title, message = warning.call_args.args[1:3]
            self.assertEqual(title, "Texture cannot be replaced")
            self.assertIn("Polygon #0", message)
            self.assertIn("no ATTS material mapping", message)
            self.assertIn("No unsupported material data was modified", message)
            self.assertEqual(obj.base_object.ades, [])
        finally:
            window.close()

    def test_direct_polygon_pick_and_box_selection_use_same_actions(self):
        window, _obj, _model = _prepare_window()
        try:
            window._on_polygon_picked(0)
            self.assertEqual(window._resolved_geometry_polys(), {0})
            window._on_polygon_picked(1)
            self.assertEqual(window._resolved_geometry_polys(), {1})
            window._on_polygon_picked(0, additive=True)
            self.assertEqual(window._resolved_geometry_polys(), {0, 1})
            window._on_polygon_picked(0, additive=True)
            self.assertEqual(window._resolved_geometry_polys(), {1})
            self.assertTrue(window.delete_geometry_action.isEnabled())

            session = window.viewport.edit_session
            session.selection = {0, 1, 2}
            window._on_edit_selection_details_changed(
                session.selection, "vertex")
            self.assertEqual(window._selected_polys, set())
            self.assertEqual(window._resolved_geometry_polys(), {0})
            self.assertTrue(window.copy_geometry_action.isEnabled())
        finally:
            window.close()

    def test_ctrl_drag_box_updates_resolver_and_actions(self):
        window, _obj, _model = _prepare_window()
        window.viewport.resize(400, 300)
        window.viewport.show()
        QApplication.processEvents()
        screen = [
            QPointF(50, 50),
            QPointF(100, 50),
            QPointF(50, 100),
            QPointF(260, 220),
        ]
        try:
            with patch.object(
                    window.viewport, "_edit_screen_points",
                    return_value=screen), patch.object(
                    window.viewport, "_available_edit_vertices",
                    return_value={0, 1, 2, 3}):
                QTest.mousePress(
                    window.viewport, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier, QPoint(20, 20))
                QTest.mouseMove(
                    window.viewport, QPoint(130, 130), delay=1)
                QTest.mouseRelease(
                    window.viewport, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.ControlModifier, QPoint(130, 130))
            self.assertEqual(
                window.viewport.edit_session.selection, {0, 1, 2})
            self.assertEqual(window._resolved_geometry_polys(), {0})
            self.assertTrue(window.copy_geometry_action.isEnabled())
            self.assertTrue(window.delete_geometry_action.isEnabled())
        finally:
            window.close()


class VertexClickDragV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.model = SkltModel(
            source_name="VERTEX.SKLT",
            points=[
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ],
            polygons=[[0, 1, 2]],
            parsed_polygon_count=1,
        )
        obj = SimpleNamespace(
            skeleton=self.model,
            base_object=SimpleNamespace(ades=[]),
        )
        self.viewport = AssetViewport()
        self.viewport.resize(400, 300)
        self.viewport._edit_session = GeometryEditSession(
            obj,
            [[1.0, 0.0, 0.0],
             [0.0, 1.0, 0.0],
             [0.0, 0.0, 1.0]],
            (0.0, 0.0, 0.0),
        )
        self.viewport._edit_owner = "root"
        self.viewport.show()
        QApplication.processEvents()

    def tearDown(self):
        self.viewport.close()

    def test_click_selected_reduces_selection_without_move_or_undo(self):
        session = self.viewport.edit_session
        session.selection = {0, 1}
        before = list(self.model.points)
        commands = []
        self.viewport.geometryCommandCommitted.connect(
            lambda *args: commands.append(args))
        with patch.object(
                self.viewport, "_nearest_edit_vertex", return_value=0):
            QTest.mouseClick(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
        self.assertEqual(session.selection, {0})
        self.assertEqual(self.model.points, before)
        self.assertEqual(commands, [])

    def test_drag_selected_keeps_original_group_and_commits_once(self):
        session = self.viewport.edit_session
        session.selection = {0, 1}
        before = list(self.model.points)
        commands = []
        self.viewport.geometryCommandCommitted.connect(
            lambda *args: commands.append(args))
        with patch.object(
                self.viewport, "_nearest_edit_vertex", return_value=0):
            QTest.mousePress(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
            QTest.mouseMove(self.viewport, QPoint(98, 87), delay=1)
            QTest.mouseRelease(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(98, 87))
        self.assertEqual(session.selection, {0, 1})
        self.assertNotEqual(self.model.points[0], before[0])
        self.assertNotEqual(self.model.points[1], before[1])
        self.assertEqual(self.model.points[2], before[2])
        self.assertEqual(len(commands), 1)

    def test_direct_rotate_and_scale_follow_mode_and_commit_once(self):
        session = self.viewport.edit_session
        for mode, intensity in (("rotate", 5.0), ("scale", 5.0)):
            with self.subTest(mode=mode):
                self.model.points[:] = [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ]
                session.selection = {0, 1, 2}
                session._undo.clear()
                session._redo.clear()
                self.viewport.set_direct_transform(mode, intensity)
                before = list(self.model.points)
                commands = []
                self.viewport.geometryCommandCommitted.connect(
                    lambda *args, target=commands: target.append(args))
                start = QPoint(260, 150)
                end = (
                    QPoint(200, 90) if mode == "rotate"
                    else QPoint(330, 150))
                with patch.object(
                        self.viewport, "_nearest_edit_vertex",
                        return_value=0):
                    QTest.mousePress(
                        self.viewport, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier, start)
                    QTest.mouseMove(
                        self.viewport, end, delay=1)
                    QTest.mouseRelease(
                        self.viewport, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier, end)
                self.assertNotEqual(self.model.points, before)
                self.assertEqual(len(commands), 1)

    def test_escape_cancels_direct_mouse_transform(self):
        session = self.viewport.edit_session
        session.selection = {0, 1}
        self.viewport.set_direct_transform("move", 0.5)
        before = list(self.model.points)
        commands = []
        self.viewport.geometryCommandCommitted.connect(
            lambda *args: commands.append(args))
        with patch.object(
                self.viewport, "_nearest_edit_vertex", return_value=0):
            QTest.mousePress(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
            QTest.mouseMove(self.viewport, QPoint(110, 95), delay=1)
            QTest.keyClick(self.viewport, Qt.Key.Key_Escape)
            QTest.mouseRelease(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(110, 95))
        self.assertEqual(self.model.points, before)
        self.assertEqual(commands, [])

    def test_click_unselected_and_ctrl_click_keep_expected_semantics(self):
        session = self.viewport.edit_session
        session.selection = {0}
        with patch.object(
                self.viewport, "_nearest_edit_vertex", return_value=2):
            QTest.mouseClick(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, QPoint(80, 80))
        self.assertEqual(session.selection, {2})
        with patch.object(
                self.viewport, "_nearest_edit_vertex", return_value=1):
            QTest.mouseClick(
                self.viewport, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ControlModifier, QPoint(80, 80))
        self.assertEqual(session.selection, {1, 2})


if __name__ == "__main__":
    unittest.main()
