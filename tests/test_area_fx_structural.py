import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from assembly_window import AssemblyWindow
from asset_family import AssetFamily, FamilyObject, rebuild_materials
from base_mapping_editor import MappingIndex
from base_parser import AmeshBlock, AttsEntry, parse_base_bytes
from fx_element_editor import (
    append_fx_element_clipboard,
    build_fx_element_clipboard,
    build_new_fx_clipboard,
    detect_fx_elements,
)
from geometry_editor import (
    GeometryClipboardError,
    apply_delete_geometry,
    append_geometry_clipboard,
    build_geometry_clipboard,
    plan_delete_geometry,
)
from polygon_reference_graph import (
    build_polygon_reference_graph,
    diagnose_polygon_references,
    plan_polygon_renumber,
)
from sklt_parser import (
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_poo2_pol2_structure,
)


UVS = ((0, 0), (255, 0), (255, 255), (0, 255))


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _area_objt(poly_id, texture_name):
    if texture_name.upper().endswith(".ANM"):
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"bmpanim.class\0")
            + _form(
                b"BANI",
                _chunk(
                    b"STRC",
                    struct.pack(">hhh", 1, 6, 1)
                    + texture_name.encode("ascii") + b"\0")),
        )
    else:
        texture = _form(
            b"OBJT",
            _chunk(b"CLID", b"ilbm.class\0")
            + _form(
                b"CIBO",
                _chunk(b"NAM2", texture_name.encode("ascii") + b"\0")
                + _chunk(
                    b"OTL2",
                    bytes(value for uv in UVS for value in uv))),
        )
    ade = _form(
        b"ADE ",
        _chunk(
            b"STRC",
            struct.pack(">hbbhhh", 1, 0, 0, -1, poly_id, 0)),
    )
    area = _form(
        b"AREA",
        ade
        + _chunk(
            b"STRC",
            struct.pack(">hHHBBBB", 1, 0, 0x88, 0, 255, 0, 255))
        + texture,
    )
    return _form(
        b"OBJT", _chunk(b"CLID", b"area.class\0") + area)


def _area_base_bytes():
    # ADES[4]/[5] are the concrete bilateral FX2 POL2 19/20 pair.
    materials = b"".join(
        _area_objt(
            poly_id,
            "FX2.ILBM" if poly_id in (19, 20)
            else "MODEL.ANM" if poly_id == 15
            else "STONE.ILBM")
        for poly_id in range(15, 22)
    )
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"FXTEST.SKLT\0")),
    )
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62))
        + skeleton
        + _form(b"ADES", materials),
    )
    return _form(
        b"MC2 ",
        _form(
            b"OBJT",
            _chunk(b"CLID", b"base.class\0") + base),
    )


def _topology():
    points = []
    polygons = []
    for poly_id in range(19):
        first = len(points)
        x = float(poly_id * 2)
        points.extend([
            (x, 0.0, 0.0),
            (x + 1.0, 0.0, 0.0),
            (x + 1.0, 1.0, 0.0),
            (x, 1.0, 0.0),
        ])
        polygons.append([first, first + 1, first + 2, first + 3])
    first = len(points)
    points.extend([
        (50.0, -1.0, -1.0),
        (50.0, 1.0, -1.0),
        (50.0, 1.0, 1.0),
        (50.0, -1.0, 1.0),
    ])
    polygons.append([first, first + 1, first + 2, first + 3])
    polygons.append([first + 3, first + 2, first + 1, first])
    first = len(points)
    points.extend([
        (60.0, 0.0, 0.0),
        (61.0, 0.0, 0.0),
        (61.0, 1.0, 0.0),
        (60.0, 1.0, 0.0),
    ])
    polygons.append([first, first + 1, first + 2, first + 3])
    return points, polygons


def _family(root):
    sklt_path = root / "FXTEST.SKLT"
    base_path = root / "FXTEST.BASE"
    points, polygons = _topology()
    save_sklt_with_poo2_pol2_structure(
        create_minimal_sklt_model(), points, polygons, sklt_path)
    original_base = _area_base_bytes()
    base_path.write_bytes(original_base)
    base_asset = parse_base_bytes(original_base, str(base_path))
    obj = FamilyObject(
        base_object=base_asset.root,
        skeleton_ref=SimpleNamespace(
            path=sklt_path, status="found", source="loose"),
        skeleton=parse_sklt_file(sklt_path),
        owner_path="root",
    )
    family = AssetFamily(
        base_path=base_path,
        base_asset=base_asset,
        root_object=obj,
    )
    rebuild_materials(obj, family)
    return family, obj, original_base


class AreaFxStructuralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_reference_graph_area_amesh_particle_and_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            _family_value, obj, _original = _family(Path(temp))
            graph = build_polygon_reference_graph(
                obj, detect_fx_elements(obj))
            refs = graph.references_for_polygon(19)
            self.assertEqual(len(refs), 1)
            self.assertEqual(
                (refs[0].class_id, refs[0].block_index, refs[0].location),
                ("area.class", 4, "FORM AREA/FORM ADE/STRC.polyID"))
            plan = plan_polygon_renumber(
                graph,
                {old: old if old < 19 else old - 2
                 for old in range(22) if old not in (19, 20)},
                {19, 20})
            self.assertEqual(plan.removed_block_indices, (4, 5))
            self.assertEqual(
                [block.ade_poly_id for block in plan.blocks],
                [15, 16, 17, 18, 19])

            obj.base_object.ades.append(AmeshBlock(
                class_id="mystery.class", payload_form_type="MYST"))
            with self.assertRaisesRegex(
                    GeometryClipboardError,
                    r"mystery\.class.*root/ADES\[7\]/MYST.*polyIDs"):
                plan_delete_geometry(obj, {19, 20}, MappingIndex(obj))

    def test_delete_19_20_compacts_area_refs_points_and_undo_redo(self):
        with tempfile.TemporaryDirectory() as temp:
            family, obj, _original = _family(Path(temp))
            elements = detect_fx_elements(obj)
            bilateral = next(element for element in elements
                             if element.poly_ids == (19, 20))
            self.assertTrue(bilateral.bilateral)
            plan = plan_delete_geometry(
                obj, set(bilateral.poly_ids), MappingIndex(obj))
            self.assertEqual(plan.deleted_polygon_indices, (19, 20))
            self.assertEqual(
                [block.ade_poly_id for block in plan.blocks],
                [15, 16, 17, 18, 19])
            old_point_count = len(obj.skeleton.points)

            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                window.viewport.load_family(family, primary_owner="root")
                window.viewport.set_selected_owner("root")
                window.viewport.enter_edit_mode("root")
                window._apply_delete_plan(
                    "root", obj, plan, label="Delete FX2 FX")
                self.assertEqual(len(obj.skeleton.polygons), 20)
                self.assertEqual(len(obj.skeleton.points), old_point_count - 4)
                self.assertEqual(len(obj.base_object.ades), 5)
                self.assertEqual(
                    [block.ade_poly_id for block in obj.base_object.ades],
                    [15, 16, 17, 18, 19])
                self.assertEqual(diagnose_polygon_references(obj), [])

                window._undo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 22)
                self.assertEqual(len(obj.base_object.ades), 7)
                self.assertEqual(
                    [block.ade_poly_id for block in obj.base_object.ades],
                    list(range(15, 22)))
                window._redo_edit()
                self.assertEqual(len(obj.skeleton.polygons), 20)
                self.assertEqual(len(obj.base_object.ades), 5)
            finally:
                window.close()

    def test_copy_cut_paste_and_add_clone_typed_area_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            _family_value, obj, _original = _family(Path(temp))
            bilateral = next(
                element for element in detect_fx_elements(obj)
                if element.poly_ids == (19, 20))
            clipboard = build_fx_element_clipboard(obj, bilateral)
            self.assertTrue(all(
                polygon.block_class_id == "area.class"
                and polygon.block_template is not None
                for polygon in clipboard.polygons))

            delete = plan_delete_geometry(
                obj, set(bilateral.poly_ids), MappingIndex(obj))
            apply_delete_geometry(
                obj.skeleton, obj.base_object.ades, delete)
            pasted = append_fx_element_clipboard(
                obj, clipboard, (5.0, 0.0, 0.0))
            self.assertEqual(len(pasted.polygon_indices), 2)
            self.assertEqual(len(obj.base_object.ades), 7)
            pasted_element = next(
                element for element in detect_fx_elements(obj)
                if set(element.poly_ids) == set(pasted.polygon_indices))
            self.assertTrue(pasted_element.bilateral)

            added = build_new_fx_clipboard(
                obj, pasted_element, {}, 3.0, True, "XZ")
            self.assertTrue(all(point[1] == 0.0 for point in added.points))
            second = append_fx_element_clipboard(
                obj, added, (10.0, 0.0, 0.0))
            self.assertEqual(len(second.polygon_indices), 2)
            self.assertEqual(len(obj.base_object.ades), 9)
            self.assertEqual(diagnose_polygon_references(obj), [])

    def test_normal_area_geometry_copy_cut_paste_delete_and_renumber(self):
        with tempfile.TemporaryDirectory() as temp:
            _family_value, obj, _original = _family(Path(temp))
            clipboard = build_geometry_clipboard(
                obj, "root", {15, 16}, MappingIndex(obj))
            self.assertTrue(all(
                polygon.block_class_id == "area.class"
                and polygon.block_template is not None
                for polygon in clipboard.polygons))
            appended = append_geometry_clipboard(
                obj.skeleton, obj.base_object.ades,
                clipboard, (0.0, 5.0, 0.0))
            self.assertEqual(appended.polygon_indices, (22, 23))
            self.assertEqual(
                [block.ade_poly_id for block in obj.base_object.ades],
                list(range(15, 24)))

            cut_clipboard = build_geometry_clipboard(
                obj, "root", {16}, MappingIndex(obj))
            delete = plan_delete_geometry(
                obj, {16}, MappingIndex(obj))
            apply_delete_geometry(
                obj.skeleton, obj.base_object.ades, delete)
            self.assertEqual(
                [block.ade_poly_id for block in obj.base_object.ades],
                list(range(15, 23)))
            restored = append_geometry_clipboard(
                obj.skeleton, obj.base_object.ades,
                cut_clipboard, (0.0, 10.0, 0.0))
            self.assertEqual(restored.polygon_indices, (23,))
            self.assertEqual(obj.base_object.ades[-1].class_id, "area.class")
            self.assertEqual(obj.base_object.ades[-1].ade_poly_id, 23)
            self.assertEqual(diagnose_polygon_references(obj), [])
            fx = next(
                element for element in detect_fx_elements(obj)
                if element.bilateral)
            self.assertEqual(fx.poly_ids, (18, 19))

    def test_area_delete_save_reload_and_set_source_stays_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            family, obj, original_base = _family(root)
            bilateral = next(
                element for element in detect_fx_elements(obj)
                if element.poly_ids == (19, 20))
            plan = plan_delete_geometry(
                obj, set(bilateral.poly_ids), MappingIndex(obj))
            apply_delete_geometry(
                obj.skeleton, obj.base_object.ades, plan)
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                target_sklt = root / "out" / "FXTEST.SKLT"
                target_base = root / "out" / "FXTEST.BASE"
                self.assertTrue(window._write_model_files(
                    "root", family, obj, target_sklt, target_base,
                    ask_replace=False))
                saved_model = parse_sklt_file(target_sklt)
                saved_base = parse_base_bytes(target_base.read_bytes()).root
                self.assertEqual(len(saved_model.polygons), 20)
                self.assertEqual(len(saved_base.ades), 5)
                self.assertEqual(
                    [block.ade_poly_id for block in saved_base.ades],
                    [15, 16, 17, 18, 19])
                reloaded = FamilyObject(
                    base_object=saved_base,
                    skeleton=saved_model,
                    owner_path="root")
                self.assertEqual(diagnose_polygon_references(reloaded), [])
                self.assertEqual(family.base_asset.tree.data, original_base)
                self.assertEqual(family.base_path.read_bytes(), original_base)
            finally:
                window.close()

    def test_area_fx_capability_label_tooltip_and_context_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            family, obj, _original = _family(Path(temp))
            window = AssemblyWindow()
            try:
                window._family = family
                window._owner_to_obj = {"root": obj}
                window._selected_owner = "root"
                window._workbench_obj = obj
                window.viewport.load_family(family, primary_owner="root")
                window.viewport.set_selected_owner("root")
                window.viewport.enter_edit_mode("root")
                window._rebuild_workbench(family, "root")
                window._refresh_fx_elements()
                index = next(
                    value for value in range(1, window.fx_combo.count())
                    if window.fx_combo.itemData(value).poly_ids == (19, 20))
                self.assertIn(
                    "Bilateral | Editable | Delete supported",
                    window.fx_combo.itemText(index))
                tooltip = window.fx_combo.itemData(
                    index, Qt.ItemDataRole.ToolTipRole)
                self.assertIn("area.class", tooltip)
                window.fx_combo.setCurrentIndex(index)
                window._select_highlight_fx()
                self.assertEqual(window._delete_geometry_reason(), "")
                menu = window._create_viewport_context_menu()
                labels = [action.text() for action in menu.actions()]
                self.assertIn("Copy FX Element", labels)
                self.assertIn("Cut FX Element", labels)
                self.assertIn("Delete FX Element...", labels)
                self.assertIn("Add Similar FX Element...", labels)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
