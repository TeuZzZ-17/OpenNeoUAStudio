import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QApplication

from assembly_viewer import AssetViewport
from geometry_editor import GeometryEditSession
from sklt_parser import SkltModel


def _viewport(points, selection):
    model = SkltModel(
        source_name="ALIGN.SKLT",
        points=list(points),
        polygons=[[0, 1, 2]] if len(points) >= 3 else [],
        parsed_polygon_count=1 if len(points) >= 3 else 0,
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[]),
    )
    viewport = AssetViewport()
    viewport.resize(400, 300)
    viewport._scale = 1.0
    viewport._yaw = 0.0
    viewport._pitch = 0.0
    viewport._edit_session = GeometryEditSession(
        obj,
        [[1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0],
         [0.0, 0.0, 1.0]],
        (0.0, 0.0, 0.0),
    )
    viewport._edit_owner = "root"
    viewport.edit_session.selection = set(selection)
    viewport.set_auto_align_enabled(True)
    selected = [model.points[index] for index in sorted(selection)]
    viewport._prepare_alignment_targets(selected, selection)
    return viewport, model


class AutoAlign3DTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_viewport_default_is_enabled(self):
        self.assertTrue(AssetViewport()._auto_align_enabled)

    def test_mirror_x_applies_move_rotate_and_scale_to_counterparts(self):
        points = [
            (1.0, 0.0, 0.0), (1.0, 2.0, 0.0),
            (-1.0, 0.0, 0.0), (-1.0, 2.0, 0.0),
        ]
        for operation in ("move", "rotate", "scale"):
            with self.subTest(operation=operation):
                viewport, _model = _viewport(points, {0, 1})
                session = viewport.edit_session
                session.set_mirror_x_enabled(True)
                self.assertTrue(session.begin_modal())
                if operation == "move":
                    session.preview_grab((0.5, 0.25, 0.0))
                elif operation == "rotate":
                    session.preview_rotate((0.0, 0.0, 1.0), 0.4)
                else:
                    session.preview_scale_axes((1.4, 0.8, 1.0))
                pending = session.points()
                self.assertEqual(
                    pending[2], (-pending[0][0], pending[0][1], pending[0][2]))
                self.assertEqual(
                    pending[3], (-pending[1][0], pending[1][1], pending[1][2]))

    def test_snap_to_origin_and_reference_vertex_uses_pixel_threshold(self):
        viewport, _model = _viewport(
            [(0.04, 0.0, 0.0), (1.0, 0.0, 0.0)], {0})
        status = []
        viewport.statusMessage.connect(status.append)
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                [(0.04, 0.0, 0.0)], (-0.02, 0.0, 0.0))
        self.assertAlmostEqual(delta[0], -0.04)
        self.assertEqual(viewport._precision_guides[0].axis, 0)
        self.assertIn("X origin", viewport._precision_guides[0].label)
        self.assertIn("Auto Align", status[-1])

        viewport, _model = _viewport(
            [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0),
             (1.0, 0.0, 0.0)],
            {0, 1})
        selected = [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0)]
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                selected, (0.77, 0.0, 0.0))
        self.assertAlmostEqual(delta[0], 0.8)
        self.assertIn("selection X max", viewport._precision_guides[0].label)
        self.assertIn("reference vertex", viewport._precision_guides[0].label)

    def test_precision_guides_draw_lines_and_marker_without_text(self):
        viewport, _model = _viewport(
            [(0.04, 0.0, 0.0), (1.0, 0.0, 0.0)], {0})
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            viewport._snap_translation(
                [(0.04, 0.0, 0.0)], (-0.02, 0.0, 0.0))
        painter = MagicMock()
        viewport._draw_precision_guides(
            painter, QRectF(0, 0, 400, 300),
            viewport._camera_state())
        self.assertGreater(painter.drawLine.call_count, 10)
        painter.drawText.assert_not_called()

    def test_alt_bypass_and_explicit_axis_priority(self):
        viewport, _model = _viewport(
            [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0),
             (1.0, 0.0, 0.0)],
            {0, 1})
        selected = [(0.0, 0.0, 0.0), (0.2, 0.0, 0.0)]
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.AltModifier):
            delta = viewport._snap_translation(
                selected, (0.77, 0.13, -0.21),
                allow_axis_lock=True)
        self.assertEqual(delta, (0.77, 0.13, -0.21))
        self.assertEqual(viewport._precision_guides, ())

        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                selected, (0.3, 0.4, 0.5), axis_name="Y")
        self.assertEqual(delta[0], 0.0)
        self.assertEqual(delta[2], 0.0)

    def test_soft_auto_axis_lock_has_hysteresis(self):
        viewport, _model = _viewport(
            [(0.0, 0.0, 0.0), (0.2, 0.2, 0.2),
             (5.0, 5.0, 5.0)],
            {0, 1})
        selected = [(0.0, 0.0, 0.0), (0.2, 0.2, 0.2)]
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            first = viewport._snap_translation(
                selected, (1.0, 0.01, 0.01),
                allow_axis_lock=True)
            second = viewport._snap_translation(
                selected, (1.1, 0.02, 0.01),
                allow_axis_lock=True)
        self.assertEqual(viewport._auto_axis_lock, 0)
        self.assertEqual(first[1:], (0.0, 0.0))
        self.assertEqual(second[1:], (0.0, 0.0))

    def test_axis_scale_snaps_without_shear_merge_or_topology_change(self):
        viewport, model = _viewport(
            [(0.0, 2.0, 0.0), (0.2, 4.0, 0.0),
             (1.0, 9.0, 0.0)],
            {0, 1})
        before_polygons = [list(poly) for poly in model.polygons]
        before_count = len(model.points)
        self.assertTrue(viewport.edit_session.begin_modal())
        viewport._prepare_alignment_targets(
            [model.points[0], model.points[1]], {0, 1})
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            factor = viewport._snap_scale_factor(8.5, "X")
        self.assertAlmostEqual(factor, 9.0)
        viewport.edit_session.preview_scale(factor, "X")
        pending = viewport.edit_session.points()
        self.assertEqual(pending[0][1:], (2.0, 0.0))
        self.assertEqual(pending[1][1:], (4.0, 0.0))
        self.assertAlmostEqual(pending[1][0], 1.0)
        self.assertEqual(len(pending), before_count)
        self.assertEqual(model.polygons, before_polygons)
        self.assertEqual(len(set(pending)), len(pending))
        viewport.edit_session.cancel_modal()
        viewport._clear_precision_guides()
        self.assertEqual(viewport._precision_guides, ())
        self.assertEqual(viewport._precision_bbox_points, ())

    def test_topology_bounds_coplanar_and_mirror_targets_are_cached(self):
        model = SkltModel(
            source_name="ALIGN_TARGETS.SKLT",
            points=[
                (0.25, 0.5, 0.75),
                (2.0, 0.0, 1.0),
                (5.0, 0.0, 1.0),
                (2.0, 4.0, 1.0),
            ],
            polygons=[[1, 2, 3]],
            parsed_polygon_count=1,
        )
        viewport = AssetViewport()
        viewport.resize(400, 300)
        viewport._scale = 1.0
        viewport._yaw = 0.0
        viewport._pitch = 0.0
        viewport._edit_session = GeometryEditSession(
            SimpleNamespace(
                skeleton=model,
                base_object=SimpleNamespace(ades=[])),
            [[1.0, 0.0, 0.0],
             [0.0, 1.0, 0.0],
             [0.0, 0.0, 1.0]],
            (0.0, 0.0, 0.0),
        )
        viewport._edit_owner = "root"
        viewport.edit_session.selection = {0}
        viewport._prepare_alignment_targets([model.points[0]], {0})
        snapshot = viewport._alignment_targets
        labels = {
            target.label
            for axis_targets in snapshot
            for target in axis_targets
        }
        values = [
            {round(target.value, 6) for target in axis_targets}
            for axis_targets in snapshot
        ]
        self.assertTrue(any("face #0 center" in label for label in labels))
        self.assertTrue(any("edge " in label and "midpoint" in label
                            for label in labels))
        self.assertTrue(any("coplanar Z" in label for label in labels))
        self.assertIn(3.5, values[0])  # unselected X bounds center
        self.assertTrue(any("mirror X at origin" in label for label in labels))
        self.assertTrue(any("owner bounds center" in label
                            for label in labels))
        self.assertFalse(any(label.startswith("selection ")
                             for label in labels))

        model.points[2] = (50.0, 0.0, 1.0)
        self.assertEqual(viewport._alignment_targets, snapshot)
        viewport._prepare_alignment_targets([model.points[0]], {0})
        self.assertNotEqual(viewport._alignment_targets, snapshot)

    def test_selected_geometry_is_not_its_own_alignment_target(self):
        viewport, model = _viewport(
            [(1.0, 2.0, 3.0), (2.0, 3.0, 4.0),
             (3.0, 4.0, 5.0)],
            {0, 1, 2})
        labels = {
            target.label
            for axis_targets in viewport._alignment_targets
            for target in axis_targets
        }
        self.assertEqual(labels, {"X origin", "Y origin", "Z origin"})
        self.assertEqual(model.polygons, [[0, 1, 2]])

    def test_mirror_x_origin_and_owner_bounds_center_snap_explicitly(self):
        viewport, _model = _viewport(
            [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)], {0})
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                [(1.0, 0.0, 0.0)], (-2.97, 0.0, 0.0),
                axis_name="X")
        self.assertAlmostEqual(delta[0], -3.0)
        self.assertIn(
            "mirror X at origin", viewport._precision_guides[0].label)

        viewport, _model = _viewport(
            [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0),
             (10.0, 0.0, 0.0)],
            {0})
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                [(0.0, 0.0, 0.0)], (7.97, 0.0, 0.0),
                axis_name="X")
        self.assertAlmostEqual(delta[0], 8.0)
        self.assertIn(
            "owner bounds center", viewport._precision_guides[0].label)

    def test_equal_spacing_requires_a_regular_three_point_edge_sequence(self):
        viewport, _model = _viewport(
            [(0.3, 0.0, 0.0), (1.0, 0.0, 0.0),
             (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)],
            {0})
        targets = viewport._alignment_targets[0]
        spacing = [
            target for target in targets
            if "equal spacing/distance X" in target.label
        ]
        self.assertEqual(
            [round(target.value, 6) for target in spacing], [4.0])
        self.assertTrue(any(
            round(target.value, 6) == 0.0 and target.label == "X origin"
            for target in targets))
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                [(0.3, 0.0, 0.0)], (3.66, 0.0, 0.0),
                axis_name="X")
        self.assertAlmostEqual(delta[0], 3.7)
        self.assertIn(
            "equal spacing/distance X", viewport._precision_guides[0].label)

        viewport, _model = _viewport(
            [(0.3, 0.0, 0.0), (1.0, 0.0, 0.0),
             (2.0, 0.0, 0.0), (3.2, 0.0, 0.0)],
            {0})
        self.assertFalse(any(
            "equal spacing/distance" in target.label
            for target in viewport._alignment_targets[0]))

    def test_distant_vaguely_similar_target_is_rejected(self):
        viewport, _model = _viewport(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)], {0})
        with patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            delta = viewport._snap_translation(
                [(0.0, 0.0, 0.0)], (0.90, 0.0, 0.0),
                axis_name="X")
        self.assertEqual(delta, (0.90, 0.0, 0.0))
        self.assertEqual(viewport._precision_guides, ())

    def test_large_vertex_corpus_is_cached_and_moves_do_not_rescan_model(self):
        points = [
            (index * 0.25, 0.0, 0.0)
            for index in range(2001)
        ]
        viewport, model = _viewport(points, {0})
        first_snapshot = viewport._alignment_targets
        viewport._prepare_alignment_targets([points[0]], {0})
        self.assertEqual(viewport._alignment_targets, first_snapshot)
        self.assertTrue(any(
            target.value > points[-1][0]
            and "equal spacing/distance X" in target.label
            for target in first_snapshot[0]))

        class NoPerMovePointScan:
            def __iter__(self):
                raise AssertionError("model points rescanned during move")

        model.points = NoPerMovePointScan()
        with patch.object(
                viewport, "_prepare_alignment_targets",
                side_effect=AssertionError("target cache rebuilt")), patch(
                "assembly_viewer.QApplication.keyboardModifiers",
                return_value=Qt.KeyboardModifier.NoModifier):
            for raw_delta in (10.0, 20.0, 30.0, 40.0):
                viewport._snap_translation(
                    [points[0]], (raw_delta, 0.0, 0.0),
                    axis_name="X")
        self.assertEqual(viewport._alignment_targets, first_snapshot)


if __name__ == "__main__":
    unittest.main()
