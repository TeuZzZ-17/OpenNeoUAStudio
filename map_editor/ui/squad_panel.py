"""Compact editing of the fields actually supported by begin_squad."""
from PySide6.QtCore import Signal, QEvent, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                              QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit,
                              QCheckBox, QPushButton, QLabel, QGroupBox, QAbstractItemView)
from ..core.ldf_model import FACTIONS
from ..render.squad_scene import squad_xz
from .preview_cards import PreviewList, set_card, faction_style, DETAIL_ROLE, form_vehicle


class WorldCoordinateSpinBox(QDoubleSpinBox):
    def __init__(self):
        super().__init__(minimum=-2147483647, maximum=2147483647, decimals=15)

    def textFromValue(self, value):
        return self.locale().toString(value, 'g', 15)


class SquadPanel(QWidget):
    selected = Signal(object)
    valuesChanged = Signal(object, str, object)
    addRequested = Signal()
    removeRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self._loading = False
        self.colors = {}
        self.vehicles = {}
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit(placeholderText='Filter squads...')
        self.faction_filter = QComboBox()
        self.faction_filter.addItem('All factions', None)
        for key, name in FACTIONS.items():
            self.faction_filter.addItem(name, key)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.faction_filter)
        layout.addLayout(filters)
        self.list = PreviewList()
        layout.addLayout(self.list.preview_controls())
        self.search.textChanged.connect(self._filter)
        self.faction_filter.currentIndexChanged.connect(self._filter)
        self.list.setMinimumHeight(170)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.itemSelectionChanged.connect(self._selected)
        self.list.viewport().installEventFilter(self)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, slot in (("Add", self.addRequested.emit),
                           ("Delete", lambda: self.removeRequested.emit(self.selected_indices())),
                           ("Deselect", lambda: self.set_selection(set()))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.multiple_notice = QLabel('Multiple selection: fields show the current squad. Changing a field updates that field on every selected squad.')
        self.multiple_notice.setWordWrap(True)
        self.multiple_notice.hide()
        form.addRow(self.multiple_notice)
        self.owner = QComboBox()
        for key, name in FACTIONS.items():
            self.owner.addItem(name, key)
        self.vehicle = QComboBox()
        self.vehicle.setEditable(True)
        self.vehicle.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.vehicle.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.vehicle.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.vehicle.setToolTip("Search by name or enter the vehicle ID")
        self.count = QSpinBox(minimum=1, maximum=2147483647, value=1)
        self.name = QLineEdit()
        self.hidden = QCheckBox("Hidden in briefing")
        self.useable = QCheckBox("Usable by the host AI")
        self.x = WorldCoordinateSpinBox()
        self.z = WorldCoordinateSpinBox()
        for title, widget in (("Faction", self.owner), ("Vehicle", self.vehicle),
                              ("Count", self.count)):
            form.addRow(title, widget)
        form.addRow(self.hidden)
        form.addRow(self.useable)
        self.advanced = QGroupBox('Advanced settings', checkable=True, checked=False)
        advanced_layout = QVBoxLayout(self.advanced)
        self.advanced_fields = QWidget()
        advanced_form = QFormLayout(self.advanced_fields)
        for title, widget in (('World X', self.x), ('World Z', self.z), ('Comment', self.name)):
            advanced_form.addRow(title, widget)
        self.advanced_fields.hide()
        self.advanced.toggled.connect(self.advanced_fields.setVisible)
        advanced_layout.addWidget(self.advanced_fields)
        form.addRow(self.advanced)
        layout.addWidget(self.form_widget)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.owner.currentIndexChanged.connect(lambda: self._change('owner', self.owner.currentData()))
        self.vehicle.activated.connect(self._vehicle_changed)
        self.vehicle.lineEdit().editingFinished.connect(self._vehicle_changed)
        self.count.valueChanged.connect(lambda v: self._change('num', v))
        self.name.textEdited.connect(lambda v: self._change('custom_name', v.strip() or None))
        self.hidden.toggled.connect(lambda v: self._change('hidden', v))
        self.useable.toggled.connect(lambda v: self._change('useable', v))
        self.x.valueChanged.connect(lambda v: self._change('pos_x', v))
        self.z.valueChanged.connect(lambda v: self._change('pos_z', v))

    def eventFilter(self, obj, event):
        if (obj is self.list.viewport() and event.type() == QEvent.Type.MouseButtonDblClick
                and self.list.itemAt(event.position().toPoint()) is None):
            self.set_selection(set())
            return True
        return super().eventFilter(obj, event)

    def selected_indices(self):
        return {self.list.row(item) for item in self.list.selectedItems()}

    def set_selection(self, indices):
        indices = set(indices)
        self.list.blockSignals(True)
        current = self.list.currentRow()
        if current not in indices:
            self.list.setCurrentRow(min(indices, default=-1))
        for i in range(self.list.count()):
            self.list.item(i).setSelected(i in indices)
        self.list.blockSignals(False)
        self._selected()

    def refresh(self, doc, vehicles, selected=None, colors=None):
        self.doc = doc
        self.vehicles = vehicles
        if colors is not None:
            self.colors = colors
        old = self.selected_indices() if selected is None else ({selected} if isinstance(selected, int) else set(selected))
        self._loading = True
        self.list.blockSignals(True)
        self.list.clear()
        for _ in doc.squads:
            self.list.addItem('')
        self.list.blockSignals(False)
        self.vehicle.blockSignals(True)
        current_vehicle = self.vehicle.currentData()
        self.vehicle.clear()
        for key, visual in sorted(vehicles.items()):
            self.vehicle.addItem(f"{key} · {visual.name or visual.model or 'Vehicle'}", key)
        self.vehicle.blockSignals(False)
        self.vehicle.setCurrentIndex(max(0, self.vehicle.findData(current_vehicle)))
        self.update_rows()
        self._loading = False
        self.set_selection({i for i in old if 0 <= i < len(doc.squads)})

    def update_rows(self):
        signals_blocked = self.list.blockSignals(True)
        for i, squad in enumerate(self.doc.squads):
            visual = self.vehicles.get(squad['veh'])
            name = (visual.name if visual else '') or f"Vehicle {squad['veh']}"
            flags = (' · Briefing hidden' if squad.get('hidden') else '') + (' · Host AI' if squad.get('useable') else '')
            preview = 'PREVIEW · ' if squad.get('_preview') else ''
            item = self.list.item(i)
            set_card(item, f"{preview}{i + 1}. {name} ×{squad['num']}",
                     f"{FACTIONS.get(squad['owner'], '?')} · ID {squad['veh']}\n"
                     f"Sector ({squad['x']}, {squad['y']}){flags}"
                     + (f"\n{squad['custom_name']}" if squad.get('custom_name') else ''),
                     (150, 150, 150) if squad.get('_preview') else
                     self.colors.get(squad['owner'], (180, 180, 180)), ('veh', squad['veh']))
        self.list.blockSignals(signals_blocked)
        self._filter()
        self.list.previewsRequested.emit()

    def _filter(self, *_args):
        if self.doc is None:
            return
        faction = self.faction_filter.currentData()
        text = self.search.text().casefold()
        for i, squad in enumerate(self.doc.squads):
            item = self.list.item(i)
            item.setHidden((faction is not None and squad['owner'] != faction) or
                           text not in f'{item.text()} {item.data(DETAIL_ROLE)}'.casefold())
        self.list.previewsRequested.emit()

    def _selected(self):
        if self._loading:
            return
        indices = self.selected_indices()
        index = self.list.currentRow() if self.list.currentRow() in indices else min(indices, default=-1)
        valid = self.doc is not None and 0 <= index < len(self.doc.squads)
        self.form_widget.setEnabled(True)
        self.multiple_notice.setVisible(len(indices) > 1)
        self.status.setText("Add a squad, then click the map to insert it." if not valid else
                           f'{len(indices)} selected · Changes apply immediately')
        if valid:
            self._loading = True
            squad = self.doc.squads[index]
            self.owner.setCurrentIndex(self.owner.findData(squad['owner']))
            faction_style(self.owner, self.colors)
            i = self.vehicle.findData(squad['veh'])
            self.vehicle.setCurrentIndex(i)
            if i < 0:
                self.vehicle.setEditText(str(squad['veh']))
            self.count.setValue(squad['num'])
            self.name.setText(squad.get('custom_name') or '')
            self.hidden.setChecked(squad.get('hidden', False))
            self.useable.setChecked(squad.get('useable', False))
            x, z = squad_xz(squad)
            self.x.setValue(x)
            self.z.setValue(z)
            self.x.setEnabled(len(indices) == 1)
            self.z.setEnabled(len(indices) == 1)
            self._loading = False
        else:
            self.x.setEnabled(True)
            self.z.setEnabled(True)
            faction_style(self.owner, self.colors)
        self.selected.emit(indices)

    def _change(self, field, value):
        if field == 'owner':
            faction_style(self.owner, self.colors)
        if not self._loading and self.doc is not None and self.selected_indices():
            self.valuesChanged.emit(self.selected_indices(), field, value)

    def _vehicle_changed(self, *_args):
        if self._loading:
            return
        try:
            vehicle = form_vehicle(self.vehicle)
        except ValueError:
            self.status.setText("Choose a vehicle or enter its numeric ID.")
            return
        if not vehicle or vehicle < 1:
            self.status.setText("Vehicle ID must be positive.")
            return
        self._change('veh', vehicle)

    def placement_values(self):
        return dict(owner=self.owner.currentData(), veh=form_vehicle(self.vehicle),
                    num=self.count.value(), hidden=self.hidden.isChecked(),
                    useable=self.useable.isChecked(), custom_name=self.name.text().strip() or None)
