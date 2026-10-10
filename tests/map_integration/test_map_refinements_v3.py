from pathlib import Path
import math
import pytest
import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QSignalSpy, QTest

from .test_special_objects import app, win
from map_editor import bootstrap
from map_editor.core.ldf_model import LdfDocument, loads_ldf, dumps_ldf, ensure_host_defaults, HGT_MIN
from map_editor.core.special_objects import add_special, update_special
from map_editor.render.camera import IsoCamera
from map_editor.render.gpu_renderer import camera_matrix
from map_editor.render.gpu_scene import WorldScene
from map_editor.render.sector_state import sector_type
from map_editor.ui.game_icons import game_icon
from map_editor.tools.brush import BrushMode


def test_title_includes_full_path_set_extension_and_dirty_marker(win):
    win.path = str(bootstrap.game_data_dir()/'Levels/Single/L0101.LDF')
    win.doc.set_number = 3
    win.dirty = True
    win._refresh_title()
    assert win.windowTitle().startswith('*'+str(Path(win.path).absolute()))
    assert 'Set 3' in win.windowTitle() and 'L0101.LDF' in win.windowTitle()


def test_hover_height_and_flatten_sample_are_explicit_without_editing_map(win):
    win.doc.grids['hgt'][3][4] = HGT_MIN + 42
    before = win.doc.snapshot()
    win._hovered(4, 3)
    assert '(4, 3)' in win.hover_height.text() and '42/60' in win.hover_height.text()
    assert not win.flatten_height.isEnabled()
    win._sample_map_height(4, 3)
    assert win.mode_combo.currentIndex() == 2 and win.flatten_height.isEnabled()
    assert win.flatten_height.value() == 42
    assert win.doc.snapshot() == before
    win._hovered(-1, -1)
    assert win.hover_height.text().endswith('—')


def test_host_viewangle_roundtrip_and_pov_follows_viewangle(win):
    host = dict(owner=1, veh=56, x=3, y=3, pos_y=-700, energy=500000,
                custom_name=None, hidden=False, viewangle=15)
    ensure_host_defaults(host)
    win.doc.host_stations = [host]
    saved = dumps_ldf(win.doc)
    assert 'body_angle' not in saved
    restored = loads_ldf(saved)
    assert restored.host_stations[0]['viewangle'] == 15
    win._refresh_squads()
    scene = WorldScene(); scene.set_library(win._lib())
    geometry = scene._squads(win.doc, win.view.terrain)
    assert len(geometry.opaque)
    win._host_pov(0)
    angle = math.radians(15)
    eye = win.view.camera.center
    ahead = tuple(eye[i]+1000*v for i,v in enumerate((-math.sin(angle),0,math.cos(angle))))
    assert win.view.camera.to_camera(ahead)[2] < 4


def test_game_projection_keeps_right_axis_and_cpu_gpu_screen_in_sync():
    for angle in (0, 45, 90, 180):
        radians = math.radians(angle)
        cam = IsoCamera(yaw=180+angle, pitch=0, width=800,height=600,
                        perspective=True,center=(1000,-700,-2000))
        forward = (-math.sin(radians),0,math.cos(radians))
        right = (math.cos(radians),0,math.sin(radians))
        point = np.asarray(cam.center)+np.asarray(forward)*1000
        assert cam.world_to_screen(point+np.asarray(right)*100)[0][0] > cam.width/2
        clip = camera_matrix(cam,(10,10)) @ np.append(point+np.asarray(right)*100,1)
        expected = ((clip[0]/clip[3]+1)*cam.width/2, (1-clip[1]/clip[3])*cam.height/2)
        assert np.allclose(expected,cam.world_to_screen(point+np.asarray(right)*100)[0],atol=.001)
    cam = IsoCamera(yaw=-45,pitch=35.26,center=(2000,0,-2000))
    point = (3200,0,-1400)
    assert np.allclose(cam.screen_to_ground(*cam.world_to_screen(point)[0]),point)


def test_loaded_building_resolves_prototype_geometry_without_changing_grids(win):
    win.doc.grids['type'][3][3] = '00'
    win.doc.grids['blg'][3][3] = '02'
    win.doc.grids['own'][3][3] = 1
    before = win.doc.snapshot()
    lib = win._lib()
    assert sector_type(win.doc,lib,3,3) == lib.buildings[2].sec_type
    scene = WorldScene(); scene.set_library(lib); scene.update(win.doc,win.view.terrain)
    assert ('sector',lib.buildings[2].sec_type,False) in scene.instances
    assert win.doc.snapshot() == before


def test_special_coords_model_countdown_update_immediately_and_undo_as_one_edit(win):
    slot = add_special(win.doc,'gate')
    update_special(win.doc,'gate',slot,dict(x=2,y=2,target=3),win.buildings)
    win._refresh_squads(); win.palette_tabs.setCurrentIndex(win.special_tab_indices['gate'])
    panel = win.special_panels['gate'];panel.refresh(win.doc,slot)
    before = win.doc.snapshot()
    panel.fields['x'].setValue(4)
    panel.fields['closed_bp'].setValue(5)
    assert win.doc.gates[slot]['x'] == 4 and win.doc.gates[slot]['closed_bp'] == 5
    assert win.view.doc.gates[slot]['x'] == 4
    win.undo()
    assert win.doc.snapshot() == before


def test_real_key_glyphs_draw_without_parent_selection_and_pick_by_cell(win):
    slot=add_special(win.doc,'gate')
    update_special(win.doc,'gate',slot,dict(x=2,y=2,keys=[(4,4)]),win.buildings)
    win._refresh_squads();win.view.selected_special=None
    icon=game_icon('gate_key',tight=False)
    assert (icon.width(),icon.height()) == (64,64)
    pixels=np.frombuffer(icon.bits(),np.uint8).reshape(64,64,4)
    assert np.any(pixels[:,:,3] == 0) and np.any(pixels[:,:,3] > 0)
    image=QImage(800,600,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.transparent)
    painter=QPainter(image)
    cam=IsoCamera(width=800,height=600,zoom=.10,center=(5400,0,-5400))
    win.special_overlay.draw(painter,cam,win.view.terrain);painter.end()
    assert np.any(np.frombuffer(image.bits(),np.uint8))
    assert win.special_overlay.pick((4,4)) == ('gate',slot,0)
    for kind in ('power','radar','flak','gate','gem','item'):
        assert not game_icon(kind).isNull()


def test_gate_glyph_drops_atlas_edge_before_tight_crop_and_centers_in_qpainter():
    for grey in (False, True):
        loose = game_icon('gate', tight=False, grey=grey)
        assert (loose.width(), loose.height()) == (64, 64)
        rgba = np.frombuffer(loose.bits(), np.uint8).reshape(
            loose.height(), loose.bytesPerLine())[:, :loose.width()*4].reshape(64, 64, 4)
        assert np.all(rgba[:, 63, 3] == 0)
        assert not np.any((rgba[:, 63, :3] == (0, 52, 255)).all(axis=1) & (rgba[:, 63, 3] > 0))

        tight = game_icon('gate', tight=True, grey=grey)
        assert (tight.width(), tight.height()) == (40, 43)
        glyph = np.frombuffer(tight.bits(), np.uint8).reshape(
            tight.height(), tight.bytesPerLine())[:, :tight.width()*4].reshape(43, 40, 4)
        ys, xs = np.where(glyph[:, :, 3] > 0)
        assert (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) == (0, 0, 40, 43)

        center = QPointF(64, 64)
        canvas = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        scale = 48 / max(tight.width(), tight.height())
        w, h = tight.width() * scale, tight.height() * scale
        painter = QPainter(canvas)
        painter.drawImage(QRectF(center.x() - w/2, center.y() - h/2, w, h), tight)
        painter.end()
        pixels = np.frombuffer(canvas.bits(), np.uint8).reshape(
            canvas.height(), canvas.bytesPerLine())[:, :canvas.width()*4].reshape(128, 128, 4)
        ys, xs = np.where(pixels[:, :, 3] > 0)
        assert abs((xs.min() + xs.max() + 1) / 2 - center.x()) <= 1
        assert abs((ys.min() + ys.max() + 1) / 2 - center.y()) <= 1

TERRAIN_OVERLAP_KINDS = ('gate', 'item', 'gem', 'host', 'squad')


def _place_test_actor_at_cell(win, kind, cell):
    if kind in ('gate', 'item', 'gem'):
        slot = add_special(win.doc, kind)
        changes = {'x': cell[0], 'y': cell[1]}
        if kind == 'gem':
            changes['actions'] = [{'target_type': 'modify_vehicle', 'id': 1,
                                   'param': 'enable', 'val': '1'}]
        update_special(win.doc, kind, slot, changes, win.buildings)
    elif kind == 'host':
        host = dict(owner=1, veh=56, x=cell[0], y=cell[1], energy=500000,
                    pos_y=-700, hidden=False, custom_name=None)
        ensure_host_defaults(host)
        win.doc.host_stations = [host]
    else:
        win.doc.squads = [dict(owner=1, veh=1, num=1, x=cell[0], y=cell[1],
                               hidden=False, useable=False, custom_name=None)]
    win._refresh_squads()
    win._refresh_hosts()
    win.host_panel.deselect()
    win.squad_panel.set_selection(set())
    win.view.selection.clear()
    win.view.selected_special = None


def _mock_actor_hits(monkeypatch, view, kind):
    calls = []
    special = ('special', (kind, 1, -1)) if kind in ('gate', 'item', 'gem') else None
    host = 0 if kind == 'host' else None
    squad = 0 if kind == 'squad' else None

    def pick(name, result):
        def _pick(*_args):
            calls.append(name)
            return result
        return _pick

    monkeypatch.setattr(view, 'pick_scene_object', pick('special', special))
    monkeypatch.setattr(view, 'pick_host', pick('host', host))
    monkeypatch.setattr(view, 'pick_squad', pick('squad', squad))
    return calls


def _actor_selection_state(win):
    return (win.host_panel.list.currentRow(), frozenset(win.squad_panel.selected_indices()),
            win.view.selected_special)


@pytest.mark.parametrize('kind', TERRAIN_OVERLAP_KINDS)
def test_terrain_click_over_actor_hit_paints_ground_without_actor_selection(win, app, monkeypatch, kind):
    cell = (4, 4)
    _place_test_actor_at_cell(win, kind, cell)
    win.set_tool('terrain')
    win.mode_combo.setCurrentIndex(win.mode_combo.findData(BrushMode.RAISE))
    win.doc.grids['hgt'][cell[1]][cell[0]] = HGT_MIN + 12
    height_before = win.doc.grids['hgt'][cell[1]][cell[0]]
    actor_state = _actor_selection_state(win)
    picks = _mock_actor_hits(monkeypatch, win.view, kind)
    ground_calls = []
    monkeypatch.setattr(win.view, 'ground_cell', lambda *args: (ground_calls.append(args) or cell))
    win.resize(1000, 700)
    win.show()
    app.processEvents()

    pressed = QSignalSpy(win.view.cellPressed)
    actor_drag = QSignalSpy(win.view.actorDragStarted)
    host_pressed = QSignalSpy(win.view.hostPressed)
    squad_pressed = QSignalSpy(win.view.squadPressed)
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=win.view.rect().center())

    assert pressed.count() == 1 and ground_calls
    assert win._last_cell == cell and win.view.selection == {cell}
    assert win.doc.grids['hgt'][cell[1]][cell[0]] != height_before
    assert win.palette_tabs.currentIndex() == 3 and win.tool == 'terrain'
    assert not picks
    assert actor_drag.count() == host_pressed.count() == squad_pressed.count() == 0
    assert _actor_selection_state(win) == actor_state


@pytest.mark.parametrize('kind', TERRAIN_OVERLAP_KINDS)
def test_terrain_context_menu_ignores_actor_hits_and_keeps_height_sampling(win, app, monkeypatch, kind):
    cell = (4, 4)
    _place_test_actor_at_cell(win, kind, cell)
    win.set_tool('terrain')
    win.doc.grids['hgt'][cell[1]][cell[0]] = HGT_MIN + 12
    actor_state = _actor_selection_state(win)
    picks = _mock_actor_hits(monkeypatch, win.view, kind)
    monkeypatch.setattr(win.view, 'ground_cell', lambda *_args: cell)
    win.resize(1000, 700)
    win.show()
    app.processEvents()
    position = QPoint(100, 100)

    win._map_context_menu(position, win.view.mapToGlobal(position))
    menu = win._context_menu
    try:
        assert win.palette_tabs.currentIndex() == 3 and win.tool == 'terrain'
        assert _actor_selection_state(win) == actor_state
        assert not picks
        sample = next(action for action in menu.actions() if action.text() == 'Sample this height')
        sample.trigger()
        assert win.flatten_height.value() == win.doc.grids['hgt'][cell[1]][cell[0]] - HGT_MIN
        assert win.palette_tabs.currentIndex() == 3 and win.tool == 'terrain'
        assert _actor_selection_state(win) == actor_state
    finally:
        menu.close()
        app.processEvents()


def test_terrain_double_click_uses_ground_cell_over_gate_hit(win, app, monkeypatch):
    cell = (4, 4)
    _place_test_actor_at_cell(win, 'gate', cell)
    win.set_tool('terrain')
    picks = _mock_actor_hits(monkeypatch, win.view, 'gate')
    ground_calls = []
    monkeypatch.setattr(win.view, 'ground_cell', lambda *args: (ground_calls.append(args) or cell))
    win.resize(1000, 700)
    win.show()
    app.processEvents()
    double_clicked = QSignalSpy(win.view.cellDoubleClicked)
    actor_drag = QSignalSpy(win.view.actorDragStarted)

    QTest.mouseDClick(win.view, Qt.MouseButton.LeftButton, pos=win.view.rect().center())

    assert double_clicked.count() == 1 and ground_calls
    assert not picks and actor_drag.count() == 0
    assert win.palette_tabs.currentIndex() == 3 and win.tool == 'terrain'
