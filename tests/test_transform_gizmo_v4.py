import math
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from assembly_viewer import AssetViewport
from assembly_window import AssemblyWindow
from geometry_editor import GeometryEditSession
from model_space_gizmo import (
    AXIS_DIRECTIONS,
    DIRECTIONS_26,
    SCALE_DIRECTIONS,
    ModelSpaceGizmo,
)
from sklt_parser import SkltModel


def _install_edit_session(viewport, points, selection):
    model = SkltModel(
        source_name="TRANSFORM.SKLT",
        points=list(points),
        polygons=[[0, 1, 2]] if len(points) >= 3 else [],
        parsed_polygon_count=1 if len(points) >= 3 else 0,
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[]),
    )
    viewport._edit_session = GeometryEditSession(
        obj,
        [[1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0],
         [0.0, 0.0, 1.0]],
        (0.0, 0.0, 0.0),
    )
    viewport._edit_owner = "root"
    viewport.edit_session.selection = set(selection)
    return model


class TransformGizmoV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mode_handles_replace_each_other(self):
        gizmo = ModelSpaceGizmo()
        self.assertEqual(gizmo.mode, "move")
        self.assertEqual(set(gizmo.directions), set(DIRECTIONS_26))
        gizmo.set_mode("rotate")
        self.assertEqual(set(gizmo.directions), set(AXIS_DIRECTIONS))
        self.assertIn(
            "right-hand rule", gizmo.direction_text((1, 0, 0)))
        gizmo.set_mode("scale")
        self.assertEqual(set(gizmo.directions), set(SCALE_DIRECTIONS))
        self.assertIn(
            "Uniform +", gizmo.direction_text((1, 1, 1)))

    def test_window_defaults_labels_and_selection_survive_mode_switch(self):
        window = AssemblyWindow()
        try:
            _install_edit_session(
                window.viewport,
                [(-1.0, 0.0, 0.0),
                 (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                {0, 1, 2},
            )
            selection = set(window.viewport.edit_session.selection)
            self.assertEqual(window._transform_mode, "move")
            self.assertEqual(window.transform_box.title(), "Transform")
            self.assertTrue(window.auto_align_check.isChecked())
            self.assertEqual(
                window.gizmo_intensity_spin.text(), "0.30 model units")
            window._set_transform_mode("rotate")
            self.assertEqual(window.transform_step_label.text(), "Rotate step")
            self.assertEqual(window.gizmo_intensity_spin.text(), "1°")
            self.assertEqual(
                window.viewport.edit_session.selection, selection)
            window._set_transform_mode("scale")
            self.assertEqual(window.transform_step_label.text(), "Scale step")
            self.assertEqual(window.gizmo_intensity_spin.text(), "1%")
            self.assertEqual(
                window.viewport.edit_session.selection, selection)
        finally:
            window.close()

    def test_move_gizmo_is_exact_even_when_auto_align_is_enabled(self):
        viewport = AssetViewport()
        model = _install_edit_session(
            viewport,
            [(0.04, 0.0, 0.0),
             (1.0, 0.0, 0.0),
             (0.0, 1.0, 0.0)],
            {0},
        )
        viewport.set_auto_align_enabled(True)
        viewport._prepare_alignment_targets([model.points[0]], {0})
        self.assertTrue(viewport.begin_model_nudge())
        self.assertTrue(
            viewport.nudge_edit_selection_model(-0.02, 0.20, -0.20))
        self.assertTrue(viewport.end_model_nudge())
        self.assertEqual(model.points[0], (0.02, 0.20, -0.20))

    def test_every_move_handle_applies_full_step_on_each_active_axis(self):
        for direction in DIRECTIONS_26:
            viewport = AssetViewport()
            model = _install_edit_session(
                viewport,
                [(0.0, 0.0, 0.0),
                 (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                {0},
            )
            viewport.set_auto_align_enabled(True)
            self.assertTrue(viewport.begin_model_nudge())
            self.assertTrue(viewport.nudge_edit_selection_model(
                *(component * 0.20 for component in direction)))
            self.assertTrue(viewport.end_model_nudge())
            self.assertEqual(
                model.points[0],
                tuple(component * 0.20 for component in direction))

    def test_rotate_each_axis_and_window_undo_redo(self):
        window = AssemblyWindow()
        try:
            model = _install_edit_session(
                window.viewport,
                [(-1.0, 0.0, 0.0),
                 (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                {0, 1, 2},
            )
            window._selected_owner = "root"
            window.global_edit_button.blockSignals(True)
            window.global_edit_button.setChecked(True)
            window.global_edit_button.blockSignals(False)
            original = list(model.points)
            for direction in ((1, 0, 0), (0, -1, 0), (0, 0, 1)):
                window._set_transform_mode("rotate")
                window._begin_gizmo_nudge(direction)
                self.assertTrue(
                    window.viewport.rotate_edit_selection_model(
                        math.radians(15.0)))
                window._finish_gizmo_nudge()
            self.assertEqual(len(window._edit_undo_stack), 3)
            transformed = list(model.points)
            self.assertNotEqual(transformed, original)
            for _ in range(3):
                window._undo_edit()
            for actual, expected in zip(model.points, original):
                for value, wanted in zip(actual, expected):
                    self.assertAlmostEqual(value, wanted, places=10)
            for _ in range(3):
                window._redo_edit()
            for actual, expected in zip(model.points, transformed):
                for value, wanted in zip(actual, expected):
                    self.assertAlmostEqual(value, wanted, places=10)
        finally:
            window.close()

    def test_positive_and_negative_rotation_math_on_every_axis(self):
        cases = (
            ((1, 0, 0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            ((-1, 0, 0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
            ((0, 1, 0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
            ((0, -1, 0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            ((0, 0, 1), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            ((0, 0, -1), (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
        )
        for direction, point, expected in cases:
            opposite = tuple(-value for value in point)
            viewport = AssetViewport()
            model = _install_edit_session(
                viewport, [point, opposite, (0.0, 0.0, 0.0)], {0, 1})
            self.assertTrue(viewport.begin_model_transform(
                "rotate", direction))
            self.assertTrue(viewport.rotate_edit_selection_model(
                math.radians(90.0)))
            self.assertTrue(viewport.end_model_transform())
            for actual, wanted in zip(model.points[0], expected):
                self.assertAlmostEqual(actual, wanted, places=12)

    def test_scale_per_axis_uniform_inverse_and_invalid_input(self):
        viewport = AssetViewport()
        model = _install_edit_session(
            viewport,
            [(-1.0, -1.0, 0.0),
             (1.0, 1.0, 0.0),
             (0.0, 0.0, 1.0)],
            {0, 1},
        )
        self.assertTrue(viewport.begin_model_transform("scale", (1, 0, 0)))
        self.assertTrue(viewport.scale_edit_selection_model(0.05))
        self.assertTrue(viewport.end_model_transform())
        self.assertAlmostEqual(model.points[0][0], -1.05)
        self.assertAlmostEqual(model.points[0][1], -1.0)

        before = list(model.points)
        self.assertTrue(viewport.begin_model_transform(
            "scale", (-1, -1, -1)))
        self.assertTrue(viewport.scale_edit_selection_model(0.05))
        self.assertTrue(viewport.end_model_transform())
        ratio = 1.0 / 1.05
        self.assertAlmostEqual(model.points[0][0], before[0][0] * ratio)
        self.assertAlmostEqual(model.points[0][1], before[0][1] * ratio)
        self.assertFalse(viewport.begin_model_transform(
            "invalid", (1, 0, 0)))
        self.assertFalse(viewport.scale_edit_selection_model(float("nan")))

    def test_repeated_rotation_previews_from_one_origin_without_drift(self):
        viewport = AssetViewport()
        model = _install_edit_session(
            viewport,
            [(-1.0, 0.0, 0.0),
             (1.0, 0.0, 0.0),
             (0.0, 1.0, 0.0)],
            {0, 1},
        )
        self.assertTrue(viewport.begin_model_transform(
            "rotate", (0, 0, 1)))
        for _ in range(18):
            self.assertTrue(viewport.rotate_edit_selection_model(
                math.radians(5.0)))
        self.assertTrue(viewport.end_model_transform())
        self.assertAlmostEqual(model.points[0][0], 0.0, places=12)
        self.assertAlmostEqual(model.points[0][1], -1.0, places=12)
        self.assertAlmostEqual(model.points[1][0], 0.0, places=12)
        self.assertAlmostEqual(model.points[1][1], 1.0, places=12)

    def test_numeric_focus_does_not_switch_modes_or_accept_invalid_text(self):
        window = AssemblyWindow()
        try:
            spin = window.gizmo_intensity_spin
            spin.setFocus()
            for key in (Qt.Key.Key_W, Qt.Key.Key_E, Qt.Key.Key_R):
                QTest.keyClick(spin, key)
            self.assertEqual(window._transform_mode, "move")
            old_value = spin.value()
            spin.lineEdit().setText("nan")
            spin.interpretText()
            self.assertTrue(math.isfinite(spin.value()))
            self.assertEqual(spin.value(), old_value)
            spin.lineEdit().clear()
            spin.interpretText()
            self.assertTrue(math.isfinite(spin.value()))
            self.assertGreater(spin.value(), 0.0)
        finally:
            window.close()

    def test_returning_to_view_mode_clears_vertices_and_polygons(self):
        window = AssemblyWindow()
        try:
            _install_edit_session(
                window.viewport,
                [(0.0, 0.0, 0.0),
                 (1.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0)],
                {0, 1, 2},
            )
            window._selected_poly = 0
            window._selected_polys = {0}
            window.viewport.set_selected_polygon(0)
            window.viewport.set_highlight_polys({0})
            window.viewport.toggle_edit_mode()
            self.assertIsNone(window.viewport.edit_session)
            self.assertEqual(window._selected_polys, set())
            self.assertIsNone(window._selected_poly)
            self.assertIsNone(window.viewport._selected_poly)
            self.assertEqual(window.viewport._highlight_polys, set())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
