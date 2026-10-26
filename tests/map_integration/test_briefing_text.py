from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf


def test_optional_briefing_roundtrip():
    doc = LdfDocument(mw=9, mh=9)
    assert 'begin_briefing_text' not in dumps_ldf(doc)
    text = 'Missione: città; settore = 2\n\n  Conserva spazi\nend\n\\path\ninclude = fake.ldf\n'
    doc.lvl_info['briefing_text'] = text
    saved = dumps_ldf(doc)
    assert 'begin_briefing_text' in saved
    restored = loads_ldf(saved)
    assert restored.lvl_info['briefing_text'] == text
    assert dumps_ldf(restored) == saved
    before = doc.snapshot()
    doc.lvl_info['briefing_text'] = ''
    assert 'begin_briefing_text' not in dumps_ldf(doc)
    doc.restore(before)
    assert doc.lvl_info['briefing_text'] == text


def test_briefing_does_not_parse_level_keywords():
    doc = LdfDocument(mw=9, mh=9)
    doc.lvl_info['briefing_text'] = 'begin_location 7\nend\ntyp_map = 99 99\n'
    restored = loads_ldf(dumps_ldf(doc))
    assert restored.mw == 9 and restored.mh == 9
    assert restored.locations == []


def test_level_info_briefing_edit_and_compatibility(app, win):
    from studio.ui.shell import compatibility_entries, MIXED
    panel = win.level_panel
    panel.briefing_edit.setPlainText('Secure the city.\n\nKeep the power station intact.')
    assert win.doc.lvl_info['briefing_text'] == panel.briefing_edit.toPlainText()
    assert win.dirty
    assert 'OpenNeoUA only' in panel.briefing_marker.toolTip()
    assert '#e75f60' in panel.briefing_marker.styleSheet()
    rows = compatibility_entries(win, 'map_editor')
    assert next(row for row in rows if row[0] == 'Level Info')[1] == MIXED
    assert loads_ldf(dumps_ldf(win.doc)).lvl_info['briefing_text'] == panel.briefing_edit.toPlainText()


from .test_special_objects import app, win
