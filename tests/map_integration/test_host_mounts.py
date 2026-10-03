from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from map_editor import bootstrap
from map_editor.core.building_defs import BuildingDef, GunMount
from map_editor.core.vehicle_defs import VehicleVisual, parse_vehicle_text
from map_editor.render import sector_mesh
from map_editor.render.sector_mesh import SectorMesh, SectorMeshLibrary


def test_vanilla_host_56_has_its_five_scripted_mounts():
    try:
        data = bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("Set NME_GAME_DATA to inspect the installed Vanilla scripts")
    scripts = bootstrap.find_ci(data, "Scripts")
    vehicles_cfg = bootstrap.find_ci(scripts, "Vehicles.cfg") if scripts else None
    if vehicles_cfg is None:
        pytest.skip("The installed game Scripts/Vehicles.cfg is unavailable")

    definitions = parse_vehicle_text(vehicles_cfg.read_text(encoding="cp1252"))
    host = definitions[56]
    assert host.model.casefold() == "robo"
    assert host.energy == 5_000_000
    assert host.viewer == (0.0, -500.0, 0.0)
    assert [mount.vehicle for mount in host.guns] == [90, 91, 92, 93, 93]
    assert host.guns[0].pos == (0.0, -200.0, 55.0)
    assert host.guns[1].direction == (0.0, 0.0, -1.0)


def test_vehicle_mount_slots_and_viewer_axes_follow_script_order():
    definitions = parse_vehicle_text("""new_vehicle 220
model = robo
energy = 100
robo_num_guns = 2
robo_viewer_x = 11
robo_act_gun = 0
robo_gun_pos_x = 4
robo_gun_type = 7
unit_act_gun = 1
unit_gun_pos_y = 12
unit_gun_dir_z = -1
unit_gun_type = 9
end
modify_vehicle 220
energy = 200
robo_viewer_y = 25
unit_act_gun = 0
unit_gun_pos_z = 14
end
""")

    host = definitions[220]
    assert host.energy == 200
    assert host.viewer == (11.0, 25.0, 0.0)
    assert len(host.guns) == 2
    assert host.guns[0] == GunMount(7, (4.0, 0.0, 14.0), (0.0, 0.0, 0.0))
    assert host.guns[1] == GunMount(9, (0.0, 12.0, 0.0), (0.0, 0.0, -1.0))


def test_new_host_starts_empty_and_unit_slot_limits_match_engine():
    definitions = parse_vehicle_text("""new_vehicle 221
model = robo
end
new_vehicle 222
model = robo
unit_num_guns = 30
unit_act_gun = 25
unit_num_guns = 2
unit_gun_type = 15
end
new_vehicle 223
model = robo
unit_num_guns = 30
unit_act_gun = 25
unit_gun_type = 257
end
""")
    assert definitions[221].energy == 0
    assert definitions[221].guns == []
    assert len(definitions[222].guns) == 2
    # unit_num_guns clamps to twenty and unit_act_gun clamps to slot nineteen.
    assert definitions[222].guns[1].vehicle == 15
    assert len(definitions[223].guns) == 20
    assert definitions[223].guns[19].vehicle == 1


@dataclass
class _Face:
    vertices: list[tuple[float, float, float]]


def test_host_mesh_reuses_building_mount_transform_without_duplicate_body(
        monkeypatch):
    library = SectorMeshLibrary.__new__(SectorMeshLibrary)
    mount = GunMount(90, (10, 20, 30), (1, 0, 0))
    library.vehicles = {50: VehicleVisual(model="robo", guns=[mount])}
    library.buildings = {8: BuildingDef(8, guns=[mount])}
    library._host_meshes = {}
    library._gun_meshes = {}

    body_face = _Face([(20, 0, 0), (21, 0, 0), (20, 1, 0)])
    gun_face = _Face([(0, 0, 0), (0, 0, 1), (0, 1, 0)])
    body = SectorMesh(-1, faces=[(body_face, 0, 0)])
    actor = SectorMesh(-1, faces=[(gun_face, 0, 0)])
    calls = []

    def vehicle_mesh(vehicle_id):
        calls.append(vehicle_id)
        return body if vehicle_id == 50 else actor

    monkeypatch.setattr(library, "vehicle_mesh", vehicle_mesh)

    host = library.host_mesh(50)
    expected_gun_vertices = (
        np.asarray(gun_face.vertices) @ sector_mesh.gun_rotation(mount.direction).T
        + mount.pos)
    assert len(host.faces) == 2
    assert host.faces[0][0] is body_face
    assert np.allclose(host.faces[1][0].vertices, expected_gun_vertices)
    assert calls == [50, 90]
    assert library.host_mesh(50) is host
    assert calls == [50, 90]
    assert library.actor_mesh(50) is host
    assert calls == [50, 90]

    building = library.building_mesh(8)
    assert np.allclose(building.faces[0][0].vertices, expected_gun_vertices)


def test_reload_definitions_invalidates_host_mesh_cache(tmp_path, monkeypatch):
    library = SectorMeshLibrary.__new__(SectorMeshLibrary)
    library.scripts = Path(tmp_path)
    library._vehicle_meshes = {50: SectorMesh(-1)}
    library._gun_meshes = {8: SectorMesh(-1)}
    library._host_meshes = {50: SectorMesh(-1)}
    library._external_surfaces = {"stale": object()}
    monkeypatch.setattr(sector_mesh, "load_vehicle_files", lambda *_args: {})
    monkeypatch.setattr(sector_mesh, "load_building_files", lambda *_args: {})

    library.reload_definitions()

    assert library._host_meshes == {}
    assert library._vehicle_meshes == {}
    assert library._gun_meshes == {}
    assert library._external_surfaces == {}
