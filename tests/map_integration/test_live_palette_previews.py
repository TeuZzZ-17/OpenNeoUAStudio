import numpy as np
import pytest
from PySide6.QtCore import Qt

from .test_special_objects import app, win  # noqa: F401
from map_editor.render.sector_sprite import sprite_input


def test_preview_levels_use_three_two_one_columns(win):
    from map_editor.ui.main_window import SECTOR_PREVIEW_COLUMNS
    assert SECTOR_PREVIEW_COLUMNS == (3, 2, 1)


def test_preview_angle_is_exact_and_refreshes_during_camera_interaction(win):
    win.view.camera.yaw = 12.345
    win.view.camera.pitch = 47.125
    assert win._camera_preview_angle() == (12.345, 47.125)
    win.view._interacting = True
    win.show()
    win._refresh_preview_angle()
    assert win._icon_angle == (12.345, 47.125)
    assert win._icons.isActive()
    assert win._icons.interval() <= 17


@pytest.mark.parametrize('angles', [(0, 90), (12.345, 35), (90, 8), (217, 60)])
def test_software_preview_fits_every_vertex_at_requested_resolution(win, angles):
    lib = win._lib()
    mesh = lib.mesh(0x1b)
    size = (450, 330)
    data = sprite_input(lib, mesh, *angles, .13, fast=True, target_size=size)
    width, height, origin, pieces = data
    assert (width, height) == size and pieces
    points = np.asarray([point for piece in pieces for point in piece.screen])
    assert (points.min(axis=0) >= 1).all()
    assert (points.max(axis=0) <= np.asarray(size) - 1).all()


def test_camera_refresh_queues_only_visible_active_cards(win, app):
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    win._icons.stop()
    win.palette_tabs.setCurrentIndex(0)
    win.view.camera.yaw += .123
    win._refresh_preview_angle()
    rect = win.sector_list.viewport().rect()
    expected = {typ for typ, item in win._icon_items.items()
                if not item.isHidden() and win.sector_list.visualItemRect(item).intersects(rect)}
    assert {typ for typ, item in win._icon_queue} == expected
    assert not win._bicon_queue


def test_palette_updates_do_not_wait_for_map_render_or_mouse_release(win, monkeypatch):
    win.show()
    win.view._interacting = True
    calls = []
    monkeypatch.setattr(win, '_make_sector_icon', lambda lib: (calls.append(True), win._icon_queue.pop(0)))
    monkeypatch.setattr(win, "_queue_visible_previews", lambda: None)
    win._icon_queue = [(0, None)]
    win._make_icon()
    assert calls


def test_noise_is_gentle_coherent_and_bounded_per_stroke():
    from map_editor.core.ldf_model import LdfDocument, HGT_MIN
    from map_editor.tools.brush import TerrainBrush, BrushMode
    doc = LdfDocument(mw=24, mh=24)
    for row in doc.grids['hgt']:
        row[:] = [HGT_MIN + 30] * len(row)
    brush = TerrainBrush(radius=9, strength=30)
    brush.begin_stroke(doc, 12, 12)
    brush._noise_phase = (0, 0)
    for _ in range(120):
        brush.apply(doc, 12, 12, BrushMode.NOISE)
    values = [doc.grids['hgt'][r][c] - HGT_MIN - 30
              for c, r, _ in brush.footprint(doc, 12, 12)]
    assert min(values) == -1 and max(values) == 1
    assert doc.grids['hgt'][2][2] == HGT_MIN + 30


def test_terrain_has_five_independent_tools(win):
    from map_editor.tools.brush import BrushMode
    assert [win.mode_combo.itemData(i) for i in range(win.mode_combo.count())] == list(BrushMode)
    for i, mode in enumerate(BrushMode):
        win.terrain_buttons[i].click()
        assert win._terrain_mode(Qt.KeyboardModifier.NoModifier) == mode


def test_finished_software_frame_is_published_even_after_camera_moves(win):
    from PySide6.QtGui import QImage
    from map_editor.render.sector_sprite import Sprite
    image = QImage(*win._preview_pixel_size(0), QImage.Format.Format_RGBA8888)
    image.fill(0xff557799)
    epoch = win._icon_epoch
    win._icon_angle = (epoch[1][0] + .123, epoch[1][1])
    win._icon_finished((win._palette_set, 0, win._icon_scale, epoch, Sprite(image,(0,0),1)))
    assert win._preview_sizes[(win._palette_set,0,0)] == epoch
    assert not win._icon_items[0].icon().isNull()


def test_squad_badge_uses_faction_and_can_be_picked(win, monkeypatch):
    import map_editor.ui.squad_overlay as module
    from PySide6.QtGui import QImage, QPainter
    from map_editor.render.special_scene import scene_object_styles
    win.doc.squads = [dict(owner=1,veh=1,num=12,x=4,y=4,hidden=False,useable=False,custom_name=None)]
    win._refresh_squads(set())
    assert scene_object_styles(win.view)[0][3] == 4
    badge = []
    monkeypatch.setattr(module, 'draw_game_badge', lambda painter,anchor,size,kind,color,moment: badge.append((anchor,size,kind,color)))
    image=QImage(win.view.size(),QImage.Format.Format_RGBA8888);image.fill(0)
    painter=QPainter(image);win.squad_overlay.draw(painter);painter.end()
    from map_editor.ui.game_icons import MAP_BADGE_SIZE
    assert len(badge)==1 and badge[0][1:3]==(MAP_BADGE_SIZE,'squad')
    assert badge[0][3].getRgb()[:3] == win.view.owner_colors[1]
    anchor=badge[0][0]
    assert win.squad_overlay.pick(anchor.x(),anchor.y()) == 0


def test_tech_special_buildings_filter_keeps_permissions_and_default_zoom(win):
    from map_editor.core.building_defs import is_special_building
    panel=win.tech_panel
    assert panel.list.preview_level==1
    panel.kind.setCurrentIndex(panel.kind.findData('blg'))
    assert not panel.show_special_buildings.isChecked()
    special=next(key for key,value in panel.buildings.items() if is_special_building(value))
    item=next(panel.list.item(i) for i in range(panel.list.count())
              if panel.list.item(i).data(Qt.ItemDataRole.UserRole)==special)
    assert item.isHidden()
    panel.show_special_buildings.setChecked(True)
    assert not item.isHidden()
    panel._clicked(item)
    enabled=list(win.doc.tech[panel.faction.currentData()]['blg'])
    assert special in enabled
    panel.show_special_buildings.setChecked(False)
    assert item.isHidden() and win.doc.tech[panel.faction.currentData()]['blg']==enabled
    assert 'white' in panel.status.styleSheet()


@pytest.mark.parametrize('vehicle',[69,70,152])
def test_vehicle_preview_fits_all_parts_in_the_requested_box(win, vehicle):
    size=(296,296)
    data=sprite_input(win._lib(),win._lib().actor_mesh(vehicle),-45,35.26,.23,fast=True,target_size=size)
    assert data[:2]==size and data[3]
    points=np.asarray([point for piece in data[3] for point in piece.screen])
    assert (points.min(axis=0)>=1).all()
    assert (points.max(axis=0)<=np.asarray(size)-1).all()


def test_resource_cache_keeps_requested_resolution_and_transparent_margin(win):
    from PySide6.QtGui import QImage
    image=QImage(296,296,QImage.Format.Format_RGBA8888);image.fill(0)
    image.setPixelColor(148,148,Qt.GlobalColor.white)
    key=(id(win._lib()),win._asset_epoch,'veh',1)
    win._resource_finished(key,image)
    assert win._resource_icons[key].availableSizes()[0].width()==296
