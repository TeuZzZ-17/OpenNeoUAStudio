"""Rejected gems explain the missing effects without altering the map."""
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtWidgets import QMessageBox, QToolTip
from PySide6.QtTest import QTest

from .test_special_objects import app, win
from map_editor.render import map_viewport
from map_editor.render.special_scene import scene_object_styles


def test_missing_effect_flashes_ghost_and_valid_effect_allows_placement(win, monkeypatch):
    notices = []
    monkeypatch.setattr(QToolTip, 'showText', lambda *args: notices.append(args[1]))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: pytest.fail('Placement must not show a modal dialog'))
    panel = win.special_panels['gem']
    win._start_special_draft('gem', panel.placement_values())
    win._move_special_draft((3, 3))
    before = win.doc.snapshot()
    undo_count = len(win.history._undo)
    assert not win.view.placement_error
    win._confirm_special((3, 3))
    assert win.doc.snapshot() == before
    assert len(win.history._undo) == undo_count
    assert win._special_draft is not None
    assert win.view.placement_error == 'Add at least one gem effect before placing it.'
    assert notices == [win.view.placement_error]
    assert win.view.preview_tint() == map_viewport.REJECTED_PREVIEW_TINT
    assert scene_object_styles(win.view)[-1] == (225, 115, 120, 3)
    assert win.view._placement_error_timer.isActive()
    win.view._placement_error_timer.timeout.emit()
    assert not win.view.placement_error
    assert win.view.preview_tint() == map_viewport.PREVIEW_TINT
    win._confirm_special((3, 3))
    win._special_changed('gem', panel.slot, {'actions': [
        dict(target_type='modify_vehicle', id=1, param='enable', val='1')]})
    assert not win.view.placement_error
    win._confirm_special((3, 3))
    assert win._special_draft is None
    assert win.doc.visible_gem_slots == 1
    assert win.doc.gems[1]['x'] == 3
    assert win.doc.gems[1]['actions']


def test_moving_or_cancelling_the_draft_clears_rejected_feedback(win, monkeypatch):
    monkeypatch.setattr(QToolTip, 'showText', lambda *args: None)
    win._start_special_draft('gem', win.special_panels['gem'].placement_values())
    win._confirm_special((3, 3))
    assert win.view.placement_error
    win._move_special_draft((4, 4))
    assert not win.view.placement_error
    win._confirm_special((4, 4))
    win._cancel_operation(clear=False)
    assert not win.view.placement_error
    assert not win.view._placement_error_timer.isActive()


def test_software_feedback_tints_whole_preview_and_preserves_other_pixels(monkeypatch):
    rgba = np.full((2, 3, 4), 100, np.uint8)
    rgba[:, :, 3] = 255
    ids = np.array([[1, -1, -10], [2, -2, 0]])
    frame = SimpleNamespace(rgba=rgba, cell_ids=ids)
    monkeypatch.setattr(map_viewport, 'render_scene', lambda *args, **kwargs: frame)
    job = map_viewport._RenderJob([], None, None, (False,), 0,
                                 SimpleNamespace(width=3), ((0, 0),), (-10,),
                                 map_viewport.REJECTED_PREVIEW_TINT)
    results = []
    job.signals.finished.connect(results.append)
    job.run()
    assert results and results[0][-1] is None
    for x in range(3):
        assert rgba[0, x, 0] > rgba[0, x, 1] + 50
    assert np.all(rgba[1, :, :3] == 100)
    assert np.all(rgba[:, :, 3] == 255)


def test_native_gpu_rejected_gem_is_tinted_inside_its_silhouette(win, app, monkeypatch):
    if app.platformName() in ('offscreen', 'minimal') or not win.view.set_renderer_mode('gpu'):
        pytest.skip('Native GPU viewport unavailable')
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    QTest.qWait(100)
    view = win.view
    if view.renderer is None or view._software_fallback:
        pytest.fail(f'GPU initialization failed: {view.renderer_error}')
    monkeypatch.setattr(QToolTip, 'showText', lambda *args: None)
    win._start_special_draft('gem', win.special_panels['gem'].placement_values())
    win._move_special_draft((3, 3))
    grey = view._canvas.grabFramebuffer().convertToFormat(map_viewport.QImage.Format.Format_RGBA8888)
    win._confirm_special((3, 3))
    red = view._canvas.grabFramebuffer().convertToFormat(map_viewport.QImage.Format.Format_RGBA8888)
    red.save('build/gem-rejected-preview.png')
    assert view.renderer is not None and not view._software_fallback
    a = np.frombuffer(grey.bits(), np.uint8).reshape(grey.height(), grey.width(), 4).astype(int)
    b = np.frombuffer(red.bits(), np.uint8).reshape(red.height(), red.width(), 4).astype(int)
    assert np.count_nonzero((b[:, :, 0] - b[:, :, 1] > 40) &
                            (b[:, :, 0] - b[:, :, 1] > a[:, :, 0] - a[:, :, 1] + 30)) > 100
