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
from asset_family import AssetFamily, FamilyObject, MaterialGroup
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

    def test_direct_polygon_pick_and_box_selection_use_same_actions(self):
        window, _obj, _model = _prepare_window()
        try:
            window._on_polygon_picked(1)
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
                    return_value=screen):
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
