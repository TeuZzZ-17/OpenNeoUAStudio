import copy
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu

from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_parser import (
    AmeshBlock,
    AttsEntry,
    BaseObject,
    TextureRef,
    parse_base_bytes,
)
from sklt_parser import (
    SkltModel,
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_poo2_pol2_structure,
)
from uv_editor_widget import UVEditorWidget, UVLoop
from uv_topology_editor import (
    UVTopologyError,
    apply_uv_vertex_deletion,
    plan_uv_vertex_deletion,
)


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _quad_base_bytes():
    atts = struct.pack(">hBBBB", 0, 10, 20, 30, 40)
    uvs = [(20, 20), (220, 20), (220, 220), (20, 220)]
    olpl = struct.pack(
        ">h" + "B" * (len(uvs) * 2),
        len(uvs), *(value for uv in uvs for value in uv))
    amesh = _form(b"AMSH", _chunk(b"ATTS", atts) + _chunk(b"OLPL", olpl))
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"QUAD.SKLT\0")))
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62)) + skeleton
        + _form(b"ADES", material))
    return _form(
        b"MC2 ", _form(
            b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


def _two_polygon_family():
    points = [
        (-2.0, -1.0, 0.0),
        (-0.5, -1.0, 0.0),
        (-0.5, 0.5, 0.0),
        (-2.0, 0.5, 0.0),
        (0.5, -1.0, 0.0),
        (2.0, -1.0, 0.0),
        (2.0, 0.5, 0.0),
        (0.5, 0.5, 0.0),
        (0.0, 1.0, 0.0),
        (1.0, 1.0, 0.0),
        (0.5, 2.0, 0.0),
    ]
    polygons = [
        [0, 1, 2, 3],
        [4, 5, 6, 7],
        [8, 9, 10],
    ]
    model = SkltModel(
        source_name="MULTI.SKLT",
        points=points,
        polygons=polygons,
        parsed_polygon_count=len(polygons),
        poo2_payload_offset=1,
        poo2_payload_size=len(points) * 12,
        pol2_payload_offset=2,
        pol2_payload_size=32,
    )
    groups = [
        [(20, 30), (80, 30), (80, 90), (20, 90)],
        [(130, 140), (190, 140), (190, 200), (130, 200)],
    ]
    block = AmeshBlock(
        class_id="amesh.class",
        texture=TextureRef(
            class_id="ilbm.class", kind="ilbm", name="BODY.ILBM"),
        atts=[
            AttsEntry(0, 1, 2, 3, 4),
            AttsEntry(1, 5, 6, 7, 8),
        ],
        olpl=copy.deepcopy(groups),
    )
    obj = FamilyObject(
        base_object=BaseObject(
            skeleton_class="sklt.class",
            skeleton_name="MULTI.SKLT",
            ades=[block]),
        skeleton=model,
        materials=[MaterialGroup(
            "BODY.ILBM",
            texture_name="BODY.ILBM",
            kind="ilbm",
            block=block,
            faces=[
                (0, list(groups[0]), 2),
                (1, list(groups[1]), 6),
            ])],
        owner_path="root",
    )
    return AssetFamily(root_object=obj), obj


def _open_memory_window(family, obj):
    window = AssemblyWindow()
    window._family = family
    window._owner_to_obj = {"root": obj}
    window._selected_owner = "root"
    window._workbench_obj = obj
    window.viewport.load_family(family, primary_owner="root")
    window.viewport.set_selected_owner("root")
    window._rebuild_workbench(family, "root")
    window._refresh_fx_elements()
    return window


class UVPhase3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_multi_polygon_edit_updates_all_olpl_with_one_undo(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            window._on_polygon_picked(1, True)
            self.assertEqual(window.uv_editor.loop_count(), 2)
            self.assertIn("2 compatible UV loop(s)", window.uv_editor.toolTip())
            self.assertNotEqual(
                window.uv_editor.loop_color(("root", 0, 0)).name(),
                window.uv_editor.loop_color(("root", 0, 1)).name())
            self.assertFalse(window.uv_add_vertex_button.isEnabled())
            self.assertIn(
                "exactly one editable UV loop/polygon",
                window.uv_add_vertex_button.toolTip())

            before = copy.deepcopy(obj.base_object.ades[0].olpl)
            window.uv_editor.select_all()
            window.uv_editor.nudge_selected(3, -2)
            after = copy.deepcopy(obj.base_object.ades[0].olpl)
            self.assertEqual(
                after,
                [[(u + 3, v - 2) for u, v in group] for group in before])
            self.assertEqual(len(window._edit_undo_stack), 1)
            self.assertEqual(
                window._edit_undo_stack[-1]["kind"], "uv_multi")
            self.assertEqual(len(window._bundle_base_edits("root", obj)[0]), 2)

            window._undo_edit()
            self.assertEqual(obj.base_object.ades[0].olpl, before)
            window._redo_edit()
            self.assertEqual(obj.base_object.ades[0].olpl, after)

            window._selected_polys = {0, 1, 2}
            window._selected_poly = 0
            window._update_uv_editor(0)
            self.assertEqual(window.uv_editor.loop_count(), 2)
            self.assertIn(
                "1 incompatible polygon(s) skipped",
                window.uv_editor.toolTip())
            self.assertIn("1 unmapped", window.uv_editor.toolTip())
        finally:
            window.close()

    def test_multi_loop_nudge_clamps_to_one_shared_boundary_delta(self):
        editor = UVEditorWidget()
        try:
            editor.set_loops(None, [
                UVLoop("left", 0, [(0, 10), (120, 20), (254, 30)]),
                UVLoop("right", 1, [(40, 40), (180, 50), (220, 60)]),
            ])
            editor.select_all()
            before = editor.loop_uvs()

            editor.nudge_selected(3, 0)
            after = editor.loop_uvs()
            deltas = {
                (after[key][index][0] - before[key][index][0],
                 after[key][index][1] - before[key][index][1])
                for key in before
                for index in range(len(before[key]))
            }
            self.assertEqual(deltas, {(1, 0)})

            editor.nudge_selected(1, 0)
            self.assertEqual(editor.loop_uvs(), after)
        finally:
            editor.close()

    def test_delete_plan_keeps_poo2_and_rejects_triangles(self):
        family, obj = _two_polygon_family()
        block = obj.base_object.ades[0]
        points_before = list(obj.skeleton.points)
        other_polygon = list(obj.skeleton.polygons[1])
        plan = plan_uv_vertex_deletion(
            obj.skeleton, block, 0, 0, 1)
        apply_uv_vertex_deletion(obj.skeleton, block, plan)

        self.assertEqual(obj.skeleton.points, points_before)
        self.assertEqual(obj.skeleton.polygons[0], [0, 2, 3])
        self.assertEqual(obj.skeleton.polygons[1], other_polygon)
        self.assertEqual(
            block.olpl[0], [(20, 30), (80, 90), (20, 90)])
        with self.assertRaises(UVTopologyError):
            plan_uv_vertex_deletion(
                obj.skeleton, block, 0, 0, 0)

    def test_delete_vertex_undo_redo_and_save_reload_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_sklt = root / "QUAD.SKLT"
            source_base = root / "QUAD.BASE"
            template = create_minimal_sklt_model()
            points = [
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (1.0, 1.0, 0.0),
                (-1.0, 1.0, 0.0),
            ]
            save_sklt_with_poo2_pol2_structure(
                template, points, [[0, 1, 2, 3]], source_sklt)
            base_bytes = _quad_base_bytes()
            source_base.write_bytes(base_bytes)
            model = parse_sklt_file(source_sklt)
            base_asset = parse_base_bytes(base_bytes)
            block = base_asset.root.ades[0]
            obj = FamilyObject(
                base_object=base_asset.root,
                skeleton_ref=type(
                    "Ref", (), {"path": source_sklt})(),
                skeleton=model,
                materials=[MaterialGroup(
                    "material", block=block,
                    faces=[(0, list(block.olpl[0]), 20)])],
                owner_path="root",
            )
            family = AssetFamily(
                base_path=source_base,
                base_asset=base_asset,
                root_object=obj,
            )
            window = _open_memory_window(family, obj)
            try:
                window._on_polygon_picked(0)
                window.uv_editor.select_point(1)
                self.assertTrue(window.uv_delete_vertex_button.isEnabled())
                window._delete_uv_vertex()
                self.assertEqual(model.polygons[0], [0, 2, 3])
                self.assertEqual(len(model.points), 4)
                self.assertEqual(len(block.olpl[0]), 3)
                self.assertEqual(len(window._edit_undo_stack), 1)

                window._undo_edit()
                self.assertEqual(model.polygons[0], [0, 1, 2, 3])
                window._redo_edit()
                self.assertEqual(model.polygons[0], [0, 2, 3])

                target_sklt = root / "export" / "QUAD.SKLT"
                target_base = root / "export" / "QUAD.BASE"
                self.assertTrue(window._write_model_files(
                    "root", family, obj, target_sklt, target_base,
                    ask_replace=False))
                saved_model = parse_sklt_file(target_sklt)
                saved_base = parse_base_bytes(
                    target_base.read_bytes()).root
                self.assertEqual(saved_model.polygons[0], [0, 2, 3])
                self.assertEqual(len(saved_model.points), 4)
                self.assertEqual(
                    saved_base.ades[0].olpl[0],
                    [(20, 20), (220, 220), (20, 220)])
                self.assertEqual(source_base.read_bytes(), base_bytes)
            finally:
                window.close()

    def test_delete_rolls_back_after_partial_failure(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            window.uv_editor.select_point(1)
            topology_before = window._capture_topology_state("root")

            def fail_after_mutation(model, block, plan):
                model.polygons[plan.poly_id].pop(plan.vertex_index)
                block.olpl[plan.atts_index].pop(plan.vertex_index)
                raise RuntimeError("simulated writer failure")

            with patch(
                    "assembly_window.apply_uv_vertex_deletion",
                    fail_after_mutation):
                window._delete_uv_vertex()

            topology_after = window._capture_topology_state("root")
            self.assertEqual(
                window._topology_content(topology_after),
                window._topology_content(topology_before))
            self.assertEqual(window._edit_undo_stack, [])
            self.assertNotIn("root", window._topology_original)
        finally:
            window.close()

    def test_delete_post_record_failure_restores_history_and_topology(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)
        try:
            window._on_polygon_picked(0)
            window.uv_editor.select_point(1)
            topology_before = window._capture_topology_state("root")

            with patch.object(
                    window.uv_editor, "select_handle",
                    side_effect=RuntimeError("simulated UI failure")):
                window._delete_uv_vertex()

            topology_after = window._capture_topology_state("root")
            self.assertEqual(
                window._topology_content(topology_after),
                window._topology_content(topology_before))
            self.assertEqual(window._edit_undo_stack, [])
            self.assertEqual(window._edit_redo_stack, [])
            self.assertNotIn("root", window._topology_original)
        finally:
            window.close()

    def test_handle_context_menu_contains_required_actions(self):
        family, obj = _two_polygon_family()
        window = _open_memory_window(family, obj)

        class CapturingMenu(QMenu):
            captured = None

            def exec(self, _position):
                type(self).captured = self

        try:
            window._on_polygon_picked(0)
            key = ("root", 0, 0)
            with patch("assembly_window.QMenu", CapturingMenu):
                window._show_uv_handle_context_menu(key, 1, window.pos())
            menu = CapturingMenu.captured
            labels = [
                action.text() for action in menu.actions()
                if not action.isSeparator()
            ]
            self.assertEqual(labels, [
                "Add Vertex After",
                "Delete Vertex",
                "Select This Vertex",
                "Select All UVs",
                "Clear Selection",
                "Revert Selected UV",
            ])
            actions = {action.text(): action for action in menu.actions()}
            self.assertTrue(actions["Add Vertex After"].isEnabled())
            self.assertTrue(actions["Delete Vertex"].isEnabled())
            self.assertFalse(actions["Clear Selection"].isEnabled())
            self.assertFalse(actions["Revert Selected UV"].isEnabled())
            self.assertTrue(
                actions["Revert Selected UV"].toolTip())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
