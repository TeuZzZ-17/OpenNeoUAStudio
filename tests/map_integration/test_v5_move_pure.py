"""Map placement regressions that run without Qt or external game data."""
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2] / 'map_editor'
for name, directory in (('v5_pure_core', ROOT / 'core'), ('v5_pure_tools', ROOT / 'tools')):
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package

from v5_pure_core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from v5_pure_core.special_objects import add_special, update_special, remove_special
from v5_pure_tools.map_clipboard import copy_grid_cells, move_grid_cell


def _power_below(doc, cell=(3, 3)):
    x, y = cell
    doc.grids['type'][y][x] = 'b2'
    doc.grids['blg'][y][x] = '01'


def _empty(doc, cell):
    x, y = cell
    assert (doc.grids['type'][y][x], doc.grids['blg'][y][x]) == ('00', '00')


@pytest.mark.parametrize('kind', ('gem', 'gate', 'item'))
def test_special_over_power_moves_twice_and_removes_without_leaving_building(kind):
    doc = LdfDocument(mw=10, mh=10)
    _power_below(doc)
    slot = add_special(doc, kind)
    changes = {'x': 3, 'y': 3}
    if kind == 'gem':
        changes['actions'] = [{'target_type': 'modify_vehicle', 'id': 1,
                               'param': 'enable', 'val': '1'}]
    assert update_special(doc, kind, slot, changes)
    assert update_special(doc, kind, slot, {'x': 4, 'y': 4})
    _empty(doc, (3, 3))
    assert update_special(doc, kind, slot, {'x': 5, 'y': 5})
    _empty(doc, (4, 4))
    remove_special(doc, kind, slot)
    _empty(doc, (5, 5))
    assert loads_ldf(dumps_ldf(doc)).grids == doc.grids


@pytest.mark.parametrize('kind', ('gem', 'gate', 'item'))
def test_special_removal_from_first_placement_flattens_underlay(kind):
    doc = LdfDocument(mw=10, mh=10)
    _power_below(doc)
    slot = add_special(doc, kind)
    values = {'x': 3, 'y': 3}
    if kind == 'gem':
        values['actions'] = [{'target_type': 'modify_vehicle', 'id': 1,
                              'param': 'enable', 'val': '1'}]
    update_special(doc, kind, slot, values)
    remove_special(doc, kind, slot)
    _empty(doc, (3, 3))


def test_building_drag_flatten_source_and_replace_sector():
    doc = LdfDocument(mw=10, mh=10)
    _power_below(doc)
    doc.grids['type'][4][4] = 'ad'
    clipboard = copy_grid_cells(doc, {(3, 3)}, 'building', ('type', 'blg'))
    assert move_grid_cell(doc, (3, 3), (4, 4), clipboard)
    _empty(doc, (3, 3))
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('b2', '01')


def test_sector_drag_replaces_building_and_flatten_source_without_moving_owner_height():
    doc = LdfDocument(mw=10, mh=10)
    doc.grids['type'][3][3] = 'a1'
    _power_below(doc, (4, 4))
    before = (doc.grids['hgt'][3][3], doc.grids['own'][3][3],
              doc.grids['hgt'][4][4], doc.grids['own'][4][4])
    clipboard = copy_grid_cells(doc, {(3, 3)}, 'sector', ('type', 'blg'))
    assert move_grid_cell(doc, (3, 3), (4, 4), clipboard)
    _empty(doc, (3, 3))
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('a1', '00')
    assert (doc.grids['hgt'][3][3], doc.grids['own'][3][3],
            doc.grids['hgt'][4][4], doc.grids['own'][4][4]) == before
