import unittest

from screen_label_layout import _segment_intersects_rect, choose_screen_label_rect


class ScreenLabelLayoutTests(unittest.TestCase):
    def test_label_avoids_anchor_vertex_and_connected_link(self):
        rect = choose_screen_label_rect(
            (100.0, 100.0),
            (12.0, 16.0),
            (0.0, 0.0, 300.0, 300.0),
            point_obstacles=((92.0, 92.0, 108.0, 108.0),),
            segments=(((100.0, 100.0), (220.0, 20.0)),),
        )
        self.assertIsNotNone(rect)
        left, top, right, bottom = rect
        self.assertFalse(left < 108.0 and right > 92.0 and top < 108.0 and bottom > 92.0)
        self.assertFalse(
            _segment_intersects_rect(((100.0, 100.0), (220.0, 20.0)), rect, 3.0)
        )

    def test_label_avoids_other_vertices_too(self):
        rect = choose_screen_label_rect(
            (100.0, 100.0),
            (18.0, 14.0),
            (0.0, 0.0, 300.0, 300.0),
            point_obstacles=(
                (93.0, 93.0, 107.0, 107.0),
                (108.0, 72.0, 128.0, 92.0),
                (108.0, 108.0, 128.0, 128.0),
            ),
        )
        self.assertIsNotNone(rect)
        for obstacle in (
            (93.0, 93.0, 107.0, 107.0),
            (108.0, 72.0, 128.0, 92.0),
            (108.0, 108.0, 128.0, 128.0),
        ):
            self.assertFalse(
                rect[0] < obstacle[2]
                and rect[2] > obstacle[0]
                and rect[1] < obstacle[3]
                and rect[3] > obstacle[1]
            )

    def test_labels_do_not_overlap_each_other(self):
        first = choose_screen_label_rect(
            (100.0, 100.0),
            (20.0, 16.0),
            (0.0, 0.0, 300.0, 300.0),
            point_obstacles=((94.0, 94.0, 106.0, 106.0),),
        )
        self.assertIsNotNone(first)
        second = choose_screen_label_rect(
            (104.0, 100.0),
            (20.0, 16.0),
            (0.0, 0.0, 300.0, 300.0),
            point_obstacles=((94.0, 94.0, 110.0, 106.0),),
            occupied_labels=(first,),
        )
        self.assertIsNotNone(second)
        self.assertFalse(
            first[0] < second[2]
            and first[2] > second[0]
            and first[1] < second[3]
            and first[3] > second[1]
        )

    def test_grid_is_not_an_obstacle(self):
        # With no actual editor geometry, the preferred nearby position is
        # accepted regardless of where a background grid line would be.
        rect = choose_screen_label_rect(
            (50.0, 50.0),
            (10.0, 12.0),
            (0.0, 0.0, 200.0, 200.0),
        )
        self.assertEqual(rect, (58.0, 30.0, 68.0, 42.0))

    def test_dense_local_geometry_uses_farther_free_candidate(self):
        blockers = tuple(
            ((40.0, y), (160.0, y)) for y in (60.0, 75.0, 90.0, 105.0, 120.0, 135.0)
        )
        rect = choose_screen_label_rect(
            (100.0, 100.0),
            (18.0, 14.0),
            (0.0, 0.0, 220.0, 220.0),
            point_obstacles=((93.0, 93.0, 107.0, 107.0),),
            segments=blockers,
        )
        self.assertIsNotNone(rect)


if __name__ == '__main__':
    unittest.main()
