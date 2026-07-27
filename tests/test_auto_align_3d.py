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


if __name__ == "__main__":
    unittest.main()
