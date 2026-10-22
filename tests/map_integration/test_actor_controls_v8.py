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
        # New stays available: it deselects the current record itself.
        assert panel.add_button.isEnabled() and panel.add_button.text() == 'New'
        assert 'Deselect' in panel.add_button.toolTip()
        assert panel.find_button.isEnabled()
        calls = []
        monkeypatch.setattr(win, '_center_' + kind, calls.append)
        panel.find_button.click()
        assert calls == [0]
        panel.add_button.click()
        # The old record is deselected and the new draft takes over the selection.
        if kind == 'squad':
            assert win._draft_squads and 0 not in panel.selected_indices()
        else:
            assert win._draft_host is not None and panel.list.currentRow() != 0
        win._cancel_operation(clear=False)
        panel.list.setCurrentRow(0)
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
def test_special_new_deselects_and_find_follows_delete(win, kind):
    slot = add_special(win.doc, kind)
    values = {'x': 3, 'y': 3}
    if kind == 'gem':
        values['actions'] = [dict(target_type='modify_vehicle', id=72, param='enable', val='1')]
    update_special(win.doc, kind, slot, values)
    panel = win.special_panels[kind]
    panel.refresh(win.doc, slot)
    assert panel.add_button.isEnabled()
    assert panel.focus.isEnabled() and panel.focus.text() == 'Find'
    outer = panel.layout()
    row = next(outer.itemAt(i).layout() for i in range(outer.count())
               if outer.itemAt(i).layout() is not None
               and outer.itemAt(i).layout().indexOf(panel.delete_button) >= 0)
    assert [row.itemAt(i).widget().text() for i in range(row.count())] == ['New', 'Delete', 'Find']
    # New = Deselect + Add: the selection is cleared and a placement draft starts.
    panel.add_button.click()
    assert win._special_draft is not None and panel.slot != slot
    win._cancel_operation(clear=False)


def test_host_world_movement_rejects_border_and_occupied_sector(win):
    win.doc.host_stations = [host(), host(owner=6, x=4, y=3)]
    win._refresh_hosts(0)
    baseline = win.doc.snapshot()
    win._host_changed(0, 'pos_x', 5500)
    assert win.doc.snapshot() == baseline
    win._host_changed(0, 'pos_z', 100)
    assert win.doc.snapshot() == baseline


def test_squad_draft_card_returns_to_normal_after_placement(win):
    from PySide6.QtCore import Qt
    from map_editor.ui.preview_cards import DRAFT_ROLE
    win.squad_panel.add_button.click()
    index = len(win.doc.squads)
    item = win.squad_panel.list.item(index)
    assert item.data(DRAFT_ROLE)
    assert item.flags() & Qt.ItemFlag.ItemIsEnabled
    win._confirm_add((4,4))
    assert not win.squad_panel.list.item(index).data(DRAFT_ROLE)


@pytest.mark.parametrize('kind', ['squad', 'host'])
def test_draft_right_drag_rotates_and_only_left_click_places(win, app, monkeypatch, kind):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest, QSignalSpy
    panel = win.squad_panel if kind == 'squad' else win.host_panel
    panel.add_button.click()
    monkeypatch.setattr(win.view, 'ground_cell', lambda *args: (4,4))
    win.resize(1100,800);win.show();app.processEvents()
    win._icons.stop()
    placed = QSignalSpy(win.view.placementConfirmed)
    pos = win.view.rect().center()
    yaw = win.view.camera.yaw
    QTest.mousePress(win.view, Qt.MouseButton.RightButton, pos=pos)
    QTest.mouseMove(win.view, pos + QPoint(50,20))
    QTest.mouseRelease(win.view, Qt.MouseButton.RightButton, pos=pos + QPoint(50,20))
    assert placed.count() == 0 and win.view.camera.yaw != yaw
    assert win.view.draft_active
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=pos)
    assert placed.count() == 1 and not win.view.draft_active


@pytest.mark.parametrize('kind', ['squad','host'])
def test_actor_pov_button_enters_and_exits_existing_view(win, monkeypatch, kind):
    win.doc.squads=[squad()];win.doc.host_stations=[host()]
    win._sync_side_panels()
    panel=win.squad_panel if kind=='squad' else win.host_panel
    panel.list.setCurrentRow(0)
    assert panel.pov_button.text()=='Enter POV' and panel.pov_button.isEnabled()
    panel.pov_button.click()
    assert win.view.camera.perspective and panel.pov_button.text()=='Exit POV'
    panel.pov_button.click()
    assert not win.view.camera.perspective and panel.pov_button.text()=='Enter POV'
    panel.add_button.click()
    assert not panel.pov_button.isEnabled()


@pytest.mark.parametrize('kind', ['squad','host'])
def test_new_actor_keeps_keyboard_navigation_available_before_placement(win, app, kind):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    win.resize(1100,800);win.show();app.processEvents()
    panel=win.squad_panel if kind=='squad' else win.host_panel
    panel.add_button.setFocus()
    panel.add_button.click()
    assert win.view.hasFocus() and win.view.draft_active
    for key in (Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D,
                Qt.Key.Key_Up,Qt.Key.Key_Down,Qt.Key.Key_Left,Qt.Key.Key_Right):
        before=win.view.camera.center,win.view.camera.pan
        QTest.keyClick(app.focusWidget(),key)
        assert (win.view.camera.center,win.view.camera.pan)!=before
        assert win.view.draft_active
    assert not win.doc.squads and not win.doc.host_stations


def test_host_preset_folder_is_shared_by_open_save_and_load(win, tmp_path, monkeypatch):
    import map_editor.core.host_ai_presets as presets
    import map_editor.ui.host_panel as module
    folder=tmp_path/'HostAI'
    monkeypatch.setattr(presets,'host_ai_preset_dir',lambda:folder)
    opened=[];dialogs=[]
    monkeypatch.setattr(module.QDesktopServices,'openUrl',lambda url:opened.append(url.toLocalFile()))
    monkeypatch.setattr(module.QInputDialog,'getText',lambda *args:('My AI',True))
    monkeypatch.setattr(module.QFileDialog,'getSaveFileName',lambda *args:(dialogs.append(args[2]) or ('','')))
    monkeypatch.setattr(module.QFileDialog,'getOpenFileName',lambda *args:(dialogs.append(args[2]) or ('','')))
    panel=win.host_panel
    panel.ai.setChecked(True)
    panel.preset_folder_button.click();panel._save_preset();panel._load_preset()
    assert folder.is_dir() and [__import__("pathlib").Path(value) for value in opened]==[folder]
    assert dialogs==[str(folder/'My AI.json'),str(folder)]
    form=panel.form_widget.layout()
    assert form.getWidgetPosition(panel.ai)[0] < form.getWidgetPosition(panel.advanced)[0]


def test_host_badges_follow_native_owner_glyph_and_actual_player_host(win):
    from map_editor.ui.game_icons import game_icon, _glyph
    from map_editor import bootstrap
    win.doc.host_stations=[host(1,3,3),host(3,5,5)]
    win.doc.player_owner=3
    win._sync_side_panels()
    badges=list(win.squad_overlay.host_badges(win.view.camera,win.view.terrain))
    assert [entry[0] for entry in badges if entry[4]]==[1]
    assert game_icon('host',3)==_glyph(str(bootstrap.game_data_dir()),'MAPROBO',27,True,False,False)
    assert not game_icon('host',3).isNull()
    win.host_panel.list.setCurrentRow(1)
    assert win.host_panel.player.text()=='Current Player Faction'
    win.host_panel.list.setCurrentRow(0)
    assert win.host_panel.player.text()=='Use This Faction as Player'
    win._host_player(0)
    assert win.host_panel.player.text()=='Current Player Faction'
    badges=list(win.squad_overlay.host_badges(win.view.camera,win.view.terrain))
    assert [entry[0] for entry in badges if entry[4]]==[0]
    for index,anchor,*rest in badges:
        assert win.squad_overlay.pick_host_badge(anchor.x(),anchor.y())==index


def test_host_energy_presets_and_custom_values_keep_exact_saved_energy(win):
    from map_editor.ui.host_panel import ENERGY_PRESETS
    win.doc.host_stations=[host()];win._sync_side_panels();win.host_panel.list.setCurrentRow(0)
    combo=win.host_panel.energy
    assert combo.count()==8 and combo.value()==500000
    combo.setCurrentIndex(combo.findData(800000))
    assert win.doc.host_stations[0]['energy']==800000
    combo.setEditText('1234567');combo.lineEdit().editingFinished.emit()
    assert win.doc.host_stations[0]['energy']==1234567
    assert loads_ldf(dumps_ldf(win.doc)).host_stations[0]['energy']==1234567
    combo.setEditText('invalid');combo.lineEdit().editingFinished.emit()
    assert combo.value()==1234567
    for title,value in ENERGY_PRESETS:
        combo.setValue(value)
        assert title in combo.currentText() and combo.value()==value


@pytest.mark.parametrize('kind',['squad','host'])
def test_settings_lock_survives_actor_selection_refresh_and_can_unlock(win, kind):
    win.doc.squads=[squad()];win.doc.host_stations=[host()];win._sync_side_panels()
    panel=win.squad_panel if kind=='squad' else win.host_panel
    panel.list.setCurrentRow(0)
    panel.settings_lock.click()
    assert not panel.owner.isEnabled()
    win._sync_side_panels()
    panel.list.setCurrentRow(0)
    assert not panel.owner.isEnabled() and panel.list.isEnabled()
    panel.settings_lock.click()
    assert panel.owner.isEnabled()


def test_palette_lock_blocks_selection_but_keeps_scrolling(win, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    win.resize(1100,800);win.show();app.processEvents();win._icons.stop()
    view=win.sector_list
    old=view.currentRow()
    win.sector_settings_lock.click()
    target=view.item(old+1)
    QTest.mouseClick(view.viewport(),Qt.MouseButton.LeftButton,pos=view.visualItemRect(target).center())
    assert view.currentRow()==old
    view.verticalScrollBar().setValue(100)
    assert view.verticalScrollBar().value()>0
    win.sector_settings_lock.click()
    view.scrollToItem(target);app.processEvents()
    QTest.mouseClick(view.viewport(),Qt.MouseButton.LeftButton,pos=view.visualItemRect(target).center())
    assert view.currentItem()==target
