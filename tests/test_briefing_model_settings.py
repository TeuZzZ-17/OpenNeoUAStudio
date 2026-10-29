import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication,QWidget
from entity_designer.script_model import *
from entity_designer.briefing import BriefingControls,BriefingPreview

def test_xyz_every_writer_and_nested_preservation():
    source='new_vehicle 1\nname = One\nbriefing_size = 50\nbriefing_xyz = 1_2_3\nbegin_fx\noffset_xyz = 9_8_7\nend\nend\n'
    p=CollisionProject(name='One',target_category=VEHICLE,briefing_xyz_enabled=True,briefing_x=-12.5,briefing_y=30,briefing_z=4)
    for canonical in (False,True):
        text,_,_=plan_script_update(source,'new_vehicle',1,p,replace_all_managed=canonical)
        assert text.count('briefing_xyz = -12.5_30_4')==1
        assert 'offset_xyz = 9_8_7' in text
        assert import_briefing_xyz_block(text,find_script_blocks(text)[0])==(True,-12.5,30,4)
    patch=build_editable_overwrite_preview('new_vehicle',1,p,{'briefing'})
    text,groups=apply_editable_overwrite_preview(source,patch,'new_vehicle',1)
    assert groups=={'briefing'} and 'briefing_xyz = -12.5_30_4' in text
    state=p.snapshot();p.briefing_x=88;p.restore(state);assert p.briefing_x==-12.5
    p.briefing_xyz_enabled=False
    text,_,_=plan_script_update(source,'new_vehicle',1,p,replace_all_managed=True)
    assert 'briefing_xyz' not in text and 'offset_xyz = 9_8_7' in text
    p.target_category=WEAPON;p.briefing_xyz_enabled=True
    assert 'briefing_xyz' not in export_collision_text(p)

def test_xyz_import_bounds_and_invalid_keep_previous():
    for value in ('nan_0_0','1_2','1__3','1_2_3_4','100001_0_0','1x_0_0'):
        text=f'new_vehicle 1\nbriefing_xyz = 3_4_5\nbriefing_xyz = {value}\nend\n'
        assert import_briefing_xyz_block(text,find_script_blocks(text)[0])==(True,3,4,5)
    text='new_vehicle 1\nbriefing_xyz = -100000_100000_0\nend\n'
    assert import_briefing_xyz_block(text,find_script_blocks(text)[0])==(True,-100000,100000,0)

def test_briefing_marker_backwards_compatibility():
    p=CollisionProject(target_category=VEHICLE,briefing_xyz_enabled=True,briefing_x=2)
    text='new_vehicle 1\nend\n'
    patch=build_editable_overwrite_preview('new_vehicle',1,p,{'briefing'})
    for label in ('Briefing Model Settings','Briefing Model Size','Briefing Radius'):
        result,_=apply_editable_overwrite_preview(text,patch.replace('Briefing Model Settings',label),'new_vehicle',1)
        assert 'briefing_xyz = 2_0_0' in result

class Editor(QWidget):
    def __init__(self):
        super().__init__();self.project=CollisionProject(target_category=VEHICLE);self._syncing=True
        self.undo=[];self.modified=0;self.briefing_preview=SimpleNamespace(refresh=lambda:None)
    def _push_undo(self):self.undo.append(self.project.snapshot())
    def _set_modified(self):self.modified+=1
    def _sync_all(self):pass

def test_arrows_step_slider_single_undo_and_reset():
    app=QApplication.instance() or QApplication([])
    e=Editor();c=BriefingControls(e);c.sync();e._syncing=False
    c.xyz_step_slider.setValue(300)
    assert c.arrow_step()==10
    c.xyz_buttons[('x',1)].click();assert e.project.briefing_x==10 and e.project.briefing_xyz_enabled and len(e.undo)==1
    c.xyz_buttons[('y',-1)].click();assert e.project.briefing_y==-10 and len(e.undo)==2
    c.xyz_values['z'].setValue(25);assert e.project.briefing_z==25
    count=len(e.undo);before=e.project.snapshot();c._reset_xyz()
    assert len(e.undo)==count+1
    assert not e.project.briefing_xyz_enabled and (e.project.briefing_x,e.project.briefing_y,e.project.briefing_z)==(0,0,0)
    e.project.restore(e.undo[-1]);assert e.project.snapshot()==before
    e.close()

def test_preview_camera_axes_and_depth_match_engine():
    app=QApplication.instance() or QApplication([])
    e=Editor();view=BriefingPreview(e);view.radius=40;view.angle=37
    classic=view.renderer._camera_vertex((1,2,3))
    e.project.briefing_xyz_enabled=True;e.project.briefing_x=15;e.project.briefing_y=20;e.project.briefing_z=30
    moved=view.renderer._camera_vertex((1,2,3))
    assert abs(moved[0]-classic[0]-24)<1e-9
    assert abs(moved[1]-classic[1]+32)<1e-9
    assert abs(moved[2]-classic[2]+48)<1e-9
    anchor = view.renderer._camera_vertex((0,0,0))
    projected = (anchor[0]/(4-anchor[2]), anchor[1]/(4-anchor[2]))
    for size in (1, 9, 50, 100, 10000):
        view.radius = 4000 / size
        point = view.renderer._camera_vertex((0,0,0))
        assert abs(point[0]/(4-point[2])-projected[0])<1e-9
        assert abs(point[1]/(4-point[2])-projected[1])<1e-9
    view.radius = 40
    e.project.briefing_xyz_enabled=False
    assert view.renderer._camera_vertex((1,2,3))==classic
    view.close();e.close()


def test_briefing_xyz_written_with_six_digits_is_not_reported_as_unsaved():
    from entity_designer.editor import CollisionEditorWindow
    from entity_designer.script_model import _SCRIPT_TAB_BRIEFING

    # The arrow step is logarithmic, so positions are long decimals: -5.347505628802473.
    project = CollisionProject(target_category=VEHICLE, briefing_xyz_enabled=True,
                               briefing_y=-5.347505628802473)
    reloaded = CollisionProject(target_category=VEHICLE, briefing_xyz_enabled=True,
                                briefing_y=float(f"{project.briefing_y:g}"))
    state = CollisionEditorWindow._script_tab_state
    assert state(project, _SCRIPT_TAB_BRIEFING) == state(reloaded, _SCRIPT_TAB_BRIEFING)
