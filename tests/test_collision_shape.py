import math
import os
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from asset_family import AssetFamily, FamilyObject
from base_parser import BaseObject
import collision_editor.editor as editor_module
import collision_editor.shape as shape_module
from collision_editor.editor import (
    CollisionEditorWindow,
    CollisionProject,
    CollisionScriptError,
    CollisionSphere,
    GeometryPartSelectionWidget,
    LEGACY,
    OPENNEOUA,
    VEHICLE,
    plan_script_update,
    script_model_references,
)
from collision_editor.shape import (
    CollisionHull,
    CollisionShape,
    ShapeGenerationResult,
    ShapeMetrics,
    collision_shape_text,
    generate_collision_shape,
    geometry_fingerprint,
    parse_collision_shape,
    sampled_surface_metrics,
    validate_hull,
    validate_shape,
    write_collision_shape,
)


def _tetrahedron():
    vertices = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    faces = ((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3))
    return CollisionHull(vertices, faces)


def _l_prism():
    outline = ((0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2))
    count = len(outline)
    vertices = [(*point, 0.0) for point in outline]
    vertices.extend((*point, 1.0) for point in outline)
    faces = []
    for index in range(count):
        following = (index + 1) % count
        faces.extend((
            (index, following, following + count),
            (index, following + count, index + count),
        ))
    for index in range(1, count - 1):
        faces.append((count, count + index, count + index + 1))
        faces.append((0, index + 1, index))
    return [tuple(vertices[index] for index in face) for face in faces]


class CollisionShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _finish_worker(self, window, response, *, exit_code=0,
                       snapshot=None, cancelled=False):
        class FinishedProcess:
            @staticmethod
            def readAllStandardError():
                return b""

            @staticmethod
            def deleteLater():
                return None

        process = FinishedProcess()
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output.json"
            output_path.write_text(json.dumps(response), encoding="utf-8")
            window._collision_bake_process = process
            window._collision_bake_cancelled = cancelled
            window._collision_bake_context = {
                "source_path": "",
                "source_model": window.project.source_model,
                "source_key": None,
                "project_snapshot": (
                    window.project.snapshot() if snapshot is None else snapshot),
                "scale": (
                    window.project.model_scale_x,
                    window.project.model_scale_y,
                    window.project.model_scale_z),
                "rotation": (
                    window.project.model_rotation_x,
                    window.project.model_rotation_y,
                    window.project.model_rotation_z),
                "owners": ({
                    "model_owner": "root/body",
                    "owner": "root/body#component:body",
                },),
                "output_path": str(output_path),
            }
            window._collision_shape_bake_finished(process, exit_code, None)

    def test_format_round_trip_keeps_asset_set_and_physical_metadata(self):
        shape = CollisionShape(
            source="VP_TIGER",
            source_hash="a" * 64,
            scale=(1.0, 2.0, 3.0),
            rotation=(0.0, 90.0, 0.0),
            hulls=(_tetrahedron(),),
            asset_set=1,
        )
        text = collision_shape_text(shape)
        self.assertIn("asset_set = 1\n", text)
        self.assertEqual(parse_collision_shape(text), shape)

    def test_asset_set_is_optional_for_assets_independent_of_set(self):
        shape = CollisionShape(
            "BASE_Model", "b" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        text = collision_shape_text(shape)
        self.assertNotIn("asset_set =", text)
        self.assertEqual(parse_collision_shape(text).asset_set, 0)

    def test_parser_rejects_nonfinite_degenerate_and_malformed_shapes(self):
        shape = CollisionShape(
            "Model", "c" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        text = collision_shape_text(shape)
        with self.assertRaisesRegex(ValueError, "finite"):
            parse_collision_shape(text.replace("vertex = 0_0_0", "vertex = NaN_0_0"))
        with self.assertRaisesRegex(ValueError, "integer"):
            parse_collision_shape(text.replace(
                "version = 1", "version = 1\nasset_set = 1.5"))
        with self.assertRaisesRegex(ValueError, "0..255"):
            parse_collision_shape(text.replace(
                "rotation = 0_0_0",
                "rotation = 0_0_0\nasset_set = 256"))
        oversized = CollisionHull(
            ((0, 0, 0), (1e7 + 1, 0, 0), (0, 1, 0), (0, 0, 1)),
            _tetrahedron().faces)
        with self.assertRaisesRegex(ValueError, "runtime limit"):
            validate_hull(oversized)
        oversized_diagonal = CollisionHull(
            ((6e6, 6e6, 6e6), (0, 0, 0), (1, 0, 0), (0, 1, 0)),
            _tetrahedron().faces)
        with self.assertRaisesRegex(ValueError, "runtime limit"):
            validate_hull(oversized_diagonal)
        bad = CollisionHull(
            ((0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0)),
            ((0, 1, 2), (0, 2, 3), (0, 3, 1), (1, 3, 2)),
        )
        with self.assertRaises(ValueError):
            validate_hull(bad)

    def test_generation_bakes_concavity_as_separate_valid_hulls(self):
        try:
            import coacd  # noqa: F401
        except ImportError:
            self.skipTest("CoACD is supplied by the packaged worker runtime")
        triangles = _l_prism()
        result = generate_collision_shape(
            [("root/body", triangles)], source="Concave_Test",
            scale=(1, 1, 1), rotation=(0, 0, 0),
            asset_set=1, quality="normal")
        self.assertGreaterEqual(len(result.shape.hulls), 2)
        notch_point = (1.5, 1.5, 0.5)
        self.assertFalse(any(
            shape_module._inside_convex_hull(
                notch_point, shape_module._convex_hull_planes(hull), 1e-5)
            for hull in result.shape.hulls
        ), "The generated union should preserve the L-prism's concavity.")
        self.assertEqual(result.shape.asset_set, 1)
        self.assertTrue(math.isfinite(result.metrics.sampled_surface_distance))
        self.assertEqual(
            result.metrics,
            sampled_surface_metrics(
                triangles, result.shape.hulls))
        for hull in result.shape.hulls:
            validate_hull(hull)

    def test_low_quality_uses_the_requested_coacd_settings(self):
        tetra = _tetrahedron()
        settings = []
        fake = types.SimpleNamespace(
            Mesh=lambda *_args, **_kwargs: object(),
            run_coacd=lambda _mesh, **kwargs: (
                settings.append(kwargs) or [(tetra.vertices, tetra.faces)]),
            set_log_level=lambda _level: None,
        )
        triangles = [
            tuple(tetra.vertices[index] for index in face)
            for face in tetra.faces
        ]
        with patch.dict(sys.modules, {"coacd": fake}):
            generate_collision_shape(
                [("root/body", triangles)], source="Low_Quality",
                quality="low")
        self.assertEqual(len(settings), 1)
        for key, expected in {
                "threshold": 0.10,
                "preprocess_resolution": 24,
                "resolution": 500,
                "mcts_nodes": 5,
                "mcts_iterations": 20,
                "seed": 0,
                "max_ch_vertex": 128,
                "extrude": True,
                "extrude_margin": 0.001,
        }.items():
            self.assertEqual(settings[0][key], expected)

    def test_dependency_error_includes_import_cause_for_source_and_frozen(self):
        import builtins

        original_import = builtins.__import__

        def import_without_coacd(name, *args, **kwargs):
            if name == "coacd":
                raise ModuleNotFoundError("No module named 'coacd'")
            return original_import(name, *args, **kwargs)

        for frozen, expected_guidance in (
                (False, "Install the Studio requirements"),
                (True, "library bundled with OpenNeoUA Studio")):
            with self.subTest(frozen=frozen):
                with patch.object(
                        shape_module.sys, "frozen", frozen, create=True), \
                        patch("builtins.__import__",
                              side_effect=import_without_coacd):
                    with self.assertRaises(RuntimeError) as raised:
                        generate_collision_shape([], source="Import_Failure")
                self.assertIn(
                    "No module named 'coacd'", str(raised.exception))
                self.assertIsInstance(
                    raised.exception.__cause__, ModuleNotFoundError)
                self.assertEqual(
                    str(raised.exception.__cause__),
                    "No module named 'coacd'")
                self.assertIn(expected_guidance, str(raised.exception))
                if frozen:
                    self.assertNotIn("pip", str(raised.exception).casefold())

    def test_open_source_mesh_is_kept_and_reported_for_auto_preprocess(self):
        tetra = _tetrahedron()
        options = []
        fake = types.SimpleNamespace(
            Mesh=lambda *_args, **_kwargs: object(),
            run_coacd=lambda *_args, **kwargs: (
                options.append(kwargs) or [(tetra.vertices, tetra.faces)]),
        )
        with patch.dict(sys.modules, {"coacd": fake}):
            result = generate_collision_shape(
                [("root/wing", [
                    tuple(tetra.vertices[index] for index in face)
                    for face in tetra.faces[:-1]])],
                source="Open_Wing")
        self.assertEqual(len(result.shape.hulls), 1)
        self.assertGreaterEqual(len(result.warnings), 2)
        self.assertTrue(any("3 open" in warning
                            for warning in result.warnings))
        self.assertTrue(any("Closed 1 simple planar boundary loops"
                            in warning for warning in result.warnings))
        self.assertEqual(len(options), 1)
        self.assertTrue(options[0]["extrude"])
        self.assertEqual(options[0]["extrude_margin"], 0.001)

    def test_real_coacd_repairs_open_mesh_and_returns_closed_hulls(self):
        try:
            import coacd  # noqa: F401
        except ImportError:
            self.skipTest("CoACD is supplied by the packaged worker runtime")
        tetra = _tetrahedron()
        triangles = [
            tuple(tetra.vertices[index] for index in face)
            for face in tetra.faces[:-1]
        ]
        result = generate_collision_shape(
            [("root/body", triangles)], source="Open_Tetra",
            quality="normal")
        self.assertTrue(result.shape.hulls)
        self.assertTrue(any("3 open" in warning for warning in result.warnings))
        self.assertTrue(math.isfinite(result.metrics.sampled_surface_distance))
        for hull in result.shape.hulls:
            validate_hull(hull)

    def test_generation_reports_hull_limit_without_truncating(self):
        tetra = _tetrahedron()
        mesh = types.SimpleNamespace()
        fake = types.SimpleNamespace(
            Mesh=lambda *_args, **_kwargs: mesh,
            run_coacd=lambda *_args, **_kwargs: [
                (tetra.vertices, tetra.faces) for _ in range(65)],
        )
        with patch.dict(sys.modules, {"coacd": fake}):
            with self.assertRaisesRegex(ValueError, "more than 64"):
                generate_collision_shape(
                    [("root/body", [
                        tuple(tetra.vertices[index] for index in face)
                        for face in tetra.faces])],
                    source="Too_Many", quality="normal")

    def test_surface_metrics_ignore_internal_faces_of_hull_union(self):
        upper = _tetrahedron()
        lower_vertices = (
            (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0), (0.0, 0.0, -1.0),
        )
        lower = CollisionHull(lower_vertices, upper.faces)
        source = [
            tuple(upper.vertices[index] for index in face)
            for face in upper.faces[1:]
        ] + [
            tuple(lower.vertices[index] for index in face)
            for face in lower.faces[1:]
        ]
        metrics = sampled_surface_metrics(source, (upper, lower))
        self.assertLess(metrics.sampled_source_to_shape, 1e-9)
        self.assertLess(metrics.sampled_shape_to_source, 1e-9)
        self.assertLess(metrics.sampled_surface_distance, 1e-9)

    def test_fingerprint_tracks_owner_and_selected_geometry(self):
        triangles = [((0, 0, 0), (1, 0, 0), (0, 1, 0))]
        base = geometry_fingerprint([("root/body", triangles)])
        self.assertNotEqual(
            base, geometry_fingerprint([("root/rotor", triangles)]))
        self.assertNotEqual(
            base, geometry_fingerprint([("root/body", [tuple(
                reversed(triangles[0]))])]))

    def test_writer_writes_a_valid_document(self):
        shape = CollisionShape(
            "Writer_Test", "d" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),), asset_set=4)
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.collision"
            write_collision_shape(path, shape)
            self.assertEqual(parse_collision_shape(path.read_text()), shape)

    def test_project_undo_restores_shape_and_script_path_with_spheres(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        original = CollisionProject(
            compound=[CollisionSphere(OPENNEOUA, x=4, radius=2)])
        window.project = original
        window._push_undo()
        window.project.collision_shape = CollisionShape(
            "Mixed_Test", "e" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),), asset_set=1)
        window.project.collision_shape_path = "Data/Models/Collision/Test.collision"
        window.project.collision_shape_file_path = "C:/Game/Data/Models/Collision/Test.collision"
        window.project.collision_shape_owners = ("root/body",)
        window.project.collision_shape_components = (
            "root/body#component:body-hash",)
        window._sync_all()
        window.undo()
        self.assertIsNone(window.project.collision_shape)
        self.assertEqual(window.project.collision_shape_path, "")
        self.assertIsNone(window.project.collision_shape_components)
        self.assertEqual(len(window.project.compound), 1)

    def test_script_writer_keeps_unrelated_assignments_and_comments(self):
        shape = CollisionShape(
            "Mixed_Test", "f" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        project = CollisionProject(
            target_category=VEHICLE,
            compound=[CollisionSphere(OPENNEOUA, x=3, radius=2)],
            collision_shape=shape,
            collision_shape_path="Data/Models/Collision/Mixed.collision",
        )
        source = (
            "; file header stays\n"
            "new_vehicle 9\n"
            "    name = Mixed\n"
            "    mass = 100\n"
            "    ; keep this comment\n"
            "    radius = 44\n"
            "    coll_num = 0\n"
            "    collision_shape = Data/Models/Collision/Old.collision\n"
            "end\n"
        )
        updated, _preview, _name = plan_script_update(
            source, "new_vehicle", 9, project,
            replace_all_managed=True)
        self.assertIn("; file header stays", updated)
        self.assertIn("; keep this comment", updated)
        self.assertIn("mass = 100", updated)
        self.assertIn(
            "collision_shape = Data/Models/Collision/Mixed.collision", updated)
        self.assertIn("coll_radius = 2", updated)
        self.assertNotIn("Old.collision", updated)

    def test_script_overwrite_removes_sphere_parameters_when_shape_is_active(self):
        shape = CollisionShape(
            "Fox", "d" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        project = CollisionProject(
            name="Fox", target_category=VEHICLE, collision_shape=shape,
            collision_shape_path="Data/Models/Collision/Fox.collision")
        source = (
            "new_vehicle 14\n"
            "    name = Fox\n"
            "    mass = 80\n"
            "    radius = 45\n"
            "    coll_num = 2\n"
            "    coll_act = 0\n"
            "    coll_x = 1\n"
            "    coll_y = 2\n"
            "    coll_z = 3\n"
            "    coll_radius = 4\n"
            "    collision_shape = Data/Models/Collision/Old.collision\n"
            "end\n")
        updated, _preview, _name = plan_script_update(
            source, "new_vehicle", 14, project, replace_all_managed=True)
        assignments = [
            line.partition("=")[0].strip()
            for line in updated.splitlines()
            if "=" in line and not line.lstrip().startswith((";", "#"))]
        self.assertNotIn("radius", assignments)
        self.assertFalse(any(key.startswith("coll_") for key in assignments))
        self.assertIn("mass = 80", updated)
        self.assertIn(
            "collision_shape = Data/Models/Collision/Fox.collision", updated)
        self.assertNotIn("Old.collision", updated)

    def test_export_validation_blocks_sphere_overcap_without_mutating_import(self):
        project = CollisionProject(
            compound=[CollisionSphere(OPENNEOUA, radius=1)
                      for _ in range(257)])
        before = project.snapshot()
        with self.assertRaisesRegex(CollisionScriptError, "256"):
            editor_module._collision_tab_data_lines(project)
        self.assertEqual(project.snapshot(), before)

    def test_shape_tab_reset_is_scoped_and_undoable(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        from collision_editor.shape import CollisionShape

        baseline_shape = CollisionShape(
            "Fox", "1" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        replacement_shape = CollisionShape(
            "Fox_New", "2" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        window.project.legacy = CollisionSphere(LEGACY, radius=20)
        window.project.compound = [CollisionSphere(OPENNEOUA, radius=3)]
        window.project.collision_shape = baseline_shape
        window.project.collision_shape_path = "Data/Collision/Fox.collision"
        window._capture_tab_reset_baseline()
        window.project.collision_shape = replacement_shape
        window.project.collision_shape_path = "Data/Collision/Fox_New.collision"
        window.project.legacy.radius = 33
        window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
        window._sync_all()
        self.assertTrue(window.reset_tab_button.isEnabled())

        with patch.object(
                editor_module.QMessageBox, "question",
                return_value=QMessageBox.StandardButton.Yes):
            window._reset_current_tab()

        self.assertEqual(window.project.collision_shape, baseline_shape)
        self.assertEqual(
            window.project.collision_shape_path,
            "Data/Collision/Fox.collision")
        self.assertEqual(window.project.legacy.radius, 33)
        self.assertEqual(len(window.project.compound), 1)
        window.undo()
        self.assertEqual(window.project.collision_shape, replacement_shape)
        self.assertEqual(window.project.legacy.radius, 33)

    def test_shape_tab_hides_shared_selection_controls_without_losing_selection(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window.project.target_category = VEHICLE
        window.project.legacy = CollisionSphere(LEGACY, radius=10)
        window.project.compound = [
            CollisionSphere(OPENNEOUA, x=1, radius=2),
            CollisionSphere(OPENNEOUA, x=3, radius=4),
        ]
        window._selected = 1
        window._selected_spheres = {0, 1, 2}
        window._sync_all()
        selected = {0, 1, 2}

        window.properties_tabs.setCurrentIndex(window.collision_shape_tab_index)
        self.assertTrue(window.transform_box.isHidden())
        self.assertTrue(window.selected_box.isHidden())
        self.assertEqual(window._selected_sphere_indices(), selected)

        for tab_index in (
                window.collision_tab_index, window.fire_points_tab_index,
                window.gun_points_tab_index):
            window.properties_tabs.setCurrentIndex(tab_index)
            self.assertFalse(window.transform_box.isHidden())
            self.assertFalse(window.selected_box.isHidden())
            self.assertEqual(window._selected_sphere_indices(), selected)

        window.properties_tabs.setCurrentIndex(window.cockpit_tab_index)
        self.assertFalse(window.transform_box.isHidden())
        self.assertTrue(window.selected_box.isHidden())
        self.assertEqual(window._selected_sphere_indices(), selected)

    def test_geometry_widget_exposes_only_selected_owner_subtree(self):
        root = FamilyObject(
            BaseObject(name="Set"), owner_path="root")
        body = FamilyObject(
            BaseObject(name="Body", skeleton_name="body"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[0]")
        wing = FamilyObject(
            BaseObject(name="Wing", skeleton_name="wing"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[0]/kid[0]")
        sibling = FamilyObject(
            BaseObject(name="Other vehicle", skeleton_name="other"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[1]")
        root.kids = [body, sibling]
        body.kids = [wing]
        widget = GeometryPartSelectionWidget()
        widget.set_geometry(
            AssetFamily(root_object=root), "root/kid[0]", (), {},
            source_key=("fixture", "subtree"))
        self.addCleanup(widget.close)
        self.assertEqual(
            set(widget.selected_owners()),
            {"root/kid[0]", "root/kid[0]/kid[0]"})

    def test_geometry_widget_lists_and_selects_disconnected_components(self):
        root = FamilyObject(
            BaseObject(name="Set"), owner_path="root")
        body = FamilyObject(
            BaseObject(name="Body", skeleton_name="body"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[0]")
        root.kids = [body]
        face = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        components = (
            types.SimpleNamespace(
                owner="root/kid[0]", component_id="body-hash",
                triangles=(face,), boundary_edges=0,
                non_manifold_edges=0, planar=False, has_volume=True),
            types.SimpleNamespace(
                owner="root/kid[0]", component_id="fx-hash",
                triangles=(face,), boundary_edges=3,
                non_manifold_edges=0, planar=True, has_volume=False),
        )
        previews = []
        widget = GeometryPartSelectionWidget(
            on_component_highlight=previews.append)
        widget.set_geometry(
            AssetFamily(root_object=root), "root/kid[0]", components,
            {"root/kid[0]": (face,)}, source_key=("fixture", "parts"))
        self.addCleanup(widget.close)
        owner_item = widget.tree.topLevelItem(0)
        self.assertEqual(owner_item.childCount(), 2)
        fx_item = owner_item.child(1)
        self.assertIn("planar", fx_item.text(2))
        self.assertIn("no volume", fx_item.text(2))
        self.assertIn("X: 0.000", fx_item.toolTip(0))
        widget.tree.setCurrentItem(fx_item)
        self.assertEqual(previews[-1], (face,))
        self.assertEqual(len(widget.selected_components()), 2)
        fx_item.setCheckState(0, Qt.CheckState.Unchecked)
        selected = widget.selected_components()
        self.assertEqual(
            [component.component_id for component in selected],
            ["body-hash"])
        self.assertEqual(widget.selected_owners(), ("root/kid[0]",))
        widget.set_geometry(
            AssetFamily(root_object=root), "root/kid[0]", components,
            {"root/kid[0]": (face,)}, source_key=("fixture", "parts"))
        self.assertEqual(
            [component.component_id for component in widget.selected_components()],
            ["body-hash"])
        widget.set_geometry(
            AssetFamily(root_object=root), "root/kid[0]", components,
            {"root/kid[0]": (face,)}, source_key=("fixture", "other"))
        self.assertEqual(len(widget.selected_components()), 2)

    def test_parent_sklt_highlight_includes_cached_subtree_triangles(self):
        root = FamilyObject(BaseObject(name="Set"), owner_path="root")
        body = FamilyObject(
            BaseObject(name="Body", skeleton_name="body"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[0]")
        wing = FamilyObject(
            BaseObject(name="Wing", skeleton_name="wing"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root/kid[0]/kid[0]")
        root.kids = [body]
        body.kids = [wing]
        body_triangle = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        wing_triangle = ((2, 0, 0), (3, 0, 0), (2, 1, 0))
        components = (
            types.SimpleNamespace(
                owner="root/kid[0]", component_id="body",
                triangles=(body_triangle,), boundary_edges=0,
                non_manifold_edges=0, planar=False, has_volume=True),
            types.SimpleNamespace(
                owner="root/kid[0]/kid[0]", component_id="wing",
                triangles=(wing_triangle,), boundary_edges=0,
                non_manifold_edges=0, planar=False, has_volume=True),
        )
        previews = []
        widget = GeometryPartSelectionWidget(
            on_component_highlight=previews.append)
        self.addCleanup(widget.close)
        widget.set_geometry(
            AssetFamily(root_object=root), "root/kid[0]", components,
            {"root/kid[0]": (body_triangle,),
             "root/kid[0]/kid[0]": (wing_triangle,)},
            source_key=("fixture", "subtree-highlight"))
        parent_item = widget.tree.topLevelItem(0)
        widget.tree.setCurrentItem(parent_item)
        self.assertEqual(previews[-1], (body_triangle, wing_triangle))

    def test_sync_uses_cached_geometry_instead_of_reextracting_triangles(self):
        from sklt_parser import SkltModel

        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window.family = AssetFamily(root_object=FamilyObject(
            BaseObject(name="Body", skeleton_name="body"),
            skeleton=SkltModel(
                source_name="body.sklt",
                points=[(0, 0, 0), (1, 0, 0), (0, 1, 0)],
                polygons=[[0, 1, 2]]),
            owner_path="root"))
        window._current_owner = "root"
        window._collision_geometry_triangles_by_owner = {
            "root": (((0, 0, 0), (1, 0, 0), (0, 1, 0)),)}
        with patch.object(
                window.viewport, "local_owner_triangles",
                side_effect=AssertionError("geometry should come from cache")):
            window._sync_all()
        self.assertTrue(window.generate_collision_spheres_button.isEnabled())

    def test_generation_decline_preserves_old_profile_and_both_sphere_types(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        face = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        root = FamilyObject(
            BaseObject(name="Fox", skeleton_name="fox.sklt"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root")
        window.family = AssetFamily(root_object=root)
        window._current_owner = "root"
        window.project.name = "Fox"
        window.project.source_model = "fox.sklt"
        previous_shape = CollisionShape(
            "Fox_Old", "3" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        window.project.collision_shape = previous_shape
        window.project.collision_shape_path = "Data/Collision/Old.collision"
        window.project.legacy = CollisionSphere(LEGACY, radius=14)
        window.project.compound = [CollisionSphere(OPENNEOUA, radius=5)]
        component = types.SimpleNamespace(
            owner="root", component_id="body", triangles=(face,),
            boundary_edges=3, non_manifold_edges=0,
            planar=True, has_volume=False)
        source_key = window._current_collision_geometry_source_key()
        window._collision_geometry_cache_key = source_key
        window._collision_geometry_components = (component,)
        window._collision_geometry_triangles_by_owner = {"root": (face,)}
        window.collision_geometry_widget.set_geometry(
            window.family, "root", (component,), {"root": (face,)},
            source_key)
        before = window.project.snapshot()

        with patch.object(
                editor_module.QMessageBox, "question",
                return_value=QMessageBox.StandardButton.No) as confirm, \
                patch.object(editor_module, "QProcess") as process_type:
            window.generate_collision_shape()

        self.assertIn("Legacy and OpenNeoUA", confirm.call_args.args[2])
        self.assertIn("after generation succeeds", confirm.call_args.args[2])
        process_type.assert_not_called()
        self.assertEqual(window.project.snapshot(), before)
        self.assertIsNone(window._collision_bake_process)
        self.assertTrue(window.collision_shape_progress.isHidden())

    def test_worker_failure_preserves_previous_shape_and_spheres(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window.project.collision_shape = CollisionShape(
            "Old", "4" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        window.project.legacy = CollisionSphere(LEGACY, radius=12)
        window.project.compound = [CollisionSphere(OPENNEOUA, radius=7)]
        before = window.project.snapshot()
        with patch.object(editor_module.QMessageBox, "warning"):
            self._finish_worker(window, {"error": "CoACD failed"}, exit_code=2)
        self.assertEqual(window.project.snapshot(), before)
        self.assertEqual(window.collision_shape_status.text(), "Generation failed.")
        self.assertTrue(window.collision_shape_progress.isHidden())

    def test_cancelled_worker_preserves_previous_shape_and_spheres(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        shape = CollisionShape(
            "Old", "a" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        window.project.collision_shape = shape
        window.project.legacy = CollisionSphere(LEGACY, radius=10)
        window.project.compound = [CollisionSphere(OPENNEOUA, radius=6)]
        before = window.project.snapshot()
        with patch.object(editor_module.QMessageBox, "warning") as warning:
            self._finish_worker(
                window, {"shape": collision_shape_text(shape)},
                cancelled=True)
        self.assertEqual(window.project.snapshot(), before)
        self.assertEqual(
            window.collision_shape_status.text(), "Generation cancelled.")
        self.assertTrue(window.collision_shape_progress.isHidden())
        warning.assert_not_called()

    def test_collision_shape_import_requires_open_set(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window._sync_collision_generation_controls()
        self.assertFalse(window.import_collision_shape_button.isEnabled())

        with patch.object(
                editor_module.QFileDialog, "getOpenFileName") as choose_path, \
                patch.object(editor_module.QMessageBox, "information") as info:
            window.import_collision_shape()
        choose_path.assert_not_called()
        info.assert_called_once()

        window._vp_embedded = types.SimpleNamespace(
            source_path="C:/Game/Data/Sets/Set1/Objects/SET.BAS")
        window._sync_collision_generation_controls()
        self.assertTrue(window.import_collision_shape_button.isEnabled())

    def test_delete_collision_shape_clears_shape_state_and_is_undoable(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        shape = CollisionShape(
            "Delete_Test", "d" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        window.project.collision_shape = shape
        window.project.collision_shape_path = "Data/Collision/Delete_Test.collision"
        window.project.collision_shape_file_path = (
            "C:/Game/Data/Collision/Delete_Test.collision")
        window.project.collision_shape_owners = ("root",)
        window.project.collision_shape_components = ("root#component:test",)
        window.project.collision_shape_warnings = ("test warning",)
        window._shape_preview_settings()
        window._sync_all()

        self.assertTrue(window.delete_collision_shape_button.isEnabled())
        menu = window._create_collision_shape_context_menu()
        self.addCleanup(menu.deleteLater)
        delete_action = next(
            action for action in menu.actions()
            if action.text() == "Delete Collision Shape")
        self.assertTrue(delete_action.isEnabled())
        delete_action.trigger()

        self.assertIsNone(window.project.collision_shape)
        self.assertEqual(window.project.collision_shape_path, "")
        self.assertEqual(window.project.collision_shape_file_path, "")
        self.assertIsNone(window.project.collision_shape_owners)
        self.assertIsNone(window.project.collision_shape_components)
        self.assertEqual(window.project.collision_shape_warnings, ())
        self.assertIsNone(window.viewport._collision_shape)
        self.assertFalse(window.delete_collision_shape_button.isEnabled())

        window.undo()
        self.assertEqual(window.project.collision_shape, shape)
        self.assertEqual(
            window.project.collision_shape_path,
            "Data/Collision/Delete_Test.collision")

    def test_importing_shape_keeps_existing_legacy_and_compound_spheres(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window._vp_embedded = types.SimpleNamespace(
            source_path="C:/Game/Data/Sets/Set1/Objects/SET.BAS")
        window.project.legacy = CollisionSphere(LEGACY, radius=11)
        window.project.compound = [CollisionSphere(OPENNEOUA, radius=8)]
        before = (window.project.legacy.clone(),
                  [sphere.clone() for sphere in window.project.compound])
        imported = CollisionShape(
            "Imported", "b" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Imported.collision"
            write_collision_shape(path, imported)
            with patch.object(
                    editor_module.QFileDialog, "getOpenFileName",
                    return_value=(str(path), "")):
                window.import_collision_shape()
        self.assertEqual(window.project.collision_shape, imported)
        self.assertEqual(window.project.legacy, before[0])
        self.assertEqual(window.project.compound, before[1])

    def test_generation_discards_result_if_project_changes_while_running(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        old_shape = CollisionShape(
            "Old", "5" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        new_shape = CollisionShape(
            "New", "6" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        window.project.collision_shape = old_shape
        window.project.legacy = CollisionSphere(LEGACY, radius=12)
        before = window.project.snapshot()
        window.project.compound.append(CollisionSphere(OPENNEOUA, radius=9))
        with patch.object(editor_module.QMessageBox, "warning") as warning:
            self._finish_worker(
                window, {"shape": collision_shape_text(new_shape)},
                snapshot=before)
        self.assertEqual(window.project.collision_shape, old_shape)
        self.assertIsNotNone(window.project.legacy)
        self.assertEqual(len(window.project.compound), 1)
        self.assertNotEqual(window.project.snapshot(), before)
        self.assertIn("project data changed", window.collision_shape_status.text())
        warning.assert_not_called()

    def test_success_removes_both_sphere_types_as_one_undo_and_redo(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        old_shape = CollisionShape(
            "Old", "7" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        new_shape = CollisionShape(
            "New", "8" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        window.project.collision_shape = old_shape
        window.project.collision_shape_path = "Data/Collision/Old.collision"
        window.project.legacy = CollisionSphere(LEGACY, radius=12)
        window.project.compound = [
            CollisionSphere(OPENNEOUA, x=2, radius=4),
            CollisionSphere(OPENNEOUA, x=5, radius=6),
        ]
        window.project.sphere_isolation_active = True
        window._selected = 2
        window._selected_spheres = {0, 1, 2}
        with patch.object(editor_module.QMessageBox, "warning") as warning:
            self._finish_worker(
                window, {"shape": collision_shape_text(new_shape)})
        warning.assert_not_called()

        self.assertEqual(window.project.collision_shape, new_shape)
        self.assertIsNone(window.project.legacy)
        self.assertEqual(window.project.compound, [])
        self.assertFalse(window.project.sphere_isolation_active)
        self.assertEqual(window._selected, -1)
        self.assertEqual(window._selected_spheres, set())
        self.assertEqual(len(window._undo), 1)
        self.assertEqual(window.collision_shape_status.text(), "Shape ready: 1 parts.")

        window.undo()
        self.assertEqual(window.project.collision_shape, old_shape)
        self.assertEqual(window.project.collision_shape_path,
                         "Data/Collision/Old.collision")
        self.assertIsNotNone(window.project.legacy)
        self.assertEqual(len(window.project.compound), 2)
        self.assertTrue(window.project.sphere_isolation_active)
        self.assertEqual(window._selected_sphere_indices(), {0, 1, 2})
        window.redo()
        self.assertEqual(window.project.collision_shape, new_shape)
        self.assertEqual(window.project.spheres(), [])
        self.assertFalse(window.project.sphere_isolation_active)

    def test_export_suggests_sanitized_unit_filename_and_model_fallback(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        window.project.name = "Fox / Hunter?"
        self.assertEqual(
            editor_module._collision_shape_export_filename(window.project),
            "Fox_Hunter.collision")
        window.project.name = "Model"
        window.project.source_model = "Data/Models/VP_FOX.SKLT"
        self.assertEqual(
            editor_module._collision_shape_export_filename(window.project),
            "VP_FOX.collision")

        window.project.name = "Fox"
        window.project.collision_shape = CollisionShape(
            "Fox", "9" * 64, (1, 1, 1), (0, 0, 0), (_tetrahedron(),))
        with patch.object(
                editor_module.QFileDialog, "getSaveFileName",
                return_value=("", "")) as choose_path:
            window.export_collision_shape()
        self.assertEqual(Path(choose_path.call_args.args[2]).name,
                         "Fox.collision")

    def test_imported_shape_without_selection_metadata_is_marked_unverified(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        root = FamilyObject(
            BaseObject(name="Body", skeleton_name="body"),
            skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
            owner_path="root")
        window.family = AssetFamily(root_object=root)
        window.project.collision_shape = CollisionShape(
            "Imported", "a" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        warnings = window._collision_shape_warnings()
        self.assertTrue(any(
            "source selection not stored" in warning for warning in warnings))
        self.assertFalse(any(
            "fingerprint differs" in warning for warning in warnings))

    def test_script_shape_from_set1_keeps_source_set_and_transform_warnings(self):
        from tempfile import TemporaryDirectory
        from sklt_parser import SkltModel
        from vp_manager import EmbeddedVPEntry, EmbeddedVPSet, VPEntry, VPTable

        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        with TemporaryDirectory() as directory:
            data = Path(directory) / "Data"
            script_path = data / "Scripts" / "Vehicles.cfg"
            set_bas = data / "Sets" / "Set1" / "Objects" / "SET.BAS"
            shape_path = data / "Sets" / "Set1" / "Collision" / "SourceSet2.collision"
            for path in (script_path.parent, set_bas.parent, shape_path.parent):
                path.mkdir(parents=True, exist_ok=True)
            set_bas.write_bytes(b"placeholder")

            shape = CollisionShape(
                "VP_Set2_Test", "2" * 64, (2, 3, 4), (0, 15, 0),
                (_tetrahedron(),), asset_set=2)
            write_collision_shape(shape_path, shape)
            script_path.write_text(
                "new_vehicle 77\n"
                "    name = Set Mismatch Test\n"
                "    vp_normal = 1\n"
                "    scale_x = 2\n"
                "    scale_y = 3\n"
                "    scale_z = 4\n"
                "    rotation_y = 15\n"
                "    collision_shape = Data/Sets/Set1/Collision/SourceSet2.collision\n"
                "end\n",
                encoding="utf-8")

            root = FamilyObject(
                BaseObject(name="Root", skeleton_name="models/root.sklt"),
                skeleton=SkltModel(
                    source_name="root.sklt",
                    points=[(-2, -1, -1), (2, -1, -1), (0, 2, 1)],
                    polygons=[[0, 1, 2]]),
                owner_path="root")
            kid = FamilyObject(
                BaseObject(name="Kid", skeleton_name="models/kid.sklt"),
                skeleton=SkltModel(
                    source_name="kid.sklt",
                    points=[(-1, -1, 0), (1, -1, 0), (0, 1, 0)],
                    polygons=[[0, 1, 2]]),
                owner_path="root/kid[0]")
            root.kids.append(kid)
            window.family = AssetFamily(root_object=root)
            window._vp_embedded = EmbeddedVPSet(
                source_path=set_bas,
                container_source_offset=0,
                entries=(
                    EmbeddedVPEntry(0, "Root.base", "models/root.sklt"),
                    EmbeddedVPEntry(1, "Kid.base", "models/kid.sklt"),
                ))
            window._vp_table = VPTable((
                VPEntry(0, "Root.base"), VPEntry(1, "Kid.base")))
            window._vp_table_source = "embedded test"
            window._fill_models(window.family)

            self.assertTrue(window.open_vehicle_script(
                script_path, object_id=77, object_kind="new_vehicle"))

            self.assertEqual(window._active_asset_set_id(), 1)
            self.assertEqual(window.project.collision_shape, shape)
            self.assertEqual(
                Path(window.project.collision_shape_file_path),
                shape_path.resolve())
            warnings = window._collision_shape_warnings()
            self.assertFalse(any(
                "set" in warning.casefold() for warning in warnings), warnings)
            self.assertEqual(window.project.collision_shape.asset_set, 2)
            self.assertIn("Shape loaded: 1 parts", window.collision_shape_status.text())
            self.assertNotIn("source_set", window.collision_shape_status.text())

            window.project.model_scale_x = 2.25
            window._sync_collision_shape_status()
            warnings = window._collision_shape_warnings()
            self.assertIn(
                "visual scale differs from generation metadata", warnings)
            self.assertNotIn(
                "visual rotation differs from generation metadata", warnings)

            window.project.model_scale_x = 2
            window.project.model_rotation_y = 15.5
            window._sync_collision_shape_status()
            warnings = window._collision_shape_warnings()
            self.assertIn(
                "visual rotation differs from generation metadata", warnings)

    def test_script_visual_override_parses_without_a_vp(self):
        text = (
            "new_vehicle 3\n"
            "    3ds_normal = Data/Models/3ds/Test.3ds\n"
            "    rotation_x = 90\n"
            "    collision_shape = Data/Models/Collision/Test.collision\n"
            "end\n")
        reference = script_model_references(text)[0]
        self.assertIsNone(reference.vp_normal)
        self.assertEqual(reference.three_ds_normal,
                         "Data/Models/3ds/Test.3ds")
        self.assertEqual(reference.rotation_x, 90)
        self.assertEqual(
            reference.collision_shape_path,
            "Data/Models/Collision/Test.collision")

    def test_visual_source_falls_back_from_missing_3ds_to_valid_base(self):
        from tempfile import TemporaryDirectory
        text = (
            "new_vehicle 3\n"
            "    3ds_normal = Data/Models/Missing.3ds\n"
            "    base_normal = Data/Models/Good.BASE\n"
            "    vp_normal = 6\n"
            "end\n")
        reference = script_model_references(text)[0]
        with TemporaryDirectory() as directory:
            data = Path(directory) / "Data"
            scripts = data / "Scripts"
            models = data / "Models"
            scripts.mkdir(parents=True)
            models.mkdir(parents=True)
            script_path = scripts / "Vehicles.cfg"
            script_path.write_text(text, encoding="utf-8")
            base_path = models / "Good.BASE"
            base_path.touch()
            root = FamilyObject(
                BaseObject(name="Good"),
                skeleton=types.SimpleNamespace(polygons=[[0, 1, 2]]),
                owner_path="root")
            family = AssetFamily(root_object=root)
            kind, resolved, value, warnings = (
                editor_module._resolve_script_visual_source(
                    reference, script_path, None, None,
                    family_loader=lambda _path: family))
        self.assertEqual(kind, "base_normal")
        self.assertEqual(resolved, base_path.resolve())
        self.assertEqual(value, "Data/Models/Good.BASE")
        self.assertTrue(any("3ds_normal ignored" in item for item in warnings))

    def test_visual_source_falls_back_from_invalid_3ds_to_valid_set_vp(self):
        from tempfile import TemporaryDirectory
        text = (
            "new_vehicle 3\n"
            "    3ds_normal = Data/Models/Broken.3ds\n"
            "    vp_normal = 6\n"
            "end\n")
        reference = script_model_references(text)[0]
        with TemporaryDirectory() as directory:
            data = Path(directory) / "Data"
            scripts = data / "Scripts"
            models = data / "Models"
            scripts.mkdir(parents=True)
            models.mkdir(parents=True)
            script_path = scripts / "Vehicles.cfg"
            script_path.write_text(text, encoding="utf-8")
            broken_3ds = models / "Broken.3ds"
            broken_3ds.touch()
            with patch.object(
                    editor_module, "read_3ds",
                    side_effect=ValueError("invalid 3DS mesh")):
                kind, resolved, value, warnings = (
                    editor_module._resolve_script_visual_source(
                        reference, script_path, None, None))
        self.assertEqual(kind, "vp_normal")
        self.assertIsNone(resolved)
        self.assertEqual(value, "")
        self.assertTrue(any("invalid 3DS mesh" in item for item in warnings))

    def test_3ds_meshes_are_wrapped_in_the_shared_asset_family(self):
        mesh = types.SimpleNamespace(
            vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            faces=[(0, 1, 2)],
        )
        with patch.object(editor_module, "read_3ds", return_value=([mesh], {})):
            family = editor_module._load_3ds_family(Path("Pilot.3ds"))
        child = family.root_object.kids[0]
        self.assertEqual(child.owner_path, "root/kid[0]")
        self.assertEqual(child.skeleton.polygons, [[0, 1, 2]])
        self.assertEqual(child.skeleton.points[1], (1, 0, 0))

    def test_open_3ds_routes_through_asset_viewport(self):
        mesh = types.SimpleNamespace(
            vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            faces=[(0, 1, 2)],
        )
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (window._set_modified(False), window.close()))
        with patch.object(editor_module, "read_3ds", return_value=([mesh], {})):
            self.assertTrue(window.open_base("Pilot.3ds"))
        self.assertIsNotNone(window.family)
        self.assertEqual(len(window.viewport.local_owner_triangles(None)), 1)
        self.assertEqual(window.viewport._family_ref, window.family)

    def test_worker_file_protocol_writes_atomic_json_response(self):
        from tempfile import TemporaryDirectory
        shape = CollisionShape(
            "Worker_Test", "9" * 64, (1, 1, 1), (0, 0, 0),
            (_tetrahedron(),))
        generated = ShapeGenerationResult(
            shape, ShapeMetrics(0.25, 0.2, 0.25, 16), ())
        request = {
            "source": "Worker_Test",
            "parts": [{"owner": "root/body", "triangles": [
                [list(point) for point in triangle]
                for triangle in (
                    tuple(_tetrahedron().vertices[index] for index in face)
                    for face in _tetrahedron().faces)
            ]}],
            "scale": [1, 1, 1],
            "rotation": [0, 0, 0],
        }
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text(
                json.dumps(request), encoding="utf-8")
            with patch.object(
                    shape_module, "generate_collision_shape",
                    return_value=generated):
                self.assertEqual(shape_module.worker_main(
                    input_path, output_path), 0)
            response = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                parse_collision_shape(response["shape"]), shape)
            self.assertEqual(response["metrics"]["sample_count"], 16)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()),
                             ["input.json", "output.json"])

    def test_worker_file_protocol_returns_error_document_without_partial_file(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.json"
            output_path = Path(directory) / "output.json"
            input_path.write_text("{}", encoding="utf-8")
            self.assertEqual(shape_module.worker_main(
                input_path, output_path), 2)
            response = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn("error", response)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()),
                             ["input.json", "output.json"])

    def test_hidden_worker_entry_runs_before_qt_startup(self):
        import main
        with patch.object(sys, "argv", [
                "main.py", "--collision-bake-worker", "input.json",
                "output.json"]), \
                patch("collision_editor.shape.worker_main", return_value=17) as worker:
            self.assertEqual(main.main(), 17)
        worker.assert_called_once_with("input.json", "output.json")


if __name__ == "__main__":
    unittest.main()
