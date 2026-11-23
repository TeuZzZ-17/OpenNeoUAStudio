"""Readable destruction-effect rows with named choices, detail editor, and whole-scene preview."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QPushButton, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from studio.ui.sorting import enable_header_sort, sort_key


def _fill_effect_combo(choice: QComboBox, catalog: dict[int, str], effect_id: int) -> None:
    choice.clear()
    for key, name in sorted(catalog.items()):
        choice.addItem(f"{name} - ID {key}", key)
    index = choice.findData(effect_id)
    if index < 0:
        choice.addItem(f"Effect {effect_id} · not defined in scripts", effect_id)
        index = choice.count() - 1
    choice.setCurrentIndex(index)


class DestructionEffectsEditor(QWidget):
    textChanged = Signal()
    previewRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._catalog = {}
        self._loading = False
        self._syncing_detail = False
        self._invalid_text = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        hint = QLabel("Effects played when this damage state is left. Select a row to inspect or edit it cleanly below.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Effect", "X", "Z", "Height"))
        enable_header_sort(self.table.horizontalHeader(), self._sort_rows)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.verticalHeader().setMinimumWidth(24)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, 56)
        self.table.setMinimumHeight(220)
        self.table.setToolTip("Side X: + right, - left. Forward Z: + forward, - behind. "
                              "Height: + above the ground. File axes are converted automatically.")
        layout.addWidget(self.table, 1)

        # Dedicated full-width detail inspector for the selected effect row (avoids cramped table edits).
        self.detail_box = QGroupBox("Selected effect", self)
        detail_form = QFormLayout(self.detail_box)
        detail_form.setContentsMargins(8, 8, 8, 8)
        detail_form.setSpacing(6)
        self.detail_effect = QComboBox(self.detail_box)
        self.detail_effect.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.detail_effect.setMinimumContentsLength(10)
        self.detail_effect.currentIndexChanged.connect(self._detail_changed)
        detail_form.addRow("Effect", self.detail_effect)

        coords_row = QHBoxLayout()
        coords_row.setSpacing(6)
        self.detail_coords: list[QSpinBox] = []
        for axis in ("X", "Z", "H"):
            coords_row.addWidget(QLabel(axis, self.detail_box))
            spin = QSpinBox(self.detail_box)
            spin.setRange(-32768, 32767)
            spin.valueChanged.connect(self._detail_changed)
            coords_row.addWidget(spin, 1)
            self.detail_coords.append(spin)
        detail_form.addRow("Offset", coords_row)
        layout.addWidget(self.detail_box)

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
            self._sync_detail_from_row()
            self.textChanged.emit()

    def _sync_detail_from_row(self):
        row = self.table.currentRow()
        has_row = 0 <= row < self.table.rowCount()
        self.detail_box.setEnabled(has_row)
        if not has_row:
            return
        combo = self.table.cellWidget(row, 0)
        if combo is None:
            return
        self._syncing_detail = True
        try:
            _fill_effect_combo(self.detail_effect, self._catalog, int(combo.currentData()))
            for idx, spin in enumerate(self.detail_coords):
                cell_spin = self.table.cellWidget(row, idx + 1)
                if cell_spin is not None:
                    spin.setValue(cell_spin.value())
        finally:
            self._syncing_detail = False

    def _detail_changed(self):
        if self._syncing_detail or self._loading:
            return
        row = self.table.currentRow()
        if not (0 <= row < self.table.rowCount()):
            return
        combo = self.table.cellWidget(row, 0)
        if combo is not None:
            combo.blockSignals(True)
            idx = combo.findData(self.detail_effect.currentData())
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        for idx, spin in enumerate(self.detail_coords):
            cell_spin = self.table.cellWidget(row, idx + 1)
            if cell_spin is not None:
                cell_spin.blockSignals(True)
                cell_spin.setValue(spin.value())
                cell_spin.blockSignals(False)
        self._invalid_text = None
        self.textChanged.emit()

    def _selection_changed(self):
        selected = self.table.currentRow() >= 0
        self.remove_button.setEnabled(selected)
        self.preview_button.setEnabled(selected)
        self._sync_detail_from_row()

    def _sort_rows(self, column, direction):
        def key(row):
            widget = self.table.cellWidget(row, column)
            return sort_key(widget.currentText()) if column == 0 else widget.value()

        # Move the visual sections only. Logical rows keep the saved effect order.
        rows = sorted(range(self.table.rowCount()), key=key,
                      reverse=direction == Qt.SortOrder.DescendingOrder)
        header = self.table.verticalHeader()
        for position, row in enumerate(rows):
            header.moveSection(header.visualIndex(row), position)

    def add_effect(self, effect=None):
        if effect is None:
            effect = (next(iter(self._catalog), 49), 0, 0, 100)
        effect_id, x, file_z, height = effect
        row = self.table.rowCount()
        self.table.insertRow(row)
        choice = QComboBox()
        choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        choice.setMinimumContentsLength(8)
        _fill_effect_combo(choice, self._catalog, effect_id)
        choice.setToolTip(choice.currentText())
        choice.currentIndexChanged.connect(lambda _idx, c=choice: (c.setToolTip(c.currentText()), self._changed()))
        self.table.setCellWidget(row, 0, choice)
        for column, value in enumerate((x, -file_z, height), 1):
            spin = QSpinBox()
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            spin.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
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
        self._sync_detail_from_row()
        self.textChanged.emit()

    def toPlainText(self):
        if self._invalid_text is not None:
            return self._invalid_text
        return "\n".join(" ".join(map(str, effect)) for effect in self.values())
