import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tests.test_selection_runtime_v3 import _prepare_window


def _bounds_center(model):
    used = {index for polygon in model.polygons for index in polygon}
    return tuple(
        (min(model.points[i][axis] for i in used)
         + max(model.points[i][axis] for i in used)) / 2.0
        for axis in range(3))


class CenterAtOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_model_center_becomes_origin_without_moving_the_view(self):
        window, _obj, model = _prepare_window()
        try:
            # Shape sits away from the origin, as a weapon cut out of a unit.
            model.points[:] = [(x + 10.0, y - 4.0, z + 2.5)
                               for x, y, z in model.points]
            original = list(model.points)
            viewport = window.viewport
            viewport.resize(800, 600)
            screen_before = viewport._edit_screen_points()
            old_origin = viewport.edit_session.position
            marker_before = viewport._camera_vertex(old_origin)
            center_before = viewport._camera_vertex(_bounds_center(model))

            def assert_view(marker):
                self.assertEqual(viewport._camera_vertex(old_origin), marker)
                for old, new in zip(screen_before, viewport._edit_screen_points()):
                    self.assertAlmostEqual(old.x(), new.x())
                    self.assertAlmostEqual(old.y(), new.y())

            window._sync_edit_action_states()
            self.assertTrue(window.edit_center_action.isEnabled())
            self.assertTrue(window.center_origin_button.isEnabled())

            window.center_origin_button.click()

            for value in _bounds_center(model):
                self.assertAlmostEqual(value, 0.0)
            # Same offset on every vertex: the shape is untouched.
            offsets = {
                tuple(round(a - b, 6) for a, b in zip(new, old))
                for new, old in zip(model.points, original)}
            self.assertEqual(len(offsets), 1)
            self.assertEqual(len(window._edit_undo_stack), 1)
            assert_view(center_before)

            window._undo_edit()
            self.assertEqual(model.points, original)
            assert_view(marker_before)
            window._redo_edit()
            assert_view(center_before)
        finally:
            window.close()

    def test_vertices_not_used_by_any_polygon_do_not_move_the_center(self):
        window, _obj, model = _prepare_window()
        try:
            model.points.append((500.0, 500.0, 500.0))
            window._sync_edit_action_states()
            self.assertTrue(window.viewport.center_edit_model_at_origin())
            for value in _bounds_center(model):
                self.assertAlmostEqual(value, 0.0)
        finally:
            window.close()

    def test_already_centered_model_is_left_alone(self):
        window, _obj, model = _prepare_window()
        try:
            window.viewport.center_edit_model_at_origin()
            centered = list(model.points)
            undo_depth = len(window._edit_undo_stack)
            self.assertFalse(window.viewport.center_edit_model_at_origin())
            self.assertEqual(model.points, centered)
            self.assertEqual(len(window._edit_undo_stack), undo_depth)
        finally:
            window.close()

    def test_selection_becomes_the_origin_and_the_model_moves_rigidly(self):
        window, _obj, model = _prepare_window()
        try:
            model.points[:] = [(x + 10.0, y - 4.0, z + 2.5)
                               for x, y, z in model.points]
            original = list(model.points)
            session = window.viewport.edit_session
            selected = [0, 1]
            window.viewport.select_edit_vertices(selected)
            window._sync_edit_action_states()
            self.assertTrue(window.center_selection_button.isEnabled())

            window.center_selection_button.click()

            for axis in range(3):
                values = [model.points[i][axis] for i in selected]
                self.assertAlmostEqual((min(values) + max(values)) / 2.0, 0.0)
            # Same offset on every vertex, selected or not: the shape is untouched.
            offsets = {
                tuple(round(a - b, 6) for a, b in zip(new, old))
                for new, old in zip(model.points, original)}
            self.assertEqual(len(offsets), 1)
            self.assertEqual(len(window._edit_undo_stack), 1)
            window._undo_edit()
            self.assertEqual(model.points, original)
        finally:
            window.close()

    def test_selection_origin_moves_the_marker_without_moving_the_view(self):
        window, _obj, model = _prepare_window()
        try:
            viewport = window.viewport
            viewport.resize(800, 600)
            selected = next(i for i, p in enumerate(model.points) if any(p))
            viewport.select_edit_vertices([selected])
            original = list(model.points)
            before = viewport._edit_screen_points()
            origin = viewport.edit_session.position
            marker_before = viewport._project(viewport._camera_vertex(origin))
            self.assertNotEqual(marker_before, before[selected])

            def assert_view(marker_position):
                for old, new in zip(before, viewport._edit_screen_points()):
                    self.assertAlmostEqual(old.x(), new.x())
                    self.assertAlmostEqual(old.y(), new.y())
                marker = viewport._project(viewport._camera_vertex(origin))
                self.assertAlmostEqual(marker.x(), marker_position.x())
                self.assertAlmostEqual(marker.y(), marker_position.y())

            window.edit_center_selection_action.trigger()
            self.assertEqual(model.points[selected], (0.0, 0.0, 0.0))
            assert_view(before[selected])
            window._undo_edit()
            self.assertEqual(model.points, original)
            assert_view(marker_before)
            window._redo_edit()
            self.assertEqual(model.points[selected], (0.0, 0.0, 0.0))
            assert_view(before[selected])
        finally:
            window.close()

    def test_origin_buttons_share_a_group_below_transform(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QGroupBox
        window, _obj, _model = _prepare_window()
        try:
            window.show()
            self.app.processEvents()
            origin = window.center_origin_button.parentWidget()
            self.assertIsInstance(origin, QGroupBox)
            self.assertEqual(origin.title(), "Origin")
            self.assertIs(window.center_selection_button.parentWidget(), origin)
            top = lambda widget: widget.mapTo(window, QPoint(0, 0)).y()
            self.assertEqual(top(window.center_origin_button), top(window.center_selection_button))
            self.assertLess(top(window.transform_mode_buttons["move"]), top(origin))
        finally:
            window.close()

    def test_emitters_box_sits_above_the_anm_box(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QGroupBox
        window, _obj, _model = _prepare_window()
        try:
            window.show()
            self.app.processEvents()
            boxes = {box.title(): box for box in window.findChildren(QGroupBox)}
            top = lambda box: box.mapTo(window, QPoint(0, 0)).y()
            self.assertLess(top(boxes["Transform"]), top(boxes["Origin"]))
            self.assertLess(top(boxes["Origin"]), top(boxes["Emitters"]))
            self.assertLess(top(boxes["Emitters"]), top(boxes["ANM"]))
        finally:
            window.close()

    def test_selection_button_needs_a_selection(self):
        window, _obj, _model = _prepare_window()
        try:
            window.viewport.select_edit_vertices([])
            window._sync_edit_action_states()
            self.assertFalse(window.center_selection_button.isEnabled())
            self.assertTrue(window.center_origin_button.isEnabled())
        finally:
            window.close()

    def test_action_is_disabled_outside_edit_mode(self):
        window, _obj, _model = _prepare_window()
        try:
            window.viewport.exit_edit_mode()
            window._sync_edit_action_states()
            self.assertFalse(window.edit_center_action.isEnabled())
            self.assertFalse(window.center_origin_button.isEnabled())
            self.assertFalse(window.center_selection_button.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
