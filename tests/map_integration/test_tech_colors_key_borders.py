import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QColor, QImage, QPainter

from .test_special_objects import app, win
from map_editor.core.special_objects import add_special, update_special
from map_editor.render.camera import IsoCamera
from .test_actor_editor import host, squad


@pytest.mark.parametrize('kind', ['squad', 'host'])
@pytest.mark.parametrize('surface', ['list', 'map'])
def test_actor_context_deselect_enables_add_without_editing_level(win, kind, surface):
    win.doc.squads = [squad()]
    win.doc.host_stations = [host()]
    win._sync_side_panels()
    panel = win.squad_panel if kind == 'squad' else win.host_panel
    tab = win.squad_tab_index if kind == 'squad' else win.host_tab_index
    win.palette_tabs.setCurrentIndex(tab)
    panel.list.setCurrentRow(0)
    baseline = win.doc.snapshot()
    if surface == 'list':
        win._panel_context_menu(panel.list, QPoint(1, 1))
    else:
        win._map_context_menu(QPoint(1, 1), QPoint(1, 1))
    label = 'Deselect Squad' if kind == 'squad' else 'Deselect Host Station'
    action = next(a for a in win._context_menu.actions() if a.text() == label)
    assert action.isEnabled()
    action.trigger()
    assert not panel.list.selectedItems()
    assert panel.add_button.isEnabled()
    assert win.doc.snapshot() == baseline
    win._context_menu.close()


def test_tech_popup_colors_do_not_follow_current_faction(win):
    panel = win.tech_panel
    for selected in range(panel.faction.count()):
        panel.faction.setCurrentIndex(selected)
        for index in range(panel.faction.count()):
            expected = QColor(*panel.colors.get(panel.faction.itemData(index), (180, 180, 180)))
            assert panel.faction.itemData(index, Qt.ItemDataRole.ForegroundRole) == expected


@pytest.mark.parametrize('kind', ['gate', 'item'])
@pytest.mark.parametrize('selected', [None, -1, 0])
def test_only_linked_key_glyphs_are_outlined_without_sector_borders(win, monkeypatch, kind, selected):
    from map_editor.ui.game_icons import game_icon
    for parent, keys in [((2, 2), [(4, 4), (5, 4)]), ((2, 6), [(5, 6)])]:
        slot = add_special(win.doc, kind)
        update_special(win.doc, kind, slot, dict(x=parent[0], y=parent[1], keys=keys), win.buildings)
    win._refresh_squads()
    win.view.selected_special = (kind, 1, selected) if selected is not None else None
    baseline = win.doc.snapshot()
    calls = []

    def icon(*args, **kwargs):
        calls.append(kwargs.get('outlined', False))
        return game_icon(*args, **kwargs)

    monkeypatch.setattr('map_editor.ui.special_overlay.game_icon', icon)
    monkeypatch.setattr('map_editor.ui.special_overlay.draw_game_badge', lambda *a, **k: None)
    image = QImage(800, 600, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)

    class Recorder:
        def __getattr__(self, name):
            return getattr(painter, name)

        def drawPolygon(self, *args):
            pytest.fail('Key glyphs must not draw enclosing sector polygons')

        def drawLine(self, *args):
            pytest.fail('Key sectors must not draw connection lines')

    camera = IsoCamera(width=800, height=600, zoom=.10, center=(5400, 0, -5400))
    try:
        win.special_overlay.draw(Recorder(), camera, win.view.terrain)
    finally:
        painter.end()
    assert calls == ([True, True, False] if selected is not None else [False, False, False])
    assert win.doc.snapshot() == baseline


@pytest.mark.parametrize('kind', ['gate_key', 'item_key', 'wave_key'])
def test_key_outline_preserves_glyph_interior_and_transparent_gaps(kind):
    import numpy as np
    from map_editor.ui.game_icons import game_icon
    def pixels(image):
        return np.frombuffer(image.bits(), np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()
    original = game_icon(kind, tight=False)
    outlined = game_icon(kind, tight=False, outlined=True)
    before, after = pixels(original), pixels(outlined)
    visible = before[:, :, 3] > 0
    assert np.array_equal(before[visible], after[visible])
    added = ~visible & (after[:, :, 3] > 0)
    assert added.any()
    assert np.all(after[added] == (255, 255, 255, 255))
    assert np.any(after[:, :, 3] == 0)
