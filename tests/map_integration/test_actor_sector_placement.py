"""Pure sector occupancy regression tests; no Qt is needed."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE = Path(__file__).resolve().parents[2] / 'map_editor' / 'core' / 'actor_placement.py'
SPEC = importlib.util.spec_from_file_location('actor_placement_test', MODULE)
PLACEMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLACEMENT)
actors_fit = PLACEMENT.actors_fit


def squad(x, y):
    return {'x': x, 'y': y, 'num': 1, 'owner': 1}


def host(x, y):
    return {'x': x, 'y': y, 'owner': 1, 'veh': 56}


def doc(squads=(), hosts=()):
    return SimpleNamespace(mw=10, mh=10, squads=list(squads), host_stations=list(hosts))


def test_squad_and_host_may_share_a_sector_but_same_kind_may_not():
    level = doc([squad(3, 3)], [host(5, 5)])
    assert actors_fit(level, [host(3, 3)], kind='host')
    assert actors_fit(level, [squad(5, 5)], kind='squad')
    assert not actors_fit(level, [squad(3, 3)], kind='squad')
    assert not actors_fit(level, [host(5, 5)], kind='host')


def test_batch_rejects_same_kind_duplicates_and_border():
    level = doc()
    assert not actors_fit(level, [squad(2, 3), squad(2, 3)], kind='squad')
    assert not actors_fit(level, [host(2, 3), host(2, 3)], kind='host')
    assert not actors_fit(level, [host(0, 4)], kind='host')
    assert not actors_fit(level, [squad(9, 4)], kind='squad')
    assert actors_fit(level, [squad(2, 3), squad(4, 3)], kind='squad')


def test_drag_excludes_only_same_kind_actors_being_moved():
    level = doc([squad(2, 2), squad(3, 2)], [host(5, 3)])
    assert actors_fit(level, [squad(3, 2), squad(4, 2)], kind='squad', exclude_squads={0, 1})
    assert actors_fit(level, [squad(5, 3)], kind='squad', exclude_squads={0})
    assert not actors_fit(level, [squad(3, 2)], kind='squad', exclude_squads={0})
    assert actors_fit(level, [host(6, 3)], kind='host', exclude_hosts={0})


def test_existing_legacy_overlap_is_not_modified():
    level = doc([squad(3, 3), squad(3, 3)], [host(3, 3)])
    before = copy.deepcopy((level.squads, level.host_stations))
    assert actors_fit(level, [host(6, 6)], kind='host')
    assert not actors_fit(level, [squad(3, 3)], kind='squad')
    assert (level.squads, level.host_stations) == before


def test_occupancy_is_independent_of_faction():
    level = doc([dict(squad(2, 2), owner=7)], [dict(host(4, 4), owner=1)])
    assert not actors_fit(level, [dict(squad(2, 2), owner=1)], kind='squad')
    assert not actors_fit(level, [dict(host(4, 4), owner=6)], kind='host')
    assert actors_fit(level, [dict(squad(4, 4), owner=1)], kind='squad')
