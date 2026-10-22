"""The compact search filters choices without editing an actor implicitly."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from .test_special_objects import app
from map_editor.core.ldf_model import LdfDocument
from map_editor.ui.squad_panel import SquadPanel
from map_editor.ui.host_panel import HostPanel
from map_editor.ui.special_panel import SpecialPanel


@pytest.mark.parametrize('kind', ['squad', 'host'])
def test_unit_search_preserves_selection_and_manual_picker(app, kind):
    panel = SquadPanel() if kind == 'squad' else HostPanel()
    doc = LdfDocument(mw=9, mh=9)
    vehicles = {
        1: SimpleNamespace(name='Tiger', model='robo'),
        2: SimpleNamespace(name='Firefly', model='robo'),
    }
    if kind == 'squad':
        panel.refresh(doc, vehicles)
    else:
        panel.refresh(doc, vehicles, {})
    panel.resize(650, 850)
    panel.show()
    app.processEvents()
    changes = []
    panel.valuesChanged.connect(lambda *args: changes.append(args))
    combo, search = panel.vehicle, panel.vehicle_row.search
    original = combo.currentData()
    before = doc.snapshot()
    QTest.keyClicks(search, 'FiRe')
    assert combo.view().isRowHidden(combo.findData(1))
    assert not combo.view().isRowHidden(combo.findData(2))
    assert combo.currentData() == original
    assert not changes and doc.snapshot() == before
    combo.showPopup()
    app.processEvents()
    result = combo.model().index(combo.findData(2), 0)
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=combo.view().visualRect(result).center())
    assert panel.placement_values()['veh'] == 2
    # Search stays applied when the main window refreshes its side panels.
    if kind == 'squad':
        panel.refresh(doc, vehicles)
    else:
        panel.refresh(doc, vehicles, {})
    assert combo.view().isRowHidden(combo.findData(1))
    assert search.geometry().center().y() == combo.geometry().center().y()
    search.clear()
    assert all(not combo.view().isRowHidden(i) for i in range(combo.count()))
    # The original editable menu remains available.
    assert panel.placement_values()['veh'] == 2
    panel.settings_lock.click()
    assert not search.isEnabled()
    panel.close()


def test_super_item_preset_label_stays_beside_compact_picker(app):
    panel = SpecialPanel('item')
    panel.resize(600, 1000)
    panel.show()
    app.processEvents()
    form = panel.form_widget.layout()
    label = form.labelForField(panel.preset)
    assert label.text() == 'Model Preset'
    assert abs(label.geometry().center().y() - panel.preset.geometry().center().y()) <= 2
    assert panel.preset.itemText(2) == 'Parasite*'
    assert panel.preset.itemData(2) == dict(inactive_bp=68, active_bp=69, trigger_bp=70)
    panel.close()


@pytest.mark.parametrize('kind', ['item', 'gem', 'gate'])
def test_special_object_count_is_centered_above_main_buttons(app, kind):
    panel = SpecialPanel(kind)
    panel.resize(600, 1000)
    panel.show()
    app.processEvents()
    assert panel.count.alignment() == Qt.AlignmentFlag.AlignCenter
    assert panel.count.geometry().bottom() < panel.add_button.geometry().top()
    assert panel.count.geometry().center().x() == panel.list.geometry().center().x()
    panel.close()
