import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QSizePolicy

from assembly_viewer import AssetViewport
from assembly_window import AssemblyWindow
from geometry_editor import GeometryEditSession
from model_space_gizmo import DIRECTIONS_26, ModelSpaceGizmo
from sklt_parser import SkltModel


def _editable_viewport():
    model = SkltModel(
        source_name="GIZMO.SKLT",
        points=[
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        ],
        polygons=[[0, 1, 2]],
        parsed_polygon_count=1,
    )
    obj = SimpleNamespace(
        skeleton=model,
        base_object=SimpleNamespace(ades=[]),
    )
    viewport = AssetViewport()
    viewport.resize(400, 300)
    viewport._scale = 1.0
    viewport._edit_session = GeometryEditSession(
        obj,
        [[1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0],
         [0.0, 0.0, 1.0]],
        (0.0, 0.0, 0.0),
    )
    viewport._edit_owner = "root"
    viewport.edit_session.selection = {0, 1}
    viewport.set_auto_align_enabled(False)
    return viewport, model


class ModelSpaceGizmoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_26_directions_labels_hit_tests_and_ua_axis_help(self):
        gizmo = ModelSpaceGizmo()
        gizmo.resize(220, 220)
        self.assertEqual(len(DIRECTIONS_26), 26)
        self.assertEqual(len(set(DIRECTIONS_26)), 26)
        self.assertNotIn((0, 0, 0), DIRECTIONS_26)
        self.assertEqual(
            {component for direction in DIRECTIONS_26
             for component in direction},
            {-1, 0, 1})
        for direction in DIRECTIONS_26:
            handle = gizmo.handle_for_direction(direction)
            self.assertIsNotNone(handle)
            self.assertEqual(gizmo.hit_test(handle.position), direction)
        for direction, label in (
                ((1, 0, 0), "+X"), ((-1, 0, 0), "-X"),
                ((0, 1, 0), "+Y"), ((0, -1, 0), "-Y"),
                ((0, 0, 1), "+Z"), ((0, 0, -1), "-Z")):
            self.assertIn(label, gizmo.direction_text(direction))
        self.assertIn(
            "-Y = Up", gizmo.direction_text((0, -1, 0)))
        self.assertIn(
            "+Y = Down", gizmo.direction_text((0, 1, 0)))

    def test_compact_layout_scales_without_clipping_handles(self):
        gizmo = ModelSpaceGizmo()
        self.assertEqual(gizmo.minimumWidth(), 240)
        self.assertEqual(gizmo.minimumHeight(), 240)
        self.assertEqual(gizmo.sizeHint().width(), 300)
        self.assertEqual(gizmo.sizeHint().height(), 300)
        self.assertEqual(
            gizmo.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding)
        self.assertEqual(
            gizmo.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Preferred)
        gizmo.resize(240, 240)
        for mode in ("move", "rotate", "scale"):
            gizmo.set_mode(mode)
            for handle in gizmo.handles:
                margin = handle.radius + 4.0
                self.assertGreaterEqual(handle.position.x(), margin)
                self.assertGreaterEqual(handle.position.y(), margin)
                self.assertLessEqual(
                    handle.position.x(), gizmo.width() - margin)
                self.assertLessEqual(
                    handle.position.y(), gizmo.height() - margin)
        gizmo.set_mode("move")
        gizmo.resize(360, 360)
        center_small = QPointF(180, 180)
        small = gizmo.handle_for_direction((1, 0, 0)).position
        small_radius = (
            (small.x() - center_small.x()) ** 2
            + (small.y() - center_small.y()) ** 2) ** 0.5
        gizmo.resize(600, 600)
        center_large = QPointF(300, 300)
        large = gizmo.handle_for_direction((1, 0, 0)).position
        large_radius = (
            (large.x() - center_large.x()) ** 2
            + (large.y() - center_large.y()) ** 2) ** 0.5
        self.assertGreater(large_radius, small_radius * 1.5)

    def test_render_camera_orientation_hover_and_click(self):
        gizmo = ModelSpaceGizmo()
        gizmo.resize(220, 220)
        before = gizmo.handle_for_direction((1, 0, 0)).position
        gizmo.set_camera_orientation(55.0, -20.0)
        after = gizmo.handle_for_direction((1, 0, 0)).position
        self.assertNotEqual(before, after)

        image = QImage(220, 220, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        gizmo.render(image)
        self.assertGreater(
            sum(1 for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0),
            100)

        clicked = []
        finished = []
        gizmo.directionTriggered.connect(
            lambda direction: clicked.append(direction))
        gizmo.nudgeFinished.connect(lambda: finished.append(True))
        handle = gizmo.handle_for_direction((0, 1, 1))
        gizmo.show()
        QApplication.processEvents()
        QTest.mouseMove(gizmo, handle.position.toPoint())
        self.assertEqual(gizmo._hover, (0, 1, 1))
        QTest.mousePress(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        self.assertTrue(gizmo._repeat_timer.isActive())
        self.assertEqual(gizmo._repeat_timer.interval(), 350)
        self.assertEqual(clicked, [])
        gizmo._repeat()
        self.assertEqual(clicked, [(0, 1, 1)])
        self.assertEqual(gizmo._repeat_timer.interval(), 70)
        QTest.mouseRelease(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        self.assertEqual(clicked, [(0, 1, 1)])
        self.assertEqual(finished, [True])

        clicked.clear()
        finished.clear()
        QTest.mousePress(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        QTest.mouseRelease(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        self.assertEqual(clicked, [(0, 1, 1)])
        self.assertEqual(finished, [True])

    def test_implicit_scale_rotate_select_all_excludes_protected_fx(self):
        viewport, _model = _editable_viewport()
        viewport.edit_session.selection.clear()
        viewport.set_edit_read_only_vertices({1})
        self.assertTrue(viewport.begin_scale_preview())
        self.assertEqual(viewport.edit_session.selection, {0, 2})
        viewport.finish_scale_preview(False)

        viewport.edit_session.selection.clear()
        self.assertTrue(viewport.begin_rotate_preview())
        self.assertEqual(viewport.edit_session.selection, {0, 2})
        viewport.finish_rotate_preview(False)

    def test_outward_drag_keeps_discrete_click_hold_behavior(self):
        gizmo = ModelSpaceGizmo()
        gizmo.resize(320, 320)
        gizmo.show()
        QApplication.processEvents()
        handle = gizmo.handle_for_direction((1, 0, 0))
        center = QPointF(gizmo.width() * 0.5, gizmo.height() * 0.5)
        radial = handle.position - center
        length = max(1.0, (radial.x() ** 2 + radial.y() ** 2) ** 0.5)
        target = handle.position + QPointF(
            radial.x() / length * 500.0,
            radial.y() / length * 500.0)
        clicks = []
        gizmo.directionTriggered.connect(clicks.append)

        QTest.mousePress(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        QTest.mouseMove(gizmo, target.toPoint(), delay=1)
        self.assertEqual(gizmo._interaction_state, "pressed")
        self.assertEqual(clicks, [])
        QTest.mouseRelease(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, target.toPoint())
        self.assertEqual(clicks, [(1, 0, 0)])

    def test_large_sideways_motion_keeps_discrete_click(self):
        gizmo = ModelSpaceGizmo()
        gizmo.resize(320, 320)
        gizmo.show()
        QApplication.processEvents()
        handle = gizmo.handle_for_direction((1, 0, 0))
        center = QPointF(gizmo.width() * 0.5, gizmo.height() * 0.5)
        radial = handle.position - center
        length = max(1.0, (radial.x() ** 2 + radial.y() ** 2) ** 0.5)
        sideways = QPointF(-radial.y() / length, radial.x() / length)
        target = handle.position + sideways * 30.0
        clicks = []
        gizmo.directionTriggered.connect(clicks.append)
        QTest.mousePress(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, handle.position.toPoint())
        QTest.mouseMove(gizmo, target.toPoint(), delay=1)
        self.assertEqual(gizmo._interaction_state, "pressed")
        self.assertTrue(gizmo._repeat_timer.isActive())
        QTest.mouseRelease(
            gizmo, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, target.toPoint())
        self.assertEqual(clicks, [(1, 0, 0)])

    def test_model_vectors_keep_full_step_and_hold_is_one_command(self):
        viewport, model = _editable_viewport()
        commands = []
        viewport.geometryCommandCommitted.connect(
            lambda *args: commands.append(args))
        self.assertTrue(viewport.begin_model_nudge())
        self.assertTrue(
            viewport.nudge_edit_selection_model(1.0, 0.0, 0.0))
        self.assertTrue(
            viewport.nudge_edit_selection_model(0.0, 2.0, -2.0))
        self.assertTrue(viewport.end_model_nudge())
        self.assertEqual(viewport._precision_guides, ())
        self.assertEqual(viewport._precision_bbox_points, ())
        self.assertEqual(model.points[0], (1.0, 2.0, -2.0))
        self.assertEqual(model.points[1], (2.0, 2.0, -2.0))
        self.assertEqual(model.points[2], (0.0, 1.0, 0.0))
        self.assertEqual(len(commands), 1)

        viewport.edit_session.select_none()
        self.assertFalse(viewport.begin_model_nudge())

    def test_window_gizmo_nudge_moves_only_paste_preview_ghost(self):
        from tests.test_geometry_paste_preview import _fixture

        viewport, model, clipboard = _fixture()
        viewport.set_auto_align_enabled(False)
        start = viewport._project(
            viewport._camera_vertex(clipboard.pivot)).toPoint()
        self.assertTrue(viewport.begin_paste_preview(clipboard, start))
        before_points = list(model.points)
        before_delta = viewport.paste_preview_delta
        window = AssemblyWindow()
        original_viewport = window.viewport
        window.viewport = viewport
        try:
            window.edit_toggle_action.blockSignals(True)
            window.edit_toggle_action.setChecked(True)
            window.edit_toggle_action.blockSignals(False)
            window.gizmo_intensity_spin.setValue(0.20)
            window._apply_gizmo_nudge((0, 1, -1))
            after_delta = viewport.paste_preview_delta
            expected = window._gizmo_intensity()
            self.assertAlmostEqual(
                after_delta[0], before_delta[0])
            self.assertAlmostEqual(
                after_delta[1] - before_delta[1], expected)
            self.assertAlmostEqual(
                after_delta[2] - before_delta[2], -expected)
            self.assertEqual(model.points, before_points)
            self.assertEqual(window._edit_undo_stack, [])
            window._finish_gizmo_nudge()
            self.assertEqual(model.points, before_points)
        finally:
            window.viewport = original_viewport
            window.close()

    def test_window_intensity_default_and_disabled_without_selection(self):
        window = AssemblyWindow()
        self.assertEqual(window.gizmo_intensity_slider.minimum(), 0)
        self.assertEqual(window.gizmo_intensity_slider.maximum(), 1000)
        self.assertEqual(
            window.gizmo_intensity_slider.value(),
            window._gizmo_intensity_to_slider(0.50))
        self.assertAlmostEqual(window._gizmo_intensity(), 0.50)
        self.assertEqual(
            window.gizmo_intensity_spin.text(), "0.50 model units")
        window.gizmo_intensity_slider.setValue(0)
        self.assertAlmostEqual(window._gizmo_intensity(), 0.001)
        window.gizmo_intensity_slider.setValue(1000)
        self.assertAlmostEqual(window._gizmo_intensity(), 1000.0)
        self.assertFalse(window.model_gizmo.isEnabled())
        self.assertTrue(window.auto_align_check.isChecked())
        self.assertEqual(
            window.model_gizmo.shortcutContext()
            if hasattr(window.model_gizmo, "shortcutContext") else None,
            None)
        window.close()


if __name__ == "__main__":
    unittest.main()
