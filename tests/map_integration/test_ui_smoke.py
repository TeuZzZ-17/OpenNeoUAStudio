import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
import time
import numpy as np

pytest.importorskip("PySide6")
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolBar

from map_editor import bootstrap
from map_editor.core.ldf_model import HGT_MAX, LdfDocument, load_ldf


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_window_edit_and_undo(app, tmp_path):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    path = str(bootstrap.game_data_dir() / "Levels" / "Single" / "L0101.LDF")
    win._new_doc(load_ldf(path), path)
    win.resize(1200, 800)
    win.show()
    app.processEvents()
    win.view.grab()
    assert win.sector_list.count() == 177
    before = win.doc.grids['hgt'][5][5]
    win.set_tool("terrain")
    win._pressed(5, 5, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    for _ in range(10):
        win._repeat_terrain()
    win._released(5, 5, Qt.KeyboardModifier.NoModifier)
    assert win.doc.grids['hgt'][5][5] > before and win.dirty
    win.undo()
    assert win.doc.grids['hgt'][5][5] == before
    win.set_tool("sector")
    win.sel_typ = 5
    win._pressed(4, 4, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win._released(4, 4, Qt.KeyboardModifier.NoModifier)
    assert win.doc.grids['type'][4][4] == "05"
    out = tmp_path / "T.LDF"
    win.path = str(out)
    win.file_save()
    assert load_ldf(out).grids['type'][4][4] == "05"
    win.close()
    win.view._pool.waitForDone(10000)


def _wait_frame(app, view):
    errors = []
    view.statusMessage.connect(errors.append)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        # QtTest.qWait trattiene il GIL e può affamare il raster Python nel worker.
        time.sleep(.015)
        if view._frame is not None and view._frame_key == view._render_key():
            return
    pytest.fail(f'Render did not finish: {errors}; job={view._job}; frame={view._frame_key}; current={view._render_key()}')


def test_palette_owner_click_hold_undo_and_save(app, tmp_path):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    win._new_doc(LdfDocument(mw=7, mh=7), None)
    win._icons.stop()
    win.resize(1100, 800)
    win.show()
    _wait_frame(app, win.view)
    assert [win.palette_tabs.tabText(i) for i in range(win.palette_tabs.count())] == [
        "Sectors", "Buildings", "Factions", "Terrain"]
    assert not win.findChildren(QToolBar)
    assert not hasattr(win.view, 'overlay')
    assert not hasattr(win.view, 'mode_isometric_lock')

    point, _ = win.view.camera.world_to_screen(win.view.terrain.cell_center(3, 3))
    position = QPoint(round(point[0]), round(point[1]))
    win.palette_tabs.setCurrentIndex(2)
    owner = win.owner_list.item(6)
    QTest.mouseClick(win.owner_list.viewport(), Qt.MouseButton.LeftButton,
                    pos=win.owner_list.visualItemRect(owner).center())
    assert win.tool == 'owner' and win.sel_owner == 6
    frame = win.view._frame
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=position)
    assert win.doc.grids['own'][3][3] == 6
    assert win.view._frame is frame
    win.undo()
    assert win.doc.grids['own'][3][3] == 0
    win.redo()
    assert win.doc.grids['own'][3][3] == 6
    _wait_frame(app, win.view)

    win.palette_tabs.setCurrentIndex(3)
    assert win.tool == 'terrain'
    before = win.doc.grids['hgt'][3][3]
    QTest.mousePress(win.view, Qt.MouseButton.LeftButton, pos=position)
    QTest.qWait(350)
    QTest.mouseRelease(win.view, Qt.MouseButton.LeftButton, pos=position)
    assert win.doc.grids['hgt'][3][3] >= before + 2
    assert not win._repeat.isActive()
    assert len(win.history._undo) == 2
    win.undo()
    assert win.doc.grids['hgt'][3][3] == before
    win.redo()
    assert win.doc.grids['hgt'][3][3] > before
    target = tmp_path / 'gestures.LDF'
    win.path = str(target)
    win.file_save()
    saved = load_ldf(target)
    assert saved.grids == win.doc.grids
    assert not win.dirty

    win.history.push(win.doc)
    win.doc.resize(5, 5)
    win.view.set_document(win.doc)
    _wait_frame(app, win.view)
    win.undo()
    assert win.doc.mw == 7 and win.view.terrain.width == 7
    _wait_frame(app, win.view)
    win.redo()
    assert win.doc.mw == 5 and win.view._frame is None
    _wait_frame(app, win.view)

    win.doc.grids['hgt'][3][3] = HGT_MAX
    win._hovered(3, 3)
    assert 'maximum height' in win.info.text()
    win.dirty = False
    win.close()
    win.view._pool.waitForDone(10000)


def test_save_failure_retains_path_and_pending_render_cannot_replace_document(app, monkeypatch, tmp_path):
    import map_editor.ui.main_window as module
    win = module.MainWindow()
    win._icons.stop()
    original = str(tmp_path / 'original.LDF')
    win.path, win.dirty = original, True
    monkeypatch.setattr(module.QMessageBox, 'critical', lambda *args: None)
    def fail(*args):
        raise OSError('test')
    monkeypatch.setattr(module, 'save_ldf', fail)
    win._save_to(str(tmp_path / 'failed.LDF'))
    assert win.path == original and win.dirty
    old_generation = win.view._generation
    old_key = win.view._render_key()
    win._new_doc(LdfDocument(mw=5, mh=5), None)
    win._icons.stop()
    win.view._render_finished((object(), old_key, old_generation, None, None))
    assert win.view._frame is None and win.doc.mw == 5
    win.close()


def test_owner_only_colors_visible_borders_and_selection_preserves_them(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    doc = LdfDocument(mw=7, mh=7)
    doc.grids['type'][3][3] = '05'
    doc.grids['own'][3][3] = 6
    doc.grids['blg'][3][3] = '01'
    doc.grids['own'][3][4] = 2
    win._new_doc(doc, None)
    win._icons.stop()
    win.resize(1100, 800)
    win.show()
    _wait_frame(app, win.view)
    view = win.view
    view.set_grid(False)
    image = view._annotations()
    base = view._annotation_base
    pixels = np.frombuffer(image.bits(), np.uint8).reshape(view.height(), view.width(), 4).copy()
    owner_image, owner_pixels = view._owner_marks
    ids = view._frame.cell_ids
    # Anche un settore con building mantiene il bordo owner sulle parti
    # terrain visibili, mentre le celle di silhouette restano occluse.
    assert np.any(ids < 0)
    assert not np.any(owner_pixels & (ids <= 0))
    for col, row, owner in ((3, 3, 6), (4, 3, 2)):
        code = row * doc.mw + col + 1
        region = ids == code
        border = owner_pixels & region
        assert border.any()
        assert np.any(np.all(pixels[border, :3] == view.owner_colors[owner], axis=1))
        assert np.count_nonzero(border) < np.count_nonzero(region) / 3
        y, x = np.nonzero(border)
        samples = np.stack((x + .5, y + .5), axis=1)
        points = np.array([view.camera.world_to_screen(v)[0]
                           for v in view.terrain.boundary(col, row)])
        distances = []
        for start, end in zip(points, np.roll(points, -1, axis=0)):
            delta = end - start
            t = np.clip((samples - start) @ delta / (delta @ delta), 0, 1)
            distances.append(np.linalg.norm(samples - (start + t[:, None] * delta), axis=1))
        assert np.min(distances, axis=0).max() < 4
    assert np.array_equal(pixels[:, :, 3] > 0, owner_pixels)
    view.selection = {(3, 3), (4, 3)}
    view.hover = (3, 3)
    view.brush_cells = {(3, 3), (4, 3)}
    marked = view._annotations()
    assert view._annotation_base is base
    marked_pixels = np.frombuffer(marked.bits(), np.uint8).reshape(view.height(), view.width(), 4)
    assert np.array_equal(marked_pixels[owner_pixels], pixels[owner_pixels])
    win.close()
    view._pool.waitForDone(10000)


def test_map_dialogs_commit_and_stop_a_held_terrain_stroke(app, monkeypatch):
    import map_editor.ui.main_window as module
    win = module.MainWindow()
    win._icons.stop()
    win.set_tool('terrain')

    def stopped(expected):
        assert not win._stroke_active and not win._repeat.isActive()
        assert win.history._pending is None and len(win.history._undo) == expected

    def ask(*args):
        stopped(1)
        return None

    monkeypatch.setattr(module.ResizeDialog, 'ask', ask)
    win._pressed(3, 3, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win.map_resize()

    class InfoDialog:
        def __init__(self, *args):
            stopped(2)
            self.musicChanged = type('SignalStub', (), {'connect': lambda self, slot: None})()

        def exec(self):
            return False

    monkeypatch.setattr(module, 'LevelInfoDialog', InfoDialog)
    win._pressed(3, 3, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win.map_level_info()
    win.dirty = False
    win.close()


def test_initial_document_loads_only_the_chosen_set(app):
    from map_editor.ui.main_window import MainWindow
    doc = LdfDocument(mw=9, mh=11, set_number=4)
    win = MainWindow(doc)
    win._icons.stop()
    assert win.doc is doc and win.view.doc is doc
    assert set(win.libs) == {4}
    assert (win.view.terrain.width, win.view.terrain.height) == (9, 11)
    assert win.view.lib.assets.set_number == 4
    win.close()


def test_view_presets_top_picking_and_view_menu_toggle_only(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow(LdfDocument(mw=7, mh=7))
    win._icons.stop()
    win.resize(1100, 800)
    win.show()
    _wait_frame(app, win.view)
    names = [win.view_preset.itemText(i) for i in range(win.view_preset.count())]
    assert names == ['Current View', 'Top', 'Isometric Front Right', 'Isometric Front Left',
                     'Isometric Back Right', 'Isometric Back Left']
    assert win.palette_menu.title() == '&View'
    assert all(action.isCheckable() for action in win.palette_menu.actions())
    center, zoom = win.view.camera.center, win.view.camera.zoom
    index = win.view_preset.findText('Top')
    win.view_preset.setCurrentIndex(index)
    win.view_preset.activated.emit(index)
    _wait_frame(app, win.view)
    assert win.view.camera.pitch == 90 and win.view_preset.currentText() == 'Top'
    assert (win.view.camera.center, win.view.camera.zoom) == (center, zoom)
    point, _ = win.view.camera.world_to_screen(win.view.terrain.cell_center(3, 3))
    assert win.view.pick_cell(*point) == (3, 3)
    QTest.keyClick(win.view, Qt.Key.Key_Q)
    assert win.view_preset.currentText() == 'Current View'
    win.close()
    win.view._pool.waitForDone(10000)


def test_brush_controls_update_shape_axes_and_preview(app):
    from map_editor.ui.main_window import MainWindow
    win = MainWindow()
    win._icons.stop()
    win.set_tool('terrain')
    win.view.hover = (6, 6)
    win.radius_x_slider.setValue(8)
    win.radius_z_slider.setValue(6)
    assert win.radius_slider.value() == 8
    assert win.radius_slider_label.text() == 'X 4 · Z 3'
    assert 'Rectangle' in win.brush_label.text()
    assert 'Force' in win.brush_label.text()
    assert len(win.view.brush_cells) == 35 and (9, 8) in win.view.brush_cells
    win.shape_combo.setCurrentIndex(1)
    assert 'Ellipse' in win.brush_label.text()
    assert len(win.view.brush_cells) == 31 and (9, 8) not in win.view.brush_cells
    assert (win.brush.radius_x, win.brush.radius_z) == (4, 3)
    win.radius_slider.setValue(10)
    assert (win.brush.radius_x, win.brush.radius_z) == (5, 5)
    assert win.radius_slider_label.text() == '5'
    assert win.radius_x_label.text() == '5' and win.radius_z_label.text() == '5'
    win.close()


def test_palette_icons_rasterize_off_ui_thread_and_reuse_cache(app, monkeypatch):
    import threading
    import map_editor.ui.main_window as module
    main_thread = threading.get_ident()
    threads = []
    original = module.rasterize_sprite
    def rasterize(*args):
        threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(module, 'rasterize_sprite', rasterize)
    win = module.MainWindow()
    win._icons.stop()
    win.show()
    win._make_icon()
    win._icon_pool.waitForDone(10000)
    app.processEvents()
    win._icons.stop()
    assert threads and all(thread != main_thread for thread in threads)
    rendered = len(threads)
    typ = next(t for number, t, scale in win._icon_cache
               if number == 1 and scale == win._icon_scale)
    assert not win._icon_cache[(1, typ, win._icon_scale)].isNull()
    item = win._icon_items[typ]
    pending = len(win._icon_queue)
    win._new_doc(LdfDocument(mw=7, mh=9), None)
    assert win._icon_items[typ] is item and len(win._icon_queue) == pending
    win._new_doc(LdfDocument(mw=7, mh=9, set_number=2), None)
    win._new_doc(LdfDocument(mw=7, mh=9), None)
    win._icons.stop()
    assert win._icon_items[typ].icon().cacheKey() == win._icon_cache[(1, typ, win._icon_scale)].cacheKey()
    assert typ not in [t for t, item in win._icon_queue]
    assert len(threads) == rendered
    win.close()
    win.view._pool.waitForDone(10000)


def test_music_preview_toggle_and_cancel_restores_level_track(app, monkeypatch):
    import map_editor.ui.main_window as module
    doc = LdfDocument(mw=5, mh=5)
    doc.lvl_info['music'] = '2'
    win = module.MainWindow(doc)
    win._icons.stop()
    assert win.sky_action.isChecked()
    original_sky = doc.lvl_info['sky']
    win.sky_action.setChecked(False)
    assert not win.view.show_sky and doc.lvl_info['sky'] == original_sky
    assert win.music_action.isChecked()
    assert win.music_preview._path.name == '2.ogg'
    win.music_action.setChecked(False)
    assert not win.music_preview.enabled
    win.music_action.setChecked(True)

    class PreviewDialog(QObject):
        musicChanged = Signal(str)

        def __init__(self, *_args):
            super().__init__()

        def exec(self):
            self.musicChanged.emit('3')
            assert win.music_preview._path.name == '3.ogg'
            return False

    monkeypatch.setattr(module, 'LevelInfoDialog', PreviewDialog)
    win.map_level_info()
    assert win.music_preview._path.name == '2.ogg'
    win.close()


def test_sector_preview_buttons_choose_three_fitted_grids(app):
    import map_editor.ui.main_window as module
    win = module.MainWindow()
    win._icons.stop()
    win.resize(1200, 800)
    win.show()
    app.processEvents()
    assert win._icon_level == 1 and win._icon_scale == 1.1
    for button, level, columns in ((win.sector_zoom_out, 0, 3),
                                   (win.sector_zoom_in, 1, 2),
                                   (win.sector_zoom_in, 2, 1)):
        button.click()
        app.processEvents()
        assert win._icon_level == level
        assert win.sector_zoom_label.text() == f'{level + 1}/3'
        assert win.sector_list.gridSize().width() * columns <= win.sector_list.viewport().width()
        assert win.sector_list.gridSize().width() * (columns + 1) > win.sector_list.viewport().width()
        assert win.sector_list.iconSize().width() < win.sector_list.gridSize().width()
        first_row = [win.sector_list.visualItemRect(win.sector_list.item(i))
                     for i in range(columns)]
        assert len({rect.x() for rect in first_row}) == columns
        assert len({rect.y() for rect in first_row}) == 1
    win._icons.stop()
    assert not win.sector_zoom_in.isEnabled()
    win.close()


def test_view_sky_toggle_and_complete_camera_frames(app):
    from map_editor.render.map_scene import SceneFrame
    from map_editor.render.map_viewport import MapViewport
    view = MapViewport()
    view.doc = LdfDocument(mw=5, mh=5)
    view.lib = object()
    view.resize(480, 360)
    old_key = view._render_key()
    rgba = np.zeros((view.height(), view.width(), 4), dtype=np.uint8)
    rgba[:, :] = (230, 70, 40, 255)
    frame = SceneFrame(rgba, np.zeros((view.height(), view.width()), dtype=np.int32))
    view.camera.yaw += 25
    view._render_finished((frame, old_key, view._generation, None, None))
    assert view._frame is frame and view._frame_key == old_key
    image = view.grab().toImage()
    for x, y in ((2, 2), (view.width() - 3, 2),
                 (2, view.height() - 30), (view.width() - 3, view.height() - 30)):
        assert image.pixelColor(x, y).red() == 230
    assert view.show_sky
    view.set_sky_visible(False)
    assert not view.show_sky
    assert not view._render_key()[-1]
    view.set_editing(True)
    assert view._render_key()[-1]
    view.set_editing(False)
    assert not view._render_key()[-1]
    view._render_timer.stop()
    view.close()
