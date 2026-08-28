import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from assembly_window import (
    AssemblyWindow,
    _BundleCommitError,
    _commit_verified_files,
)
from asset_family import (
    AssetFamily,
    FamilyObject,
    MaterialGroup,
    load_asset_family,
)
from base_mapping_editor import (
    MappingIndex,
    RepairPlan,
    StructuralBlockState,
    save_model_base_copy,
)
from base_parser import AttsEntry, parse_base_bytes
from geometry_editor import (
    append_geometry_clipboard,
    build_geometry_clipboard,
    plan_delete_geometry,
)
from sklt_parser import (
    create_minimal_sklt_model,
    parse_sklt_file,
    save_sklt_with_poo2_pol2_structure,
)
from tests.synthetic_indexed_profile import attach_synthetic_indexed_profile


def _chunk(tag, payload):
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type, children):
    return _chunk(b"FORM", form_type + children)


def _minimal_base():
    atts = struct.pack(">hBBBB", 0, 10, 20, 30, 40)
    olpl = struct.pack(">hBBBBBB", 3, 0, 0, 255, 0, 0, 255)
    amesh = _form(b"AMSH", _chunk(b"ATTS", atts) + _chunk(b"OLPL", olpl))
    material = _form(
        b"OBJT", _chunk(b"CLID", b"amesh.class\0") + amesh)
    skeleton = _form(
        b"OBJT",
        _chunk(b"CLID", b"sklt.class\0")
        + _form(b"SKLC", _chunk(b"NAME", b"COPY.SKLT\0")))
    base = _form(
        b"BASE",
        _chunk(b"STRC", bytes(62)) + skeleton
        + _form(b"ADES", material))
    return _form(
        b"MC2 ", _form(
            b"OBJT", _chunk(b"CLID", b"base.class\0") + base))


class StructuralSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_sklt_structural_round_trip_preserves_new_topology(self):
        model = create_minimal_sklt_model()
        points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                  (0.0, 1.0, 0.0)]
        polygons = [[0, 1, 2]]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "COPY.SKLT"
            save_sklt_with_poo2_pol2_structure(
                model, points, polygons, path)
            parsed = parse_sklt_file(path)
        self.assertEqual(parsed.points, points)
        self.assertEqual(parsed.polygons, polygons)

    def test_base_mapping_append_round_trip_preserves_old_entry(self):
        original = _minimal_base()
        parsed = parse_base_bytes(original)
        self.assertEqual(len(parsed.root.ades[0].atts), 1)
        plan = RepairPlan(
            poly_id=1,
            block_index=0,
            color_val=11,
            shade_val=22,
            tracy_val=33,
            pad=44,
            uvs=[(1, 2), (3, 4), (5, 6)],
            method="geometry-paste",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "COPY.BASE"
            save_model_base_copy(original, [], [], path, [plan])
            reparsed = parse_base_bytes(path.read_bytes())
        block = reparsed.root.ades[0]
        self.assertEqual(
            (block.atts[0].poly_id, block.atts[0].pad), (0, 40))
        self.assertEqual(
            (block.atts[1].poly_id, block.atts[1].color_val,
             block.atts[1].shade_val, block.atts[1].tracy_val,
             block.atts[1].pad),
            (1, 11, 22, 33, 44))
        self.assertEqual(block.olpl[1], [(1, 2), (3, 4), (5, 6)])

    def test_complete_base_writer_supports_shrink_and_renumber(self):
        original = _minimal_base()
        plan = RepairPlan(
            poly_id=1, block_index=0,
            color_val=11, shade_val=22, tracy_val=33, pad=44,
            uvs=[(1, 2), (3, 4), (5, 6)],
            method="geometry-paste",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            grown_path = root / "GROWN.BASE"
            save_model_base_copy(original, [], [], grown_path, [plan])
            grown = grown_path.read_bytes()
            shrunk_path = root / "SHRUNK.BASE"
            state = StructuralBlockState(
                block_index=0,
                atts=(AttsEntry(0, 11, 22, 33, 44),),
                olpl=(((1, 2), (3, 4), (5, 6)),),
            )
            save_model_base_copy(
                grown, [], [], shrunk_path,
                structural_blocks=[state])
            shrunk = shrunk_path.read_bytes()
        block = parse_base_bytes(shrunk).root.ades[0]
        self.assertLess(len(shrunk), len(grown))
        self.assertEqual(block.atts, [AttsEntry(0, 11, 22, 33, 44)])
        self.assertEqual(block.olpl, [[(1, 2), (3, 4), (5, 6)]])

    def test_bundle_rolls_back_if_second_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_a = root / "new-a"
            source_b = root / "new-b"
            target_a = root / "target-a"
            target_b = root / "target-b"
            source_a.write_bytes(b"new-a")
            source_b.write_bytes(b"new-b")
            target_a.write_bytes(b"old-a")
            target_b.write_bytes(b"old-b")
            real_replace = os.replace

            def failing_replace(source, target):
                if Path(source).suffix == ".stage" \
                        and Path(target) == target_b:
                    raise OSError("simulated second-file failure")
                return real_replace(source, target)

            with patch("assembly_window.os.replace", failing_replace):
                with self.assertRaises(_BundleCommitError) as raised:
                    _commit_verified_files([
                        (source_a, target_a),
                        (source_b, target_b),
                    ])
            self.assertTrue(raised.exception.rollback_complete)
            self.assertEqual(target_a.read_bytes(), b"old-a")
            self.assertEqual(target_b.read_bytes(), b"old-b")

    def test_coordinated_window_save_and_second_session_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_sklt = root / "SOURCE.SKLT"
            source_base = root / "SOURCE.BASE"
            initial = create_minimal_sklt_model()
            initial_points = [
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ]
            save_sklt_with_poo2_pol2_structure(
                initial, initial_points, [[0, 1, 2]], source_sklt)
            source_base.write_bytes(_minimal_base())
            model = parse_sklt_file(source_sklt)
            base_asset = parse_base_bytes(source_base.read_bytes())
            block = base_asset.root.ades[0]
            obj = FamilyObject(
                base_object=base_asset.root,
                skeleton= model,
                skeleton_ref=type("Ref", (), {"path": source_sklt})(),
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
            attach_synthetic_indexed_profile(family, root)
            window = AssemblyWindow()
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window._mapping_index = MappingIndex(obj)
            clipboard = build_geometry_clipboard(
                obj, "root", {0}, window._mapping_index)
            append_geometry_clipboard(
                model, obj.base_object.ades, clipboard, (2.0, 0.0, 0.0))
            window._refresh_object_material_faces(obj)

            output = root / "out"
            target_sklt = output / "COPY.SKLT"
            target_base = output / "COPY.BASE"
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            saved_sklt = parse_sklt_file(target_sklt)
            saved_base = parse_base_bytes(target_base.read_bytes())
            self.assertEqual(
                (len(saved_sklt.points), len(saved_sklt.polygons)), (6, 2))
            self.assertEqual(
                (len(saved_base.root.ades[0].atts),
                 len(saved_base.root.ades[0].olpl)), (2, 2))

            append_geometry_clipboard(
                model, obj.base_object.ades, clipboard, (4.0, 0.0, 0.0))
            window._refresh_object_material_faces(obj)
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            saved_sklt = parse_sklt_file(target_sklt)
            saved_base = parse_base_bytes(target_base.read_bytes())
            self.assertEqual(
                (len(saved_sklt.points), len(saved_sklt.polygons)), (9, 3))
            self.assertEqual(
                (len(saved_base.root.ades[0].atts),
                 len(saved_base.root.ades[0].olpl)), (3, 3))
            reloaded = load_asset_family(target_base)
            self.assertIsNotNone(reloaded.root_object.skeleton)
            self.assertEqual(
                (len(reloaded.root_object.skeleton.points),
                 len(reloaded.root_object.skeleton.polygons)), (9, 3))
            self.assertEqual(
                len(reloaded.root_object.materials[0].faces), 3)
            window.close()

    def test_save_after_delete_undo_and_redo_shrinks_and_grows_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_sklt = root / "SOURCE.SKLT"
            source_base = root / "SOURCE.BASE"
            initial = create_minimal_sklt_model()
            save_sklt_with_poo2_pol2_structure(
                initial,
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                [[0, 1, 2]], source_sklt)
            source_base.write_bytes(_minimal_base())
            model = parse_sklt_file(source_sklt)
            base_asset = parse_base_bytes(source_base.read_bytes())
            block = base_asset.root.ades[0]
            obj = FamilyObject(
                base_object=base_asset.root,
                skeleton=model,
                skeleton_ref=type("Ref", (), {"path": source_sklt})(),
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
            attach_synthetic_indexed_profile(family, root)
            clipboard = build_geometry_clipboard(
                obj, "root", {0}, MappingIndex(obj))
            append_geometry_clipboard(
                model, obj.base_object.ades, clipboard, (2.0, 0.0, 0.0))
            window = AssemblyWindow()
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            window.viewport.load_family(family, primary_owner="root")
            window.viewport.set_selected_owner("root")
            window.viewport.enter_edit_mode_with_vertices(
                "root", [3, 4, 5], pick_polygons=True)
            window._selected_polys = {1}
            window._selected_poly = 1
            window._refresh_object_material_faces(obj)
            delete_plan = plan_delete_geometry(
                obj, {1}, MappingIndex(obj))
            window._apply_delete_plan(
                "root", obj, delete_plan, label="Delete Geometry")

            target_sklt = root / "out" / "COPY.SKLT"
            target_base = root / "out" / "COPY.BASE"
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            self.assertEqual(len(parse_sklt_file(target_sklt).polygons), 1)
            self.assertEqual(
                len(parse_base_bytes(target_base.read_bytes())
                    .root.ades[0].atts), 1)

            window._undo_edit()
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            self.assertEqual(len(parse_sklt_file(target_sklt).polygons), 2)
            self.assertEqual(
                len(parse_base_bytes(target_base.read_bytes())
                    .root.ades[0].atts), 2)

            window._redo_edit()
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            self.assertEqual(len(parse_sklt_file(target_sklt).polygons), 1)
            self.assertEqual(
                len(parse_base_bytes(target_base.read_bytes())
                    .root.ades[0].atts), 1)
            window.close()

    def test_embedded_decoded_sources_export_loose_pair_without_source_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            embedded_sklt_path = root / "DECODED.SKLT"
            initial = create_minimal_sklt_model()
            save_sklt_with_poo2_pol2_structure(
                initial,
                [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                [[0, 1, 2]], embedded_sklt_path)
            model = parse_sklt_file(embedded_sklt_path)
            original_sklt = bytes(model.original_data)
            archive_data = _minimal_base()
            base_asset = parse_base_bytes(archive_data, "SET.BAS")
            block = base_asset.root.ades[0]
            obj = FamilyObject(
                base_object=base_asset.root,
                skeleton=model,
                skeleton_ref=type(
                    "Ref", (), {
                        "path": None,
                        "status": "setbas",
                        "source": "SET.BAS",
                    })(),
                materials=[MaterialGroup(
                    "material", block=block,
                    faces=[(0, list(block.olpl[0]), 20)])],
                owner_path="root",
            )
            family = AssetFamily(
                base_path=None,
                base_asset=base_asset,
                root_object=obj,
            )
            attach_synthetic_indexed_profile(family, root)
            window = AssemblyWindow()
            window._family = family
            window._owner_to_obj = {"root": obj}
            window._selected_owner = "root"
            window._workbench_obj = obj
            target_sklt = root / "export" / "COPY.SKLT"
            target_base = root / "export" / "COPY.BASE"
            self.assertTrue(window._write_model_files(
                "root", family, obj, target_sklt, target_base,
                ask_replace=False))
            self.assertEqual(model.original_data, original_sklt)
            self.assertEqual(base_asset.tree.data, archive_data)
            self.assertEqual(
                parse_sklt_file(target_sklt).polygons, [[0, 1, 2]])
            self.assertEqual(
                len(parse_base_bytes(target_base.read_bytes())
                    .root.ades[0].atts), 1)
            window.close()


if __name__ == "__main__":
    unittest.main()
