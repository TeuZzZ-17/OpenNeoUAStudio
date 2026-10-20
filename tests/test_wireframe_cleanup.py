"""Raw wireframe lines are tidied into the clean look of the original outlines."""

import numpy as np

from entity_designer.wireframe_cleanup import clean_segments, snap_distance


def test_dashes_on_one_edge_become_one_line():
    raw = [((0, 0), (40, 0)), ((46, 0.4), (90, -0.3)), ((97, 0), (200, 0.2))]
    result = clean_segments(raw, 300)
    assert len(result) == 1
    (a, b), = result
    assert abs(a[0]) < 1 and abs(b[0]-200) < 1


def test_lines_that_almost_meet_share_the_corner():
    raw = [((0, 0), (100, 0)), ((102.5, 1.5), (102.5, 100))]   # gap and overshoot of a pixel or two
    result = clean_segments(raw, 600)
    assert len(result) == 2
    ends = [tuple(p) for segment in result for p in segment]
    corner = [p for p in ends if np.linalg.norm(np.array(p)-(101.25, 0.75)) < 3]
    assert len(corner) == 2 and np.allclose(corner[0], corner[1])


def test_an_end_point_next_to_another_line_is_pulled_onto_it():
    raw = [((0, 0), (200, 0)), ((100, 60), (101, 2))]
    result = clean_segments(raw, 300)
    vertical = next(s for s in result if abs(s[0][1]-s[1][1]) > 10)
    assert min(abs(vertical[0][1]), abs(vertical[1][1])) < 0.5


def test_short_stubs_and_specks_are_removed_but_real_lines_stay():
    raw = [((0, 0), (200, 0)), ((200, 0), (200, 150)),     # a corner
           ((100, 0), (100, 6)),                           # a stub on a line
           ((50, 90), (58, 95))]                           # a lone speck
    result = clean_segments(raw, 300)
    assert len(result) == 2


def test_duplicate_parallel_lines_become_one_and_input_is_not_empty_after():
    raw = [((0, 0), (200, 0)), ((0, 1.2), (200, 1.0))]
    assert len(clean_segments(raw, 300)) == 1
    assert clean_segments([], 300) == []


def test_snap_distance_follows_the_model_size_within_limits():
    assert snap_distance(10) == 2.5
    assert snap_distance(500) == 4.0
    assert snap_distance(5000) == 8.0


def test_a_line_that_stops_just_short_of_a_junction_is_prolonged_to_it():
    raw = [((0, 0), (400, 0)), ((200, 200), (200, 14))]   # stops 14 px above the horizontal line
    result = clean_segments(raw, 600)
    vertical = next(s for s in result if abs(s[0][0]-s[1][0]) < 1)
    assert min(abs(vertical[0][1]), abs(vertical[1][1])) < 0.5


def test_a_line_is_not_prolonged_over_a_long_empty_distance():
    raw = [((0, 0), (400, 0)), ((200, 300), (200, 150))]
    result = clean_segments(raw, 600)
    vertical = next(s for s in result if abs(s[0][0]-s[1][0]) < 1)
    assert min(abs(vertical[0][1]), abs(vertical[1][1])) > 100
