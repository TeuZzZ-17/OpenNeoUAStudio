from map_editor.core.history import History
from map_editor.core.ldf_model import HGT_MAX, HGT_MIN, LdfDocument
from map_editor.tools.brush import BrushMode, BrushShape, TerrainBrush
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
    assert len(ellipse) == 31 and (13, 12) not in ellipse
    assert (13, 10) in ellipse and (10, 12) in ellipse
    brush.begin_stroke(doc, 10, 10)
    brush.apply(doc, 10, 10, BrushMode.RAISE)
    assert doc.grids['hgt'][12][13] == 127
    assert doc.grids['hgt'][10][10] == 135
    brush.shape = BrushShape.SQUARE
    brush.begin_stroke(doc, 10, 10)
    brush.apply(doc, 10, 10, BrushMode.RAISE)
    assert doc.grids['hgt'][12][13] > 127


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

