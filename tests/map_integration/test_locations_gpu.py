"""Run explicitly with QT_QPA_PLATFORM=windows and NME_RENDERER=gpu."""
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from .test_special_objects import app, win
from .test_locations import area


def test_locations_overlay_uses_native_gpu_view(win, app, monkeypatch):
    win.resize(1100, 800)
    win.show()
    win.view.set_renderer_mode('gpu')
    app.processEvents()
    QTest.qWait(100)
    assert win.view.renderer_mode == 'gpu', win.view.renderer_error
    win._icons.stop()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    points = area()['points']
    screen = [(80, 100), (300, 100), (300, 300), (80, 300)]
    monkeypatch.setattr(win.location_tools, '_screen', lambda p: QPointF(*screen[points.index(p)]))
    before = win.view._canvas.grabFramebuffer()
    win.doc.locations = [area() | {'color': 'yellow'}]
    win.location_tools.selected = 0
    win.view.update()
    app.processEvents()
    after = win.view._canvas.grabFramebuffer()
    assert before != after
    # A selected polygon's yellow stroke is drawn through the shared native overlay.
    ratio = after.width() / win.view._canvas.width()
    colors = [after.pixelColor(round(80 * ratio) + dx, round(180 * ratio)) for dx in (-1, 0, 1)]
    assert any(c.red() > 200 and c.green() > 150 and c.blue() < 150 for c in colors)
