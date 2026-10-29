import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QLabel
import pytest

from map_editor.core.ldf_model import LdfDocument
from map_editor.core.special_objects import add_special, set_item_key_road, update_special
from map_editor.render.special_scene import actor_code, scene_object_styles, special_members
from map_editor.ui.main_window import MainWindow
from map_editor.ui.special_overlay import SpecialOverlay
from map_editor.ui.special_panel import ITEM_TYPES, SpecialPanel


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    window = MainWindow(LdfDocument(mw=9, mh=9))
    window._icons.stop()
    yield window
    window._cancel_operation(clear=False)
    window._released(-1, -1, Qt.KeyboardModifier.NoModifier)
    window.dirty = False
    for timer in (window._repeat, window._script_timer, window._live_timer,
                  window.view._render_timer, window.view._settle, window.view._nav_timer):
        timer.stop()
    app.processEvents()
    window._icons.stop()
    for receiver in (window, *window.findChildren(QObject)):
        QCoreApplication.removePostedEvents(receiver)
    window.close()
    window._icon_pool.waitForDone(10000)
    window.view._pool.waitForDone(10000)
    window._briefing_pool.waitForDone(10000)
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_special_controls_use_requested_order_and_fixed_presets_disable_ids(app):
    panel = SpecialPanel('gate')
    doc = LdfDocument(mw=9, mh=9)
    slot = add_special(doc, 'gate')
    update_special(doc, 'gate', slot, {'targets': [42]})
    panel.refresh(doc, slot)

    assert not panel.fields['closed_bp'].isEnabled()
    assert not panel.fields['opened_bp'].isEnabled()
    panel.preset.setCurrentIndex(0)
    panel._preset()
    assert panel.fields['closed_bp'].isEnabled()
    assert panel.fields['opened_bp'].isEnabled()

    layouts = [panel.layout().itemAt(index).layout()
               for index in range(panel.layout().count())]
    action_row = next(layout for layout in layouts
                      if layout is not None and layout.indexOf(panel.add_button) >= 0)
    assert [action_row.itemAt(index).widget().text() for index in range(action_row.count())] == [
        'New', 'Delete', 'Find']
    placement_row = next(layout for layout in layouts
                         if layout is not None and layout.indexOf(panel.place) >= 0)
    assert placement_row.indexOf(panel.place) < placement_row.indexOf(panel.clear_button)
    form = panel.form_widget.layout()
    sector = next(label for label in panel.findChildren(QLabel) if label.text() == 'Sector')
    sector_row, _role = form.getWidgetPosition(sector)
    hidden_row, _role = form.getWidgetPosition(panel.hidden)
    assert hidden_row == sector_row + 1
    panel.deleteLater()


@pytest.mark.parametrize(('kind', 'values', 'field', 'expected'), [
    ('gate', {'targets': [42]}, 'targets', [42]),
    ('item', {'countdown': 45001, 'type': 2}, 'countdown', 45001),
    ('gem', {'blg': 65, 'type': 42}, 'type', 42),
])
def test_new_deselects_and_keeps_settings_for_each_special_kind(app, kind, values, field, expected):
    panel = SpecialPanel(kind)
    doc = LdfDocument(mw=9, mh=9)
    slot = add_special(doc, kind)
    update_special(doc, kind, slot, values)
    panel.refresh(doc, slot)
    emitted = []
    panel.addRequested.connect(lambda: emitted.append(True))
    panel.add_button.click()
    assert emitted == [True]
    assert panel.slot == 0
    assert panel.placement_values()[field] == expected
    assert 'then New' in panel.status.text()
    panel.deleteLater()


def test_item_type_picker_offers_only_supported_values_and_preserves_unknown(app):
    panel = SpecialPanel('item')
    doc = LdfDocument(mw=9, mh=9)
    slot = add_special(doc, 'item')
    update_special(doc, 'item', slot, {'type': 8})
    panel.refresh(doc, slot)

    picker = panel.fields['type']
    assert picker.itemText(0) == 'Default'
    assert any('parasite' in picker_text.lower() and 'available only for some sets' in picker_text.lower()
               for picker_text in (panel.preset.itemData(index, Qt.ItemDataRole.ToolTipRole) or ''
                                   for index in range(panel.preset.count())))
    selectable = [picker.itemData(index) for index in range(picker.count())
                  if picker.model().item(index).isEnabled()]
    assert ITEM_TYPES[0][0] == 1
    assert selectable == [1, 2]
    assert picker.value() == 8

    update_special(doc, 'item', slot, {'type': 9})
    panel.refresh(doc, slot)
    assert picker.value() == 9
    assert picker.count() == 3
    panel.deleteLater()


def test_item_key_find_and_roads_annotation(app):
    panel = SpecialPanel('item')
    doc = LdfDocument(mw=9, mh=9)
    slot = add_special(doc, 'item')
    update_special(doc, 'item', slot, {'x': 2, 'y': 2, 'keys': [(4, 3)]})
    set_item_key_road(doc, slot, 0, True)
    panel.refresh(doc, slot)

    assert 'with roads' in panel.keys.item(0).text().lower()
    panel.keys.setCurrentRow(0)
    focused = []
    panel.focusRequested.connect(focused.append)
    panel.key_find.click()
    assert focused == [(4, 3)]
    panel.deleteLater()


def test_key_add_and_move_preview_uses_drag_ghost_style():
    doc = LdfDocument(mw=9, mh=9)
    slot = add_special(doc, 'gate')
    update_special(doc, 'gate', slot, {'x': 2, 'y': 2, 'keys': [(3, 2), (3, 3)]})
    view = SimpleNamespace(doc=doc, lib=None, dragged_codes=set(), selected_special=None,
                           owner_colors={})

    for index, target in ((-1, (4, 2)), (0, (5, 2))):
        preview, preview_cell = MainWindow._key_preview_doc(
            SimpleNamespace(doc=doc, _key_preview_cell=target),
            doc, ('gate', slot, 'key', index))
        assert preview_cell == target
        preview_members = list(special_members(preview))
        preview_index = next(i for i, member in enumerate(preview_members)
                             if member.kind == 'gate' and member.slot == slot
                             and member.cell == target)
        assert preview_members[preview_index].preview

        view.doc = preview
        view.dragged_codes.clear()
        preview_style = scene_object_styles(view)[preview_index]
        assert preview_style == (160, 160, 160, 3)

        actual_members = list(special_members(doc))
        dragged_index = next(i for i, member in enumerate(actual_members)
                             if member.kind == 'gate' and member.slot == slot
                             and member.key == (0 if index < 0 else index))
        view.doc = doc
        view.dragged_codes = {actor_code(doc, dragged_index)}
        dragged_style = scene_object_styles(view)[dragged_index]
        assert dragged_style == preview_style

    assert doc.gates[slot]['keys'] == [(3, 2), (3, 3)]


def test_key_count_badges_are_drawn_only_for_gates(app, monkeypatch):
    doc = LdfDocument(mw=9, mh=9)
    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {'x': 2, 'y': 2, 'keys': [(3, 2), (3, 3)]})
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {'x': 5, 'y': 5, 'keys': [(6, 5)]})

    class Terrain:
        width = height = 9

        @staticmethod
        def cell_center(x, y):
            return x * 100.0, 0.0, y * 100.0

    class Camera:
        perspective = False

        @staticmethod
        def world_to_screen(position):
            return (position[0], position[2]), 0.0

    class Library:
        buildings = {}

        @staticmethod
        def mesh(_typ):
            return type('Mesh', (), {'bounds': (0, 100, 0)})()

    view = type('View', (), {
        'doc': doc, 'camera': Camera(), 'terrain': Terrain(), 'lib': Library(),
        'dragged_codes': set(), 'selected_special': None, 'owner_colors': {},
        'width': lambda self: 900, 'height': lambda self: 900,
    })()
    overlay = SpecialOverlay(type('Window', (), {'view': view})())
    icons = []
    monkeypatch.setattr('map_editor.ui.special_overlay.draw_game_badge',
                        lambda _painter, _anchor, _size, icon, *_args: icons.append(icon))
    monkeypatch.setattr('map_editor.ui.special_overlay.game_icon',
                        lambda *_args, **_kwargs: QImage(2, 2, QImage.Format.Format_RGBA8888))
    image = QImage(900, 900, QImage.Format.Format_RGBA8888)
    painter = QPainter(image)
    overlay.draw(painter)
    painter.end()

    assert icons.count('gate_key') == 2
    assert 'item_key' not in icons


def test_special_selection_highlight_moves_between_items_gates_and_buildings(window):
    doc = window.doc
    gate = add_special(doc, 'gate')
    update_special(doc, 'gate', gate, {'x': 2, 'y': 2, 'keys': [(2, 3), (3, 3)]})
    item = add_special(doc, 'item')
    update_special(doc, 'item', item, {'x': 5, 'y': 2, 'keys': [(5, 3), (6, 3)]})
    window._refresh_specials()

    window.palette_tabs.setCurrentIndex(window.special_tab_indices['item'])
    assert window.view.selected_special[:2] == ('item', item)
    window.palette_tabs.setCurrentIndex(window.special_tab_indices['gate'])
    assert window.view.selected_special[:2] == ('gate', gate)

    members = list(special_members(doc, window.view.lib))
    offset = len(doc.squads) + len(doc.host_stations)
    styles = scene_object_styles(window.view)[offset:offset + len(members)]
    gate_key_styles = [style[3] for member, style in zip(members, styles)
                       if member.kind == 'gate' and member.key >= 0]
    item_key_styles = [style[3] for member, style in zip(members, styles)
                       if member.kind == 'item' and member.key >= 0]
    assert gate_key_styles == [2, 2]
    assert item_key_styles == [1, 1]

    doc.grids['blg'][6][6] = '01'
    window.palette_tabs.setCurrentIndex(1)
    window._begin_building_drag((6, 6))
    window._finish_building_drag()
    assert window.view.selected_building == (6, 6)
    assert window.view.selected_special is None

    members = list(special_members(doc, window.view.lib))
    styles = scene_object_styles(window.view)[offset:offset + len(members)]
    assert all(style[3] != 2 for member, style in zip(members, styles)
               if member.kind in ('gate', 'item') and member.key >= 0)
