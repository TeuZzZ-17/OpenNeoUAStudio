import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton
from map_editor.core.ldf_model import LdfDocument, HGT_MIN, dumps_ldf, load_ldf, loads_ldf
from map_editor.render.squad_scene import squad_members, ground_height
from map_editor.render.gpu_scene import WorldScene
from map_editor.render.map_scene import scene_polygons, render_scene
from map_editor.render.terrain_mesh import TerrainMesh
from map_editor.render.camera import IsoCamera
from map_editor.ui.main_window import MainWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app):
    window = MainWindow(LdfDocument(mw=9, mh=9))
    window._icons.stop()
    yield window
    window.dirty = False
    window.close()
    window.view._pool.waitForDone(10000)


def test_reset_fill_and_buttons_share_history(win, app, monkeypatch):
    # One cell already has the requested sector type but still has a building.
    # Fill must clear that building even when painting the type itself is a no-op.
    for row in range(1, win.doc.mh - 1):
        for col in range(1, win.doc.mw - 1):
            win.doc.grids['type'][row][col] = '04'
    win.doc.grids['type'][4][4] = '05'
    win.doc.grids['blg'][4][4] = '03'
    win.doc.grids['blg'][3][3] = '08'
    before_fill = win.doc.snapshot()
    win._fill_sectors(5)
    assert win.doc.grids['type'][4][4] == '05'
    assert win.doc.grids['type'][3][3] == '05'
    assert win.doc.grids['blg'][4][4] == win.doc.grids['blg'][3][3] == '00'
    assert win.doc.grids['type'][0] == before_fill['grids']['type'][0]
    assert win.doc.grids['blg'][0] == before_fill['grids']['blg'][0]
    assert win.undo_button.isEnabled()
    after_fill = win.doc.snapshot()
    reloaded = loads_ldf(dumps_ldf(win.doc))
    assert all(value == '00' for row in reloaded.grids['blg'][1:-1]
               for value in row[1:-1])
    win.undo_button.click()
    assert win.doc.snapshot() == before_fill
    win.redo_button.click()
    assert win.doc.snapshot() == after_fill

    # The palette row shares the same Reset QAction, between Undo and Redo.
    win.show()
    app.processEvents()
    assert win.reset_button.defaultAction() is win.reset_action
    buttons = (win.undo_button, win.reset_button, win.redo_button)
    centers = [button.mapTo(win, button.rect().center()).x() for button in buttons]
    assert centers[0] < centers[1] < centers[2]

    win.doc.squads.append(dict(owner=1, veh=1, num=1, hidden=False,
                               useable=True, custom_name=None, x=3, y=3))
    win.doc.script_content = 'modify_vehicle 1\nend'
    before = win.doc.snapshot()
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
    win.reset_button.click()
    assert win.doc.mw == 9 and win.doc.set_number == 1
    assert not win.doc.squads and win.doc.grids['type'][4][4] == '00'
    reset = win.doc.snapshot()
    win.undo_button.click()
    assert win.doc.snapshot() == before
    win.redo_button.click()
    assert win.doc.snapshot() == reset


def test_fill_selected_changes_only_selection_and_clears_buildings(win):
    for row in range(1, win.doc.mh - 1):
        for col in range(1, win.doc.mw - 1):
            win.doc.grids['type'][row][col] = '02'
            win.doc.grids['blg'][row][col] = '03'
    selected = {(2, 2), (3, 2), (5, 5)}
    win._fill_sectors(5, selected)
    for row in range(1, win.doc.mh - 1):
        for col in range(1, win.doc.mw - 1):
            if (col, row) in selected:
                assert win.doc.grids['type'][row][col] == '05'
                assert win.doc.grids['blg'][row][col] == '00'
            else:
                assert win.doc.grids['type'][row][col] == '02'
                assert win.doc.grids['blg'][row][col] == '03'


def test_save_as_text_filter_adds_extension_and_reloads_ldf(win, tmp_path, monkeypatch):
    path = tmp_path / 'untitled'
    calls = []

    def save_dialog(*args, **kwargs):
        calls.append((args, kwargs))
        return str(path), 'Text maps (*.txt)'

    monkeypatch.setattr(QFileDialog, 'getSaveFileName', save_dialog)
    win.file_save_as()

    assert calls
    assert calls[0][0][3] == 'Levels (*.LDF);;Text maps (*.txt)'
    assert path.with_suffix('.txt').exists()
    assert win.path == str(path.with_suffix('.txt'))
    assert not win.dirty
    loaded = load_ldf(str(path.with_suffix('.txt')))
    assert loaded.snapshot() == win.doc.snapshot()


def test_script_pending_keyboard_undo_save_and_reload(win, tmp_path):
    original = win.doc.script_content
    win.palette_tabs.setCurrentIndex(win.script_tab_index)
    win.script_edit.setPlainText('; custom\ninclude data:scripts/example.scr\n')
    assert win._script_pending and win.undo_button.isEnabled()
    win.undo_button.click()
    assert win.doc.script_content == original and not win._script_pending
    win.redo_button.click()
    assert 'example.scr' in win.script_edit.toPlainText()
    win.script_edit.appendPlainText('; final')
    win.path = str(tmp_path / 'script.LDF')
    win.file_save()
    assert not win._script_pending and not win.dirty
    assert loads_ldf((tmp_path / 'script.LDF').read_text()).script_content.strip() == win.doc.script_content.strip()
    win.show()
    win.script_edit.setFocus()
    QTest.keyClick(win.script_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert '; final' not in win.doc.script_content


def test_numeric_flatten_sampling_and_one_stroke_undo(win):
    win.set_tool('terrain')
    win.flatten_height.setValue(42)
    win.terrain_buttons[2].click()
    win._pressed(4, 4, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(4, 4, Qt.KeyboardModifier.NoModifier)
    assert win.doc.grids['hgt'][4][4] == HGT_MIN + 42
    win.undo()
    assert win.doc.grids['hgt'][4][4] == HGT_MIN + 30
    win.doc.grids['hgt'][4][4] = HGT_MIN + 18
    win.sample_height.click()
    count = len(win.history._undo)
    win._pressed(4, 4, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert win.flatten_height.value() == 18 and len(win.history._undo) == count
    assert not win.sample_height.isChecked()


def _squad(**values):
    squad = dict(owner=1, veh=1, num=3, hidden=False, useable=True,
                 custom_name='Original', x=2, y=2,
                 pos_x=3100.25, pos_z=-3100.5)
    squad.update(values)
    return squad


def test_squad_draft_cancel_and_left_click_confirm_are_single_history_steps(win, app, monkeypatch):
    win.show()
    app.processEvents()
    add = next(button for button in win.squad_panel.findChildren(QPushButton)
               if button.text() == 'Add')
    before = win.doc.snapshot()
    monkeypatch.setattr(win.view, 'ground_cell', lambda *_: (4, 4))

    QTest.mouseClick(add, Qt.MouseButton.LeftButton)
    assert win.doc.squads == [] and len(win._draft_squads) == 1
    assert win.view.draft_active and not win.history._undo
    QTest.keyClick(win.view, Qt.Key.Key_Escape)
    assert win.doc.snapshot() == before
    assert not win._draft_squads and not win.view.draft_active

    QTest.mouseClick(add, Qt.MouseButton.LeftButton)
    assert not win.doc.squads and win.view.draft_active
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
    assert len(win.doc.squads) == 1 and not win._draft_squads
    assert (win.doc.squads[0]['x'], win.doc.squads[0]['y']) == (4, 4)
    assert len(win.history._undo) == 1
    placed = win.doc.snapshot()
    win.undo()
    assert win.doc.snapshot() == before
    win.redo()
    assert win.doc.snapshot() == placed


def test_squad_live_edit_preserves_exact_coordinates_and_roundtrips_ldf(win):
    win.doc.squads = [_squad()]
    win._refresh_squads({0})
    win.set_tool('squad')
    panel = win.squad_panel
    panel.count.setValue(7)
    panel.x.setValue(4567.75)
    panel.z.setValue(-4389.125)
    panel.hidden.setChecked(True)
    panel.useable.setChecked(False)
    win._finish_live()

    squad = win.doc.squads[0]
    assert squad['num'] == 7 and squad['hidden'] and not squad['useable']
    assert squad['pos_x'] == 4567.75 and squad['pos_z'] == -4389.125
    output = dumps_ldf(win.doc)
    assert 'pos_x = 4567.75' in output and 'pos_z = -4389.125' in output
    assert loads_ldf(output).squads[0] == squad
    assert win.palette_tabs.currentIndex() == win.squad_tab_index

    panel.count.setValue(2147483647)
    win._finish_live()
    terrain = TerrainMesh()
    terrain.rebuild(win.doc.grids['hgt'])
    # Members outside the level have no ground surface and are omitted.
    # An extreme runtime formation must remain bounded without changing its anchor.
    assert len(list(squad_members(win.doc, terrain, win._lib()))) <= 256
    assert win.doc.squads[0]['num'] == 2147483647
    assert 'complete count is saved' in panel.status.text()


def test_squad_live_group_count_and_faction_are_one_undo_and_preserve_other_fields(win):
    first = _squad()
    second = _squad(owner=2, veh=3, num=11, hidden=True, useable=False,
                    custom_name='Other', x=5, y=4, pos_x=6700.75, pos_z=-5500.125)
    win.doc.squads = [dict(first), dict(second)]
    win._refresh_squads({0, 1})
    original = [dict(squad) for squad in win.doc.squads]
    history_length = len(win.history._undo)

    win.squad_panel.count.setValue(12)
    owner_index = win.squad_panel.owner.findData(3)
    assert owner_index >= 0
    win.squad_panel.owner.setCurrentIndex(owner_index)
    win._finish_live()

    changed = [dict(squad) for squad in win.doc.squads]
    assert len(win.history._undo) == history_length + 1
    for before, after in zip(original, changed):
        assert after['num'] == 12 and after['owner'] == 3
        assert {key: value for key, value in after.items() if key not in ('num', 'owner')} == {
            key: value for key, value in before.items() if key not in ('num', 'owner')}
    win.undo()
    assert win.doc.squads == original
    win.redo()
    assert win.doc.squads == changed


def test_squad_ctrl_selection_and_empty_double_click(win, app, monkeypatch):
    win.doc.squads = [_squad(), _squad(owner=2, x=5, y=4,
                                      pos_x=6700, pos_z=-5500)]
    win._refresh_squads({0})
    win.show()
    app.processEvents()
    monkeypatch.setattr(win.view, 'pick_squad', lambda x, _y: 1 if x >= 80 else 0)
    monkeypatch.setattr(win.view, 'pick_cell', lambda *_: None)
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, QPoint(100, 100))
    assert win.squad_panel.selected_indices() == {0, 1}

    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier, QPoint(100, 100))
    assert win.squad_panel.selected_indices() == {0}

    cleared = QSignalSpy(win.view.selectionCleared)
    monkeypatch.setattr(win.view, 'pick_squad', lambda *_: None)
    QTest.mouseDClick(win.view, Qt.MouseButton.LeftButton, pos=QPoint(250, 200))
    assert cleared.count() == 1
    assert not win.squad_panel.selected_indices()


def test_squad_group_drag_cancel_and_one_undo(win, app, monkeypatch):
    win.doc.squads = [_squad(), _squad(owner=2, x=4, y=4,
                                      pos_x=5500.25, pos_z=-5500.5)]
    win._refresh_squads({0, 1})
    original = [dict(squad) for squad in win.doc.squads]
    win.show()
    app.processEvents()
    monkeypatch.setattr(win.view, 'pick_squad', lambda *_: 0)
    monkeypatch.setattr(win.view, 'pick_cell', lambda *_: None)
    monkeypatch.setattr(win.view, 'ground_cell', lambda x, _y: (2, 2) if x < 140 else (3, 3))

    QTest.mousePress(win.view, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(win.view, QPoint(160, 160), 10)
    assert win.view.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert win.doc.squads[0]['pos_x'] == original[0]['pos_x'] + 1200
    assert win.doc.squads[1]['pos_z'] == original[1]['pos_z'] - 1200
    QTest.keyClick(win.view, Qt.Key.Key_Escape)
    assert win.view.cursor().shape() == Qt.CursorShape.ArrowCursor
    QTest.mouseRelease(win.view, Qt.MouseButton.LeftButton, pos=QPoint(160, 160))
    assert win.doc.squads == original
    assert win.history._pending is None

    QTest.mousePress(win.view, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
    QTest.mouseMove(win.view, QPoint(160, 160), 10)
    assert win.view.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    QTest.mouseRelease(win.view, Qt.MouseButton.LeftButton, pos=QPoint(160, 160))
    assert win.view.cursor().shape() == Qt.CursorShape.ArrowCursor
    moved = [dict(squad) for squad in win.doc.squads]
    assert moved[0]['pos_x'] == original[0]['pos_x'] + 1200
    assert moved[1]['pos_z'] == original[1]['pos_z'] - 1200
    assert len(win.history._undo) == 1
    win.undo()
    assert win.doc.squads == original
    win.redo()
    assert win.doc.squads == moved


def test_real_member_geometry_and_picking_in_software_and_gpu_scene(win):
    doc, lib = win.doc, win._lib()
    doc.squads = [dict(owner=1, veh=1, num=7, x=4, y=4, pos_x=5400, pos_z=-5400)]
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    members = list(squad_members(doc, terrain, lib))
    assert len(members) == 7
    mesh_bottom = lib.vehicle_mesh(1).bounds[4]
    for member in members:
        surface = ground_height(doc, terrain, lib, member.position[0], member.position[2])
        assert np.isclose(member.position[1] + mesh_bottom, surface)
    camera = IsoCamera(yaw=0, pitch=90, zoom=.32, width=500, height=400, center=(5400, 0, -5400))
    polygons = scene_polygons(lib, doc, terrain, camera)
    code = -(doc.mw * doc.mh + 1)
    assert any(p.payload[1] == code for p in polygons)
    frame = render_scene(polygons, camera, lib.tables)
    assert np.any(frame.cell_ids == code)
    # An edited squad must not remain clickable at the old rendered position
    # while the replacement software frame is still being generated.
    view = win.view
    view.resize(500, 400)
    view.camera = camera
    view._frame = frame
    view._frame_key = view._render_key()
    y, x = np.argwhere(frame.cell_ids == code)[0]
    assert view.pick_squad(int(x), int(y)) == 0
    view._scene_revision += 1
    assert view.pick_squad(int(x), int(y)) is None
    gpu = WorldScene()
    gpu.set_library(lib)
    changed = gpu.update(doc, terrain)
    actors = gpu.chunks[-1, -1]
    assert (-1, -1) in changed and len(actors.opaque) > 0
    assert np.all(actors.opaque[:, 11] == code)
    assert np.all(actors.opaque[:, 12:16] == -1)
    doc.grids['type'][4][4] = '05'
    changed = gpu.update(doc, terrain, {(4, 4)})
    assert (-1, -1) in changed
    assert gpu.chunks[-1, -1] is not actors
    doc.squads.clear()
    gpu.update(doc, terrain)
    assert len(gpu.chunks[-1, -1].opaque) == 0


def test_slope_preview_places_each_member_on_its_own_ground_point(win):
    doc, lib = win.doc, win._lib()
    doc.grids['hgt'][4][4] += 10
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    height = ground_height(doc, terrain, lib, 4810, -5400)
    assert -1000 < height <= 0
    doc.squads = [dict(owner=1, veh=1, num=4, x=4, y=4, pos_x=4810, pos_z=-5400)]
    members = list(squad_members(doc, terrain, lib))
    mesh_bottom = lib.vehicle_mesh(1).bounds[4]
    assert len(members) == 4
    for member in members:
        surface = ground_height(doc, terrain, lib, member.position[0], member.position[2])
        assert np.isclose(member.position[1] + mesh_bottom, surface)
    assert len({round(member.position[1], 4) for member in members}) > 1
