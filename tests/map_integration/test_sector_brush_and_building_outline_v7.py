"""Map Editor v7 regressions; logic checks run even without Qt."""
from pathlib import Path
import ast

from map_editor.core.history import History
from map_editor.core.ldf_model import LdfDocument
from map_editor.render.building_outline import building_outline_pixels
from map_editor.tools.paint import cells_between, paint_cells


ROOT = Path(__file__).resolve().parents[2] / 'map_editor'


def _method(filename, name):
    module = ast.parse(filename.read_text(encoding='utf-8'))
    return next(node for cls in module.body if isinstance(cls, ast.ClassDef)
                for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_sector_drag_uses_paint_path_not_grid_move():
    source = ROOT / 'render' / 'map_viewport.py'
    press = _method(source, 'mousePressEvent')
    lines = ast.get_source_segment(source.read_text(), press)
    assert "if building:" in lines
    assert 'if building or sector' not in lines
    assert "self._paint_gesture = self.active_tool in ('sector','building','owner','terrain')" in lines
    motion = _method(source, 'mouseMoveEvent')
    motion_lines = ast.get_source_segment(source.read_text(), motion)
    assert 'cells_between(self._paint_last, cell)' in motion_lines
    assert 'self.cellDragged.emit(*crossed' in motion_lines


def test_one_sector_stroke_paints_crossed_buildings_and_undo():
    doc = LdfDocument(mw=9, mh=9)
    doc.grids['blg'][2][5] = '02'
    doc.grids['type'][2][5] = 'f4'
    doc.grids['type'][2][2] = 'ab'
    original = doc.snapshot()
    history = History()
    history.begin(doc)
    changed = False
    for cell in cells_between((2, 2), (6, 2)):
        changed |= paint_cells(doc, [cell], 'type', '42')
        changed |= paint_cells(doc, [cell], 'blg', '00')
    assert history.commit(doc, changed)
    for col in range(2, 7):
        assert doc.grids['type'][2][col] == '42'
        assert doc.grids['blg'][2][col] == '00'
    assert history.undo(doc)
    assert doc.snapshot() == original


def test_outline_follows_visible_model_pixels_with_yellow_under_white():
    import numpy as np

    ids = np.zeros((7, 7), dtype=np.int32)
    building_grid = [['00'] * 3 for _ in range(3)]
    building_grid[1][1] = '01'
    code = -(1 * 3 + 1 + 1)
    ids[1:5, 1:3] = code
    ids[4:6, 1:6] = code

    idle = building_outline_pixels(ids, building_grid)
    selected = building_outline_pixels(ids, building_grid, selected=(1, 1))
    dragging = building_outline_pixels(ids, building_grid, selected=(1, 1), previews={(1, 1)})

    assert tuple(idle[4, 1]) == (255, 204, 40, 255)
    assert tuple(selected[4, 1]) == (255, 204, 40, 255)
    assert tuple(selected[0, 1]) == (255, 255, 255, 255)
    assert tuple(dragging[4, 1]) == (160, 160, 160, 255)
    assert tuple(dragging[0, 1]) == (0, 0, 0, 0)
    assert not np.any(idle[0, :, 3])

    owned = building_outline_pixels(ids, building_grid, owners=[['00'] * 3, ['00', '03', '00'], ['00'] * 3],
                                    owner_colors={3: (10, 20, 30)})
    assert tuple(owned[4, 1]) == (10, 20, 30, 255)


def test_single_delete_action_in_map_context_menu():
    path = ROOT / 'ui' / 'main_window.py'
    code = ast.get_source_segment(path.read_text(), _method(path, '_map_context_menu'))
    assert code.count("menu.addAction('Delete',") == 1
    assert 'Delete building at this sector' not in code
    assert 'Remove selected buildings' not in code
    assert 'Delete selected Host Station' not in code
    assert 'Delete selected squads' not in code
    assert '_delete_special_key(' in code


def test_mouse_drag_paints_existing_sectors_instead_of_grabbing_them():
    """Exercise the real mouse handlers without starting the Qt renderer."""
    import sys
    from types import SimpleNamespace as Obj

    source = ROOT / 'render' / 'map_viewport.py'
    tree = ast.parse(source.read_text())
    viewport_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MapViewport')
    assert viewport_class is not None
    methods = [n for n in viewport_class.body if isinstance(n, ast.FunctionDef) and n.name in
               ('mousePressEvent', 'mouseMoveEvent', 'mouseReleaseEvent')]
    code = compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(source), 'exec')
    qt = Obj(MouseButton=Obj(LeftButton=1, RightButton=2, MiddleButton=3),
             KeyboardModifier=Obj(ShiftModifier=2, ControlModifier=1),
             CursorShape=Obj(CrossCursor=1, PointingHandCursor=2, ClosedHandCursor=3,
                             OpenHandCursor=4, ForbiddenCursor=5))
    namespace = {'Qt': qt, '__package__': 'map_editor.render'}
    exec(code, namespace)

    class Pos:
        def __init__(self, x, y):
            self._x, self._y = x, y

        def x(self):
            return self._x

        def y(self):
            return self._y

        def __sub__(self, other):
            return Pos(self._x - other._x, self._y - other._y)

        def manhattanLength(self):
            return abs(self._x) + abs(self._y)

    class Event:
        def __init__(self, pos):
            self.pos = pos

        def button(self):
            return qt.MouseButton.LeftButton

        def position(self):
            return self.pos

        def modifiers(self):
            return 0

    press, sweep, release = [], [], []
    doc = LdfDocument(mw=9, mh=9)
    doc.grids['type'][2][2] = '4e'
    doc.grids['blg'][2][4] = '02'
    view = Obj(doc=doc, active_tool='sector', camera=Obj(perspective=False),
               _space_pan_active=False, draft_active=False, clear_view=False,
               special_overlay=None, _press_pos=None, _last=Pos(0, 0), hover=None,
               sample_active=False, _paint_gesture=False, _paint_started=False,
               _moved=False, _sweep_gesture=False, _drag_actor=False,
               _paint_last=None, selection=set(), preview_cells=set(),
               cellPressed=Obj(emit=lambda *a: press.append(a)),
               cellDragged=Obj(emit=lambda *a: sweep.append(a)),
               cellReleased=Obj(emit=lambda *a: release.append(a)),
               cellHovered=Obj(emit=lambda *a: None),
               actorDragStarted=Obj(emit=lambda *a: (_ for _ in ()).throw(AssertionError('sector grabbed'))),
               setFocus=lambda: None, setCursor=lambda *_: None,
               unsetCursor=lambda: None, update=lambda: None,
               ground_cell=lambda x, y: (round(x/10), 2),
               pick_cell=lambda x, y: (round(x/10), 2),
               pick_scene_object=lambda *args: None,
               pick_host=lambda *args: None,
               pick_squad=lambda *args: None)
    namespace['mousePressEvent'](view, Event(Pos(20, 20)))
    namespace['mouseMoveEvent'](view, Event(Pos(60, 20)))
    namespace['mouseReleaseEvent'](view, Event(Pos(60, 20)))
    assert press and press[0][:2] == (2, 2)
    assert [(col, row) for col, row, *_ in sweep] == [
        (2, 2), (3, 2), (4, 2), (5, 2), (6, 2)]
    assert len(release) == 1
