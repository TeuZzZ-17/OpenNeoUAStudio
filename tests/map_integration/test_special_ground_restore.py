from types import SimpleNamespace
import pytest
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.special_objects import add_special, update_special, remove_special, set_item_key_road, special_store

BUILDINGS={50:SimpleNamespace(sec_type=123),51:SimpleNamespace(sec_type=124)}
ACTIONS=[{'target_type':'modify_vehicle','id':1,'param':'enable','val':'1'}]


def place(doc,kind,x=3,y=3):
    slot=add_special(doc,kind)
    values={'x':x,'y':y}
    if kind=='gem':values.update(blg=50,actions=ACTIONS)
    update_special(doc,kind,slot,values,BUILDINGS)
    return slot


@pytest.mark.parametrize('kind',['gate','item','gem'])
@pytest.mark.parametrize('reopen',[False,True])
def test_moving_and_removing_special_recovers_each_original_sector(kind,reopen):
    doc=LdfDocument(mw=9,mh=9)
    doc.grids['type'][3][3]='a1';doc.grids['type'][4][4]='b2'
    doc.grids['hgt'][3][3]+=8;doc.grids['hgt'][4][4]+=5
    heights=doc.grids['hgt'][3][3],doc.grids['hgt'][4][4]
    slot=place(doc,kind)
    if reopen:doc=loads_ldf(dumps_ldf(doc))
    update_special(doc,kind,slot,{'x':4,'y':4},BUILDINGS)
    assert (doc.grids['type'][3][3],doc.grids['blg'][3][3])==('a1','00')
    if reopen:doc=loads_ldf(dumps_ldf(doc))
    remove_special(doc,kind,slot,BUILDINGS)
    assert (doc.grids['type'][4][4],doc.grids['blg'][4][4])==('b2','00')
    assert (doc.grids['hgt'][3][3],doc.grids['hgt'][4][4])==heights


@pytest.mark.parametrize('kind',['gate','item','gem'])
def test_special_preserves_building_and_its_own_ground_history(kind):
    doc=LdfDocument(mw=9,mh=9);doc.grids['type'][3][3]='a1'
    doc.remember_building_ground(3,3,1)
    doc.grids['type'][3][3]='b2';doc.grids['blg'][3][3]='01'
    slot=place(doc,kind)
    doc=loads_ldf(dumps_ldf(doc))
    remove_special(doc,kind,slot,BUILDINGS)
    assert (doc.grids['type'][3][3],doc.grids['blg'][3][3])==('b2','01')
    doc.remove_building(3,3)
    assert doc.grids['type'][3][3]=='a1'


def test_item_key_recovers_ground_after_road_toggle_move_save_and_delete():
    doc=LdfDocument(mw=9,mh=9);doc.grids['type'][4][4]='a1';doc.grids['type'][5][5]='b2'
    slot=place(doc,'item')
    update_special(doc,'item',slot,{'keys':[(4,4)]})
    set_item_key_road(doc,slot,0,True)
    doc=loads_ldf(dumps_ldf(doc))
    update_special(doc,'item',slot,{'keys':[(5,5)]})
    assert doc.grids['type'][4][4]=='a1'
    assert doc.grids['type'][5][5]=='f4'
    doc=loads_ldf(dumps_ldf(doc));remove_special(doc,'item',slot)
    assert doc.grids['type'][5][5]=='b2'


def test_manual_terrain_edit_takes_priority_over_special_backup():
    doc=LdfDocument(mw=9,mh=9);doc.grids['type'][3][3]='a1'
    slot=place(doc,'item');doc.grids['type'][3][3]='c3'
    update_special(doc,'item',slot,{'x':4,'y':4})
    assert doc.grids['type'][3][3]=='c3'


def test_comment_history_follows_objects_when_unplaced_slots_are_omitted():
    doc=LdfDocument(mw=9,mh=9);add_special(doc,'gate')
    doc.grids['type'][3][3]='a1';slot=place(doc,'gate')
    assert slot==2
    restored=loads_ldf(dumps_ldf(doc))
    remove_special(restored,'gate',1)
    assert restored.grids['type'][3][3]=='a1'


from .test_special_objects import app, win
from PySide6.QtCore import Qt


def test_flak_same_loaded_id_still_remembers_the_original_skyscraper(win):
    bid=next(key for key,b in win.buildings.items() if b.has_guns and not b.is_radar)
    win.doc.grids['type'][3][3]='1b';win.doc.grids['blg'][3][3]=f'{bid:02x}'
    win.set_tool('building');win.building_list.setCurrentItem(win._bicon_items[bid])
    win._pressed(3,3,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    win._released(3,3,Qt.KeyboardModifier.NoModifier)
    assert win.doc.building_underlays[3,3][0]=='1b'
    win.doc=loads_ldf(dumps_ldf(win.doc));before=win.doc.snapshot()
    win._delete_building_at((3,3))
    assert (win.doc.grids['type'][3][3],win.doc.grids['blg'][3][3])==('1b','00')
    win.undo();assert win.doc.snapshot()==before


def test_flak_moves_twice_and_recovers_each_destination_ground(win):
    bid=next(key for key,b in win.buildings.items() if b.has_guns and not b.is_radar)
    win.doc.grids['type'][3][3]='1b';win.doc.grids['type'][4][4]='a1'
    win.set_tool('building');win.building_list.setCurrentItem(win._bicon_items[bid])
    win._pressed(3,3,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    win._released(3,3,Qt.KeyboardModifier.NoModifier)
    win._begin_actor_drag('building',(3,3));win._move_actor_drag((4,4));win.view._moved=True
    win._finish_building_drag()
    assert win.doc.grids['type'][3][3]=='1b'
    win.doc=loads_ldf(dumps_ldf(win.doc));win._delete_building_at((4,4))
    assert win.doc.grids['type'][4][4]=='a1'
