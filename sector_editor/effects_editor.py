"""Readable destruction-effect rows with named choices and a whole-scene preview action."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QComboBox, QSpinBox, QPushButton, QHeaderView, QAbstractItemView)


class DestructionEffectsEditor(QWidget):
    textChanged = Signal()
    previewRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._catalog = {}
        self._loading = False
        self._invalid_text = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Effects played when this damage state is left. Select an effect by name, "
                      "then place it beside, in front of, or above the model.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Effect", "Side X", "Forward Z", "Height"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(180)
        self.table.setToolTip("Side X: + right, - left. Forward Z: + forward, - behind. "
                              "Height: + above the ground. File axes are converted automatically.")
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.add_button = QPushButton("Add effect")
        self.remove_button = QPushButton("Remove")
        self.preview_button = QPushButton("Preview selected")
        self.add_button.clicked.connect(lambda: self.add_effect())
        self.remove_button.clicked.connect(self.remove_selected)
        self.preview_button.clicked.connect(self.preview_selected)
        for button in (self.add_button, self.remove_button, self.preview_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self._selection_changed()

    def load_catalog(self):
        from studio.assets.catalog import effect_objects_by_id, load_game_objects
        from .transition_fx import _ScriptsOnly
        from map_editor import bootstrap
        install = bootstrap.installation() or _ScriptsOnly()
        self._catalog = {obj.object_id: obj.name.replace("_", " ")
                         for obj in effect_objects_by_id(load_game_objects(install)).values()}

    def _changed(self):
        if not self._loading:
            self._invalid_text = None
            self.textChanged.emit()

    def _selection_changed(self):
        selected = self.table.currentRow() >= 0
        self.remove_button.setEnabled(selected)
        self.preview_button.setEnabled(selected)

    def add_effect(self, effect=None):
        if effect is None:
            effect = (next(iter(self._catalog), 49), 0, 0, 100)
        effect_id, x, file_z, height = effect
        row = self.table.rowCount()
        self.table.insertRow(row)
        choice = QComboBox()
        choice.setMinimumWidth(140)
        for key, name in sorted(self._catalog.items()):
            choice.addItem(f"{name} - ID {key}", key)
        index = choice.findData(effect_id)
        if index < 0:
            choice.addItem(f"Effect {effect_id} · not defined in scripts", effect_id)
            index = choice.count() - 1
        choice.setCurrentIndex(index)
        choice.currentIndexChanged.connect(self._changed)
        self.table.setCellWidget(row, 0, choice)
        for column, value in enumerate((x, -file_z, height), 1):
            spin = QSpinBox()
            spin.setRange(-32768, 32767)
            spin.setValue(value)
            spin.valueChanged.connect(self._changed)
            self.table.setCellWidget(row, column, spin)
        self.table.selectRow(row)
        self._changed()

    def values(self):
        result = []
        for row in range(self.table.rowCount()):
            value = [self.table.cellWidget(row, col).value() for col in range(1, 4)]
            result.append((int(self.table.cellWidget(row, 0).currentData()), value[0], -value[1], value[2]))
        return result

    def remove_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self._changed()
            self._selection_changed()

    def preview_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.previewRequested.emit(self.values()[row])

    def setPlainText(self, text):
        # Preserve the form/document boundary and exact SET.SDF tuple order.
        try:
            values = [tuple(map(int, line.split())) for line in text.splitlines() if line.strip()]
            if any(len(value) != 4 for value in values):
                raise ValueError
        except ValueError:
            # Keep the existing form API: validation happens on Apply, never while loading text.
            self._invalid_text = text
            self.textChanged.emit()
            return
        self._invalid_text = None
        self._loading = True
        try:
            self.table.setRowCount(0)
            for effect in values:
                self.add_effect(effect)
        finally:
            self._loading = False
        self.textChanged.emit()

    def toPlainText(self):
        if self._invalid_text is not None:
            return self._invalid_text
        return "\n".join(" ".join(map(str, effect)) for effect in self.values())
