"""Compact editing of the fields actually supported by begin_squad."""
from PySide6.QtCore import Signal, QEvent, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
                              QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QToolButton,
                              QCheckBox, QPushButton, QLabel, QAbstractItemView, QSlider)
from ..core.ldf_model import FACTIONS
from ..core.actor_comments import default_squad_comment, is_custom, squad_comment
from ..render.squad_scene import squad_xz
from .settings_lock import SettingsLockButton
from .preview_cards import PreviewList, set_card, faction_style, color_faction_items, DETAIL_ROLE, DRAFT_ROLE, form_vehicle, VehicleSearchRow


class WorldCoordinateSpinBox(QDoubleSpinBox):
    def __init__(self):
        super().__init__(minimum=-2147483647, maximum=2147483647, decimals=15, singleStep=50)

    def textFromValue(self, value):
        return self.locale().toString(value, 'f', 2)

    def valueFromText(self, text):
        # Merely focusing a compact field must preserve the imported precision.
        if text == self.textFromValue(self.value()):
            return self.value()
        return super().valueFromText(text)


class PositionControls(QWidget):
    """Read-only world position, movement speed and an arrow pad that moves the selection.

    ``moved`` carries the world displacement (dx, dz) of one click, already scaled by the speed.
    """
    moved = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.readout = QLabel('–')
        self.readout.setToolTip('Current world position (X / Z). Use the arrows below to move.')
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(1, 200)
        self.slider.setValue(50)
        self.slider.setToolTip('World units moved per arrow click (1–200). Default: 50.')
        self.pad = QWidget()
        grid = QGridLayout(self.pad)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        self.buttons = {}
        # Moves follow the world axes (X right, Z forward), never the camera and never the height.
        for name, arrow, (row, col), direction, label in (
                ('up', Qt.ArrowType.UpArrow, (0, 1), (0, 1), 'forward'),
                ('left', Qt.ArrowType.LeftArrow, (1, 0), (-1, 0), 'left'),
                ('down', Qt.ArrowType.DownArrow, (1, 1), (0, -1), 'back'),
                ('right', Qt.ArrowType.RightArrow, (1, 2), (1, 0), 'right')):
            button = QToolButton()
            button.setArrowType(arrow)
            button.setAutoRepeat(True)
            button.setFixedSize(30, 26)
            button.setToolTip(f'Move {label}')
            button.clicked.connect(lambda _=False, d=direction: self._step(*d))
            grid.addWidget(button, row, col)
            self.buttons[name] = button
        self._origin = None

    def add_rows(self, form):
        form.addRow('World X / Z', self.readout)
        form.addRow('Movement speed', self.slider)
        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(self.pad)
        centered.addStretch(1)
        form.addRow(centered)

    def set_position(self, x, z):
        # Whole units are enough to read the position; the stored value keeps its precision.
        self._origin = (x, z)
        self.readout.setText('–' if x is None else f'{round(x)} / {round(z)}')

    def _step(self, dx, dz):
        if self._origin is None or self._origin[0] is None:
            return
        speed = self.slider.value()
        self.moved.emit(dx * speed, dz * speed)


class SquadPanel(QWidget):
    selected = Signal(object)
    valuesChanged = Signal(object, str, object)
    addRequested = Signal()
    removeRequested = Signal(object)
    clearRequested = Signal()
    findRequested = Signal()
    povRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self._loading = False
        self._auto_comment = None
        self.settings_lock = SettingsLockButton()
        self.colors = {}
        self.vehicles = {}
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit(placeholderText='Filter squads')
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
        for text, slot in (("New", self._new),
                           ("Delete", lambda: self.removeRequested.emit(self.selected_indices())),
                           ("Find", self.findRequested.emit)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
            if text == 'New':
                self.add_button = button
                button.setToolTip('Deselect the current squad and start a new one with its settings.')
            elif text == 'Find':
                self.find_button = button
            elif text == 'Delete':
                self.delete_button = button
        layout.addLayout(row)
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
        self.vehicle_row = VehicleSearchRow(self.vehicle)
        self.count = QSpinBox(minimum=1, maximum=2147483647, value=1)
        self.name = QLineEdit(placeholderText='Automatic comment: squad number, vehicle and count')
        self.hidden = QCheckBox("Hidden in Briefing")
        self.useable = QCheckBox("Usable by the Host AI")
        self.position = PositionControls()
        self.coordinate_speed = self.position.slider
        for title, widget in (("Faction", self.owner), ("Vehicle", self.vehicle_row),
                              ("Count", self.count)):
            form.addRow(title, widget)
        form.addRow(self.hidden)
        form.addRow(self.useable)
        form.addRow('Comment', self.name)
        self.position.add_rows(form)
        layout.addWidget(self.settings_lock.wrap(self.form_widget))
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.owner.currentIndexChanged.connect(lambda: self._change('owner', self.owner.currentData()))
        self.vehicle.activated.connect(self._vehicle_changed)
        self.vehicle.lineEdit().editingFinished.connect(self._vehicle_changed)
        self.count.valueChanged.connect(lambda v: self._change('num', v))
        self.name.textEdited.connect(self._comment_edited)
        self.hidden.toggled.connect(lambda v: self._change('hidden', v))
        self.useable.toggled.connect(lambda v: self._change('useable', v))
        self.position.moved.connect(self._move)

    def _current_squad(self):
        indices = self.selected_indices()
        if self.doc is None or len(indices) != 1:
            return None
        index = next(iter(indices))
        return self.doc.squads[index] if 0 <= index < len(self.doc.squads) else None

    def _current_index(self):
        indices = self.selected_indices()
        index = next(iter(indices)) if len(indices) == 1 else -1
        return index if self.doc is not None and 0 <= index < len(self.doc.squads) else -1

    def _comment_edited(self, text):
        # Text equal to the automatic comment is not a user comment; it keeps following the squad.
        index = self._current_index()
        text = text.strip()
        if index >= 0 and text == default_squad_comment(self.doc.squads, index, self.vehicles):
            text = ''
        self._change('custom_name', text or None)

    def _sync_comment(self):
        # While typing, the field belongs to the user.
        index = self._current_index()
        if index < 0 or self.name.hasFocus():
            return
        self._show_comment(index)

    def _show_comment(self, index):
        self.name.setText(squad_comment(self.doc, index, self.vehicles))
        # Remembered so deselecting can clear an automatic comment but keep a user one.
        self._auto_comment = None if is_custom(self.doc.squads[index], self.vehicles) else self.name.text()

    def _move(self, dx, dz):
        squad = self._current_squad()
        if squad is None:
            return
        x, z = squad_xz(squad)
        self._change('pos_x', x + dx)
        self._change('pos_z', z + dz)
        self._show_position()

    def _show_position(self):
        squad = self._current_squad()
        self.position.set_position(*(squad_xz(squad) if squad else (None, None)))

    def _new(self):
        """New = Deselect + Add."""
        self.set_selection(set())
        self.addRequested.emit()

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
            color_faction_items(self.faction_filter, colors)
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
        self.vehicle_row.filter_items()
        self.update_rows()
        self._loading = False
        self.set_selection({i for i in old if 0 <= i < len(doc.squads)})

    def update_rows(self):
        signals_blocked = self.list.blockSignals(True)
        # Draft squads can change the row count before a full form refresh.
        while self.list.count() < len(self.doc.squads):
            self.list.addItem('')
        while self.list.count() > len(self.doc.squads):
            self.list.takeItem(self.list.count() - 1)
        for i, squad in enumerate(self.doc.squads):
            visual = self.vehicles.get(squad['veh'])
            name = (visual.name if visual else '') or f"Vehicle {squad['veh']}"
            flags = (' · Briefing hidden' if squad.get('hidden') else '') + (' · Host AI' if squad.get('useable') else '')
            preview = 'PREVIEW · ' if squad.get('_preview') else ''
            item = self.list.item(i)
            item.setData(DRAFT_ROLE, bool(squad.get("_preview")))
            set_card(item, f"{preview}{i + 1}. {name} ×{squad['num']}",
                     f"{FACTIONS.get(squad['owner'], '?')} · ID {squad['veh']}\n"
                     f"Sector ({squad['x']}, {squad['y']}){flags}"
                     + (f"\n{squad['custom_name']}" if squad.get('custom_name') else ''),
                     (150, 150, 150) if squad.get('_preview') else
                     self.colors.get(squad['owner'], (180, 180, 180)), ('veh', squad['veh']))
        self.list.blockSignals(signals_blocked)
        self._show_position()
        self._sync_comment()
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
        self.add_button.setEnabled(True)
        self.find_button.setEnabled(valid)
        self.delete_button.setEnabled(bool(indices))
        self.clear_button.setEnabled(bool(self.doc and self.doc.squads))
        self.total.setText(f'{len(self.doc.squads) if self.doc else 0} squads')
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
            self._show_comment(index)
            self.hidden.setChecked(squad.get('hidden', False))
            self.useable.setChecked(squad.get('useable', False))
            self.position.set_position(*squad_xz(squad))
            # Moving several squads by absolute coordinates would stack them, so one at a time.
            self.position.pad.setEnabled(len(indices) == 1)
            self._loading = False
        else:
            self.position.set_position(None, None)
            self.position.pad.setEnabled(False)
            if self.name.text() == self._auto_comment:
                self.name.clear()
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
