from map_editor.core.history import History
from map_editor.core.ldf_model import HGT_MAX, HGT_MIN, LdfDocument
from map_editor.tools.brush import (
    BrushMode,
    BrushShape,
    TerrainBrush,
    brush_outlines,
    brush_footprint,
)
from map_editor.tools.paint import paint_cells


def test_brush_raise_lower_limits_and_borders():
    doc = LdfDocument(mw=12, mh=12)
    brush = TerrainBrush(radius=2, strength=3.0)
    brush.begin_stroke(doc, 5, 5)
    for _ in range(40):
        brush.apply(doc, 5, 5, BrushMode.RAISE)
    assert doc.grids['hgt'][5][5] == HGT_MAX
    for _ in range(200):
        brush.apply(doc, 5, 5, BrushMode.LOWER)
    assert doc.grids['hgt'][5][5] == HGT_MIN
    brush.apply(doc, 1, 1, BrushMode.RAISE)
    assert doc.grids['hgt'][0][0] == doc.grids['hgt'][1][1]
    assert not doc.border_heights_need_normalize()


def test_brush_flatten_and_smooth():
    doc = LdfDocument(mw=12, mh=12)
    doc.grids['hgt'][5][5] = 0x90
    brush = TerrainBrush(radius=1.5, strength=2.0)
    brush.begin_stroke(doc, 6, 6)
    for _ in range(10):
        brush.apply(doc, 5, 5, BrushMode.FLATTEN)
    assert doc.grids['hgt'][5][5] == 0x7F
    doc.grids['hgt'][5][5] = 0x90
    brush.apply(doc, 5, 5, BrushMode.SMOOTH)
    assert doc.grids['hgt'][5][5] < 0x90


def test_brush_raise_lower_are_uniform_and_time_based():
    doc = LdfDocument(mw=13, mh=13)
    brush = TerrainBrush(radius=3, strength=8)
    brush.begin_stroke(doc, 6, 6)
    brush.apply(doc, 6, 6, BrushMode.RAISE, dt=.5)
    brush.apply(doc, 6, 6, BrushMode.RAISE, dt=.5)
    hgt = doc.grids['hgt']
    assert hgt[6][6] == 127 + 8
    assert all(hgt[r][c] == 135 for r in range(4, 9) for c in range(4, 9))
    assert hgt[6][3] == 127
    hgt[6][6] = HGT_MAX
    brush.apply(doc, 6, 6, BrushMode.RAISE, dt=10)
    brush.apply(doc, 6, 6, BrushMode.LOWER, dt=.125)
    assert hgt[6][6] == HGT_MAX - 1
    assert all(hgt[r][c] == HGT_MAX - 1 for r in range(4, 9) for c in range(4, 9))


def test_history_undo_redo_limit():
    doc = LdfDocument(mw=6, mh=6)
    hist = History(limit=100)
    for i in range(120):
        hist.begin(doc)
        paint_cells(doc, [(2, 2)], 'own', i % 7 + 1 if i % 7 + 1 != doc.grids['own'][2][2] else 7)
        hist.commit(doc)
    assert len(hist._undo) == 100
    before = doc.grids['own'][2][2]
    assert hist.undo(doc) and doc.grids['own'][2][2] != before
    assert hist.redo(doc) and doc.grids['own'][2][2] == before


def test_rectangular_and_elliptical_brushes_follow_independent_axes():
    doc = LdfDocument(mw=21, mh=21)
    brush = TerrainBrush(strength=8)
    brush.radius_x, brush.radius_z = 4, 3
    rectangle = {(c, r) for c, r, weight in brush.footprint(doc, 10, 10)}
    assert rectangle == {(c, r) for c in range(7, 14) for r in range(8, 13)}
    brush.shape = BrushShape.ROUND
    ellipse = {(c, r) for c, r, weight in brush.footprint(doc, 10, 10)}
    assert len(ellipse) == 23 and (13, 12) not in ellipse
    assert (13, 10) in ellipse and (10, 12) in ellipse
    brush.begin_stroke(doc, 10, 10)
    brush.apply(doc, 10, 10, BrushMode.RAISE)
    assert doc.grids['hgt'][12][13] == 127
    assert doc.grids['hgt'][10][10] == 135
    brush.shape = BrushShape.SQUARE
    brush.begin_stroke(doc, 10, 10)
    brush.apply(doc, 10, 10, BrushMode.RAISE)
    assert doc.grids['hgt'][12][13] > 127


def test_brush_shapes_have_distinct_radius_two_footprints():
    doc = LdfDocument(mw=13, mh=13)
    round_cells = {(c, r) for c, r, _ in brush_footprint(
        doc, 6, 6, 2, 2, BrushShape.ROUND)}
    square_cells = {(c, r) for c, r, _ in brush_footprint(
        doc, 6, 6, 2, 2, BrushShape.SQUARE)}
    assert len(round_cells) == 5
    assert len(square_cells) == 9
    assert (5, 5) not in round_cells and (5, 5) in square_cells
    assert (6, 6) in round_cells


def test_brush_shapes_use_independent_axes_and_their_own_footprints():
    doc = LdfDocument(mw=21, mh=21)
    expected_axis_cells = {
        BrushShape.ROUND: (True, True),
        BrushShape.DIAMOND: (True, True),
        BrushShape.CROSS: (True, True),
        BrushShape.RING: (True, True),
    }
    for shape, (has_horizontal, has_vertical) in expected_axis_cells.items():
        cells = {(c, r) for c, r, _ in brush_footprint(
            doc, 10, 10, 4, 2, shape)}
        assert ((13, 10) in cells) is has_horizontal
        assert ((10, 11) in cells) is has_vertical
        assert (14, 10) not in cells and (10, 12) not in cells

    diamond = {(c, r) for c, r, _ in brush_footprint(
        doc, 10, 10, 4, 2, BrushShape.DIAMOND)}
    assert (12, 11) not in diamond  # Manhattan distance reaches the boundary.
    cross = {(c, r) for c, r, _ in brush_footprint(
        doc, 10, 10, 4, 2, BrushShape.CROSS)}
    assert (13, 11) not in cross and (11, 11) in cross


def test_ring_is_an_annulus_and_small_rings_fall_back_to_center():
    doc = LdfDocument(mw=15, mh=15)
    ring = {(c, r) for c, r, _ in brush_footprint(
        doc, 7, 7, 4, 4, BrushShape.RING)}
    assert (7, 7) not in ring
    assert (8, 7) not in ring
    assert (9, 7) in ring and (10, 7) in ring

    for shape in (BrushShape.ROUND, BrushShape.RING):
        small = brush_footprint(doc, 7, 7, 0.5, 0.5, shape)
        assert [(c, r) for c, r, _ in small] == [(7, 7)]


def test_brush_outlines_match_shape_topology_and_preserve_axis_radii():
    square = brush_outlines(BrushShape.SQUARE, 4, 2)
    round_outline = brush_outlines(BrushShape.ROUND, 4, 2)
    diamond = brush_outlines(BrushShape.DIAMOND, 4, 2)
    cross = brush_outlines(BrushShape.CROSS, 4, 2)
    ring = brush_outlines(BrushShape.RING, 4, 2)

    assert len(square) == 1 and len(square[0]) == 4
    assert square[0] == [(-4.0, -2.0), (4.0, -2.0), (4.0, 2.0), (-4.0, 2.0)]
    assert len(round_outline) == 1 and len(round_outline[0]) == 64
    assert max(x for x, _ in round_outline[0]) == 4.0
    assert len(diamond) == 1 and diamond[0] == [(-4.0, 0.0), (0.0, -2.0),
                                                   (4.0, 0.0), (0.0, 2.0)]
    assert len(cross) == 1 and len(cross[0]) == 12
    assert len(ring) == 2 and [len(loop) for loop in ring] == [64, 64]
    assert max(x for x, _ in ring[0]) == 4.0
    assert max(x for x, _ in ring[1]) == 1.8


def test_brush_shapes_keep_cells_inside_map_and_recalculate_borders():
    doc = LdfDocument(mw=9, mh=9)
    for shape in BrushShape:
        cells = brush_footprint(doc, 1, 1, 4, 3, shape)
        assert cells
        assert all(1 <= c < doc.mw - 1 and 1 <= r < doc.mh - 1
                   for c, r, _ in cells)
        brush = TerrainBrush(strength=2)
        brush.radius_x, brush.radius_z = 4, 3
        brush.shape = shape
        brush.begin_stroke(doc, 1, 1)
        brush.apply(doc, 1, 1, BrushMode.RAISE)
        assert not doc.border_heights_need_normalize()


def test_zero_radius_retains_single_cell_brush_behavior():
    doc = LdfDocument(mw=7, mh=7)
    brush = TerrainBrush(radius=0, strength=1)
    brush.begin_stroke(doc, 3, 3)
    assert brush.apply(doc, 3, 3, BrushMode.RAISE) == [(3, 3)]
    assert doc.grids['hgt'][3][3] == 128
    assert doc.grids['hgt'][3][2] == doc.grids['hgt'][2][3] == 127



def test_snapshot_copies_grid_rows_and_nested_metadata_independently():
    doc=LdfDocument(mw=7,mh=7)
    saved=doc.snapshot()
    doc.grids['own'][3][3]=6
    doc.gates[1]['keys'].append((3,3))
    assert saved['grids']['own'][3][3]==0 and saved['gates'][1]['keys']==[]
    doc.restore(saved)
    doc.grids['type'][3][3]='05'
    doc.gates[1]['keys'].append((4,4))
    assert saved['grids']['type'][3][3]=='00' and saved['gates'][1]['keys']==[]
