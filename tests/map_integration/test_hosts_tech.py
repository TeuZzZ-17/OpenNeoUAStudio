import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import struct
from pathlib import Path
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication

from map_editor import bootstrap
from map_editor.core.ldf_model import LdfDocument, loads_ldf, dumps_ldf, ensure_host_defaults, make_host_ai
from map_editor.core.vehicle_defs import load_vehicle_files, VehicleVisual
from map_editor.core.building_defs import load_building_files, parse_building_text
from map_editor.core.three_ds import read_3ds
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.core.asset_bridge import SetAssets
from map_editor.render.gpu_scene import WorldScene
from map_editor.render.squad_scene import host_members
from map_editor.render.terrain_mesh import TerrainMesh
from map_editor.render.map_scene import scene_polygons, render_scene
from map_editor.render.camera import IsoCamera
from map_editor.ui.main_window import MainWindow
from map_editor.ui.preview_cards import RESOURCE_ROLE


def host(owner=1, vehicle=56, x=3, y=3):
    value = dict(owner=owner, veh=vehicle, x=x, y=y, energy=500000,
                 pos_y=-700, hidden=False, custom_name=None)
    ensure_host_defaults(value)
    return value


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    window = MainWindow(LdfDocument(mw=9, mh=9))
    window._icons.stop()
    yield window
    window._cancel_operation(clear=False)
    window._released(-1, -1, Qt.KeyboardModifier.NoModifier)
    window.dirty = False
    window.close()
    window._icon_pool.waitForDone(10000)
    window.view._pool.waitForDone(10000)


def test_manifest_include_order_inline_mod_and_inactive_files(tmp_path):
    scripts = tmp_path / 'Data' / 'Scripts'
    scripts.mkdir(parents=True)
    (scripts / 'Startup.cfg').write_text('include data:scripts/base.scr\ninclude mod.cfg\n')
    (scripts / 'base.scr').write_text('new_vehicle 222\nname = Mod Host\nmodel = robo\nvp_normal = 3\nenable = 1\nenable = 6\nend\nnew_building 199\nname = Mod Building\nsec_type = 201\nenable = 1\nend')
    (scripts / 'mod.cfg').write_text('modify_vehicle 222\nvp_normal = 4\ndisable = 1\nend\nmodify_building 199\npower = 70\nend\ninclude Startup.cfg')
    (scripts / 'inactive.cfg').write_text('new_vehicle 223\nname = Not Loaded\nend')
    vehicles = load_vehicle_files(scripts, 'modify_vehicle 222\nbase_normal = Data/Models/mod.BASE\nend')
    assert set(vehicles) == {222}
    assert vehicles[222].vp_normal == 4 and vehicles[222].model == 'robo'
    assert vehicles[222].enabled_factions == {6}
    assert vehicles[222].base_normal == 'Data/Models/mod.BASE'
    buildings = load_building_files(scripts)
    assert buildings[199].name == 'Mod Building' and buildings[199].sec_type == 201
    assert buildings[199].power == 70 and buildings[199].enabled_factions == {1}


def test_empty_permissions_and_last_repeated_block_survive_save():
    doc = LdfDocument(mw=5, mh=5)
    raw = dumps_ldf(doc).replace('; --- USER SCRIPT ---',
                                'begin_enable 6\nvehicle = 222\nend\nbegin_enable 6\nend\n; --- USER SCRIPT ---')
    loaded = loads_ldf(raw)
    assert loaded.tech[6] == {'veh': [], 'blg': []}
    assert loaded.tech_explicit == {6}
    saved = dumps_ldf(loaded)
    assert 'begin_enable 6' in saved
    assert loads_ldf(saved).tech_explicit == {6}
    assert 'begin_enable' not in dumps_ldf(doc)


def test_partial_building_mount_modification_preserves_other_slots():
    definitions = parse_building_text('''new_building 199
sbact_act = 0
sbact_vehicle = 5
sbact_pos_x = 10
sbact_act = 1
sbact_vehicle = 6
sbact_pos_y = 20
sbact_dir_z = -1
end
modify_building 199
sbact_act = 1
sbact_pos_x = 30
end''')
    first, second = definitions[199].guns
    assert first.vehicle == 5 and first.pos == (10, 0, 0)
    assert second.vehicle == 6 and second.pos == (30, 20, 0)
    assert second.direction == (0, 0, -1)


def test_placed_host_cannot_overwrite_another_host_draft(win):
    win.doc.host_stations = [host(), host(owner=6, vehicle=57, x=4, y=4)]
    win._refresh_squads()
    win._refresh_hosts()
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    win._add_host()
    expected = dict(win._draft_host)
    win.host_panel.list.setCurrentRow(0)
    assert not win.host_panel.form_widget.isEnabled()
    win._host_changed(0, 'energy', 999)
    assert win._draft_host == expected
    assert win.doc.host_stations[0]['energy'] == 500000
    win._cancel_operation()
    assert win.host_panel.form_widget.isEnabled()


def test_imported_host_world_coordinates_are_preserved_until_moved(win):
    doc = LdfDocument(mw=9, mh=9)
    doc.host_stations = [dict(host(), pos_x=4200.125, pos_z=-4300.75, pos_y=-700.5)]
    saved = dumps_ldf(doc)
    loaded = loads_ldf(saved)
    assert loaded.host_stations[0]['pos_x'] == 4200.125
    assert loaded.host_stations[0]['pos_z'] == -4300.75
    assert loaded.host_stations[0]['pos_y'] == -700.5
    assert dumps_ldf(loaded) == saved
    win._new_doc(loaded, None)
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    win._refresh_hosts(0)
    win._begin_actor_drag('host', (3, 3))
    win._move_actor_drag((5, 5))
    win._finish_actor_drag()
    assert win.doc.host_stations[0]['pos_x'] == 6600.125


def test_host_draft_placement_cancel_move_delete_and_history(win):
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    before = win.doc.snapshot()
    win._add_host()
    assert not win.doc.host_stations and len(win.view.doc.host_stations) == 1
    assert win.view.doc.host_stations[0]['_preview']
    win._hovered(4, 5)
    assert win._draft_host['x'] == 4
    win._cancel_operation()
    assert win.doc.snapshot() == before and not win.view.doc.host_stations
    win._add_host()
    win._confirm_placement((4, 5))
    assert len(win.doc.host_stations) == 1 and win.doc.host_stations[0]['y'] == 5
    placed = win.doc.snapshot()
    win.undo()
    assert win.doc.snapshot() == before
    win.redo()
    assert win.doc.snapshot() == placed
    win._refresh_hosts(0)
    win._begin_actor_drag('host', (4, 5))
    win._move_actor_drag((6, 6))
    win._cancel_operation()
    assert win.doc.snapshot() == placed
    win._begin_actor_drag('host', (4, 5))
    win._move_actor_drag((6, 6))
    win._finish_actor_drag()
    assert win.doc.host_stations[0]['x'] == 6
    win._delete_host(0)
    assert not win.doc.host_stations
    win.undo()
    assert win.doc.host_stations[0]['x'] == 6


def test_host_comment_is_one_live_edit_and_ai_player_order(win):
    win.doc.host_stations = [host(1), host(6, 70, 5, 5), host(1, 56, 6, 6)]
    win._refresh_squads()
    win._refresh_hosts(1)
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    count = len(win.history._undo)
    win.host_panel.advanced.setChecked(True)
    QTest.keyClicks(win.host_panel.name, 'custom host')
    win._finish_live()
    assert win.doc.host_stations[1]['custom_name'] == 'custom host'
    assert win.host_panel.name.text() == 'custom host'
    assert len(win.history._undo) == count + 1
    win._host_changed(1, 'ai_preset', 'Defensive')
    win._finish_live()
    assert win.doc.host_stations[1]['ai'] == make_host_ai('Defensive')
    win._host_player(1)
    loaded = loads_ldf(dumps_ldf(win.doc))
    assert loaded.player_owner == 6 and loaded.host_stations[0]['owner'] == 6
    win._refresh_hosts(2)
    assert win.host_panel.ai.isEnabled()


def test_dynamic_map_scripts_populate_all_panels_and_undo(win):
    script = 'new_vehicle 222\nname = Mod Host\nmodel = robo\nvp_normal = 3\nend\nnew_vehicle 223\nname = Mod Tank\nmodel = tank\nvp_normal = 4\nend'
    win.script_edit.setPlainText(script)
    win._finish_script()
    assert win.host_panel.vehicle.findData(222) >= 0
    assert win.squad_panel.vehicle.findData(223) >= 0
    keys = {win.tech_panel.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(win.tech_panel.list.count())}
    assert {222, 223} <= keys
    win.undo()
    assert win.host_panel.vehicle.findData(222) < 0
    win.redo()
    assert win.host_panel.vehicle.findData(222) >= 0


def test_tech_click_seeds_inherited_permissions_preserves_unknown_and_undo(win, app):
    win.doc.host_stations = [host(1)]
    win._refresh_squads()
    win._refresh_hosts()
    lib = win._lib()
    inherited = {key for key, visual in lib.vehicles.items() if 1 in visual.enabled_factions}
    inherited_buildings = {key for key, definition in win.buildings.items() if 1 in definition.enabled_factions}
    assert 1 in inherited
    win.palette_tabs.setCurrentIndex(win.tech_tab_index)
    before = win.doc.snapshot()
    target = next(win.tech_panel.list.item(i) for i in range(win.tech_panel.list.count())
                  if win.tech_panel.list.item(i).data(Qt.ItemDataRole.UserRole) == 1)
    assert target.checkState() == Qt.CheckState.Checked
    win.tech_panel._clicked(target)
    assert set(win.doc.tech[1]['veh']) == inherited - {1}
    assert set(win.doc.tech[1]['blg']) == inherited_buildings
    win.undo()
    assert win.doc.snapshot() == before
    win.doc.tech[1] = {'veh': [999], 'blg': []}
    win.tech_panel.rebuild()
    unknown = next(win.tech_panel.list.item(i) for i in range(win.tech_panel.list.count())
                   if win.tech_panel.list.item(i).data(Qt.ItemDataRole.UserRole) == 999)
    assert 'missing' in unknown.toolTip()
    win.tech_panel._clicked(unknown)
    assert win.doc.tech_explicit == {1}
    assert 'begin_enable 1' in dumps_ldf(win.doc)
    assert loads_ldf(dumps_ldf(win.doc)).tech[1] == {'veh': [], 'blg': []}


def test_tech_preview_decoration_does_not_change_permissions(win):
    win.doc.host_stations = [host()]
    win._refresh_squads()
    win._refresh_hosts()
    before = win.doc.snapshot()
    key = win.tech_panel.list.item(0).data(RESOURCE_ROLE)
    pix = QPixmap(4, 4)
    pix.fill(Qt.GlobalColor.red)
    win._resource_finished((id(win._lib()), win._asset_epoch, *key), pix.toImage())
    assert win.doc.snapshot() == before and not win.history.can_undo
    assert not win.tech_panel.list.item(0).icon().isNull()


def chunk(tag, data):
    return struct.pack('<HI', tag, len(data) + 6) + data


def minimal_3ds():
    vertices = chunk(0x4110, struct.pack('<H9f', 3, 0, 0, 0, 100, 0, 0, 0, 100, 200))
    faces = chunk(0x4120, struct.pack('<H4H', 1, 0, 1, 2, 0))
    return chunk(0x4D4D, chunk(0x3D3D, chunk(0x4000, b'custom\0' + chunk(0x4100, vertices + faces))))


def test_3ds_normal_wins_and_invalid_file_falls_back_to_vp(app, tmp_path, monkeypatch):
    lib = SectorMeshLibrary(SetAssets(1).load())
    models = tmp_path / 'Models'
    models.mkdir()
    path = models / 'CUSTOM.3ds'
    path.write_bytes(minimal_3ds())
    meshes, _ = read_3ds(path)
    assert meshes[0].vertices[2] == (0, -200, 100)
    assert meshes[0].faces[0] == (2, 1, 0)
    monkeypatch.setattr(bootstrap, 'game_data_dir', lambda: tmp_path)
    lib.vehicles[222] = VehicleVisual(vp_normal=lib.vehicles[1].vp_normal,
                                      three_ds_normal='Data/models/custom.3DS')
    mesh = lib.vehicle_mesh(222)
    assert len(mesh.faces) == 1 and mesh.bounds[1] == -200
    assert lib.surface_for(mesh.faces[0][0]).kind == 'solid'
    path.write_bytes(b'broken')
    lib._vehicle_meshes.pop(222)
    fallback = lib.vehicle_mesh(222)
    assert len(fallback.faces) == len(lib.vehicle_mesh(1).faces)


def test_hosts_render_and_have_distinct_pick_ids_on_both_paths(app):
    lib = SectorMeshLibrary(SetAssets(1).load())
    doc = LdfDocument(mw=7, mh=7)
    doc.host_stations = [host()]
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    code = -(doc.mw * doc.mh + 1)
    scene = WorldScene()
    scene.set_library(lib)
    scene.update(doc, terrain)
    assert np.any(scene.chunks[-1, -1].opaque[:, 11] == code)
    position = next(host_members(doc, terrain, lib)).position
    camera = IsoCamera(center=position, zoom=.18, width=400, height=320)
    frame = render_scene(scene_polygons(lib, doc, terrain, camera), camera, lib.tables)
    assert np.any(frame.cell_ids == code)


def test_3ds_legacy_absolute_texture_uses_sibling(app, tmp_path):
    from PIL import Image
    lib = SectorMeshLibrary(SetAssets(1).load())
    material = chunk(0xAFFF, chunk(0xA000, b'paint\0') +
                     chunk(0xA200, chunk(0xA300, b'C:\\old-machine\\texture.PNG\0')))
    vertices = chunk(0x4110, struct.pack('<H9f', 3, 0, 0, 0, 100, 0, 0, 0, 100, 200))
    faces = chunk(0x4120, struct.pack('<H4H', 1, 0, 1, 2, 0) +
                  chunk(0x4130, b'paint\0' + struct.pack('<HH', 1, 0)))
    uvs = chunk(0x4140, struct.pack('<H6f', 3, 0, 0, 1, 0, 0, 1))
    path = tmp_path / 'custom.3ds'
    path.write_bytes(chunk(0x4D4D, chunk(0x3D3D, material +
                     chunk(0x4000, b'custom\0' + chunk(0x4100, vertices + faces + uvs)))))
    Image.new('RGB', (2, 2), 'red').save(tmp_path / 'texture.png')
    face = next(lib._three_ds_faces(path))
    assert lib._external_surfaces[face.material].kind == 'texture'
