import pytest

from map_editor.core.ambient_sound import (ambient_candidates, ambient_sound_value, import_into_data,
                                           relative_to_data)
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from .test_special_objects import app, win


def test_ambient_sound_roundtrip_and_vanilla_default():
    doc = LdfDocument(mw=9, mh=9)
    assert 'ambient_sound' not in dumps_ldf(doc)
    assert loads_ldf(dumps_ldf(doc)).lvl_info['ambient_sound'] == ''
    doc.lvl_info['ambient_sound'] = 'Sounds/New/Ambient/Heavy Wind.wav'
    text = dumps_ldf(doc)
    restored = loads_ldf(text)
    assert restored.lvl_info['ambient_sound'] == 'Sounds/New/Ambient/Heavy Wind.wav'
    assert dumps_ldf(restored) == text
    text = text.replace('ambient_sound        = Sounds/New/Ambient/Heavy Wind.wav',
                        'ambient_sound = sounds\\new\\ambient\\ ; comment')
    assert loads_ldf(text).lvl_info['ambient_sound'] == 'sounds/new/ambient'


def test_value_cleanup_rejects_unsafe_text():
    assert ambient_sound_value('  Sounds\\New/ ') == 'Sounds/New'
    assert ambient_sound_value('a;b') == '' and ambient_sound_value('a\nb') == '' and ambient_sound_value(None) == ''


def test_candidates_import_and_paths(tmp_path):
    data = tmp_path / 'Data'
    folder = data / 'Sounds' / 'New' / 'Ambient'
    folder.mkdir(parents=True)
    (folder / 'b.ogg').write_bytes(b'1')
    (folder / 'A.wav').write_bytes(b'1')
    (folder / 'notes.txt').write_bytes(b'1')
    assert [p.name for p in ambient_candidates(data, 'sounds/new/ambient')] == ['A.wav', 'b.ogg']
    assert [p.name for p in ambient_candidates(data, 'Sounds/New/Ambient/b.ogg')] == ['b.ogg']
    assert ambient_candidates(data, 'Sounds/Missing') == []
    assert relative_to_data(data, folder / 'A.wav') == 'Sounds/New/Ambient/A.wav'
    assert relative_to_data(data, tmp_path / 'outside.wav') is None
    outside = tmp_path / 'rain.wav'
    outside.write_bytes(b'1234')
    assert import_into_data(data, outside) == 'Sounds/New/Ambient/rain.wav'
    assert import_into_data(data, outside) == 'Sounds/New/Ambient/rain.wav'
    (folder / 'rain.wav').write_bytes(b'12')
    with pytest.raises(FileExistsError):
        import_into_data(data, outside)


def test_level_info_ambient_sound_row(app, win):
    panel = win.level_panel
    assert 'OpenNeoUA only' in panel.ambient_marker.toolTip()
    assert '#e8873a' in panel.ambient_marker.styleSheet()
    assert win.doc.lvl_info['ambient_sound'] == ''
    panel._set_ambient('Sounds/New/Ambient')
    assert win.doc.lvl_info['ambient_sound'] == 'Sounds/New/Ambient'
    assert 'ambient_sound' in dumps_ldf(win.doc)
    assert 'Folder with' in panel.ambient_status.text()
    panel._set_ambient('Sounds/New/Ambient/Wind.wav')
    assert panel.ambient_status.text() == ''
    panel._set_ambient('Sounds/Nope')
    assert 'No sound found' in panel.ambient_status.text()
    panel._set_ambient('')
    assert 'ambient_sound' not in dumps_ldf(win.doc)


def test_import_reuses_existing_folder_spelling(tmp_path):
    # Linux file systems are case-sensitive: an existing lowercase folder must not be duplicated.
    data = tmp_path / 'Data'
    (data / 'sounds' / 'new' / 'ambient').mkdir(parents=True)
    source = tmp_path / 'rain.wav'
    source.write_bytes(b'1')
    assert import_into_data(data, source) == 'sounds/new/ambient/rain.wav'
    assert [p.name for p in data.iterdir()] == ['sounds']
