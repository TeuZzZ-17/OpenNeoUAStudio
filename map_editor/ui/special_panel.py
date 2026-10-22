"""Consistent, compact controls for beamgates, super items and gems."""
import re

import copy
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QValidator, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
                              QListWidget, QListWidgetItem, QPushButton, QSpinBox,
                              QComboBox, QCheckBox, QLabel, QGroupBox, QDialog, QDialogButtonBox)

from studio.ui.widgets import use_standard_list_style
from ..core.ldf_model import GEM_ACTION_PARAMS_BY_TARGET, MAX_SPECIAL_SLOTS, GAME_TICKS_PER_SECOND
from .settings_lock import SettingsLockButton
from .preview_cards import PreviewList, set_card, RESOURCE_ROLE
from ..render.special_scene import SPECIAL_COLORS as COLORS
from ..core.special_objects import SPECIAL_KINDS, special_store, special_slots, special_cell


class DurationSpinBox(QSpinBox):
    """Display game ticks as MM:SS without losing imported subsecond values."""

    _MAX_TICKS = 2147483647

    def __init__(self, parent=None):
        self._raw_value = 0
        self._input_edited = False
        super().__init__(parent)
        self.setRange(0, self._MAX_TICKS)
        self.setSingleStep(GAME_TICKS_PER_SECOND)
        self.setToolTip('Game time: 1024 ticks = 1 second. Imported fractional seconds are preserved.')
        self.lineEdit().textEdited.connect(self._mark_edited)

    def _mark_edited(self, _text):
        self._input_edited = True

    def setValue(self, value):
        super().setValue(value)
        self._raw_value = super().value()
        self._input_edited = False

    def stepBy(self, steps):
        self._input_edited = True
        super().stepBy(steps)

    def textFromValue(self, value):
        return format_duration(value)

    @classmethod
    def _parse_ticks(cls, text):
        match = re.fullmatch(r'(\d+):(\d{1,2})', text.strip())
        if match is None:
            return None
        minutes, seconds = map(int, match.groups())
        if seconds > 59:
            return None
        ticks = (minutes * 60 + seconds) * GAME_TICKS_PER_SECOND
        return ticks if ticks <= cls._MAX_TICKS else None

    def valueFromText(self, text):
        if not self._input_edited and text == self.textFromValue(self._raw_value):
            return self._raw_value
        value = self._parse_ticks(text)
        if value is None:
            return super().value()
        self._raw_value = value
        return value

    def validate(self, text, position):
        if text == '' or re.fullmatch(r'\d+', text) or re.fullmatch(r'\d+:', text):
            return QValidator.State.Intermediate, text, position
        if self._parse_ticks(text) is not None:
            return QValidator.State.Acceptable, text, position
        return QValidator.State.Invalid, text, position


def format_duration(ticks):
    # The game's countdown rounds a remaining fractional second upward.
    minutes, seconds = divmod((int(ticks) + GAME_TICKS_PER_SECOND - 1) // GAME_TICKS_PER_SECOND, 60)
    return f'{minutes:02d}:{seconds:02d}'


# Super item types the engine knows (TMapSuperItem): 1 bomb is the default, 2 wave.
ITEM_TYPES = ((1, 'Default'), (2, '2 · Wave'))
# Presets that fix the building IDs; only Custom lets those fields be edited.
BUILDING_FIELDS = {'gate': ('closed_bp', 'opened_bp'),
                   'item': ('inactive_bp', 'active_bp', 'trigger_bp'),
                   'gem': ('blg',)}


class ItemTypeBox(QComboBox):
    """Super item type picker with the QSpinBox calls the panel already uses."""

    valueChanged = Signal(int)

    def __init__(self):
        super().__init__()
        for value, label in ITEM_TYPES:
            self.addItem(label, value)
        self.activated.connect(lambda _index: self.valueChanged.emit(self.value()))

    def setRange(self, low, high):
        """Only the engine's own types are selectable, whatever range is asked."""

    def value(self):
        return int(self.currentData())

    def setValue(self, value):
        value = int(value)
        index = self.findData(value)
        if value not in {item[0] for item in ITEM_TYPES}:
            # Preserve a loaded value, but do not offer unsupported engine types for editing.
            unknown_index = len(ITEM_TYPES)
            if self.count() == unknown_index:
                self.addItem(f'{value} · Unknown', value)
            else:
                self.setItemText(unknown_index, f'{value} · Unknown')
                self.setItemData(unknown_index, value)
            index = unknown_index
            self.model().item(index).setEnabled(False)
        else:
            if self.count() > len(ITEM_TYPES):
                self.removeItem(len(ITEM_TYPES))
            index = self.findData(value)
        self.setCurrentIndex(index)


class SpecialPanel(QWidget):
    selected = Signal(int)
    addRequested = Signal()
    clearRequested = Signal()
    removeRequested = Signal(int)
    valuesChanged = Signal(int, object)
    placementRequested = Signal(str, int)
    focusRequested = Signal(object)

    def __init__(self, kind):
        super().__init__()
        self.settings_lock = SettingsLockButton()
        self.kind, self.doc, self._loading = kind, None, False
        self.slot = 0
        self.template = SPECIAL_KINDS[kind][2]()
        layout = QVBoxLayout(self)
        self.search = QLineEdit(placeholderText=f'Filter {SPECIAL_KINDS[kind][0]}')
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.list = PreviewList()
        self.list.preview_level = 2
        self.list.setMinimumHeight(140)
        layout.addLayout(self.list.preview_controls())
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list, 1)
        self.count = QLabel()
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.count)
        row = QHBoxLayout()
        self.add_button = QPushButton('New')
        self.add_button.clicked.connect(self._new)
        self.delete_button = QPushButton('Delete')
        self.delete_button.clicked.connect(lambda: self.removeRequested.emit(self.slot))
        self.focus = QPushButton('Find')
        self.focus.clicked.connect(lambda: self.focusRequested.emit(special_cell(self.record())))
        self.add_button.setToolTip('Deselect the current object and start a new one with its settings.')
        for button in (self.add_button, self.delete_button, self.focus):
            row.addWidget(button)
        layout.addLayout(row)
        # Place / Move fills the row up to Delete all.
        self.place = QPushButton('Place / Move')
        self.place.clicked.connect(lambda: self.placementRequested.emit('object', -1))
        self.clear_button = QPushButton('Delete all')
        self.clear_button.clicked.connect(self.clearRequested.emit)
        count_row = QHBoxLayout()
        count_row.addWidget(self.settings_lock)
        count_row.addWidget(self.place, 1)
        count_row.addWidget(self.clear_button)
        layout.addLayout(count_row)
        self.form_widget = QWidget()
        form = QFormLayout(self.form_widget)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fields = {}
        coords = QHBoxLayout()
        for field, title in (('x', 'X'), ('y', 'Y')):
            spin = QSpinBox(minimum=-1, maximum=254)
            spin.setSpecialValueText('Unplaced')
            self.fields[field] = spin
            spin.valueChanged.connect(self._coordinates)
            coords.addWidget(QLabel(title))
            coords.addWidget(spin, 1)
        form.addRow('Sector', coords)
        self.hidden = QCheckBox('Hidden in Briefing')
        self.hidden.toggled.connect(lambda value: self._change({'hidden': value}))
        form.addRow(self.hidden)
        self.preset = QComboBox()
        self.preset.addItem('Custom', None)
        if kind == 'gate':
            self.preset.addItem('No road · 25 / 26', {'closed_bp': 25, 'opened_bp': 26})
            self.preset.addItem('With roads · 5 / 6', {'closed_bp': 5, 'opened_bp': 6})
            self.target_levels = QLineEdit()
            self.target_levels.setPlaceholderText('e.g. 40, 50')
            self.target_levels.setToolTip('All levels unlocked by this gate, separated by commas. Leave empty for no destination.')
            self.target_levels.editingFinished.connect(self._targets_changed)
            form.addRow('Target levels', self.target_levels)
            specs = [('closed_bp', 'Closed building', 0, 255),
                     ('opened_bp', 'Opened building', 0, 255)]
        elif kind == 'item':
            self.preset.addItem('Standard · 35 / 36 / 37', {'inactive_bp': 35, 'active_bp': 36, 'trigger_bp': 37})
            self.preset.addItem('Parasite*', {'inactive_bp': 68, 'active_bp': 69, 'trigger_bp': 70})
            self.preset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            self.preset.setMinimumContentsLength(12)
            specs = [('countdown', 'Countdown (MM:SS)', 0, 2147483647), ('type', 'Type', 1, 2),
                     ('inactive_bp', 'Inactive building', 0, 255), ('active_bp', 'Active building', 0, 255),
                     ('trigger_bp', 'Triggered building', 0, 255)]
        else:
            for key, title in ((4, 'Vehicle unlock'), (7, 'With flak'), (15, 'Weapon power'),
                               (16, 'New building'), (50, 'More shield'), (51, 'Heavy weapon'),
                               (60, 'More roboflak'), (61, 'Roboflak power'), (65, 'More shield')):
                self.preset.addItem(f'{key} · {title}', {'blg': key})
            specs = [('blg', 'Building ID', 0, 255), ('type', 'Type', 0, 2147483647)]
        self.preset.activated.connect(self._preset)
        form.addRow('Model Preset', self.preset)
        for field, title, low, high in specs:
            spin = (DurationSpinBox() if kind == 'item' and field == 'countdown'
                    else ItemTypeBox() if kind == 'item' and field == 'type'
                    else QSpinBox(minimum=low, maximum=high))
            spin.setRange(low, high)
            self.fields[field] = spin
            spin.valueChanged.connect(lambda value, f=field: self._change({f: value}))
            form.addRow(title, spin)
        if kind == 'gem':
            self.fields['type'].setToolTip('1 = Weapon power, 2 = Shield, 3 = Tech / Unlock. Custom IDs are preserved.')
        if kind in ('gate', 'item'):
            self._build_keys(form)
        else:
            self._build_actions(form)
        layout.addWidget(self.settings_lock.wrap(self.form_widget))
        self.status = QLabel('Add an object, configure it, then place it on the map.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.form_widget.setEnabled(True)
        self.delete_button.setEnabled(False)

    def _build_keys(self, form):
        group = QGroupBox('Key Sectors')
        self.keys_group = group
        layout = QVBoxLayout(group)
        self.keys = QListWidget()
        use_standard_list_style(self.keys)
        self.keys.setMaximumHeight(105)
        self.keys.currentRowChanged.connect(self._key_selected)
        layout.addWidget(self.keys)
        row = QHBoxLayout()
        for title, callback in (('Add on map', lambda: self.placementRequested.emit('key', -1)),
                                ('Move', lambda: self.placementRequested.emit('key', self.keys.currentRow()) if self.keys.currentRow() >= 0 else None),
                                ('Find', self._find_key),
                                ('Remove', self._remove_key)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
            if title == 'Find':
                self.key_find = button
        layout.addLayout(row)
        self.key_x, self.key_y = QSpinBox(), QSpinBox()
        coords = QHBoxLayout()
        for title, spin in (('X', self.key_x), ('Y', self.key_y)):
            spin.setRange(1, 254)
            spin.valueChanged.connect(self._key_coordinates)
            coords.addWidget(QLabel(title))
            coords.addWidget(spin, 1)
        layout.addLayout(coords)
        if self.kind == 'item':
            self.key_road = QCheckBox('Key Sector with Roads')
            self.key_road.toggled.connect(lambda road: self.valuesChanged.emit(self.slot, {'_key_road': (self.keys.currentRow(), road)}) if not self._loading and self.keys.currentRow() >= 0 else None)
            layout.addWidget(self.key_road)
        form.addRow(group)

    def _build_actions(self, form):
        group = QGroupBox('Effects on Capture')
        layout = QVBoxLayout(group)
        self.actions = QListWidget()
        use_standard_list_style(self.actions)
        self.actions.setMaximumHeight(110)
        self.actions.currentRowChanged.connect(self._action_selected)
        layout.addWidget(self.actions)
        self.effect_dialog = QDialog(self)
        self.effect_dialog.resize(400, 230)
        dialog_layout = QVBoxLayout(self.effect_dialog)
        fields = QFormLayout()
        self.target = QComboBox()
        for target, title in (('modify_vehicle', 'Vehicle'), ('modify_building', 'Building'), ('modify_weapon', 'Weapon')):
            self.target.addItem(title, target)
        self.target.currentIndexChanged.connect(self._action_params)
        self.target_id = QComboBox()
        self.target_id.setEditable(True)
        self.target_id.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.target_id.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.target_id.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.target_id.setToolTip('Search by name or enter a numeric ID. Names are not written to the level.')
        self.param = QComboBox()
        self.action_value = QLineEdit('1')
        for title, widget in (('Modify', self.target), ('ID', self.target_id), ('Parameter', self.param), ('Value', self.action_value)):
            fields.addRow(title, widget)
        dialog_layout.addLayout(fields)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.effect_dialog.accept)
        buttons.rejected.connect(self.effect_dialog.reject)
        dialog_layout.addWidget(buttons)
        self.catalogs = {}
        row = QHBoxLayout()
        for title, callback in (('Add effect', lambda: self._edit_action(False)),
                                ('Edit', lambda: self._edit_action(True)), ('Remove', self._remove_action)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self._action_params()
        self.actions.itemDoubleClicked.connect(lambda: self._edit_action(True))
        form.addRow(group)

    def record(self):
        return special_store(self.doc, self.kind).get(self.slot, self.template) if self.doc and self.slot else self.template

    def refresh(self, doc, selected=None):
        slot = self.slot if selected is None else selected
        self.doc, self._loading = doc, True
        self.fields['x'].setMaximum(doc.mw - 2)
        self.fields['y'].setMaximum(doc.mh - 2)
        self.list.clear()
        for index in special_slots(doc, self.kind):
            value = special_store(doc, self.kind)[index]
            cell = special_cell(value)
            title = SPECIAL_KINDS[self.kind][3]
            detail = f'Target levels {", ".join(map(str, value["targets"])) or "none"} · {len(value["keys"])} keys' if self.kind == 'gate' else (
                f'{format_duration(value["countdown"])} · {len(value["keys"])} keys' if self.kind == 'item' else
                f'Building {value["blg"]} · {len(value["actions"])} effects')
            item = QListWidgetItem()
            building = value['closed_bp'] if self.kind == 'gate' else value['inactive_bp'] if self.kind == 'item' else value['blg']
            set_card(item, f'{title} {index} · {cell if cell else "Unplaced"}',
                     detail + (' · Hidden' if value.get('hidden') else ''), COLORS[self.kind], ('blg', building))
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
            if index == slot:
                self.list.setCurrentItem(item)
        count = self.list.count()
        self.count.setText(f'{count} / {MAX_SPECIAL_SLOTS} objects')
        self.add_button.setEnabled(count < MAX_SPECIAL_SLOTS)
        self.clear_button.setEnabled(count > 0)
        self._loading = False
        self._selected(self.list.currentItem())
        self._filter()
        self.list.previewsRequested.emit()

    def _selected(self, item, previous=None):
        if self._loading:
            return
        self.slot = item.data(Qt.ItemDataRole.UserRole) if item else 0
        self.form_widget.setEnabled(True)
        value = self.record()
        if self.slot:
            self.template = {k: copy.deepcopy(v) for k, v in value.items() if not k.startswith('_')}
            self.template.update(x=-1, y=-1)
            if 'keys' in self.template:
                self.template['keys'] = []
        self.add_button.setEnabled(self.list.count() < MAX_SPECIAL_SLOTS)
        self.delete_button.setEnabled(bool(self.slot))
        self._loading = True
        if self.kind == 'gate':
            self.target_levels.setText(', '.join(map(str, value['targets'])))
        for key, widget in self.fields.items():
            widget.setValue(value.get(key, -1))
        for key in ('x', 'y'):
            self.fields[key].setEnabled(bool(self.slot))
        self.hidden.setChecked(value.get('hidden', False))
        self.preset.setCurrentIndex(0)
        for index in range(1, self.preset.count()):
            if all(value.get(k) == v for k, v in self.preset.itemData(index).items()):
                self.preset.setCurrentIndex(index)
                break
        self._sync_preset_fields()
        if self.kind == 'gem':
            index = self.actions.currentRow()
            self.actions.clear()
            for action in value['actions']:
                self.actions.addItem(f'{action["target_type"].removeprefix("modify_")} {action["id"]} · {action["param"]} = {action["val"]}')
            self.actions.setCurrentRow(min(index, self.actions.count() - 1))
        else:
            index = self.keys.currentRow()
            self.keys.clear()
            self.keys.addItems([self._key_label(i, x, y, value) for i, (x, y) in enumerate(value['keys'])])
            self.keys.setCurrentRow(min(index, self.keys.count() - 1))
            self.keys_group.setEnabled(bool(self.slot) and not value.get('_preview'))
            if self.doc:
                self.key_x.setMaximum(self.doc.mw - 2)
                self.key_y.setMaximum(self.doc.mh - 2)
        self.place.setEnabled(bool(self.slot))
        self.focus.setEnabled(bool(self.slot) and special_cell(value) is not None)
        self._loading = False
        if self.kind == 'gem':
            self._action_selected(self.actions.currentRow())
        else:
            self._key_selected(self.keys.currentRow())
        self.status.setText('Configure the next object, then New. Click to place; Esc cancels.' if not self.slot else
                            'Preview only · Click to insert · Esc cancels' if value.get('_preview') else
                            'Changes apply immediately. Drag the object or its keys to move them.')
        self.selected.emit(self.slot)

    def _new(self):
        """New = Deselect + Add: the next object starts from the current settings."""
        self.list.setCurrentRow(-1)
        self.addRequested.emit()

    def _targets_changed(self):
        if self._loading:
            return
        text = self.target_levels.text().strip()
        parts = text.split(',') if text else []
        if any(not part.strip().isdecimal() or int(part.strip()) > 2147483647 for part in parts):
            self.status.setText('Enter numeric level IDs (0–2147483647), separated by commas.')
            self.target_levels.setText(', '.join(map(str, self.record()['targets'])))
            return
        self._change({'targets': [int(part.strip()) for part in parts]})

    def update_preview_position(self, doc, slot):
        if self.slot != slot or self.list.count() != getattr(doc, SPECIAL_KINDS[self.kind][1]):
            self.refresh(doc, slot)
            return
        self.doc = doc
        value = self.record()
        self._loading = True
        for key in ('x', 'y'):
            self.fields[key].setValue(value[key])
        item = self.list.currentItem()
        if item is not None:
            building = value['closed_bp'] if self.kind == 'gate' else value['inactive_bp'] if self.kind == 'item' else value['blg']
            resource = ('blg', building)
            if item.data(RESOURCE_ROLE) != resource:
                item.setIcon(QIcon())
            detail = f'Target levels {", ".join(map(str, value["targets"])) or "none"} · {len(value["keys"])} keys' if self.kind == 'gate' else (
                f'{format_duration(value["countdown"])} · {len(value["keys"])} keys' if self.kind == 'item' else
                f'Building {value["blg"]} · {len(value["actions"])} effects')
            set_card(item, f'{SPECIAL_KINDS[self.kind][3]} {slot} · {special_cell(value)}',
                     detail + (' · Hidden' if value.get('hidden') else ''), COLORS[self.kind], resource)
        if self.kind != 'gem':
            for index, (x, y) in enumerate(value['keys']):
                item = self.keys.item(index)
                if item is not None:
                    item.setText(self._key_label(index, x, y, value))
        self._loading = False

    def placement_values(self):
        return {k: copy.deepcopy(v) for k, v in self.record().items() if not k.startswith('_')}

    def _filter(self):
        text = self.search.text().strip().casefold()
        for index in range(self.list.count()):
            item = self.list.item(index)
            item.setHidden(text not in item.text().casefold())

    def _change(self, values):
        if self._loading:
            return
        if self.slot:
            self.valuesChanged.emit(self.slot, values)
        else:
            self.template.update(copy.deepcopy(values))
            if len(values) > 1 or any(k in values for k in ('actions', 'keys')):
                self._selected(None)

    def _coordinates(self):
        x, y = self.fields['x'].value(), self.fields['y'].value()
        self._change({'x': x if x >= 0 and y >= 0 else -1, 'y': y if x >= 0 and y >= 0 else -1})

    def _preset(self):
        if self.preset.currentData():
            self._change(self.preset.currentData())
        self._sync_preset_fields()

    def _sync_preset_fields(self):
        """Building IDs are edited only with the Custom preset; a fixed preset greys them out."""
        custom = self.preset.currentIndex() == 0
        for field in BUILDING_FIELDS[self.kind]:
            self.fields[field].setEnabled(custom)

    def _key_label(self, index, x, y, value):
        """Key row text; a super item key on a road sector says so."""
        text = f'{index + 1} · Sector ({x}, {y})'
        if self.kind != 'item':
            return text
        roads = value.get('_key_roads', {})
        if index in roads:
            road = roads[index]
        else:
            road = (self.doc is not None and 0 <= x < self.doc.mw and 0 <= y < self.doc.mh
                    and str(self.doc.grids['type'][y][x]).lower() == 'f4')
        return text + (' · with Roads' if road else '')

    def _find_key(self):
        keys = self.record().get('keys', [])
        index = self.keys.currentRow()
        if 0 <= index < len(keys):
            self.focusRequested.emit(tuple(keys[index]))

    def _key_selected(self, index):
        keys = self.record().get('keys', [])
        valid = 0 <= index < len(keys)
        self.key_find.setEnabled(valid)
        self.key_x.setEnabled(valid)
        self.key_y.setEnabled(valid)
        if valid:
            self._loading = True
            self.key_x.setValue(keys[index][0])
            self.key_y.setValue(keys[index][1])
            if self.kind == 'item':
                x, y = keys[index]
                self.key_road.setChecked(0 <= x < self.doc.mw and 0 <= y < self.doc.mh and str(self.doc.grids['type'][y][x]).lower() == 'f4')
            self._loading = False
        if self.kind == 'item':
            self.key_road.setEnabled(valid)

    def _key_coordinates(self):
        if self._loading:
            return
        index = self.keys.currentRow()
        if index >= 0:
            keys = list(self.record()['keys'])
            keys[index] = (self.key_x.value(), self.key_y.value())
            self._change({'keys': keys})

    def _remove_key(self):
        index = self.keys.currentRow()
        if index >= 0:
            keys = list(self.record()['keys'])
            del keys[index]
            self._change({'keys': keys})

    def set_catalogs(self, catalogs):
        self.catalogs = catalogs
        self._lookup_targets()

    def _lookup_targets(self):
        if not hasattr(self, 'target_id'):
            return
        previous = self.target_id.currentText().split(';', 1)[0].strip() or '0'
        self.target_id.clear()
        for key, name in sorted(self.catalogs.get(self.target.currentData(), {}).items()):
            self.target_id.addItem(f'{key} ; {name}', key)
        index = self.target_id.findData(int(previous)) if previous.isdecimal() else -1
        self.target_id.setCurrentIndex(index)
        if index < 0:
            self.target_id.setEditText(previous)

    def _edit_action(self, update):
        if update and self.actions.currentRow() < 0:
            return
        if update:
            self._action_selected(self.actions.currentRow())
        self.effect_dialog.setWindowTitle('Edit Gem Effect' if update else 'Add gem effect')
        if self.effect_dialog.exec():
            self._save_action(update)

    def _action_params(self):
        self._lookup_targets()
        self.param.clear()
        self.param.addItems(GEM_ACTION_PARAMS_BY_TARGET[self.target.currentData()])

    def _action_selected(self, index):
        actions = self.record().get('actions', [])
        if not 0 <= index < len(actions):
            return
        action = actions[index]
        self.target.setCurrentIndex(self.target.findData(action['target_type']))
        target_index = self.target_id.findData(action['id'])
        self.target_id.setCurrentIndex(target_index)
        if target_index < 0:
            self.target_id.setEditText(str(action['id']))
        self.param.setCurrentText(action['param'])
        self.action_value.setText(str(action['val']))

    def _save_action(self, update):
        actions = list(self.record().get('actions', []))
        value = self.action_value.text().strip()
        if not value or '\n' in value or '\r' in value:
            self.status.setText('Enter an effect value on a single line.')
            return
        text = self.target_id.currentText().split(';', 1)[0].strip()
        if not text.isdecimal() or not 0 <= int(text) <= 2147483647:
            self.status.setText('Enter a valid numeric target ID (0–2147483647).')
            return
        action = dict(target_type=self.target.currentData(), id=int(text),
                      param=self.param.currentText(), val=value)
        index = self.actions.currentRow()
        if update:
            if index < 0:
                return
            actions[index] = action
        else:
            actions.append(action)
        self._change({'actions': actions})

    def _remove_action(self):
        index = self.actions.currentRow()
        if index >= 0:
            actions = list(self.record()['actions'])
            del actions[index]
            self._change({'actions': actions})
