from pathlib import Path
from map_editor.core.ldf_model import LdfDocument,dumps_ldf,loads_ldf,save_ldf,load_ldf
import pytest

def original():
    doc=LdfDocument(mw=5,mh=5)
    doc.squads=[dict(owner=1,veh=4,num=1,hidden=False,useable=False,custom_name=None,x=2,y=2)]
    text=dumps_ldf(doc).replace('   num = 1','\t num  =  1 ; manual squad comment\n   authored_extra = 987')
    return '; personal header\n'+text.replace('   sky = objects/x7.bas','   sky = objects/x7.bas ; preserve this\n   custom_level_option = hello')+'\n; manual footer\nbegin_custom_data\n   value = 123\nend\n'

def test_no_change_is_identical():
    text=original()
    assert dumps_ldf(loads_ldf(text))==text

def test_adding_three_squads_only_inserts_blocks():
    text=original();doc=loads_ldf(text)
    for x in (1,2,3):
        doc.squads.append(dict(owner=2,veh=5,num=1,hidden=False,useable=False,custom_name=None,x=x,y=1))
    result=dumps_ldf(doc)
    assert 'authored_extra = 987' in result and 'begin_custom_data' in result
    # Removing only the three newly inserted squad blocks recovers every original byte.
    import re
    matches=list(re.finditer(r'(?ms)^begin_squad\r?\n.*?^end\r?\n',result))
    assert len(matches)==4
    for match in reversed(matches[1:]):result=result[:match.start()]+result[match.end():]
    assert result==text

def test_one_field_preserves_unknown_values_and_comment():
    text=original();doc=loads_ldf(text);doc.squads[0]['num']=3
    assert dumps_ldf(doc)==text.replace('\t num  =  1 ;','\t num  =  3 ;')

def test_one_cell_preserves_other_bytes():
    text=original();doc=loads_ldf(text);doc.grids['own'][2][2]=3
    result=dumps_ldf(doc)
    assert result.count('03')==text.count('03')+1
    assert loads_ldf(result).grids['own'][2][2]==3
    import difflib
    changes=[line for line in difflib.ndiff(text.splitlines(),result.splitlines()) if line.startswith(('- ','+ '))]
    assert len(changes)==2

def test_repeated_save_and_undo_preserve_source(tmp_path):
    path=tmp_path/'level.ldf';path.write_bytes(original().encode('cp1252'))
    doc=load_ldf(path);before=doc.snapshot();doc.squads[0]['num']=3
    save_ldf(doc,path);saved=path.read_bytes();save_ldf(doc,path);assert path.read_bytes()==saved
    doc.restore(before);save_ldf(doc,path);assert path.read_bytes()==original().encode('cp1252')

def test_external_changes_are_not_overwritten(tmp_path):
    path=tmp_path/'level.ldf';path.write_bytes(original().encode('cp1252'))
    doc=load_ldf(path);doc.squads[0]['num']=3
    external=path.read_bytes()+b'; added outside editor\n';path.write_bytes(external)
    with pytest.raises(ValueError,match='changed|modific'):
        save_ldf(doc,path)
    assert path.read_bytes()==external


def test_script_edit_does_not_join_or_erase_maps():
    text=original();doc=loads_ldf(text)
    doc.script_content='modify_vehicle 4\n  num_weapons = 3\nend'
    output=dumps_ldf(doc)
    restored=loads_ldf(output)
    assert 'num_weapons = 3' in restored.script_content
    assert restored.grids==doc.grids
    assert 'authored_extra = 987' in output
    assert 'begin_custom_data' in output


def test_remove_squad_only_removes_its_block():
    import re
    text=original();doc=loads_ldf(text);doc.squads.clear()
    result=dumps_ldf(doc)
    expected=re.sub(r'(?ms)^begin_squad\r?\n.*?^end\r?\n','',text)
    assert result==expected


def test_resize_replaces_only_grid_data():
    text=original();doc=loads_ldf(text);doc.resize(7,7)
    result=dumps_ldf(doc);restored=loads_ldf(result)
    assert restored.mw==7 and restored.mh==7
    assert 'custom_level_option = hello' in result
    assert 'authored_extra = 987' in result
    assert result.endswith('; manual footer\nbegin_custom_data\n   value = 123\nend\n')


def test_briefing_inserted_after_map_art_and_remove_is_reversible():
    doc=LdfDocument(mw=5,mh=5)
    doc.lvl_info['mbmap']='MB_10.IFF';doc.lvl_info['dbmap']='DB_10.IFF'
    text=dumps_ldf(doc);restored=loads_ldf(text)
    restored.lvl_info['briefing_text']='Manual mission briefing'
    output=dumps_ldf(restored)
    assert output.index('begin_dbmap')<output.index('begin_briefing_text')<output.index('begin_maps')
    reparsed=loads_ldf(output);reparsed.lvl_info['briefing_text']=''
    assert dumps_ldf(reparsed)==text


def test_changed_nested_gem_action_keeps_manual_fields():
    from map_editor.core.ldf_model import new_gem
    doc=LdfDocument(mw=7,mh=7);doc.visible_gem_slots=1
    doc.gems[1]=new_gem()|dict(x=2,y=2,actions=[dict(target_type='modify_vehicle',id=4,param='add_energy',val='20')])
    text=dumps_ldf(doc).replace('      add_energy = 20','      add_energy = 20 ; handmade\n      manual_extension = 123')
    restored=loads_ldf(text);restored.gems[1]['actions'][0]['val']='30'
    output=dumps_ldf(restored)
    assert output==text.replace('add_energy = 20','add_energy = 30')


def test_original_crlf_and_encoding_preserved(tmp_path):
    doc=LdfDocument(mw=5,mh=5);doc.lvl_info['title']='Città'
    raw=dumps_ldf(doc).encode('cp1252');path=tmp_path/'accent.ldf';path.write_bytes(raw)
    restored=load_ldf(path);save_ldf(restored,path)
    assert path.read_bytes()==raw
    restored.lvl_info['title']='Città nuova';save_ldf(restored,path)
    result=path.read_bytes()
    assert b'\xe0' in result and b'\n' not in result.replace(b'\r\n',b'')


def test_delete_first_modify_second_keeps_second_manual_fields():
    doc=LdfDocument(mw=7,mh=7)
    doc.squads=[dict(owner=1,veh=4,num=1,hidden=False,useable=False,custom_name=None,x=1,y=1),
                dict(owner=2,veh=5,num=2,hidden=False,useable=False,custom_name=None,x=3,y=3)]
    text=dumps_ldf(doc).replace('   num = 1','   num = 1\n   custom_first = 111').replace('   num = 2','   num = 2\n   custom_second = 222')
    restored=loads_ldf(text);before=restored.snapshot()
    del restored.squads[0];restored.squads[0]['num']=7
    result=dumps_ldf(restored)
    assert 'custom_first' not in result
    assert 'custom_second = 222' in result
    assert loads_ldf(result).squads[0]['num']==7
    restored.restore(before)
    assert dumps_ldf(restored)==text


def test_reordered_squads_keep_their_own_manual_extensions():
    doc=LdfDocument(mw=7,mh=7)
    doc.squads=[dict(owner=1,veh=4,num=1,hidden=False,useable=False,custom_name=None,x=1,y=1),
                dict(owner=2,veh=5,num=2,hidden=False,useable=False,custom_name=None,x=3,y=3)]
    text=dumps_ldf(doc).replace('   num = 1','   num = 1\n   custom_first = 111').replace('   num = 2','   num = 2\n   custom_second = 222')
    restored=loads_ldf(text);restored.squads.reverse()
    restored.squads[0]['num']=8;restored.squads[1]['num']=9
    result=dumps_ldf(restored)
    assert result.index('custom_second')<result.index('custom_first')
    assert [s['num'] for s in loads_ldf(result).squads]==[8,9]
