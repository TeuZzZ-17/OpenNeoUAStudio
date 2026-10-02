import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from map_editor.core.ldf_model import LdfDocument
from map_editor.ui.main_window import MainWindow
from map_editor.render.camera import IsoCamera, ISO_YAW, ISO_PITCH
from map_editor.render.gpu_renderer import camera_matrix

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def win(app):
    window=MainWindow(LdfDocument(mw=9,mh=9))
    window._icons.stop()
    yield window
    window.dirty=False
    window.close()
    window.view._pool.waitForDone(10000)

def test_grid_copy_preview_placement_and_undo(win):
    win.doc.grids['type'][2][2]='05'
    win.doc.grids['blg'][2][2]='03'
    win.doc.grids['type'][2][3]='04'
    win.view.selection={(2,2),(3,2)}
    win.set_tool('sector')
    before=win.doc.snapshot()
    win._copy_elements()
    assert win.view.draft_active
    win._paste_elements()
    assert win.doc.snapshot()==before
    assert win.view.draft_active
    win._hovered(5,5)
    assert win.view.doc.grids['type'][5][5]=='05'
    assert win.view.doc.grids['type'][5][6]=='04'
    win._confirm_placement((5,5))
    assert win.doc.grids['blg'][5][5]=='03'
    assert not win.view.draft_active
    win.undo()
    assert win.doc.snapshot()==before
    win.redo()
    assert win.doc.grids['type'][5][6]=='04'

def test_group_squad_clipboard_is_independent_and_cancel_does_not_write(win):
    win.doc.squads=[dict(owner=1,veh=1,num=1,x=3,y=3,hidden=False,useable=False,custom_name=None),
                    dict(owner=2,veh=1,num=2,x=4,y=3,hidden=False,useable=False,custom_name='second')]
    win._refresh_squads({0,1})
    win.set_tool('squad')
    win._copy_elements()
    win.doc.squads[0]['num']=3
    win._paste_elements()
    assert win._draft_squads[0]['num']==1
    before=win.doc.snapshot()
    win._cancel_operation(clear=False)
    assert win.doc.snapshot()==before
    win._paste_elements()
    win._confirm_placement((4,4))
    assert len(win.doc.squads)==4
    assert win.doc.squads[3]['x']-win.doc.squads[2]['x']==1
    win.undo()
    assert win.doc.snapshot()==before

@pytest.mark.parametrize('tool,layer,value',[('owner','own','02'),('terrain','hgt',35)])
def test_faction_and_height_paste_preserve_other_layers(win,tool,layer,value):
    win.doc.grids[layer][2][2]=value
    win.view.selection={(2,2)}
    win.set_tool(tool)
    before=win.doc.snapshot()
    other={name:grid[5][5] for name,grid in win.doc.grids.items() if name!=layer}
    win._copy_elements()
    win._paste_elements()
    assert win.doc.snapshot()==before
    win._confirm_placement((5,5))
    assert win.doc.grids[layer][5][5]==value
    assert all(win.doc.grids[name][5][5]==entry for name,entry in other.items())
    win.undo()
    assert win.doc.snapshot()==before

def test_level_info_and_script_clipboards_use_document_history(win):
    win.palette_tabs.setCurrentIndex(win.level_tab_index)
    win.doc.lvl_info['title']='Copied title'
    win._copy_elements()
    win.doc.lvl_info['title']='Other title'
    before=win.doc.snapshot()
    win._paste_elements()
    assert win.doc.lvl_info['title']=='Copied title'
    win.undo()
    assert win.doc.snapshot()==before
    win.palette_tabs.setCurrentIndex(win.script_tab_index)
    win.script_edit.setPlainText('; custom command')
    win._finish_script()
    win.script_edit.selectAll()
    win._copy_elements()
    win.script_edit.moveCursor(win.script_edit.textCursor().MoveOperation.End)
    before=win.doc.snapshot()
    win._paste_elements()
    assert win.script_edit.toPlainText()=='; custom command; custom command'
    win._released(-1,-1,Qt.KeyboardModifier.NoModifier)
    win.undo()
    assert win.doc.snapshot()==before

def test_comment_and_camera_reset_are_live_without_map_mutation(win,app):
    win.doc.squads=[dict(owner=1,veh=1,num=1,x=3,y=3,hidden=False,useable=False,custom_name=None)]
    win._refresh_squads({0})
    win.squad_panel.advanced.setChecked(True)
    QTest.keyClicks(win.squad_panel.name,'test comment')
    assert 'test comment' in win.squad_panel.list.item(0).text()
    before=win.doc.snapshot()
    win._squad_pov(0)
    assert win.view.camera.perspective
    assert win.view.camera.yaw==180
    assert win.view.camera.center[1]<win.view.terrain.cell_y(3,3)
    win.reset_camera_button.click()
    assert not win.view.camera.perspective
    assert (win.view.camera.yaw,win.view.camera.pitch)==(ISO_YAW,ISO_PITCH)
    assert win.view.camera.center==(5400,0,-5400)
    assert win.doc.snapshot()==before

def test_shift_sweep_adds_crossed_cells_without_rectangle_or_paint(win,app):
    win.view.pick_cell=lambda x,y:(int(x)//20+1,2)
    win.view.ground_cell=win.view.pick_cell
    win.view.pick_squad=lambda x,y:None
    win.set_tool('sector')
    before=win.doc.snapshot()
    win.show(); app.processEvents()
    QTest.mousePress(win.view,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,QPoint(20,30))
    QTest.mouseMove(win.view,QPoint(60,30))
    QTest.mouseRelease(win.view,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.ShiftModifier,QPoint(60,30))
    assert {(2,2),(3,2),(4,2)}<=win.view.selection
    assert not hasattr(win.view,'selection_box')
    assert win.doc.snapshot()==before

def test_perspective_cpu_and_gpu_projection_agree_and_ground_ray_returns_point():
    cam=IsoCamera(yaw=20,pitch=12,center=(2500,-200, -3000),width=800,height=600,perspective=True)
    point=(2500,0,-6000)
    expected,_=cam.world_to_screen(point)
    clip=camera_matrix(cam,(9,9))@np.array((*point,1))
    ndc=clip[:3]/clip[3]
    actual=((ndc[0]+1)*cam.width/2,(1-ndc[1])*cam.height/2)
    assert actual==pytest.approx(expected,abs=.0002)
    ray=cam.screen_to_ground(*expected)
    assert ray==pytest.approx(point,abs=1e-5)
    assert cam.copy().perspective

@pytest.mark.parametrize('tool,layer,value',[('sector','type','05'),('owner','own',1),('building','blg',None)])
def test_left_drag_paints_crossed_cells_in_one_undo(win,app,tool,layer,value):
    if tool=='building':
        win.sel_building=next(iter(win.buildings))
        value=f'{win.buildings[win.sel_building].id:02x}'
    win.sel_typ=5
    win.sel_owner=1
    win.view.pick_cell=lambda x,y:(int(x)//20+1,2)
    win.view.ground_cell=win.view.pick_cell
    win.view.pick_squad=lambda x,y:None
    win.set_tool(tool)
    before=win.doc.snapshot()
    win.show(); app.processEvents()
    QTest.mousePress(win.view,Qt.MouseButton.LeftButton,pos=QPoint(20,30))
    QTest.mouseMove(win.view,QPoint(80,30))
    QTest.mouseRelease(win.view,Qt.MouseButton.LeftButton,pos=QPoint(80,30))
    assert all(win.doc.grids[layer][2][c]==value for c in range(2,6))
    assert not win._stroke_active
    assert len(win.history._undo)==1
    win.undo()
    assert win.doc.snapshot()==before
    win.redo()
    assert all(win.doc.grids[layer][2][c]==value for c in range(2,6))

@pytest.mark.parametrize('shape',['Round','Diamond','Ring'])
def test_terrain_preset_ignores_previous_selection(win,shape):
    win.view.selection={(1,1),(1,2),(7,7)}
    win.set_tool('terrain')
    win.shape_combo.setCurrentText(shape)
    win.radius_x_slider.setValue(2)
    expected={(c,r) for c,r,_ in win.brush.footprint(win.doc,4,4)}
    win._hovered(4,4)
    assert win.view.brush_cells==expected
    before=win.doc.snapshot()
    win._pressed(4,4,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    win._released(4,4,Qt.KeyboardModifier.NoModifier)
    changed={(c,r) for r in range(win.doc.mh) for c in range(win.doc.mw)
             if before['grids']['hgt'][r][c]!=win.doc.grids['hgt'][r][c]}
    interior={(c,r) for c,r in changed if 1<=c<win.doc.mw-1 and 1<=r<win.doc.mh-1}
    assert interior==expected
    win.undo()
    assert win.doc.snapshot()==before

def test_script_is_penultimate_and_list_click_centers_only_camera(win,app):
    assert win.palette_tabs.tabText(win.palette_tabs.count()-2)=='Script'
    assert win.palette_tabs.tabText(win.palette_tabs.count()-1)=='Level Info'
    win.doc.squads=[dict(owner=1,veh=1,num=2,x=3,y=3,hidden=False,useable=False,custom_name=None)]
    win._refresh_squads({0})
    win.set_tool('squad')
    win.show(); app.processEvents()
    before=win.doc.snapshot()
    cam=win.view.camera.copy()
    win._select_squad(0)
    assert win.view.camera.center==cam.center
    item=win.squad_panel.list.item(0)
    QTest.mouseClick(win.squad_panel.list.viewport(),Qt.MouseButton.LeftButton,
                    pos=win.squad_panel.list.visualItemRect(item).center())
    assert win.view.camera.center!=cam.center
    assert (win.view.camera.zoom,win.view.camera.yaw,win.view.camera.pitch)==(cam.zoom,cam.yaw,cam.pitch)
    assert win.doc.snapshot()==before

def test_copy_starts_grid_preview_without_modifying_source(win):
    win.doc.grids['type'][2][2]='05'
    win.view.selection={(2,2),(3,2)}
    win.set_tool('sector')
    before=win.doc.snapshot()
    win._copy_elements()
    assert win.view.draft_active and win.view.preview_cells
    assert win.doc.snapshot()==before
    win._cancel_operation(clear=False)
    assert not win.view.draft_active
    assert win.doc.snapshot()==before

def test_pov_faces_vehicle_forward_and_right_click_exits(win,app):
    win.doc.squads=[dict(owner=1,veh=1,num=1,x=3,y=3,hidden=False,useable=False,custom_name=None)]
    win._refresh_squads({0})
    win.show(); app.processEvents()
    before=win.doc.snapshot()
    win._squad_pov(0)
    cam=win.view.camera
    x,y,z=cam.center
    ahead=cam.to_camera((x,y,z+1000))
    behind=cam.to_camera((x,y,z-1000))
    assert ahead[2]<4<behind[2]
    QTest.mouseClick(win.view,Qt.MouseButton.RightButton,pos=QPoint(60,60))
    assert win._context_menu.actions()[0].text()=='Exit POV'
    win._context_menu.actions()[0].trigger()
    win._context_menu.close()
    assert not win.view.camera.perspective
    assert win.doc.snapshot()==before

@pytest.mark.parametrize('direction',[(0,0,1),(1,0,0),(0,0,-1),(0,-1,0)])
def test_gun_pov_uses_rendered_mount_position_and_forward(win,direction):
    from copy import deepcopy
    from map_editor.render.sector_mesh import gun_rotation
    definition=deepcopy(next(b for b in win.buildings.values() if b.guns))
    definition.guns[0].direction=direction
    win.buildings[definition.id]=definition
    win.doc.grids['type'][4][4]=f'{definition.sec_type:02x}'
    win.doc.grids['blg'][4][4]=f'{definition.id:02x}'
    win.view.terrain.rebuild(win.doc.grids['hgt'])
    before=win.doc.snapshot()
    win._gun_pov((4,4),0)
    mount=definition.guns[0]
    rotation=gun_rotation(direction)
    origin=win.view.terrain.cell_center(4,4)
    offset=rotation @ (0,win.view.lib.vehicle_mesh(mount.vehicle).bounds[1]-30,0)
    expected=tuple(origin[a]+mount.pos[a]+offset[a] for a in range(3))
    assert win.view.camera.center==pytest.approx(expected)
    ahead=win.view.camera.to_camera(tuple(expected[a]+500*direction[a] for a in range(3)))
    assert ahead[:2]==pytest.approx((0,0),abs=1e-6)
    assert ahead[2]<4
    assert win.doc.snapshot()==before
