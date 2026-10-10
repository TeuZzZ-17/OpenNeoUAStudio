"""Host duplication uses the existing single-host placement and occupancy rules."""
import copy

import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[2] / 'map_editor' / 'core' / 'actor_placement.py'
SPEC = importlib.util.spec_from_file_location('actor_placement_v6', MODULE)
PLACEMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLACEMENT)
actors_fit = PLACEMENT.actors_fit
host_copy_preview = PLACEMENT.host_copy_preview


def _host():
    record = dict(owner=6, veh=57, energy=320000, x=2, y=2,
                  pos_x=3100.5, pos_z=-3200.75, pos_y=-950,
                  viewangle=91, body_angle=180, hidden=True,
                  custom_name='Copy me', reload_const=500000, ai={'preset': 'Custom', 'con_budget': 20})
    return record


def test_host_copy_has_independent_ai_and_uses_new_sector_position():
    original = _host()
    before = copy.deepcopy(original)
    draft = host_copy_preview(original, (5, 4))
    assert (draft['x'], draft['y']) == (5, 4)
    assert draft['_preview'] is True
    assert 'pos_x' not in draft and 'pos_z' not in draft
    for key in ('owner', 'veh', 'energy', 'pos_y', 'viewangle', 'body_angle',
                'hidden', 'custom_name', 'reload_const'):
        assert draft[key] == original[key]
    assert draft['ai'] == original['ai']
    draft['ai']['con_budget'] = 17
    assert original == before


def test_duplicated_host_rejects_another_host_but_allows_squad():
    doc = SimpleNamespace(mw=9, mh=9, squads=[], host_stations=[])
    original = _host()
    doc.host_stations = [original]
    doc.squads = [dict(x=5, y=4, num=2, owner=6)]
    assert actors_fit(doc, [host_copy_preview(original, (5, 4))], kind='host')
    assert not actors_fit(doc, [host_copy_preview(original, (2, 2))], kind='host')
    assert not actors_fit(doc, [host_copy_preview(original, (0, 0))], kind='host')
    assert doc.host_stations == [original]
