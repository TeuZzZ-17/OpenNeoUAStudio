from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.building_defs import parse_building_text
from map_editor.core.vehicle_defs import WHITE_TINT, parse_tint, parse_vehicle_text
from map_editor.render.sector_mesh import SectorMeshLibrary, visual_rotation_matrix
from map_editor.render.sector_sprite import rasterize_sprite, sprite_input
from studio.rendering.asset_viewport_common import ViewFace
from studio.rendering.indexed import IndexedSurface, IndexedTables


def _tables_blue_to_red() -> IndexedTables:
    palette = [(0, 0, 0)] * 256
    palette[1] = (30, 80, 240)
    palette[2] = (200, 200, 200)
    shader_pixels = bytes(range(256)) * 256
    tracy_pixels = bytes(range(256)) * 256
    return IndexedTables(palette, shader_pixels, tracy_pixels)


def test_parse_tint_and_vehicle_visual_params():
    assert parse_tint("255|50|50") == (255, 50, 50, 255)
    assert parse_tint("300|-5|128|64 ; comment") == (255, 0, 128, 64)
    assert parse_tint("bad|value") == WHITE_TINT

    vehicles = parse_vehicle_text("""
new_vehicle 10
    model = plane
    tint = 255|50|50|255
    scale_xyz = 2.0|0.0|-1.5
    rotation_xyz = 10|20|30
    decoration_fx_mode = persistent
    decoration_fx_vp = 99
    decoration_fx_scale_xyz = 0.5|0.5|0.5
    decoration_fx_tint = 10|20|30|200
    decoration_fx_offset_xyz = 1|2|3
    begin_fx
        tint = 0|0|0|0
    end
end
modify_vehicle 10
    rotation_xyz = 0|90|0
end
""")
    v = vehicles[10]
    assert v.tint == (255, 50, 50, 255)
    assert v.scale == (2.0, 1.0, 1.0)
    assert v.rotation == (0.0, 90.0, 0.0)
    assert v.decoration.mode == "persistent"
    assert v.decoration.vp == 99
    assert v.decoration.scale == (0.5, 0.5, 0.5)
    assert v.decoration.tint == (10, 20, 30, 200)
    assert v.decoration.offset == (1.0, 2.0, 3.0)


def test_parse_building_tint_and_modify():
    buildings = parse_building_text("""
new_building 12
    name = GreenPower
    model = kraftwerk
    sec_type = 10
    tint = 40|200|80|255
    decoration_fx_mode = persistent
    decoration_fx_vp = 77
    decoration_fx_tint = 120|255|120|255
end
modify_building 12
    power = 50
end
""")
    b = buildings[12]
    assert b.tint == (40, 200, 80, 255)
    assert b.decoration.mode == "persistent"
    assert b.decoration.vp == 77
    assert b.decoration.tint == (120, 255, 120, 255)


def test_visual_rotation_matrix_matches_engine_orientation():
    rot = visual_rotation_matrix((90.0, 0.0, 0.0))
    # RotateX(+90 deg): y -> -z, z -> y (see matrix.h RotateX with sin(-angle))
    pt = rot @ np.array([0.0, 1.0, 0.0])
    assert np.allclose(pt, [0.0, 0.0, -1.0], atol=1e-6)


def test_sector_mesh_applies_scale_rotation_tint_and_unit_guns(monkeypatch):
    lib = object.__new__(SectorMeshLibrary)
    lib._vehicle_meshes = {}
    lib._host_meshes = {}
    lib._gun_meshes = {}
    lib._building_decorations = {}
    lib._vp_names = ["", "BODY.BASE", "GUN.BASE"]

    body_face = ViewFace(
        vertices=[(10.0, 0.0, 0.0), (0.0, 10.0, 0.0), (0.0, 0.0, 0.0)],
        uvs=[(0, 0), (1, 0), (0, 1)],
        material=0,
        poly_id=1,
    )
    gun_face = ViewFace(
        vertices=[(2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 0.0)],
        uvs=[(0, 0), (1, 0), (0, 1)],
        material=0,
        poly_id=2,
    )
    lib.vehicles = parse_vehicle_text("""
new_vehicle 1
    model = plane
    vp_normal = 1
    scale_xyz = 2.0|1.0|1.0
    rotation_xyz = 0|0|90
    tint = 255|50|50|255
    unit_num_guns = 1
    unit_act_gun = 0
    unit_gun_type = 2
    unit_gun_pos_xyz = 0|5|0
    unit_gun_dir_xyz = 0|0|1
end
new_vehicle 2
    model = gun
    vp_normal = 2
end
""")
    monkeypatch.setattr(lib, "_base_faces", lambda name, set_only=False: [body_face] if name == "BODY.BASE" else [gun_face])

    vm = lib.vehicle_mesh(1)
    assert len(vm.faces) == 1
    tface = vm.faces[0][0]
    assert tface.tint == (255, 50, 50, 255)
    # (10,0,0) scaled by (2,1,1) -> (20,0,0), rotated +90 deg Z -> (0, -20, 0)
    assert np.allclose(tface.vertices[0], (0.0, -20.0, 0.0), atol=1e-5)

    actor = lib.actor_mesh(1)
    assert len(actor.faces) == 2


def test_cpu_sprite_and_map_scene_render_colorized_tint():
    tables = _tables_blue_to_red()
    surface = IndexedSurface("blue", "solid", None, 0, 0, 1, "none", 0, "none", "linear")
    face = ViewFace(
        vertices=[(-120.0, -120.0, 0.0), (0.0, 120.0, 0.0), (120.0, -120.0, 0.0)],
        uvs=[(0, 0), (0, 255), (255, 0)],
        material=-1,
        poly_id=1,
        tint=(255, 40, 40, 255),
    )

    class _FakeLib:
        def __init__(self):
            self.tables = tables

        def face_uvs(self, f):
            return f.uvs

        def surface_for(self, f):
            return surface

    lib = _FakeLib()
    mesh = SimpleNamespace(faces=[(face, 0.0, 0.0)])
    data = sprite_input(lib, mesh, -45, 35.26, 0.25, target_size=(64, 64))
    sprite = rasterize_sprite(data, tables, 0.25)
    assert sprite is not None
    arr = np.frombuffer(sprite.image.bits(), dtype=np.uint8).reshape(64, 64, 4)
    opaque = arr[arr[:, :, 3] > 0]
    assert len(opaque) > 0
    assert opaque[:, 0].mean() > opaque[:, 2].mean() * 2.0


def test_red_spore_in_set3_renders_red_tinted():
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    try:
        data_dir = bootstrap.game_data_dir()
    except RuntimeError:
        pytest.skip("NME_GAME_DATA not available")
    if not bootstrap.set_dir(3).is_dir():
        pytest.skip("Set 3 not installed")
    assets = SetAssets(3).load()
    lib = SectorMeshLibrary(assets)
    mesh = lib.actor_mesh(146)  # Red_Spore
    assert mesh.faces
    assert any(getattr(f, "tint", None) == (255, 50, 50, 255) for f, _, _ in mesh.faces)
    data = sprite_input(lib, mesh, -45, 35.26, 0.25, target_size=(80, 80))
    sprite = rasterize_sprite(data, lib.tables, 0.25)
    assert sprite is not None
    arr = np.frombuffer(sprite.image.bits(), dtype=np.uint8).reshape(80, 80, 4)
    opaque = arr[arr[:, :, 3] > 0]
    assert len(opaque) > 0
    assert opaque[:, 0].mean() > opaque[:, 2].mean()
