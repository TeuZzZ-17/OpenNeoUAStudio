"""Selection controls, precise movement, level restore and named gem IDs."""
import copy

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMessageBox

from tests.map_integration.test_actor_editor import app, win, host, squad
from map_editor.core.ldf_model import dumps_ldf, loads_ldf
from map_editor.core.special_objects import add_special, update_special


def test_add_find_and_delete_all_follow_selection(win, monkeypatch):
    win.doc.squads = [squad()]
    win.doc.host_stations = [host()]
    win._sync_side_panels()
    for kind, panel in [('squad', win.squad_panel), ('host', win.host_panel)]:
        panel.list.setCurrentRow(0)
        assert not panel.add_button.isEnabled()
        assert 'Deselect' in panel.add_button.toolTip()
        assert panel.find_button.isEnabled()
        calls = []
        monkeypatch.setattr(win, '_center_' + kind, calls.append)
        panel.find_button.click()
        assert calls == [0]
        if kind == 'squad':
            panel.set_selection(set())
        else:
            panel.deselect()
        assert panel.add_button.isEnabled()
        assert not panel.find_button.isEnabled()
        baseline = win.doc.snapshot()
        monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
        panel.clear_button.click()
        assert not (win.doc.squads if kind == 'squad' else win.doc.host_stations)
        win.undo()
        assert win.doc.snapshot() == baseline


def test_compact_coordinates_keep_precision_and_speed_is_shared(win):
    record = squad()
    record.update(pos_x=4201.123456789012, pos_z=-4202.987654321012)
    win.doc.squads = [record]
    win._refresh_squads({0})
    panel = win.squad_panel
    before = win.doc.snapshot()
    assert panel.x.text() == panel.x.locale().toString(record['pos_x'], 'f', 2)
    assert panel.x.valueFromText(panel.x.text()) == record['pos_x']
    panel.x.interpretText()
    assert win.doc.snapshot() == before
    for panel in (win.squad_panel, win.host_panel):
        assert panel.x.singleStep() == panel.z.singleStep() == 50
        panel.coordinate_speed.setValue(100)
        assert panel.x.singleStep() == panel.z.singleStep() == 100


def test_host_world_movement_roundtrip_and_undo(win):
    win.doc.host_stations = [host()]
    win._refresh_hosts(0)
    before = win.doc.snapshot()
    panel = win.host_panel
    panel.x.setValue(4300.125)
    panel.z.setValue(-4400.25)
    win._finish_live()
    value = win.doc.host_stations[0]
    assert (value['pos_x'], value['pos_z'], value['x'], value['y']) == (4300.125, -4400.25, 3, 3)
    saved = loads_ldf(dumps_ldf(win.doc)).host_stations[0]
    assert saved['pos_x'] == value['pos_x'] and saved['pos_z'] == value['pos_z']
    win.undo()
    assert win.doc.snapshot() == before


def test_reset_level_restores_all_opened_content_and_is_undoable(win, monkeypatch):
    doc = copy.deepcopy(win.doc)
    doc.squads = [squad()]
    doc.host_stations = [host()]
    doc.script_content = '; original script\n'
    win._new_doc(doc, None)
    original = win.doc.snapshot()
    win.doc.squads.clear()
    win.doc.host_stations.clear()
    win.doc.script_content = '; changed\n'
    win.doc.grids['hgt'][3][3] += 2
    win.doc.lvl_info['music'] = 'None'
    changed = win.doc.snapshot()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    win.level_reset()
    assert win.doc.snapshot() == original
    win.undo()
    assert win.doc.snapshot() == changed


def test_gem_named_id_saves_numeric_and_rejects_invalid(win):
    panel = win.special_panels['gem']
    panel.set_catalogs({'modify_vehicle': {72: 'Slime_lord'}})
    panel.target_id.setCurrentIndex(panel.target_id.findData(72))
    assert panel.target_id.currentText() == '72 ; Slime_lord'
    panel._save_action(False)
    assert panel.template['actions'][-1]['id'] == 72
    previous = copy.deepcopy(panel.template)
    for value in ('bad', '-1', '2147483648'):
        panel.target_id.setEditText(value)
        panel._save_action(False)
        assert panel.template == previous
    panel.target_id.setEditText('9999')
    panel._save_action(False)
    assert panel.template['actions'][-1]['id'] == 9999


def test_actor_context_menus_use_find_and_add(win):
    win.doc.squads = [squad()]
    win.doc.host_stations = [host()]
    win._sync_side_panels()
    for panel, label in [(win.squad_panel, 'Squad'), (win.host_panel, 'Host Station')]:
        win._panel_context_menu(panel.list, QPoint(0, 0))
        actions = {a.text(): a for a in win._context_menu.actions()}
        assert 'Find ' + label in actions and 'Add ' + label in actions
        assert not any('POV' in text or 'Focus' in text for text in actions)
        win._context_menu.close()


@pytest.mark.parametrize('kind', ['gem', 'gate', 'item'])
def test_special_add_requires_deselection_and_find_follows_delete(win, kind):
    slot = add_special(win.doc, kind)
    values = {'x': 3, 'y': 3}
    if kind == 'gem':
        values['actions'] = [dict(target_type='modify_vehicle', id=72, param='enable', val='1')]
    update_special(win.doc, kind, slot, values)
    panel = win.special_panels[kind]
    panel.refresh(win.doc, slot)
    assert not panel.add_button.isEnabled()
    assert panel.focus.isEnabled() and panel.focus.text() == 'Find'
    row = panel.delete_button.parentWidget().layout().itemAt(3).layout()
    assert [row.itemAt(i).widget().text() for i in range(row.count())] == ['Add', 'Delete', 'Find', 'Deselect']
    panel.deselect_button.click()
    assert panel.add_button.isEnabled()
    assert not panel.focus.isEnabled()


def test_host_world_movement_rejects_border_and_occupied_sector(win):
    win.doc.host_stations = [host(), host(owner=6, x=4, y=3)]
    win._refresh_hosts(0)
    baseline = win.doc.snapshot()
    win._host_changed(0, 'pos_x', 5500)
    assert win.doc.snapshot() == baseline
    win._host_changed(0, 'pos_z', 100)
    assert win.doc.snapshot() == baseline
