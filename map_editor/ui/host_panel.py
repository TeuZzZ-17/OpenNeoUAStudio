"""Host stations use the existing LDF fields and the shared model cards."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                              QComboBox, QSpinBox, QLineEdit, QCheckBox, QPushButton,
                              QLabel, QGroupBox, QGridLayout, QFileDialog, QInputDialog, QMessageBox)
from ..core.ldf_model import FACTIONS, HOST_AI_FIELDS, HOST_AI_BUDGET_FIELDS, make_host_ai, DEFAULT_HOST_POS_Y
from ..core.host_ai_presets import HOST_AI_PRESETS, CUSTOM_AI_PRESETS
from .preview_cards import PreviewList, set_card, faction_style, DETAIL_ROLE, form_vehicle
from .squad_panel import WorldCoordinateSpinBox


class HostPanel(QWidget):
    selected = Signal(int)
    valuesChanged = Signal(int, str, object)
    addRequested = Signal()
    removeRequested = Signal(int)
    playerRequested = Signal(int)

    def __init__(self):
        super().__init__()
        self.doc = None
        self.vehicles, self.colors = {}, {}
        self._loading = False
        layout = QVBoxLayout(self)
        self.search = QLineEdit(placeholderText='Filter host stations...')
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = PreviewList()
        layout.addLayout(self.list.preview_controls())
        layout.addWidget(self.list, 1)
        self.list.currentRowChanged.connect(self._selected)
        buttons = QHBoxLayout()
        for title, slot in (('Add', self.addRequested.emit),
                            ('Delete', lambda: self.removeRequested.emit(self.list.currentRow())),
                            ('Deselect', self.deselect)):
            button = QPushButton(title)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.owner = QComboBox()
        for key, name in FACTIONS.items():
            if key:
                self.owner.addItem(name, key)
        self.vehicle = QComboBox()
        self.vehicle.setEditable(True)
        self.vehicle.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.vehicle.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.vehicle.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.vehicle.setToolTip('Search a host from the scripts or enter its vehicle ID')
        self.energy = QSpinBox(minimum=0, maximum=2147483647, value=500000)
        self.height = WorldCoordinateSpinBox()
        self.height.setValue(DEFAULT_HOST_POS_Y)
        self.height.setToolTip('Height offset from the ground; negative values are above the surface.')
        self.hidden = QCheckBox('Hidden in briefing')
        self.player = QPushButton('Use this faction as player')
        self.player.clicked.connect(lambda: self.playerRequested.emit(self.list.currentRow()))
        for title, widget in (('Faction', self.owner), ('Host station', self.vehicle), ('Energy', self.energy), ('Height', self.height)):
            form.addRow(title, widget)
        form.addRow(self.hidden)
        form.addRow(self.player)
        advanced = QGroupBox('Advanced settings', checkable=True, checked=False)
        self.advanced = advanced
        advanced_layout = QVBoxLayout(advanced)
        self.advanced_fields = QWidget()
        fields = QFormLayout(self.advanced_fields)
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fields = {'pos_y': self.height}
        for key, title, low, high in (('reload_const', 'Reload constant', 0, 2147483647),
                                     ('viewangle', 'View angle', -2147483647, 2147483647)):
            spin = QSpinBox(minimum=low, maximum=high)
            self.fields[key] = spin
            if key == 'reload_const':
                default = QPushButton('Game default')
                default.setToolTip('0 lets the game derive the reload value from the host energy.')
                default.clicked.connect(lambda: self.fields['reload_const'].setValue(0))
                row = QHBoxLayout()
                row.addWidget(spin, 1)
                row.addWidget(default)
                fields.addRow(title, row)
            else:
                spin.setToolTip('Heading of the host view in degrees; does not rotate the host body.')
                fields.addRow(title, spin)
            spin.valueChanged.connect(lambda value, field=key: self._change(field, value))
        self.name = QLineEdit()
        fields.addRow('Comment', self.name)
        self.advanced_fields.hide()
        advanced.toggled.connect(self.advanced_fields.setVisible)
        advanced_layout.addWidget(self.advanced_fields)
        form.addRow(advanced)
        self.ai = QGroupBox('Host AI', checkable=True, checked=False)
        ai_layout = QVBoxLayout(self.ai)
        self.ai_preset = QComboBox()
        self.ai_preset.addItems(list(HOST_AI_PRESETS) + ['Custom'])
        self.ai_preset.activated.connect(lambda: self._change('ai_preset', self.ai_preset.currentText()))
        ai_layout.addWidget(self.ai_preset)
        preset_buttons = QHBoxLayout()
        for text, slot in (('Save preset…', self._save_preset), ('Load preset…', self._load_preset)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            preset_buttons.addWidget(button)
        ai_layout.addLayout(preset_buttons)
        self.ai_fields = QWidget()
        grid = QGridLayout(self.ai_fields)
        for col, title in enumerate(('Task', 'Budget %', 'Delay ms')):
            grid.addWidget(QLabel(title), 0, col)
        tasks = ('Conquest', 'Defense', 'Recon', 'Host attack', 'Power', 'Radar', 'Safety', 'Completion')
        self.ai_spins = {}
        for row, title in enumerate(tasks, 1):
            grid.addWidget(QLabel(title), row, 0)
            for col, key in enumerate(HOST_AI_FIELDS[(row-1)*2:row*2], 1):
                spin = QSpinBox(minimum=0, maximum=100 if key in HOST_AI_BUDGET_FIELDS else 2147483647)
                self.ai_spins[key] = spin
                spin.valueChanged.connect(lambda value, field=key: self._change('ai.' + field, value))
                grid.addWidget(spin, row, col)
        self.ai_fields.hide()
        self.ai.toggled.connect(self.ai_fields.setVisible)
        ai_layout.addWidget(self.ai_fields)
        form.addRow(self.ai)
        layout.addWidget(self.form_widget)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.status.hide()
        self.owner.currentIndexChanged.connect(lambda: self._change('owner', self.owner.currentData()))
        self.vehicle.activated.connect(self._vehicle_changed)
        self.vehicle.lineEdit().editingFinished.connect(self._vehicle_changed)
        self.energy.valueChanged.connect(lambda value: self._change('energy', value))
        self.height.valueChanged.connect(lambda value: self._change('pos_y', value))
        self.hidden.toggled.connect(lambda value: self._change('hidden', value))
        self.name.textEdited.connect(lambda value: self._change('custom_name', value.strip() or None))
        self._loading = True
        for key, spin in self.ai_spins.items():
            spin.setValue(make_host_ai()[key])
        self.ai_preset.setCurrentText('Balanced')
        self._loading = False

    def refresh(self, doc, vehicles, colors, selected=None):
        index = self.list.currentRow() if selected is None else selected
        self.doc, self.vehicles, self.colors = doc, vehicles, colors
        self._loading = True
        self.list.clear()
        self.list.addItems([''] * len(doc.host_stations))
        self.update_rows()
        previous_vehicle = self.vehicle.currentData()
        self.vehicle.clear()
        for key, visual in sorted(vehicles.items()):
            if visual.model.casefold() == 'robo':
                self.vehicle.addItem(f'{key} · {visual.name or "Host station"}', key)
        self.vehicle.setCurrentIndex(max(0, self.vehicle.findData(previous_vehicle)))
        self._loading = False
        self.list.setCurrentRow(index if 0 <= index < self.list.count() else -1)
        self._selected(self.list.currentRow())
        self._filter()
        self.list.previewsRequested.emit()

    def update_rows(self):
        ordered = self.doc.host_stations_for_save([host for host in self.doc.host_stations
                                                  if self.doc.cell_is_valid(host) and not host.get('_preview')])
        self.player_host = ordered[0] if ordered else None
        for index, host in enumerate(self.doc.host_stations):
            visual = self.vehicles.get(host['veh'])
            name = (visual.name if visual else '') or host.get('custom_name') or f"Host {host['veh']}"
            role = 'PLAYER' if host is self.player_host else 'AI'
            set_card(self.list.item(index), ('PREVIEW · ' if host.get('_preview') else '') + name,
                     f"{FACTIONS.get(host['owner'], '?')} · {role} · ID {host['veh']}\n"
                     f"Sector ({host['x']}, {host['y']}) · Energy {host['energy']:,}"
                     + (' · Briefing hidden' if host.get('hidden') else '')
                     + (f"\nAI: {host['ai'].get('preset', 'Custom')}" if role == 'AI' else '')
                     + (f"\n{host['custom_name']}" if host.get('custom_name') else ''),
                     (150, 150, 150) if host.get('_preview') else self.colors.get(host['owner'], (180, 180, 180)),
                     ('veh', host['veh']))
        self._filter()

    def _filter(self, *_args):
        text = self.search.text().casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text not in f'{item.text()} {item.data(DETAIL_ROLE)}'.casefold())
        self.list.previewsRequested.emit()

    def _selected(self, index):
        if self._loading:
            return
        valid = self.doc is not None and 0 <= index < len(self.doc.host_stations)
        self.form_widget.setEnabled(True)
        if valid:
            self._loading = True
            host = self.doc.host_stations[index]
            placing = any(item.get('_preview') for item in self.doc.host_stations)
            self.form_widget.setEnabled(not placing or bool(host.get('_preview')))
            self.owner.setCurrentIndex(self.owner.findData(host['owner']))
            faction_style(self.owner, self.colors)
            self.vehicle.setCurrentIndex(self.vehicle.findData(host['veh']))
            if self.vehicle.currentIndex() < 0:
                self.vehicle.setEditText(str(host['veh']))
            self.energy.setValue(host['energy'])
            self.hidden.setChecked(host.get('hidden', False))
            self.name.setText(host.get('custom_name') or '')
            for key, spin in self.fields.items():
                spin.setValue(host[key])
            for key, spin in self.ai_spins.items():
                spin.setValue(host['ai'][key])
            preset_name = host['ai'].get('preset', 'Custom')
            if preset_name not in HOST_AI_PRESETS and preset_name != 'Custom':
                CUSTOM_AI_PRESETS[preset_name] = dict(host['ai'])
            if self.ai_preset.findText(preset_name) < 0:
                self.ai_preset.addItem(preset_name)
            self.ai_preset.setCurrentText(preset_name)
            preset = HOST_AI_PRESETS.get(self.ai_preset.currentText(), {})
            self.ai_preset.setToolTip(preset.get('description', 'Adjust the budgets and delays below.'))
            self.ai.setEnabled(host is not self.player_host)
            self.player.setEnabled(host['owner'] != self.doc.player_owner and not host.get('_preview'))
            self.status.clear()
            self.status.hide()
            if placing and not host.get('_preview'):
                self.status.setText('Finish placement or press Esc to edit this host.')
                self.status.show()
            self._loading = False
        else:
            self.ai.setEnabled(True)
            self.player.setEnabled(False)
            faction_style(self.owner, self.colors)
        self.selected.emit(index)

    def _change(self, field, value):
        if self._loading:
            return
        if field == 'owner':
            faction_style(self.owner, self.colors)
        if field in ('ai_preset', 'ai_values'):
            values = ({**{key: spin.value() for key, spin in self.ai_spins.items()}, 'preset': 'Custom'}
                      if field == 'ai_preset' and value == 'Custom' else
                      make_host_ai(value) if field == 'ai_preset' else value)
            self._loading = True
            for key, spin in self.ai_spins.items():
                spin.setValue(values[key])
            if self.ai_preset.findText(values['preset']) < 0:
                self.ai_preset.addItem(values['preset'])
            self.ai_preset.setCurrentText(values['preset'])
            self._loading = False
            field, value = 'ai_values', values
        elif field.startswith('ai.'):
            self.ai_preset.setCurrentText('Custom')
        if self.doc is not None and self.list.currentRow() >= 0:
            self.valuesChanged.emit(self.list.currentRow(), field, value)

    def _vehicle_changed(self, *_args):
        if self._loading:
            return
        try:
            value = form_vehicle(self.vehicle)
        except ValueError:
            self.status.setText('Choose a host station or enter its numeric vehicle ID.')
            self.status.show()
            return
        if value is not None and value > 0:
            self._change('veh', value)

    def deselect(self):
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self._selected(-1)

    def placement_values(self):
        return dict(owner=self.owner.currentData(), veh=form_vehicle(self.vehicle),
                    energy=self.energy.value(), pos_y=self.height.value(),
                    reload_const=self.fields['reload_const'].value(), viewangle=self.fields['viewangle'].value(),
                    custom_name=self.name.text().strip() or None, hidden=self.hidden.isChecked(),
                    ai={**{key: spin.value() for key, spin in self.ai_spins.items()},
                        'preset': self.ai_preset.currentText()})

    def _save_preset(self):
        from ..core.host_ai_presets import save_ai_preset
        name, accepted = QInputDialog.getText(self, 'Save Host AI', 'Preset name:')
        if not accepted or not name.strip():
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save Host AI preset', name.strip() + '.json', 'Host AI (*.json)')
        if path:
            try:
                values = save_ai_preset(path, name.strip(), {key: spin.value() for key, spin in self.ai_spins.items()})
                self._change('ai_values', values)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, 'Host AI preset', str(error))

    def _load_preset(self):
        from ..core.host_ai_presets import load_ai_preset
        path, _ = QFileDialog.getOpenFileName(self, 'Load Host AI preset', '', 'Host AI (*.json)')
        if path:
            try:
                self._change('ai_values', load_ai_preset(path))
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, 'Host AI preset', str(error))
