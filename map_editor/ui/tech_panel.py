"""Per-faction build permissions from discovered scripts and existing LDF IDs."""
from PySide6.QtCore import Qt, Signal, QPointF
from PySide6.QtGui import QColor, QTextLayout, QTextCharFormat
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QLabel,
                               QTreeWidget, QTreeWidgetItem, QHeaderView, QStyledItemDelegate, QCheckBox)
from ..core.ldf_model import FACTIONS
from ..core.building_defs import is_special_building
from .preview_cards import PreviewList, set_card
from .settings_lock import SettingsLockButton

# Vehicle classes a host station never builds as units: gun turrets and host stations.
NON_BUILDABLE_CLASSES = ('gun', 'robo')


def hidden_vehicle_class(definition) -> bool:
    return str(getattr(definition, 'model', '')).casefold() in NON_BUILDABLE_CLASSES


class FactionSummaryDelegate(QStyledItemDelegate):
    """Keep faction names coloured while counts stay white, including the closed combo."""
    def paint(self, painter, option, index):
        from PySide6.QtWidgets import QStyleOptionViewItem, QStyle
        content = QStyleOptionViewItem(option)
        self.initStyleOption(content, index)
        text = content.fontMetrics.elidedText(content.text, Qt.TextElideMode.ElideRight,
                                             max(0, content.rect.width() - 8))
        content.text = ''
        self.parent().style().drawControl(QStyle.ControlElement.CE_ItemViewItem, content, painter, self.parent())
        layout = QTextLayout(text, content.font)
        name_length = len(text.split(' · ', 1)[0])
        formats = []
        for start, length, color in ((0, name_length, index.data(Qt.ItemDataRole.ForegroundRole)),
                                    (name_length, len(text) - name_length, QColor(255,255,255))):
            span = QTextLayout.FormatRange()
            span.start, span.length = start, length
            span.format = QTextCharFormat()
            span.format.setForeground(color or QColor(235,235,235))
            formats.append(span)
        layout.setFormats(formats)
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(max(1, content.rect.width()-8))
        layout.endLayout()
        painter.save()
        painter.setClipRect(content.rect)
        layout.draw(painter, QPointF(content.rect.left()+4,
                                    content.rect.center().y()-line.height()/2))
        painter.restore()


class TechPanel(QWidget):
    permissionChanged = Signal(int, str, int, bool)

    def __init__(self):
        super().__init__()
        self.doc = None
        self.vehicles, self.buildings, self.colors = {}, {}, {}
        self._loading = False
        self.settings_lock = SettingsLockButton()
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.faction = QComboBox()
        self.faction.setItemDelegate(FactionSummaryDelegate(self.faction))
        self.faction.setLabelDrawingMode(QComboBox.LabelDrawingMode.UseDelegate)
        for key, name in FACTIONS.items():
            if key:
                self.faction.addItem(name, key)
        self.kind = QComboBox()
        self.kind.addItem('Vehicles', 'veh')
        self.kind.addItem('Buildings', 'blg')
        row.addWidget(self.faction, 1)
        row.addWidget(self.kind)
        header = QWidget()
        header.setLayout(row)
        lock_row = QHBoxLayout()
        lock_row.addWidget(self.settings_lock)
        lock_row.addWidget(self.settings_lock.wrap(header),1)
        layout.addLayout(lock_row)
        self.search = QLineEdit(placeholderText='Filter by name or ID')
        layout.addWidget(self.search)
        self.show_special_buildings = QCheckBox("Show Special Buildings")
        self.show_special_buildings.hide()
        self.show_special_buildings.toggled.connect(self._filter)
        layout.addWidget(self.show_special_buildings)
        self.status = QLabel()
        self.status.setFixedHeight(18)
        self.summary = QTreeWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.summary)
        self.summary.setHeaderLabels(['ID', 'Name'])
        self.summary.setRootIsDecorated(False)
        self.summary.setItemsExpandable(False)
        self.summary.setMaximumHeight(126)
        self.summary.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.summary.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.summary.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.status)
        layout.addWidget(self.summary)
        self.list = PreviewList(large_text=False)
        self.list.preview_level = 1
        self.settings_lock.bind_palette(self.list)
        layout.addLayout(self.list.preview_controls())
        layout.addWidget(self.list, 1)
        self.faction.currentIndexChanged.connect(self.rebuild)
        self.kind.currentIndexChanged.connect(self.rebuild)
        self.search.textChanged.connect(self._filter)
        self.list.itemChanged.connect(self._changed)
        self.list.itemClicked.connect(self._clicked)
        self.summary.itemClicked.connect(self._summary_clicked)

    def refresh(self, doc, vehicles, buildings, colors):
        self.doc, self.vehicles, self.buildings, self.colors = doc, vehicles, buildings, colors
        self.rebuild()

    def definitions(self, kind):
        """Definitions the lists may show; gun and robo vehicle classes are never offered."""
        if kind != 'veh':
            return self.buildings
        return {key: definition for key, definition in self.vehicles.items()
                if not hidden_vehicle_class(definition)}

    def permissions(self, kind, faction=None):
        faction = self.faction.currentData() if faction is None else faction
        explicit = faction in self.doc.tech_explicit or any(self.doc.tech[faction].values())
        if explicit:
            keys = self.doc.tech[faction][kind]
        else:
            keys = [key for key, definition in (self.vehicles if kind == 'veh' else self.buildings).items()
                    if faction in definition.enabled_factions]
        if kind != 'veh':
            return keys
        hidden = {key for key, definition in self.vehicles.items() if hidden_vehicle_class(definition)}
        # Hidden classes may still be stored in the level; they are kept, just not listed.
        return keys if not hidden else [key for key in keys if key not in hidden]

    def _faction_label(self, faction):
        vehicles, buildings = len(self.permissions('veh', faction)), len(self.permissions('blg', faction))
        return (f'{FACTIONS[faction]} · {vehicles} vehicle{"" if vehicles == 1 else "s"}'
                f' · {buildings} building{"" if buildings == 1 else "s"}')

    def update_permissions(self):
        if self.doc is None:
            return
        self._loading = True
        faction, kind = self.faction.currentData(), self.kind.currentData()
        definitions = self.definitions(kind)
        active = self.permissions(kind)
        for i in range(self.list.count()):
            item = self.list.item(i)
            key = item.data(Qt.ItemDataRole.UserRole)
            definition = definitions.get(key)
            name = (definition.name if definition else '') or self.doc.custom_tech_names.get(key) or f'Unknown ID {key}'
            set_card(item, f'{key} · {name}',
                     f'{FACTIONS[faction]} · {"Enabled" if key in active else "Disabled"}'
                     + ('\nDefinition missing from scripts' if definition is None else ''),
                     self.colors.get(faction, (180, 180, 180)), (kind, key))
            item.setCheckState(Qt.CheckState.Checked if key in active else Qt.CheckState.Unchecked)
        enabled = self.permissions(kind)
        self.status.setText(f'Enabled {self.kind.currentText()} ({len(enabled)})')
        self.status.setStyleSheet('QLabel { color: white; font-weight: bold; }')
        self.summary.clear()
        for key in enabled:
            definition = definitions.get(key)
            name = ((definition.name if definition else '') or
                    self.doc.custom_tech_names.get(key) or f'Unknown ID {key}')
            row = QTreeWidgetItem([str(key), str(name)])
            row.setData(0, Qt.ItemDataRole.UserRole, key)
            self.summary.addTopLevelItem(row)
        # Existing permissions stay visible even if a mod definition is missing.
        self.list.setEnabled(True)
        # Each popup entry keeps its faction color and its own counts; only the closed field follows the selection.
        for index in range(self.faction.count()):
            self.faction.setItemText(index, self._faction_label(self.faction.itemData(index)))
            self.faction.setItemData(index, QColor(*self.colors.get(self.faction.itemData(index),
                                     (180,180,180))), Qt.ItemDataRole.ForegroundRole)
        self._loading = False

    def rebuild(self, *_args):
        if self.doc is None:
            return
        self._loading = True
        scroll = self.list.verticalScrollBar().value()
        selected = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        self.list.clear()
        kind = self.kind.currentData()
        self.show_special_buildings.setVisible(kind == 'blg')
        definitions = self.definitions(kind)
        keys = set(definitions) | set(self.permissions(kind))
        for key in sorted(keys):
            self.list.addItem('')
            item = self.list.item(self.list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            if key == selected:
                self.list.setCurrentItem(item)
        self.update_permissions()
        self._filter()
        self.list.verticalScrollBar().setValue(scroll)
        self.list.previewsRequested.emit()

    def _filter(self, *_args):
        text = self.search.text().casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            definition = self.buildings.get(item.data(Qt.ItemDataRole.UserRole))
            special = (self.kind.currentData() == 'blg' and definition is not None
                       and is_special_building(definition))
            item.setHidden(text not in item.text().casefold() or
                           (special and not self.show_special_buildings.isChecked()))
        self.list.previewsRequested.emit()

    def _clicked(self, item):
        # The whole card is the toggle target; keep one change per click.
        item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)

    def _summary_clicked(self, row, _column):
        key = row.data(0, Qt.ItemDataRole.UserRole)
        target = next((self.list.item(i) for i in range(self.list.count())
                       if self.list.item(i).data(Qt.ItemDataRole.UserRole) == key), None)
        if target is None:
            return
        if target.isHidden():
            definition = self.buildings.get(key)
            if self.kind.currentData() == "blg" and definition is not None and is_special_building(definition):
                self.show_special_buildings.setChecked(True)
            self.search.clear()
        self.list.setCurrentItem(target)
        self.list.scrollToItem(target, self.list.ScrollHint.PositionAtCenter)
        self.list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _changed(self, item):
        if not self._loading:
            self.permissionChanged.emit(self.faction.currentData(), self.kind.currentData(),
                                        item.data(Qt.ItemDataRole.UserRole),
                                        item.checkState() == Qt.CheckState.Checked)
