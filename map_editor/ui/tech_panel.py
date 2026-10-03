"""Per-faction build permissions from discovered scripts and existing LDF IDs."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QLabel, QPlainTextEdit
from ..core.ldf_model import FACTIONS
from .preview_cards import PreviewList, set_card, faction_style


class TechPanel(QWidget):
    permissionChanged = Signal(int, str, int, bool)

    def __init__(self):
        super().__init__()
        self.doc = None
        self.vehicles, self.buildings, self.colors = {}, {}, {}
        self._loading = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.faction = QComboBox()
        for key, name in FACTIONS.items():
            if key:
                self.faction.addItem(name, key)
        self.kind = QComboBox()
        self.kind.addItem('Vehicles', 'veh')
        self.kind.addItem('Buildings', 'blg')
        row.addWidget(self.faction, 1)
        row.addWidget(self.kind)
        layout.addLayout(row)
        self.search = QLineEdit(placeholderText='Filter by name or ID...')
        layout.addWidget(self.search)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFixedHeight(78)
        layout.addWidget(self.summary)
        self.list = PreviewList(large_text=False)
        layout.addLayout(self.list.preview_controls())
        layout.addWidget(self.list, 1)
        self.faction.currentIndexChanged.connect(self.rebuild)
        self.kind.currentIndexChanged.connect(self.rebuild)
        self.search.textChanged.connect(self._filter)
        self.list.itemChanged.connect(self._changed)
        self.list.itemClicked.connect(self._clicked)

    def refresh(self, doc, vehicles, buildings, colors):
        self.doc, self.vehicles, self.buildings, self.colors = doc, vehicles, buildings, colors
        self.rebuild()

    def permissions(self, kind):
        faction = self.faction.currentData()
        explicit = faction in self.doc.tech_explicit or any(self.doc.tech[faction].values())
        if explicit:
            return self.doc.tech[faction][kind]
        return [key for key, definition in
                (self.vehicles if kind == 'veh' else self.buildings).items()
                if faction in definition.enabled_factions]

    def update_permissions(self):
        if self.doc is None:
            return
        self._loading = True
        faction, kind = self.faction.currentData(), self.kind.currentData()
        definitions = self.vehicles if kind == 'veh' else self.buildings
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
        summaries = []
        for label, category, catalog in (('Vehicles', 'veh', self.vehicles), ('Buildings', 'blg', self.buildings)):
            enabled = self.permissions(category)
            names = [f'{key} {catalog[key].name}' if key in catalog else f'ID {key}' for key in enabled]
            summaries.append(f'{label} ({len(enabled)}): ' + (', '.join(names) or 'None'))
        self.summary.setPlainText('\n'.join(summaries))
        self.summary.setStyleSheet(f'QPlainTextEdit {{ color: rgb{self.colors.get(faction, (180,180,180))}; }}')
        # Existing permissions stay visible even if a mod definition is missing.
        self.list.setEnabled(True)
        faction_style(self.faction, self.colors)
        self._loading = False

    def rebuild(self, *_args):
        if self.doc is None:
            return
        self._loading = True
        scroll = self.list.verticalScrollBar().value()
        selected = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        self.list.clear()
        kind = self.kind.currentData()
        definitions = self.vehicles if kind == 'veh' else self.buildings
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
            item.setHidden(text not in item.text().casefold())
        self.list.previewsRequested.emit()

    def _clicked(self, item):
        # The whole card is the toggle target; keep one change per click.
        item.setCheckState(Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                           else Qt.CheckState.Checked)

    def _changed(self, item):
        if not self._loading:
            self.permissionChanged.emit(self.faction.currentData(), self.kind.currentData(),
                                        item.data(Qt.ItemDataRole.UserRole),
                                        item.checkState() == Qt.CheckState.Checked)
