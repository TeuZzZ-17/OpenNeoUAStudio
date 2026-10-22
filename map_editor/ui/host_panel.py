"""Host stations use the existing LDF fields and the shared model cards."""
from PySide6.QtCore import Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                              QComboBox, QSpinBox, QLineEdit, QCheckBox, QPushButton,
                              QLabel, QGroupBox, QGridLayout, QFileDialog, QInputDialog, QMessageBox)
from ..core.ldf_model import FACTIONS, HOST_AI_FIELDS, HOST_AI_BUDGET_FIELDS, make_host_ai, DEFAULT_HOST_POS_Y
from ..core.host_ai_presets import HOST_AI_PRESETS, CUSTOM_AI_PRESETS
from .settings_lock import SettingsLockButton
from .preview_cards import PreviewList, set_card, faction_style, DETAIL_ROLE, DRAFT_ROLE, form_vehicle, VehicleSearchRow
from .squad_panel import WorldCoordinateSpinBox, coordinate_speed_slider
from ..render.squad_scene import squad_xz


ENERGY_PRESETS = (
    ('Very low',150000), ('Low',300000), ('Medium-low',500000), ('Medium',800000),
    ('Medium-high',1000000), ('High',1200000), ('Very high',2000000), ('Extreme',3000000),
)


class HostEnergyCombo(QComboBox):
    """Named energy choices retain the exact integer, including imported custom values."""
    valueChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self._value = 500000
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for title,value in ENERGY_PRESETS:
            self.addItem(f'{title} ({value:,})', value)
        self.setValue(self._value)
        self.currentIndexChanged.connect(self._commit_energy)
        self.lineEdit().editingFinished.connect(self._commit_energy)
        self.setToolTip('Choose an energy preset, or replace the text with an exact integer (0-2147483647).')

    def value(self):
        return self._value

    def setValue(self, value):
        value = max(0,min(2147483647,int(value)))
        changed = value != self._value
        self._value = value
        blocked = self.blockSignals(True)
        index = self.findData(value)
        self.setCurrentIndex(index)
        if index < 0:
            self.setEditText(str(value))
        self.blockSignals(blocked)
        if changed:
            self.valueChanged.emit(value)

    def _commit_energy(self, *_args):
        if self.currentIndex() >= 0 and self.currentText() == self.itemText(self.currentIndex()):
            value = self.currentData()
        else:
            try:
                value = int(self.currentText().strip())
            except ValueError:
                self.setValue(self._value)
                return
        if not 0 <= value <= 2147483647:
            self.setValue(self._value)
            return
        self.setValue(value)


class HostPanel(QWidget):
    selected = Signal(int)
    valuesChanged = Signal(int, str, object)
    addRequested = Signal()
    removeRequested = Signal(int)
    playerRequested = Signal(int)
    clearRequested = Signal()
    findRequested = Signal()
    povRequested = Signal()

    def __init__(self):
        super().__init__()
        self.doc = None
        self.vehicles, self.colors = {}, {}
        self._loading = False
        self.settings_lock = SettingsLockButton()
        layout = QVBoxLayout(self)
        self.search = QLineEdit(placeholderText='Filter host stations')
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = PreviewList()
        self.list.preview_level = 1
        layout.addLayout(self.list.preview_controls())
        layout.addWidget(self.list, 1)
        self.list.currentRowChanged.connect(self._selected)
        self.player = QPushButton('Use This Faction as Player')
        self.player.clicked.connect(lambda: self.playerRequested.emit(self.list.currentRow()))
        layout.addWidget(self.settings_lock.wrap(self.player))
        buttons = QHBoxLayout()
        for title, slot in (('New', self._new),
                            ('Delete', lambda: self.removeRequested.emit(self.list.currentRow())),
                            ('Find', self.findRequested.emit)):
            button = QPushButton(title)
            button.clicked.connect(slot)
            buttons.addWidget(button)
            if title == 'New':
                self.add_button = button
                button.setToolTip('Deselect the current host and start a new one with its settings.')
            elif title == 'Find':
                self.find_button = button
            elif title == 'Delete':
                self.delete_button = button
        layout.addLayout(buttons)
        count_row = QHBoxLayout()
        self.total = QLabel()
        count_row.addWidget(self.total, 1)
        self.pov_button = QPushButton('Enter POV')
        self.pov_button.setEnabled(False)
        self.pov_button.clicked.connect(self.povRequested.emit)
        count_row.addWidget(self.settings_lock)
        count_row.addWidget(self.pov_button)
        self.clear_button = QPushButton('Delete all')
        self.clear_button.clicked.connect(self.clearRequested.emit)
        count_row.addWidget(self.clear_button)
        layout.addLayout(count_row)
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
        self.vehicle_row = VehicleSearchRow(self.vehicle)
        self.energy = HostEnergyCombo()
        self.height = WorldCoordinateSpinBox()
        self.height.setValue(DEFAULT_HOST_POS_Y)
        self.height.setToolTip('Height offset from the ground; negative values are above the surface.')
        self.hidden = QCheckBox('Hidden in Briefing')
        for title, widget in (('Faction', self.owner), ('Host station', self.vehicle_row), ('Energy', self.energy), ('Height', self.height)):
            form.addRow(title, widget)
        form.addRow(self.hidden)
        advanced = QGroupBox('Advanced Settings', checkable=True, checked=False)
        self.advanced = advanced
        advanced_layout = QVBoxLayout(advanced)
        self.advanced_fields = QWidget()
        fields = QFormLayout(self.advanced_fields)
        fields.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.x, self.z = WorldCoordinateSpinBox(), WorldCoordinateSpinBox()
        fields.addRow('World X', self.x)
        fields.addRow('World Z', self.z)
        self.coordinate_speed = coordinate_speed_slider(fields, self.x, self.z)
        self.x.valueChanged.connect(lambda value: self._change('pos_x', value))
        self.z.valueChanged.connect(lambda value: self._change('pos_z', value))
        self.fields = {'pos_y': self.height}
        for key, title, low, high in (('reload_const', 'Reload constant', 0, 2147483647),
                                     ('viewangle', 'View angle (POV)', -2147483647, 2147483647)):
            spin = QSpinBox(minimum=low, maximum=high)
            self.fields[key] = spin
            if key == 'reload_const':
                default = QPushButton('Game Default')
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
        self.ai = QGroupBox('Host AI', checkable=True, checked=False)
        ai_layout = QVBoxLayout(self.ai)
        self.ai_preset = QComboBox()
        self.ai_preset.addItems(list(HOST_AI_PRESETS) + ['Custom'])
        self.ai_preset.activated.connect(lambda: self._change('ai_preset', self.ai_preset.currentText()))
        ai_layout.addWidget(self.ai_preset)
        preset_buttons = QHBoxLayout()
        for text, slot in (('Save preset', self._save_preset), ('Load preset', self._load_preset),
                           ('Open Preset Folder', self._open_preset_folder)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            preset_buttons.addWidget(button)
            if text == "Open Preset Folder":
                self.preset_folder_button = button
        ai_layout.addLayout(preset_buttons)
        self.ai_fields = QWidget()
        grid = QGridLayout(self.ai_fields)
        for col, title in enumerate(('Task', 'Budget %', 'Delay ticks')):
            grid.addWidget(QLabel(title), 0, col)
        tasks = ('Conquest', 'Defense', 'Recon', 'Host attack', 'Power', 'Radar', 'Safety', 'Completion')
        self.ai_spins = {}
        for row, title in enumerate(tasks, 1):
            grid.addWidget(QLabel(title), row, 0)
            for col, key in enumerate(HOST_AI_FIELDS[(row-1)*2:row*2], 1):
                spin = QSpinBox(minimum=0, maximum=100 if key in HOST_AI_BUDGET_FIELDS else 2147483647)
                if key not in HOST_AI_BUDGET_FIELDS:
                    spin.setToolTip('Game time: 1024 ticks = 1 second. Raw script values are preserved.')
                self.ai_spins[key] = spin
                spin.valueChanged.connect(lambda value, field=key: self._change('ai.' + field, value))
                grid.addWidget(spin, row, col)
        self.ai_fields.hide()
        self.ai.toggled.connect(self.ai_fields.setVisible)
        ai_layout.addWidget(self.ai_fields)
        form.addRow(self.ai)
        form.addRow(advanced)
        layout.addWidget(self.settings_lock.wrap(self.form_widget))
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
        self.vehicle_row.filter_items()
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
            self.list.item(index).setData(DRAFT_ROLE, bool(host.get("_preview")))
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
        self.add_button.setEnabled(True)
        self.find_button.setEnabled(valid)
        self.delete_button.setEnabled(valid)
        self.clear_button.setEnabled(bool(self.doc and self.doc.host_stations))
        self.total.setText(f'{len(self.doc.host_stations) if self.doc else 0} hosts')
        self.form_widget.setEnabled(True)
        self.x.setEnabled(valid)
        self.z.setEnabled(valid)
        if valid:
            self._loading = True
            host = self.doc.host_stations[index]
            x, z = squad_xz(host)
            self.x.setValue(x)
            self.z.setValue(z)
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
            current_player_faction = host['owner'] == self.doc.player_owner
            self.player.setText('Current Player Faction' if current_player_faction else 'Use This Faction as Player')
            self.player.setEnabled(not current_player_faction and not host.get('_preview'))
            self.status.clear()
            self.status.hide()
            if placing and not host.get('_preview'):
                self.status.setText('Finish placement or press Esc to edit this host.')
                self.status.show()
            self._loading = False
        else:
            self.ai.setEnabled(True)
            self.player.setEnabled(False)
            self.player.setText('Use This Faction as Player')
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

    def _new(self):
        """New = Deselect + Add."""
        self.deselect()
        self.addRequested.emit()

    def placement_values(self):
        return dict(owner=self.owner.currentData(), veh=form_vehicle(self.vehicle),
                    energy=self.energy.value(), pos_y=self.height.value(),
                    reload_const=self.fields['reload_const'].value(), viewangle=self.fields['viewangle'].value(),
                    custom_name=self.name.text().strip() or None, hidden=self.hidden.isChecked(),
                    ai={**{key: spin.value() for key, spin in self.ai_spins.items()},
                        'preset': self.ai_preset.currentText()})

    def _preset_folder(self):
        from ..core.host_ai_presets import host_ai_preset_dir
        folder = host_ai_preset_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, 'Host AI presets', str(error))
            return None
        return folder

    def _open_preset_folder(self):
        folder = self._preset_folder()
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _save_preset(self):
        from ..core.host_ai_presets import save_ai_preset
        name, accepted = QInputDialog.getText(self, 'Save Host AI', 'Preset name:')
        if not accepted or not name.strip():
            return
        folder = self._preset_folder()
        if folder is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save Host AI preset', str(folder / (name.strip() + '.json')), 'Host AI (*.json)')
        if path:
            try:
                values = save_ai_preset(path, name.strip(), {key: spin.value() for key, spin in self.ai_spins.items()})
                self._change('ai_values', values)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, 'Host AI preset', str(error))

    def _load_preset(self):
        from ..core.host_ai_presets import load_ai_preset
        folder = self._preset_folder()
        if folder is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Load Host AI preset', str(folder), 'Host AI (*.json)')
        if path:
            try:
                self._change('ai_values', load_ai_preset(path))
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, 'Host AI preset', str(error))
