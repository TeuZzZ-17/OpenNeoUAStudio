import pytest

from map_editor.core.ldf_model import DEFAULT_HGT, LdfDocument, ensure_host_defaults, dumps_ldf, loads_ldf
from map_editor.render import squad_scene
from map_editor.render.squad_scene import host_members, host_position
from map_editor.render.terrain_mesh import TerrainMesh


@pytest.mark.parametrize('height', [DEFAULT_HGT, DEFAULT_HGT + 20, DEFAULT_HGT - 15])
@pytest.mark.parametrize('offset', [-700, 0, 125.5])
def test_host_height_is_relative_to_terrain_without_rewriting_ldf(height, offset):
    doc = LdfDocument(mw=7, mh=7)
    doc.grids['hgt'] = [[height] * 7 for _ in range(7)]
    host = dict(owner=1, veh=56, x=3, y=3, pos_y=offset, energy=500000, custom_name=None, hidden=False)
    ensure_host_defaults(host)
    doc.host_stations = [host]
    before = doc.snapshot()
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    member = next(host_members(doc, terrain))
    assert member.position[1] == pytest.approx(-(height - DEFAULT_HGT) * 100 + offset + .3)
    assert doc.snapshot() == before
    assert loads_ldf(dumps_ldf(doc)).host_stations[0]['pos_y'] == offset


def test_host_uses_collision_surface_at_exact_world_position(monkeypatch):
    doc = LdfDocument(mw=7, mh=7)
    terrain, library = object(), object()
    host = dict(x=3, y=3, pos_x=4210.125, pos_z=-4215.75, pos_y=-600.5)
    requests = []

    def surface(received_doc, received_terrain, received_lib, x, z):
        requests.append((received_doc, received_terrain, received_lib, x, z))
        return -2375.25

    monkeypatch.setattr(squad_scene, 'ground_height', surface)
    position = host_position(host, doc, terrain, library)
    assert position == pytest.approx((4210.425, -2975.45, -4215.45))
    assert requests == [(doc, terrain, library, 4210.425, -4215.45)]
