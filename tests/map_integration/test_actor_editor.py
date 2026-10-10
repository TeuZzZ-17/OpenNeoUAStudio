import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import json
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QPushButton
from map_editor.core.ldf_model import LdfDocument, ensure_host_defaults, dumps_ldf, loads_ldf, make_host_ai
from map_editor.core.host_ai_presets import save_ai_preset, load_ai_preset
from map_editor.ui.main_window import MainWindow


def host(owner=1, x=3, y=3):
    value = dict(owner=owner, veh=56, x=x, y=y, energy=500000, pos_y=-700,
                 hidden=False, custom_name=None)
    ensure_host_defaults(value)
    return value


def squad():
    return dict(owner=1, veh=1, num=1, x=3, y=3, hidden=False, useable=False, custom_name=None)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    window = MainWindow(LdfDocument(mw=9, mh=9))
    window._icons.stop()
    yield window
    window._cancel_operation(clear=False)
    window._released(-1, -1, Qt.KeyboardModifier.NoModifier)
    window.dirty = False
    window.close()
    window._icon_pool.waitForDone(10000)
    window.view._pool.waitForDone(10000)


def test_default_sizes_building_capabilities_and_search(win):
    assert win._icon_level == 1 and win._bicon_level == 2
    assert [panel.list.preview_level for panel in (win.squad_panel, win.host_panel, win.tech_panel)] == [0, 1, 0]
    visible = set(win._bicon_items)
    assert visible and visible < set(win.buildings)
    assert all(win.buildings[key].has_power or win.buildings[key].is_radar or win.buildings[key].has_guns
               for key in visible)
    name = win.buildings[min(visible)].name
    win.building_filter.setText(name.lower())
    assert not win._bicon_items[min(visible)].isHidden()
    assert all(item.isHidden() == (name.lower() not in item.toolTip().lower())
               for item in win._bicon_items.values())
    win.show_special_buildings.setChecked(True)
    assert set(win._bicon_items) == set(win.buildings)
    win.building_filter.clear()
    assert not any(item.isHidden() for item in win._bicon_items.values())


def test_unselected_squad_form_prepares_add_without_changing_existing(win):
    win.doc.squads = [squad()]
    win._refresh_squads({0})
    win.set_tool('squad')
    win.squad_panel.count.setValue(3)
    assert win.doc.squads[0]['num'] == 3
    win._finish_live()
    win.squad_panel.set_selection(set())
    before = win.doc.snapshot()
    panel = win.squad_panel
    assert panel.form_widget.isEnabled()
    panel.owner.setCurrentIndex(panel.owner.findData(6))
    panel.vehicle.setCurrentIndex(panel.vehicle.findData(5))
    panel.count.setValue(7)
    panel.hidden.setChecked(True)
    win._add_squad()
    assert win.doc.snapshot() == before
    draft = win._draft_squads[0]
    assert (draft['owner'], draft['veh'], draft['num'], draft['hidden']) == (6, 5, 7, True)
    win._confirm_placement((4, 4))
    assert win.doc.squads[-1]['num'] == 7
    win.undo()
    assert win.doc.snapshot() == before
    assert any(button.text() == 'Deselect' for button in panel.findChildren(QPushButton))


def test_unselected_host_form_prepares_height_and_ai(win):
    win.doc.host_stations = [host()]
    win._refresh_squads()
    win._refresh_hosts(0)
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    win.host_panel.deselect()
    panel = win.host_panel
    assert panel.form_widget.isEnabled() and panel.ai.isEnabled()
    panel.owner.setCurrentIndex(panel.owner.findData(6))
    panel.energy.setValue(123456)
    panel.height.setValue(-123.25)
    panel.fields['viewangle'].setValue(45)
    panel._change('ai_preset', 'Robo Hunter')
    before = win.doc.snapshot()
    win._add_host()
    assert win.doc.snapshot() == before
    assert win._draft_host['energy'] == 123456 and win._draft_host['pos_y'] == -123.25
    assert win._draft_host['ai']['preset'] == 'Robo Hunter'
    win._confirm_placement((5, 5))
    assert win.doc.host_stations[-1]['ai']['preset'] == 'Robo Hunter'
    win.undo()
    assert win.doc.snapshot() == before
    titles = [button.text() for button in panel.findChildren(QPushButton)]
    assert 'Deselect' in titles and 'Place / Move' not in titles
    assert set(panel.fields) == {'pos_y', 'viewangle', 'reload_const'}


def test_host_drag_reuses_history_and_preserves_exact_coordinates(win):
    win.doc.host_stations = [dict(host(), pos_x=4200.25, pos_z=-4300.5)]
    win._refresh_squads()
    win._refresh_hosts(0)
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    before = win.doc.snapshot()
    win._begin_actor_drag('host', (3, 3))
    win._move_actor_drag((5, 4))
    win._finish_actor_drag()
    moved = win.doc.host_stations[0]
    assert moved['pos_x'] == 6600.25 and moved['pos_z'] == -5500.5
    assert len(win.history._undo) == 1
    win.undo()
    assert win.doc.snapshot() == before
    win._begin_actor_drag('host', (3, 3))
    win._move_actor_drag((5, 4))
    win._cancel_operation(clear=False)
    assert win.doc.snapshot() == before


def test_ground_click_deselects_both_actor_forms(win):
    win.doc.squads = [squad()]
    win.doc.host_stations = [host()]
    win._refresh_squads({0})
    win._refresh_hosts(0)
    win._select_cell((4, 4), Qt.KeyboardModifier.NoModifier)
    assert not win.squad_panel.selected_indices() and win.host_panel.list.currentRow() == -1
    assert win.squad_panel.form_widget.isEnabled() and win.host_panel.form_widget.isEnabled()


def test_comment_increases_card_height_and_scroll_requests_previews(win, app):
    win.doc.squads = [squad() for _ in range(20)]
    win._refresh_squads()
    win.set_tool('squad')
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    view = win.squad_panel.list
    view.doItemsLayout()
    before = view.visualItemRect(view.item(0)).height()
    win.doc.squads[0]['custom_name'] = 'A long comment with several words that must stay visible. ' * 7
    win.squad_panel.update_rows()
    view.doItemsLayout()
    assert view.visualItemRect(view.item(0)).height() > before
    spy = QSignalSpy(view.previewsRequested)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    assert spy.count() > 0
    assert view.font().pointSizeF() == win.host_panel.list.font().pointSizeF()


def test_tech_edit_without_host_keeps_scroll_and_summary(win, app):
    win.palette_tabs.setCurrentIndex(win.tech_tab_index)
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    panel = win.tech_panel
    assert not win.doc.host_stations and panel.list.isEnabled()
    panel.list.verticalScrollBar().setValue(400)
    app.processEvents()
    scroll = panel.list.verticalScrollBar().value()
    point = panel.list.viewport().rect().center()
    item = panel.list.itemAt(point)
    key = item.data(Qt.ItemDataRole.UserRole)
    state = item.checkState()
    QTest.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=point)
    expected = state != Qt.CheckState.Checked
    assert (key in win.doc.tech[1]['veh']) == expected
    assert panel.list.verticalScrollBar().value() == scroll

    kind = panel.kind.currentData()
    enabled = set(panel.permissions(kind))
    assert panel.status.text() == f'Enabled {panel.kind.currentText()} ({len(enabled)})'
    rows = [panel.summary.topLevelItem(i) for i in range(panel.summary.topLevelItemCount())]
    assert len(rows) == len(enabled)
    assert {row.data(0, Qt.ItemDataRole.UserRole) for row in rows} == enabled
    assert rows

    row = rows[0]
    summary_key = row.data(0, Qt.ItemDataRole.UserRole)
    target = next(panel.list.item(i) for i in range(panel.list.count())
                  if panel.list.item(i).data(Qt.ItemDataRole.UserRole) == summary_key)
    checked = target.checkState()
    before_summary_click = win.doc.snapshot()
    rect = panel.summary.visualItemRect(row)
    QTest.mouseClick(panel.summary.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert panel.list.currentItem().data(Qt.ItemDataRole.UserRole) == summary_key
    assert target.checkState() == checked
    assert win.doc.snapshot() == before_summary_click


def test_custom_ai_file_and_map_round_trip(tmp_path):
    values = make_host_ai('Robo Hunter')
    values['con_budget'] = 17
    path = tmp_path / 'my-host-ai.json'
    saved = save_ai_preset(path, 'Custom invasion', values)
    assert load_ai_preset(path) == saved
    doc = LdfDocument(mw=9, mh=9)
    doc.host_stations = [host(), dict(host(owner=6), ai=saved)]
    text = dumps_ldf(doc)
    assert '; Studio AI preset: Custom invasion' in text
    loaded = loads_ldf(text)
    assert loaded.host_stations[1]['ai'] == saved
    invalid = json.loads(path.read_text())
    invalid['values']['con_budget'] = 101
    path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        load_ai_preset(path)


def test_host_pov_uses_script_viewer_and_view_angle(win):
    win.doc.host_stations = [dict(host(), viewangle=90)]
    win._refresh_squads()
    visual = win._lib().vehicles[56]
    win._host_pov(0)
    x, y, z = win.view.camera.center
    assert y == -700 + .3 + visual.viewer[1]
    assert win.view.camera.perspective
    assert win.view.camera.to_camera((x-1000, y, z))[2] < 4 < win.view.camera.to_camera((x+1000, y, z))[2]


def test_active_host_pov_viewangle_edit_turns_camera_without_changing_body(win):
    value = dict(host(), viewangle=0, pos_x=4200.25, pos_z=-4300.5)
    win.doc.host_stations = [value]
    win._refresh_squads()
    win._refresh_hosts(0)
    win.host_panel.advanced.setChecked(True)
    body_before = copy.deepcopy(win.doc.host_stations[0])
    win._host_pov(0)
    camera = win.view.camera
    center, pitch, yaw = camera.center, camera.pitch, camera.yaw

    win.host_panel.fields['viewangle'].setValue(90)

    assert camera.perspective
    assert camera.yaw == pytest.approx(-90)
    assert camera.yaw != yaw
    assert camera.center == center and camera.pitch == pitch
    assert win.doc.host_stations[0] == body_before | {'viewangle': 90}


def test_host_focus_and_pov_follow_raised_terrain(win):
    from map_editor.core.ldf_model import DEFAULT_HGT
    from map_editor.render.squad_scene import host_position
    win.doc.grids['hgt'] = [[DEFAULT_HGT+20] * win.doc.mw for _ in range(win.doc.mh)]
    win.view.terrain_changed()
    win.doc.host_stations = [host()]
    win._refresh_squads()
    win._refresh_hosts(0)
    position = host_position(win.doc.host_stations[0], win.doc, win.view.terrain, win._lib())
    assert position[1] < -2000
    win._center_host(0)
    assert win.view.camera.center == position
    win._host_pov(0)
    assert win.view.camera.center[1] == position[1] + win._lib().vehicles[56].viewer[1]
    assert win.doc.host_stations[0]['pos_y'] == -700


@pytest.mark.parametrize('kind', ['host', 'squad'])
def test_actor_mouse_drag_cursors_and_ground_deselection(win, app, monkeypatch, kind):
    win.doc.host_stations = [host()] if kind == 'host' else []
    win.doc.squads = [squad()] if kind == 'squad' else []
    win._refresh_squads()
    win._refresh_hosts()
    win.palette_tabs.setCurrentIndex(win.host_tab_index if kind == 'host' else win.squad_tab_index)
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    view = win.view
    monkeypatch.setattr(view, 'pick_host', lambda x, y: 0 if kind == 'host' and x < 150 else None)
    monkeypatch.setattr(view, 'pick_squad', lambda x, y: 0 if kind == 'squad' and x < 150 else None)
    monkeypatch.setattr(view, 'ground_cell', lambda x, y: (3, 3) if x < 150 else (5, 4))
    QTest.mouseMove(view, QPoint(250, 100))
    QTest.mouseMove(view, QPoint(100, 100))
    assert view.cursor().shape() == Qt.CursorShape.OpenHandCursor
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(view, QPoint(250, 100))
    assert view.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(250, 100))
    record = (win.doc.host_stations if kind == 'host' else win.doc.squads)[0]
    assert (record['x'], record['y']) == (5, 4)
    QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=QPoint(250, 100))
    assert not win.squad_panel.selected_indices() and win.host_panel.list.currentRow() == -1


def test_host_expanded_settings_fit_palette_width(win, app):
    win.palette_tabs.setCurrentIndex(win.host_tab_index)
    win.host_panel.advanced.setChecked(True)
    win.host_panel.ai.setChecked(True)
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    scroll = win.palette_tabs.widget(win.host_tab_index)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert win.host_panel.ai_fields.height() >= win.host_panel.ai_fields.minimumSizeHint().height()


def test_sector_occupied_by_squad_allows_new_host(win):
    win.doc.squads = [squad()]
    win._draft_host = dict(host(), _preview=True)
    win._confirm_host((3, 3))
    assert (win.doc.host_stations[-1]['x'], win.doc.host_stations[-1]['y']) == (3, 3)
    assert win._draft_host is None
    assert (win.doc.squads[0]['x'], win.doc.squads[0]['y']) == (3, 3)


def test_sector_occupied_by_host_allows_new_squad(win):
    win.doc.host_stations = [host(x=4, y=4)]
    draft = dict(squad(), _preview=True)
    win._draft_squads = [draft]
    win._draft_origin = copy.deepcopy(win._draft_squads)
    win._confirm_add((4, 4))
    assert (win.doc.squads[-1]['x'], win.doc.squads[-1]['y']) == (4, 4)
    assert not win._draft_squads
    assert (win.doc.host_stations[0]['x'], win.doc.host_stations[0]['y']) == (4, 4)


def test_dragging_squad_onto_host_is_allowed_and_undoable(win):
    initial_squad = dict(squad(), x=2, y=2)
    win.doc.squads = [initial_squad]
    win.doc.host_stations = [host(x=4, y=4)]
    win._refresh_squads({0})
    win._refresh_hosts()
    before = win.doc.snapshot()
    win._begin_actor_drag('squad', (2, 2))
    win._move_actor_drag((4, 4))
    win._finish_actor_drag()
    assert (win.doc.squads[0]['x'], win.doc.squads[0]['y']) == (4, 4)
    assert win.history.can_undo
    win.undo()
    assert win.doc.snapshot() == before


def test_dragging_host_onto_squad_is_allowed_and_undoable(win):
    win.doc.squads = [dict(squad(), x=4, y=4)]
    win.doc.host_stations = [host(x=2, y=2)]
    win._refresh_squads()
    win._refresh_hosts(0)
    before = win.doc.snapshot()
    win._begin_actor_drag('host', (2, 2))
    win._move_actor_drag((4, 4))
    win._finish_actor_drag()
    assert (win.doc.host_stations[0]['x'], win.doc.host_stations[0]['y']) == (4, 4)
    assert win.history.can_undo
    win.undo()
    assert win.doc.snapshot() == before


