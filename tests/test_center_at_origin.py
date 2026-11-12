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

    def test_whole_model_moves_rigidly_and_undo_restores_it(self):
        window, _obj, model = _prepare_window()
        try:
            # Shape sits away from the origin, as a weapon cut out of a unit.
            model.points[:] = [(x + 10.0, y - 4.0, z + 2.5)
                               for x, y, z in model.points]
            original = list(model.points)
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

            window._undo_edit()
            self.assertEqual(model.points, original)
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

    def test_selection_goes_to_the_origin_and_the_rest_stays(self):
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
            for index in range(len(original)):
                if index not in selected:
                    self.assertEqual(model.points[index], original[index])
            self.assertEqual(len(window._edit_undo_stack), 1)
            window._undo_edit()
            self.assertEqual(model.points, original)
        finally:
            window.close()

    def test_buttons_sit_under_mirror_select_in_this_order(self):
        from PySide6.QtCore import QPoint
        window, _obj, _model = _prepare_window()
        try:
            window.show()
            self.app.processEvents()
            top = lambda widget: widget.mapTo(window.transform_box, QPoint(0, 0)).y()
            self.assertLess(top(window.mirror_select_check), top(window.center_origin_button))
            self.assertEqual(top(window.center_origin_button), top(window.center_selection_button))
            self.assertLess(top(window.center_origin_button), top(window.transform_mode_buttons["move"]))
            self.assertLess(top(window.model_gizmo), top(window.mirror_select_check))
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
            self.assertLess(top(boxes["Transform"]), top(boxes["Emitters"]))
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
