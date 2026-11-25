import unittest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from studio.ui.uv_editor import (
    build_unified_uv_proxy,
    evaluate_unified_uv_proxy,
    spread_unified_uvs,
    uv_loops_are_seamless_patch,
    UVEditorWidget,
    UVLoop,
)


class UVEditorToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = UVEditorWidget()
        self.widget.resize(400, 400)
        self.widget.set_loops(None, [
            UVLoop("a", 1, [(80, 80), (120, 80), (120, 120), (80, 120)]),
            UVLoop("b", 2, [(160, 160), (200, 160), (200, 200), (160, 200)]),
        ])

    def tearDown(self):
        self.widget.close()

    def test_alternating_palette_and_opposite_selected_handles(self):
        self.assertEqual(self.widget.loop_color("a").name(), "#ffdc00")
        self.assertEqual(self.widget.loop_color("b").name(), "#00dcff")
        self.widget.select_handle("a", 0)
        self.widget.select_handle("b", 0, additive=True)
        self.assertEqual(
            self.widget._opposite_loop_color(self.widget.loop_color("a")).name(),
            "#00dcff")
        self.assertEqual(
            self.widget._opposite_loop_color(self.widget.loop_color("b")).name(),
            "#ffdc00")

    def test_rotate_scale_and_flip_are_single_transactions(self):
        self.widget.select_handles({("a", 0), ("a", 1)})
        events = []
        self.widget.loopsChanged.connect(lambda value: events.append(value))
        self.widget.editFinished.connect(lambda: events.append("finished"))
        self.assertTrue(self.widget.rotate_selected(90))
        self.assertEqual(self.widget.loop_uvs()["a"][:2], [(100, 60), (100, 100)])
        self.assertEqual(events[-1], "finished")
        self.assertTrue(self.widget.scale_selected(200))
        self.assertFalse(self.widget.flip_selected(horizontal=True))
        self.assertEqual(len(events), 4)

    def test_invalid_transform_is_atomic_and_rejects_collapse(self):
        self.widget.select_all()
        before = self.widget.loop_uvs()
        self.assertFalse(self.widget.scale_selected(-100))
        self.assertEqual(self.widget.loop_uvs(), before)
        self.widget.select_handles({("a", 0), ("a", 1)})
        before = self.widget.loop_uvs()
        self.assertFalse(self.widget.rotate_selected(float("nan")))
        self.assertEqual(self.widget.loop_uvs(), before)

    def test_fit_selection_and_cursor_anchored_zoom(self):
        self.widget.select_handle("a", 0)
        self.widget.fit_selection()
        self.assertGreaterEqual(self.widget._zoom, 1.0)
        self.widget.reset_view()
        cursor = QPointF(100, 140)
        before = self.widget._screen_to_uv(cursor)
        event = QWheelEvent(
            cursor, cursor, QPoint(0, 120), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False)
        self.widget.wheelEvent(event)
        after = self.widget._screen_to_uv(cursor)
        self.assertEqual(before, after)

    def test_replace_loop_uvs_is_one_finished_edit(self):
        finished = []
        self.widget.editFinished.connect(lambda: finished.append(True))
        target = [(1, 2), (3, 4), (5, 6), (7, 8)]
        self.widget.replace_loop_uvs({"a": target, "b": target[:3]})
        self.assertEqual(self.widget.loop_uvs()["a"], target)
        self.assertEqual(self.widget.loop_uvs()["b"][0], (160, 160))
        self.assertEqual(finished, [True])

    def test_unify_uv_proxy_simplifies_contour_and_updates_seamlessly(self):
        # Two adjacent triangles forming one outer triangle (A, B, C) with midpoint M on A-B
        loops = {
            "t1": [(10, 10), (50, 10), (50, 90)],
            "t2": [(50, 10), (90, 10), (50, 90)],
        }
        proxy_uvs, weights = build_unified_uv_proxy(loops)
        self.assertEqual(len(proxy_uvs), 3)
        self.assertEqual(evaluate_unified_uv_proxy(proxy_uvs, weights), loops)
        # Move the top-left corner; shared midpoint M follows along the edge
        moved_proxy = [(u + 20, v) if (u, v) == (10, 10) else (u, v) for u, v in proxy_uvs]
        updated = evaluate_unified_uv_proxy(moved_proxy, weights)
        self.assertEqual(updated["t1"][1], updated["t2"][0])
        self.assertEqual(updated["t1"][1], (60, 10))

        # 2x2 grid of 4 quads collapses into a single 4-corner quad
        grid = {
            "q00": [(20, 20), (60, 20), (60, 60), (20, 60)],
            "q10": [(60, 20), (100, 20), (100, 60), (60, 60)],
            "q01": [(20, 60), (60, 60), (60, 100), (20, 100)],
            "q11": [(60, 60), (100, 60), (100, 100), (60, 100)],
        }
        grid_proxy, grid_weights = build_unified_uv_proxy(grid)
        self.assertEqual(len(grid_proxy), 4)
        self.assertEqual(evaluate_unified_uv_proxy(grid_proxy, grid_weights), grid)
        self.assertTrue(uv_loops_are_seamless_patch(grid))

    def test_spread_unified_uvs_converts_overlapping_tiles_into_continuous_map(self):
        # 4 quads arranged in a 2x2 roof in 3D, all overlapping on the same UV rect!
        pts = {
            "q00": [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 1.0), (0.0, 0.0, 1.0)],
            "q10": [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 0.0, 1.0), (1.0, 0.0, 1.0)],
            "q01": [(0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 0.0, 2.0), (0.0, 0.0, 2.0)],
            "q11": [(1.0, 0.0, 1.0), (2.0, 0.0, 1.0), (2.0, 0.0, 2.0), (1.0, 0.0, 2.0)],
        }
        overlapping_uvs = {k: [(80, 60), (140, 60), (140, 160), (80, 160)] for k in pts}
        self.assertFalse(uv_loops_are_seamless_patch(overlapping_uvs))
        spread = spread_unified_uvs(
            pts, overlapping_uvs, camera_project=lambda p: (p[0], p[2]))
        # Now the 4 quads tile the exact same rect (80,60)-(140,160) with zero overlap!
        self.assertTrue(uv_loops_are_seamless_patch(spread))
        self.assertEqual(spread["q00"][2], (110, 110))
        self.assertEqual(spread["q10"][3], (110, 110))
        self.assertEqual(spread["q01"][1], (110, 110))
        self.assertEqual(spread["q11"][0], (110, 110))
        # Respects 90-degree preview rotation seamlessly
        self.widget.rotate_view_90()
        spread_rot = spread_unified_uvs(
            pts, overlapping_uvs,
            camera_project=lambda p: (p[0], p[2]),
            to_view=self.widget.uv_to_view_space,
            from_view=self.widget.view_space_to_uv)
        self.assertTrue(uv_loops_are_seamless_patch(spread_rot))
        self.assertEqual(spread_rot["q00"][2], spread_rot["q11"][0])

    def test_rotate_view_90_is_visual_only_and_roundtrips_hit_coords(self):
        before = self.widget.loop_uvs()
        for expected in (90, 180, 270, 0):
            self.assertEqual(self.widget.rotate_view_90(), expected)
            self.assertEqual(self.widget.view_rotation_degrees(), expected)
            self.assertEqual(self.widget.loop_uvs(), before)
            uv = (80, 120)
            screen_pt = self.widget._uv_to_screen(uv)
            self.assertEqual(self.widget._screen_to_uv(screen_pt), uv)


if __name__ == "__main__":
    unittest.main()
