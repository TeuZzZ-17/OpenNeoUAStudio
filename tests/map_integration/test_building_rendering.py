from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter

from map_editor.core.building_defs import parse_building_text
from map_editor.core.vehicle_defs import parse_vehicle_text
from map_editor.core.asset_bridge import SetAssets
from map_editor.render.sector_mesh import SectorMeshLibrary, gun_rotation
from map_editor.render.gpu_scene import WorldScene
from map_editor.core.ldf_model import LdfDocument
from map_editor.render.terrain_mesh import TerrainMesh


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_nested_fx_does_not_truncate_building_mounts():
    text = '''new_building 1
model = kraftwerk
sec_type = 201
begin_fx
begin_visual
vp_model = 100
end
end
power = 128
sbact_act = 0
sbact_vehicle = 102
sbact_pos_y = -190
end
'''
    building = parse_building_text(text)[1]
    assert building.has_power and building.energy_levels == 3
    assert building.guns[0].vehicle == 102
    assert building.guns[0].pos == (0, -190, 0)


def test_vehicle_visual_ignores_nested_fx_and_applies_modify():
    result = parse_vehicle_text('''new_vehicle 102
vp_normal = 42
begin_fx
vp_normal = 99
end
base_normal = Data/Models/Base/Gun/Gun.BASE
end
modify_vehicle 102
vp_normal = 43
end
''')
    assert result[102].vp_normal == 43
    assert result[102].base_normal == 'Data/Models/Base/Gun/Gun.BASE'


def test_gun_direction_is_applied_without_changing_mount_height():
    for direction in ((0,0,1),(1,0,0),(1,-1,1),(0,1,0),(0,-1,0)):
        rotation = gun_rotation(direction)
        forward = np.asarray(direction)/np.linalg.norm(direction)
        assert np.allclose(rotation @ (0,0,1), forward)
        assert np.allclose(rotation.T @ rotation, np.eye(3))


def test_real_guns_share_map_geometry_and_building_preview(app):
    lib = SectorMeshLibrary(SetAssets(1).load())
    guns = lib.building_mesh(2)
    assert guns.faces and not guns.missing
    preview = lib.building_preview(2)
    assert len(preview.faces) == len(lib.mesh(lib.buildings[2].sec_type).faces) + len(guns.faces)
    doc = LdfDocument(mw=5,mh=5)
    doc.grids['type'][2][2] = f'{lib.buildings[2].sec_type:02x}'
    doc.grids['blg'][2][2] = '02'
    terrain = TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
    scene = WorldScene(); scene.set_library(lib); scene.update(doc,terrain)
    assert ('building',2,False) in scene.instances
    template, locations = scene.instances['building',2,False]
    assert len(template.opaque) and len(locations) == 1
    doc.grids['blg'][2][2] = '00'
    scene.update(doc,terrain)
    assert ('building',2,False) not in scene.instances


def test_external_base_gun_uses_real_family_and_missing_or_invalid_falls_back(app, tmp_path, monkeypatch):
    from map_editor import bootstrap
    from map_editor.core.vehicle_defs import VehicleVisual
    from map_editor.core.building_defs import BuildingDef, GunMount
    lib = SectorMeshLibrary(SetAssets(1).load())
    lib.buildings[250] = BuildingDef(250, guns=[GunMount(250)])
    lib.vehicles[250] = VehicleVisual(vp_normal=42,
        base_normal='Data/Models/Base/VP_MYKO2/VP_MYKO2.BASE')
    mesh = lib.building_mesh(250)
    assert mesh.faces and not mesh.missing
    assert all(face.material in lib._material_adapters for face,x,z in mesh.faces)
    assert all(lib.surface_for(face) is not None for face,x,z in mesh.faces)
    monkeypatch.setattr(bootstrap, 'game_data_dir', lambda: tmp_path)
    for filename in ('missing.BASE','invalid.BASE'):
        if filename == 'invalid.BASE':
            (tmp_path / filename).write_bytes(b'bad data')
        lib.vehicles[250].base_normal = f'Data/{filename}'
        lib._gun_meshes.pop(250)
        fallback = lib.building_mesh(250)
        assert fallback.faces and not fallback.missing


def test_invalid_and_hex_building_numbers_keep_safe_defaults():
    result = parse_building_text('''new_building 1
model = kraftwerk
sec_type = 0xc9
power = infinity
sbact_act = 0
sbact_vehicle = 0x66
end
''')[1]
    assert result.sec_type == 201 and not result.has_power
    assert result.guns[0].vehicle == 102


def test_gpu_building_preview_success_schedules_remaining_queue(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow(); win._icons.stop()
    lib = win._lib()
    image = QImage(4,4,QImage.Format.Format_RGBA8888); image.fill(0xff0088aa)
    win.view.render_icon = Mock(return_value=SimpleNamespace(image=image))
    win._bicon_queue = [1,2]
    win._make_building_icon(lib)
    assert win._bicon_queue == [2]
    assert win._icons.isActive()
    assert not win._bicon_rows[1].pixmap().isNull()
    win._icons.stop(); win.close()


def test_marker_color_comes_from_sector_owner_and_moves_with_camera(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow(); win._icons.stop()
    win.doc.grids['blg'][4][4] = '01'
    win.doc.grids['own'][4][4] = 1
    overlay = win.building_overlay
    captured = []
    overlay._draw_icons = lambda painter,anchor,definition,color,*args: captured.append((anchor,color.getRgb()[:3]))
    image = QImage(win.view.size(),QImage.Format.Format_RGBA8888)
    image.fill(0); painter = QPainter(image)
    camera = win.view.camera
    camera.zoom = .03
    camera.center = win.view.terrain.cell_center(4,4)
    overlay.draw(painter)
    camera.pan = (35,-20)
    overlay.draw(painter)
    painter.end()
    assert len(captured) == 2
    assert captured[0][1] == win.view.owner_colors[1]
    assert np.allclose(np.subtract(captured[1][0],captured[0][0]),(35,-20))
    win.close()


def test_palette_batch_finishes_multiple_icons_and_stops_on_hidden_rows(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow(); win._icons.stop()
    win.palette_dock.isVisible = lambda: True
    win.palette_tabs.setCurrentIndex(0)
    win.view._job = None; win.view._interacting = False
    win._icon_queue = [(1, None), (2, None), (3, None)]
    win._make_sector_icon = lambda lib: win._icon_queue.pop(0)
    win._make_icon()
    assert not win._icon_queue and not win._icons.isActive()
    win._icon_queue = [(1, None)]
    win._make_sector_icon = lambda lib: None
    win._make_icon()
    assert len(win._icon_queue) == 1 and not win._icons.isActive()
    win.close()


def test_building_resize_reuses_rendered_source_and_stale_results_are_ignored(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow(); win._icons.stop()
    image = QImage(180,180,QImage.Format.Format_RGBA8888); image.fill(0xff0088aa)
    win._bicon_finished(1, image)
    win._bicon_queue.clear()
    win.view.render_icon = Mock(side_effect=AssertionError('cached resize must not render'))
    win._building_zoom_step(1)
    assert not win._bicon_queue
    assert win._bicon_cache[(win._palette_set, 1, win.devicePixelRatioF())] is image
    assert not win._bicon_rows[1].pixmap().isNull()
    win._bicon_finished = Mock()
    win._bicon_from_sector(1, (win._palette_set, 0, 0, win._asset_epoch - 1,
                              SimpleNamespace(image=image)))
    win._bicon_finished.assert_not_called()
    win._icons.stop(); win.close()


def test_key_sector_and_flak_keep_both_sector_and_building_geometry(app):
    from map_editor.core.special_objects import add_special, update_special
    from map_editor.render.special_scene import special_cells
    lib = SectorMeshLibrary(SetAssets(1).load())
    doc = LdfDocument(mw=8, mh=8)
    slot = add_special(doc, 'item')
    update_special(doc, 'item', slot, {'x': 2, 'y': 2, 'keys': [(4, 4)]})
    doc.grids['blg'][4][4] = '02'
    member = special_cells(doc, lib)[(4, 4)][1]
    assert member.typ == 0xf3 and member.building == 2
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    scene = WorldScene()
    scene.set_library(lib)
    scene.update(doc, terrain)
    structure_key = ('key-building-sector', lib.buildings[2].sec_type, False)
    assert structure_key in scene.instances
    assert ('sector', 0xf3, False) in scene.instances
    assert ('building', 2, False) in scene.instances
