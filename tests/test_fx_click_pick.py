"""Visuals & FX preview: click an effect in the 3D view to select it."""

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('NME_RENDERER', 'software')

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer.viewport import CollisionViewport


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _decal(source, x):
    return fx.PreviewItem(source, fx.MODE_DECAL, 0, (x, 0.0, 0.0), fx.IDENTITY,
                          (1.0, 1.0, 1.0), 1.0, None, "decal", texture="",
                          decal_size=(120.0, 120.0))


def _smoke(source, x):
    return fx.PreviewItem(source, "decal_smoke", 0, (x, -60.0, 0.0), fx.IDENTITY,
                          (90.0, 90.0, 90.0), 0.8, (200, 200, 200, 255), "smoke")


def _paint(viewport):
    viewport._yaw, viewport._pitch, viewport._scale = 35.0, 32.0, 1.0 / 260.0
    image = QImage(600, 400, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.black)
    painter = QPainter(image)
    viewport._paint_fx_viewport(painter)
    painter.end()
    return image


def _screen(viewport, point):
    target, camera = viewport.rect().toRectF(), viewport._camera_state()
    return viewport._fx_project(point, target, camera)


@pytest.fixture
def viewport(app):
    widget = CollisionViewport()
    widget.resize(600, 400)
    yield widget
    widget.close()


def test_click_picks_the_effect_under_the_point(viewport):
    viewport.set_fx_preview(active=True, items=[_decal(3, -300.0), _smoke(5, 300.0)],
                            show_unit=False, weapon_context=True)
    _paint(viewport)
    assert viewport.fx_pick_at(_screen(viewport, (-300.0, 0.0, 0.0))) == 3
    assert viewport.fx_pick_at(_screen(viewport, (300.0, -60.0, 0.0))) == 5
    assert viewport.fx_pick_at(QPoint(2, 2).toPointF()) is None


def test_unit_and_hidden_items_are_not_pickable(viewport):
    unit = fx.PreviewItem(-1, "unit", 0, (0.0, 0.0, 0.0), fx.IDENTITY, (1.0, 1.0, 1.0))
    viewport.set_fx_preview(active=True, items=[unit], show_unit=False)
    _paint(viewport)
    assert viewport._fx_pick_shapes == {}
    viewport.set_fx_preview(active=True, items=[], show_unit=False)
    _paint(viewport)
    assert viewport.fx_pick_at(_screen(viewport, (0.0, 0.0, 0.0))) is None


def test_selected_effect_gets_a_white_outline(viewport):
    items = [_decal(3, 0.0)]
    viewport.set_fx_preview(active=True, items=items, show_unit=False, highlight=None)
    plain = _paint(viewport)
    viewport.set_fx_preview(active=True, items=items, show_unit=False, highlight=3)
    outlined = _paint(viewport)
    white = [(x, y) for x in range(0, 600) for y in range(0, 400)
             if outlined.pixelColor(x, y).red() > 250
             and outlined.pixelColor(x, y).green() > 250
             and outlined.pixelColor(x, y).blue() > 250
             and plain.pixelColor(x, y) != outlined.pixelColor(x, y)]
    assert len(white) > 40
    viewport.set_fx_preview(active=True, items=items, show_unit=False, highlight=9)
    assert _paint(viewport) == plain, "another effect selected: no outline here"


def test_click_without_drag_emits_the_effect_and_a_drag_does_not(viewport):
    viewport.set_fx_preview(active=True, items=[_decal(3, 0.0)], show_unit=False)
    _paint(viewport)
    picked = []
    viewport.fxEffectPicked.connect(picked.append)
    centre = _screen(viewport, (0.0, 0.0, 0.0)).toPoint()
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=centre)
    assert picked == [3]
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=centre)
    QTest.mouseMove(viewport, centre + QPoint(30, 12))
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=centre + QPoint(30, 12))
    assert picked == [3], "a camera drag must not pick"


def test_no_picking_when_the_fx_preview_is_off(viewport):
    viewport.set_fx_preview(active=False)
    picked = []
    viewport.fxEffectPicked.connect(picked.append)
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=QPoint(300, 200))
    assert picked == []


def test_outline_follows_selected_rows_not_a_bare_current_row(app):
    from PySide6.QtWidgets import QListWidgetItem
    from entity_designer.fx_panel import DestructionFxPanel

    panel = DestructionFxPanel()
    try:
        view = panel.effects_view
        view.list.clear()
        for index in range(3):
            row = QListWidgetItem(f"effect {index}")
            row.setData(Qt.ItemDataRole.UserRole, index)
            view.list.addItem(row)
        view.list.setCurrentRow(1)
        assert panel.highlight == 1
        view.list.clearSelection()       # current row stays, nothing is selected
        assert panel.highlight is None
        view.select_effect(2)
        assert panel.highlight == 2
    finally:
        panel.close()


def test_double_click_away_from_effects_deselects_and_on_an_effect_does_not(viewport):
    viewport.set_fx_preview(active=True, items=[_decal(3, 0.0)], show_unit=False)
    _paint(viewport)
    requests = []
    viewport.fxDeselectRequested.connect(lambda: requests.append(1))
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert requests == [1]
    centre = _screen(viewport, (0.0, 0.0, 0.0)).toPoint()
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=centre)
    assert requests == [1]


def test_deselect_all_clears_rows_and_outline(app):
    from PySide6.QtWidgets import QListWidgetItem
    from entity_designer.fx_panel import DestructionFxPanel

    panel = DestructionFxPanel()
    try:
        view = panel.effects_view
        view.list.clear()
        row = QListWidgetItem("effect")
        row.setData(Qt.ItemDataRole.UserRole, 0)
        view.list.addItem(row)
        view.select_effect(0)
        assert panel.highlight == 0
        view.deselect_all()
        assert panel.highlight is None and not view.list.selectedItems()
    finally:
        panel.close()
