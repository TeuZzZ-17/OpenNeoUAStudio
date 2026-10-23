import copy
import pytest

from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.locations import (LOCATION_COLORS, new_location, validate_location,
                                       contains_point, notice_opacity)
from map_editor.ui.location_panel import LOCATION_DRAFT_ROLE
from .test_special_objects import app, win


def area():
    return new_location(1) | {'name': 'Dairòpoli', 'points': [(1200, -1200), (4800, -1200),
                                                            (4800, -4800), (1200, -4800)]}


def test_location_roundtrip_and_absent_vanilla_data():
    doc = LdfDocument(mw=9, mh=9)
    assert 'begin_location' not in dumps_ldf(doc)
    assert loads_ldf(dumps_ldf(doc)).locations == []
    value = area() | {'sound': 'Data/Sounds/Locations/City.wav'}
    doc.locations = [value]
    text = dumps_ldf(doc)
    assert 'begin_location 1' in text and 'name = Dairòpoli' in text
    restored = loads_ldf(text)
    assert restored.locations == [value]
    assert dumps_ldf(restored) == text
    before = doc.snapshot()
    doc.locations.clear()
    doc.restore(before)
    assert doc.locations == [value]


def test_location_color_roundtrip_and_vanilla_default():
    doc = LdfDocument(mw=9, mh=9)
    value = area() | {'color': 'yellow'}
    doc.locations = [value]
    text = dumps_ldf(doc)
    assert 'color = yellow' in text
    assert loads_ldf(text).locations == [value]
    assert dumps_ldf(loads_ldf(text)) == text
    doc.locations = [area()]
    assert 'color =' not in dumps_ldf(doc)
    assert loads_ldf(dumps_ldf(doc)).locations == [area()]


def test_concave_polygon_and_closed_border():
    points = [(0, 0), (4, 0), (4, -1), (1, -1), (1, -4), (0, -4)]
    assert contains_point(points, .5, -3)
    assert not contains_point(points, 3, -3)
    assert contains_point(points, 4, -.5)
    assert contains_point(points, 0, 0)


@pytest.mark.parametrize('points', [[], [(1, -1), (2, -2)],
    [(0, 0), (2, -2), (0, -2), (2, 0)],
    [(0, 0), (1, -1), (2, -2)], [(0, 0), (1, float('nan')), (2, -2)]])
def test_invalid_polygons_are_rejected(points):
    with pytest.raises(ValueError):
        validate_location(area() | {'points': points})


def test_location_limits_paths_and_duplicates():
    validate_location(area())
    for change in ({'name': ''}, {'name': 'x' * 97}, {'sound': 'C:/Windows/test.wav'},
                   {'sound': 'Data/../test.wav'}, {'color': 'neon'}, {'fade_in_ms': -1}, {'hold_ms': 60001}):
        with pytest.raises(ValueError):
            validate_location(area() | change)
    doc = LdfDocument(mw=9, mh=9)
    doc.locations = [area(), area()]
    with pytest.raises(ValueError):
        dumps_ldf(doc)


def test_title_fade_timing():
    value = area()
    assert notice_opacity(value, 0) == 0
    assert notice_opacity(value, 225) == pytest.approx(.5)
    assert notice_opacity(value, 450) == 1
    assert notice_opacity(value, 3250) == 1
    assert notice_opacity(value, 3625) == pytest.approx(.5)
    assert notice_opacity(value, 4000) == 0
    assert notice_opacity(value | {'fade_in_ms': 0, 'fade_out_ms': 0}, 0) == 1


def test_location_drawing_fields_close_undo_and_cancel(win):
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools, panel = win.location_tools, win.location_panel
    before = win.doc.snapshot()
    tools.new()
    tools.draft['points'] = area()['points'][:]
    panel.name.setText('Dairòpoli')
    panel.change()
    assert tools.draft['points'] == area()['points']
    assert win.doc.snapshot() == before
    tools.close()
    assert len(win.doc.locations) == 1
    assert win.doc.locations[0]['name'] == 'Dairòpoli'
    assert loads_ldf(dumps_ldf(win.doc)).locations == win.doc.locations
    win.undo()
    assert win.doc.locations == []
    win.redo()
    assert len(win.doc.locations) == 1
    tools.new()
    tools.draft['points'] = [(1200, -1200)]
    tools.close()
    assert len(win.doc.locations) == 1 and tools.draft is not None
    tools.cancel()
    assert len(win.doc.locations) == 1


def test_location_lock_stays_after_find(win):
    from PySide6.QtWidgets import QPushButton
    row = win.location_panel.layout().itemAt(1).layout()
    buttons = [row.itemAt(index).widget() for index in range(row.count())]
    assert buttons[-2].text() == 'Find'
    assert buttons[-1] is win.location_panel.lock
    labels = {button.text() for button in win.location_panel.findChildren(QPushButton)}
    assert 'Draw perimeter' not in labels and 'Close Area' not in labels
    assert 'Cancel' in labels


def test_new_adds_immediate_draft_row(win):
    from PySide6.QtCore import Qt
    win.doc.locations = []
    win._sync_side_panels()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools, panel = win.location_tools, win.location_panel
    tools.new()
    assert win.doc.locations == []
    assert tools.draft is not None
    assert panel.list.count() == 1 and panel.list.currentRow() == 0
    item = panel.list.item(0)
    assert item.data(LOCATION_DRAFT_ROLE)
    assert item.text() == 'New location'
    panel.name.setText('Draft city')
    panel.color.setCurrentText('Cyan')
    panel.change()
    assert item.text() == 'Draft city'
    assert item.data(Qt.ItemDataRole.UserRole) == 'cyan'
    assert 'draft' in item.toolTip()
    panel.select(0)
    assert tools.draft is not None and win.doc.locations == []
    tools.cancel()
    assert panel.list.count() == 0


def test_location_context_menu_closes_draft(win):
    from PySide6.QtCore import QPoint
    win.doc.locations = []
    win._sync_side_panels()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools = win.location_tools
    tools.new()
    tools.draft['points'] = area()['points'][:2]
    win._map_context_menu(QPoint(60, 60), QPoint(60, 60))
    actions = {action.text(): action for action in win._context_menu.actions()}
    assert 'Close Area' in actions and not actions['Close Area'].isEnabled()
    win._context_menu.close()
    tools.draft['points'] = area()['points'][:]
    win._map_context_menu(QPoint(60, 60), QPoint(60, 60))
    actions = {action.text(): action for action in win._context_menu.actions()}
    assert actions['Close Area'].isEnabled()
    actions['Close Area'].trigger()
    assert tools.draft is None
    assert len(win.doc.locations) == 1
    assert win.doc.locations[0]['points'] == area()['points']
    win._context_menu.close()


def test_location_map_perimeter_uses_location_color(win, monkeypatch):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools = win.location_tools
    tools.selected = -1
    win.view.camera.perspective = False
    screen = dict(zip(area()['points'],
                      [QPointF(20, 20), QPointF(140, 20), QPointF(140, 140), QPointF(20, 140)]))
    monkeypatch.setattr(tools, '_screen', lambda point: screen[point])

    def edge(color):
        win.doc.locations = [area() | {'color': color}]
        image = QImage(160, 160, QImage.Format.Format_ARGB32)
        image.fill(QColor(20, 20, 20))
        painter = QPainter(image)
        tools.draw(painter)
        painter.end()
        return [image.pixelColor(80, y) for y in range(14, 27)]

    yellow, cyan = edge('yellow'), edge('cyan')
    assert max(pixel.red() for pixel in yellow) > 150
    assert max(pixel.blue() for pixel in cyan) > max(pixel.blue() for pixel in yellow) + 50


def test_location_color_selector_updates_name_field_and_list(win):
    from PySide6.QtCore import Qt
    win.doc.locations = [area()]
    win._sync_side_panels()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    panel = win.location_panel
    panel.list.setCurrentRow(0)
    assert panel.color.currentText() == 'White'
    assert panel.color.currentData() == 'white'
    panel.color.setCurrentText('Yellow')
    assert win.doc.locations[0]['color'] == 'yellow'
    red, green, blue = LOCATION_COLORS['yellow']
    assert f'{red}, {green}, {blue}' in panel.name.styleSheet()
    item = panel.list.item(0)
    assert item.text() == 'Dairòpoli'
    assert item.data(Qt.ItemDataRole.UserRole) == 'yellow'
    assert 'color = yellow' in dumps_ldf(win.doc)
    panel.color.setCurrentText('White')
    assert win.doc.locations[0]['color'] == 'white'
    assert 'color =' not in dumps_ldf(win.doc)


def test_locations_tab_checker_and_complete_rows(win):
    from studio.ui.shell import compatibility_entries, install_compatibility_markers, OPENNEOUA
    rows = compatibility_entries(win, 'map_editor')
    assert next(row for row in rows if row[0] == 'Locations')[1] == OPENNEOUA
    install_compatibility_markers(win, 'map_editor')
    assert win.palette_tabs._buttons[win.location_tab_index].marker.name() == '#e75f60'
    grid = win.palette_tabs._button_grid
    rows = {}
    for index in range(grid.count()):
        row, column, height, span = grid.getItemPosition(index)
        rows.setdefault(row, []).append((column, span))
    assert [len(row) for _, row in sorted(rows.items())] == [4, 3, 3, 3]
    assert all(sum(span for _, span in row) == 12 for row in rows.values())


def test_location_mouse_drawing_closure_vertices_and_navigation(win, app, monkeypatch):
    from PySide6.QtCore import Qt, QPoint, QPointF
    from PySide6.QtTest import QTest
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools = win.location_tools
    tools.new()
    expected = area()['points']
    for index, point in enumerate(expected):
        monkeypatch.setattr(tools, '_point', lambda pos, p=point: p)
        QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=QPoint(90 + index * 50, 100))
    assert tools.draft['points'] == expected
    yaw = win.view.camera.yaw
    pos = win.view.rect().center()
    QTest.mousePress(win.view, Qt.MouseButton.RightButton, pos=pos)
    QTest.mouseMove(win.view, pos + QPoint(50, 20))
    QTest.mouseRelease(win.view, Qt.MouseButton.RightButton, pos=pos + QPoint(50, 20))
    assert win.view.camera.yaw != yaw and tools.draft['points'] == expected
    for key in (Qt.Key.Key_W, Qt.Key.Key_Up):
        before = win.view.camera.center, win.view.camera.pan
        QTest.keyClick(win.view, key)
        assert (win.view.camera.center, win.view.camera.pan) != before
    QTest.keyClick(win.view, Qt.Key.Key_Backspace)
    assert tools.draft['points'] == expected[:-1]
    monkeypatch.setattr(tools, '_point', lambda pos: expected[0])
    monkeypatch.setattr(tools, '_screen', lambda p: QPointF(110, 120))
    QTest.mouseClick(win.view, Qt.MouseButton.LeftButton, pos=QPoint(110, 120))
    assert tools.draft is None and len(win.doc.locations[0]['points']) == 3
    # Vertex edits remain temporary until release and create one undoable change.
    previous = copy.deepcopy(win.doc.locations)
    QTest.mousePress(win.view, Qt.MouseButton.LeftButton, pos=QPoint(110, 120))
    monkeypatch.setattr(tools, '_point', lambda pos: (1300, -1300))
    QTest.mouseMove(win.view, QPoint(120, 130))
    assert win.doc.locations == previous
    QTest.mouseRelease(win.view, Qt.MouseButton.LeftButton, pos=QPoint(120, 130))
    assert win.doc.locations[0]['points'][0] == (1300, -1300)
    win.undo()
    assert win.doc.locations == previous


def test_location_cancel_switch_lock_and_resize(win):
    tools, panel = win.location_tools, win.location_panel
    win.doc.locations = [area()]
    win._sync_side_panels()
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    tools.new()
    tools.draft['points'] = area()['points'][:]
    win.palette_tabs.setCurrentIndex(0)
    assert tools.draft is None and len(win.doc.locations) == 1
    win.palette_tabs.setCurrentIndex(win.location_tab_index)
    panel.list.setCurrentRow(0)
    panel.lock.setChecked(True)
    panel.name.setText('Blocked')
    panel.change()
    assert win.doc.locations[0]['name'] == 'Dairòpoli'
    panel.lock.setChecked(False)
    assert win.doc.count_resize_out_of_bounds(3, 3)['locations'] == 1
    with pytest.raises(ValueError):
        win.doc.resize(3, 3)
    assert len(win.doc.locations) == 1
    win.doc.resize(3, 3, remove_out_of_bounds=True)
    assert win.doc.locations == []


def test_location_comments_cp1252_and_invalid_sound_components():
    doc = LdfDocument(mw=9, mh=9)
    doc.locations = [area()]
    text = dumps_ldf(doc).replace('begin_location 1', 'begin_location 1 ; comment')
    assert loads_ldf(text.encode('cp1252').decode('cp1252')).locations == doc.locations
    for sound in ('Data/', 'Data/./a.wav', 'Data//a.wav', 'Data/a.wav/'):
        with pytest.raises(ValueError):
            validate_location(area() | {'sound': sound})
