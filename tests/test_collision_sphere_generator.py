import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from collision_editor import (
    CollisionEditorWindow,
    CollisionSphere,
    LEGACY,
    OPENNEOUA,
)
from collision_editor.sphere_generator import (
    ACCURACY_PRESETS,
    UNIT_COLL_MAX_COUNT,
    generate_collision_spheres,
)


def _box(x0, y0, z0, x1, y1, z1):
    vertices = [
        (x0, y0, z0), (x1, y0, z0),
        (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1),
        (x1, y1, z1), (x0, y1, z1),
    ]
    faces = [
        (0, 1, 2, 3), (4, 7, 6, 5),
        (0, 4, 5, 1), (1, 5, 6, 2),
        (2, 6, 7, 3), (4, 0, 3, 7),
    ]
    return [
        triangle
        for a, b, c, d in faces
        for triangle in (
            (vertices[a], vertices[b], vertices[c]),
            (vertices[a], vertices[c], vertices[d]),
        )
    ]


def _inside(point, sphere, epsilon=1e-7):
    distance_squared = sum(
        (point[axis] - sphere.center[axis]) ** 2 for axis in range(3))
    return distance_squared <= (sphere.radius + epsilon) ** 2


class CollisionSphereGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        window = CollisionEditorWindow()
        self.addCleanup(lambda: (
            window._set_modified(False), window.close()))
        return window

    def test_generation_is_deterministic_and_covers_mesh_vertices(self):
        triangles = _box(-12, -3, -4, 12, 3, 4)
        first = generate_collision_spheres(triangles, "high")
        second = generate_collision_spheres(triangles, "high")
        self.assertEqual(first, second)
        self.assertTrue(first.spheres)
        for point in {point for triangle in triangles for point in triangle}:
            self.assertTrue(any(_inside(point, sphere)
                                for sphere in first.spheres))

    def test_accuracy_presets_tighten_geometry_without_sphere_targets(self):
        triangles = _box(-25, -2, -3, 25, 2, 3)
        results = [
            generate_collision_spheres(triangles, preset.key)
            for preset in ACCURACY_PRESETS
        ]
        self.assertEqual(
            [preset.tolerance_fraction for preset in ACCURACY_PRESETS],
            sorted(
                (preset.tolerance_fraction for preset in ACCURACY_PRESETS),
                reverse=True))
        self.assertLessEqual(results[1].measured_error,
                             results[0].measured_error)
        self.assertLessEqual(results[2].measured_error,
                             results[1].measured_error)
        self.assertLessEqual(results[3].measured_error,
                             results[2].measured_error)
        self.assertEqual([len(result.spheres) for result in results],
                         sorted(len(result.spheres) for result in results))
        for preset, result in zip(ACCURACY_PRESETS, results):
            self.assertLessEqual(
                result.measured_error,
                preset.tolerance_fraction
                + preset.minimum_gain_fraction + 1e-9)

    def test_open_non_watertight_surface_uses_surface_fallback(self):
        triangles = [
            ((-5.0, 0.0, -2.0), (5.0, 0.0, -2.0), (5.0, 0.0, 2.0)),
            ((-5.0, 0.0, -2.0), (5.0, 0.0, 2.0), (-5.0, 0.0, 2.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
        ]
        result = generate_collision_spheres(triangles, "medium")
        self.assertGreater(len(result.spheres), 1)
        self.assertLessEqual(len(result.spheres), UNIT_COLL_MAX_COUNT)
        self.assertTrue(all(sphere.radius > 0 for sphere in result.spheres))

    def test_asymmetric_protrusion_receives_collision_coverage(self):
        body = _box(-8, -3, -4, 8, 3, 4)
        protrusion = _box(8, -1, -1, 18, 1, 1)
        result = generate_collision_spheres(body + protrusion, "high")
        protrusion_tip = (18, 1, 1)
        self.assertTrue(any(_inside(protrusion_tip, sphere)
                            for sphere in result.spheres))
        self.assertTrue(any(sphere.x > 8 for sphere in result.spheres))

    def test_runtime_count_is_only_a_safety_cap(self):
        triangles = _box(-30, -2, -2, 30, 2, 2)
        result = generate_collision_spheres(
            triangles, "ultra", max_spheres=3)
        self.assertLessEqual(len(result.spheres), 3)
        self.assertTrue(result.hit_safety_cap)

    def test_ui_generation_is_one_undo_and_preserves_legacy_radius(self):
        window = self._window()
        window.project.legacy = CollisionSphere(
            LEGACY, 0.0, 0.0, 0.0, 77.0, False)
        before = window.project.snapshot()
        triangles = _box(-10, -3, -4, 10, 3, 4)
        with patch.object(
                window.viewport, "local_owner_triangles",
                return_value=triangles):
            window.generate_collision_spheres("medium")
        self.assertTrue(window.project.compound)
        self.assertEqual(len(window._undo), 1)
        self.assertEqual(
            (window.project.legacy.radius, window.project.legacy.visible),
            (77.0, False))
        self.assertIn("Medium Accuracy", window.statusBar().currentMessage())
        window.undo()
        self.assertEqual(window.project.snapshot(), before)

    def test_existing_compound_is_not_replaced_when_confirmation_is_cancelled(self):
        window = self._window()
        window.project.compound = [
            CollisionSphere(OPENNEOUA, 1, 2, 3, 4)]
        before = window.project.snapshot()
        with (
            patch.object(
                window.viewport, "local_owner_triangles",
                return_value=_box(-5, -2, -2, 5, 2, 2)),
            patch.object(
                window, "_confirm_generated_replacement",
                return_value=False) as confirmation,
        ):
            window.generate_collision_spheres("high")
        confirmation.assert_called_once_with()
        self.assertEqual(window.project.snapshot(), before)
        self.assertFalse(window._undo)

    def test_generated_spheres_use_the_existing_editing_operations(self):
        window = self._window()
        with patch.object(
                window.viewport, "local_owner_triangles",
                return_value=_box(-8, -3, -4, 8, 3, 4)):
            window.generate_collision_spheres("low")
        generated = window.project.snapshot()
        generated_count = len(window.project.compound)
        self.assertTrue(all(
            sphere.category == OPENNEOUA
            for sphere in window.project.compound))

        window.duplicate_sphere()
        self.assertEqual(len(window.project.compound), generated_count + 1)
        window.undo()
        self.assertEqual(window.project.snapshot(), generated)
        window.redo()
        self.assertEqual(len(window.project.compound), generated_count + 1)

        window._set_all_sphere_visibility(False)
        self.assertTrue(all(
            not sphere.visible for sphere in window.project.compound))
        window._set_all_sphere_visibility(True)
        self.assertTrue(all(
            sphere.visible for sphere in window.project.compound))
        before_mirror = len(window.project.compound)
        window.mirror_selected_sphere("x")
        self.assertEqual(len(window.project.compound), before_mirror + 1)
        window.delete_sphere()
        self.assertEqual(len(window.project.compound), before_mirror)

    def test_panel_and_add_menu_share_the_same_accuracy_actions(self):
        window = self._window()
        panel_actions = window.generate_collision_spheres_button.menu().actions()
        add_actions = window.generate_collision_spheres_menu.actions()
        self.assertEqual(panel_actions, add_actions)
        self.assertEqual([action.text() for action in panel_actions], [
            "Low Accuracy", "Medium Accuracy",
            "High Accuracy", "Ultra Accuracy",
        ])
        self.assertFalse(hasattr(window, "create_suggested_action"))
        self.assertFalse(hasattr(window, "create_suggested_button"))

    def test_runtime_and_editor_safety_cap_is_512(self):
        self.assertEqual(UNIT_COLL_MAX_COUNT, 512)

    def test_live_sphere_counter_tracks_openneoua_compound_only(self):
        window = self._window()
        window.project.legacy = CollisionSphere(LEGACY, radius=40.0)
        window.project.compound = [
            CollisionSphere(OPENNEOUA, radius=1.0),
            CollisionSphere(OPENNEOUA, radius=2.0),
            CollisionSphere(OPENNEOUA, radius=3.0),
        ]
        window._sync_all()
        self.assertEqual(
            window.collision_sphere_count_label.text(),
            "OpenNeoUA Spheres: 3 / 512")

        window._selected = 1
        window._selected_spheres = {1}
        window.delete_sphere()
        self.assertEqual(
            window.collision_sphere_count_label.text(),
            "OpenNeoUA Spheres: 2 / 512")

    def test_manual_creation_stops_at_runtime_safety_cap(self):
        window = self._window()
        window.project.compound = [
            CollisionSphere(OPENNEOUA, radius=1.0)
            for _ in range(UNIT_COLL_MAX_COUNT)
        ]
        window._sync_all()
        before = window.project.snapshot()

        self.assertFalse(window.add_openneoua_action.isEnabled())
        window.add_compound(OPENNEOUA)

        self.assertEqual(len(window.project.compound), UNIT_COLL_MAX_COUNT)
        self.assertEqual(window.project.snapshot(), before)
        self.assertIn("512 / 512", window.statusBar().currentMessage())

    def test_duplicate_and_mirror_are_atomic_when_they_would_exceed_cap(self):
        window = self._window()
        window.project.compound = [
            CollisionSphere(OPENNEOUA, x=float(index), radius=1.0)
            for index in range(UNIT_COLL_MAX_COUNT - 1)
        ]
        window._selected = 0
        window._selected_spheres = {0, 1}
        window._sync_all()
        before = window.project.snapshot()

        window.duplicate_sphere()
        self.assertEqual(window.project.snapshot(), before)
        self.assertIn("Duplicate selection", window.statusBar().currentMessage())

        window.mirror_selected_sphere("x")
        self.assertEqual(window.project.snapshot(), before)
        self.assertIn("Mirror selection", window.statusBar().currentMessage())

    def test_legacy_to_compound_conversion_respects_cap(self):
        window = self._window()
        legacy = CollisionSphere(LEGACY, radius=40.0)
        window.project.legacy = legacy
        window.project.compound = [
            CollisionSphere(OPENNEOUA, radius=1.0)
            for _ in range(UNIT_COLL_MAX_COUNT)
        ]
        window._selected = 0
        window._selected_spheres = {0}
        window._sync_all()
        before = window.project.snapshot()

        window.change_sphere_type(OPENNEOUA)

        self.assertEqual(window.project.snapshot(), before)
        self.assertIs(window.project.legacy, legacy)
        self.assertIn("Convert Legacy Radius", window.statusBar().currentMessage())


if __name__ == "__main__":
    unittest.main()
