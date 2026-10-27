"""Mirror Symmetry of the wireframe editor: left <-> right around the X=0 axis."""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from studio.formats.sklt import create_minimal_sklt_model
from wireframe_editor.window import (
    WireframeEditorWindow,
    _apply_mirror_positions,
    _choose_mirror_axis,
    _match_mirror_vertices,
)


class MirrorHelperTests(unittest.TestCase):
    def test_pairs_are_left_right_not_front_back(self):
        # 0/1 mirror each other across X=0; 2 sits on the axis; 3/4 are a front/back
        # pair on the same side, which must NOT be paired.
        points = [(-100.0, 50.0), (100.0, 50.0), (0.0, 0.0), (80.0, 50.0), (80.0, -50.0)]
        pairs, centre = _match_mirror_vertices(points, 1.0)
        self.assertEqual(pairs.get(0), 1)
        self.assertEqual(pairs.get(1), 0)
        self.assertNotIn(4, pairs)
        self.assertEqual(centre, {2})

    def test_single_move_drags_the_opposite_side(self):
        points = [(-100.0, 50.0), (100.0, 50.0)]
        pairs, centre = _match_mirror_vertices(points, 1.0)
        moved, guides = _apply_mirror_positions(
            {1: (130.0, 70.0)}, pairs, centre, dict(enumerate(points))
        )
        self.assertEqual(moved[1], (130.0, 70.0))
        self.assertEqual(moved[0], (-130.0, 70.0))
        self.assertTrue(guides[0][2])

    def test_axis_vertex_stays_on_axis(self):
        points = [(0.0, 10.0)]
        pairs, centre = _match_mirror_vertices(points, 1.0)
        moved, _guides = _apply_mirror_positions(
            {0: (25.0, 40.0)}, pairs, centre, dict(enumerate(points))
        )
        self.assertEqual(moved[0], (0.0, 40.0))

    def test_both_sides_selected_stay_exactly_mirrored(self):
        points = [(-100.0, 50.0), (100.0, 50.0)]
        pairs, centre = _match_mirror_vertices(points, 1.0)
        moved, _guides = _apply_mirror_positions(
            {0: (-90.0, 60.0), 1: (110.0, 80.0)}, pairs, centre, dict(enumerate(points))
        )
        self.assertEqual(moved[0], (-100.0, 70.0))
        self.assertEqual(moved[1], (100.0, 70.0))

    def test_unpaired_vertex_moves_alone(self):
        points = [(-100.0, 50.0), (300.0, 50.0)]
        pairs, centre = _match_mirror_vertices(points, 1.0)
        moved, guides = _apply_mirror_positions(
            {1: (310.0, 60.0)}, pairs, centre, dict(enumerate(points))
        )
        self.assertEqual(set(moved), {1})
        self.assertFalse(guides[0][2])

    def test_sideways_shape_uses_the_top_bottom_axis(self):
        # Missile lying sideways: top/bottom pairs around Z=0, nothing left/right.
        points = [(-300.0, 80.0), (-300.0, -80.0), (400.0, 40.0), (400.0, -40.0), (500.0, 0.0)]
        axis, pairs, _centre = _choose_mirror_axis(points, 1.0)
        self.assertEqual(axis, 1)
        self.assertEqual(pairs.get(0), 1)
        self.assertEqual(pairs.get(2), 3)

    def test_upright_shape_keeps_the_left_right_axis(self):
        points = [(-100.0, 50.0), (100.0, 50.0), (-60.0, -50.0), (60.0, -50.0)]
        axis, _pairs, _centre = _choose_mirror_axis(points, 1.0)
        self.assertEqual(axis, 0)


class MirrorDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = WireframeEditorWindow()
        self.editor = self.window.outline_editor
        self.canvas = self.editor.canvas
        model = create_minimal_sklt_model()
        model.points = [
            (-100.0, 0.0, 50.0), (100.0, 0.0, 50.0),
            (-100.0, 0.0, -50.0), (100.0, 0.0, -50.0),
            (0.0, 0.0, 0.0),
        ]
        model.polygons = [[0, 1], [2, 3], [0, 2], [1, 3], [4, 4]]
        self.window._show_model(model, Path("mirror.SKL"))
        self.window.show()
        self.canvas.setFocus()
        self.app.processEvents()
        self.addCleanup(self.window.close)
        self.addCleanup(self.editor.mark_clean)

    def screen(self, point):
        return self.canvas._to_screen(point, self.canvas._make_transform())

    def mouse(self, kind, point, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        position = self.screen(point)
        global_pos = QPointF(self.canvas.mapToGlobal(position.toPoint()))
        event = QMouseEvent(kind, position, position, global_pos, button, buttons,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.canvas, event)
        self.app.processEvents()

    def drag(self, start, end):
        left = Qt.MouseButton.LeftButton
        self.mouse(QEvent.Type.MouseButtonPress, start, left, left)
        self.mouse(QEvent.Type.MouseMove, end, buttons=left)
        self.mouse(QEvent.Type.MouseButtonRelease, end, left)

    def test_dragging_the_right_side_moves_the_left_side(self):
        self.editor.auto_align_check.setChecked(False)
        self.window.mirror_symmetry_check.setChecked(True)
        self.drag((100.0, 50.0), (140.0, 70.0))
        points = self.editor.projected_points
        self.assertAlmostEqual(points[1][0], 140.0, delta=1.0)
        self.assertAlmostEqual(points[1][2], 70.0, delta=1.0)
        self.assertAlmostEqual(points[0][0], -points[1][0], places=4)
        self.assertAlmostEqual(points[0][2], points[1][2], places=4)

    def test_with_auto_align_the_left_side_still_follows(self):
        self.editor.auto_align_check.setChecked(True)
        self.window.mirror_symmetry_check.setChecked(True)
        self.drag((100.0, 50.0), (140.0, 70.0))
        points = self.editor.projected_points
        self.assertAlmostEqual(points[0][0], -points[1][0], places=4)
        self.assertAlmostEqual(points[0][2], points[1][2], places=4)
        self.assertGreater(points[1][0], 100.0)

    def test_sideways_shape_follows_across_the_horizontal_axis(self):
        model = create_minimal_sklt_model()
        model.points = [
            (-300.0, 0.0, 80.0), (-300.0, 0.0, -80.0),
            (400.0, 0.0, 40.0), (400.0, 0.0, -40.0),
            (500.0, 0.0, 0.0),
        ]
        model.polygons = [[0, 2], [1, 3], [2, 4], [3, 4]]
        self.window._show_model(model, Path("sideways.SKL"))
        self.app.processEvents()
        self.editor.auto_align_check.setChecked(False)
        self.window.mirror_symmetry_check.setChecked(True)
        self.drag((400.0, 40.0), (430.0, 70.0))
        points = self.editor.projected_points
        self.assertAlmostEqual(points[2][0], 430.0, delta=1.0)
        self.assertAlmostEqual(points[2][2], 70.0, delta=1.0)
        self.assertAlmostEqual(points[3][0], points[2][0], places=4)
        self.assertAlmostEqual(points[3][2], -points[2][2], places=4)

    def test_mirror_off_moves_only_the_dragged_vertex(self):
        self.editor.auto_align_check.setChecked(False)
        self.window.mirror_symmetry_check.setChecked(False)
        self.drag((100.0, 50.0), (140.0, 70.0))
        points = self.editor.projected_points
        self.assertAlmostEqual(points[0][0], -100.0, places=4)
        self.assertAlmostEqual(points[0][2], 50.0, places=4)


if __name__ == "__main__":
    unittest.main()
