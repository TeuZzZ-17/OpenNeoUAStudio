"""Shared, view-only ordering for Studio lists and tables."""

import re
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QToolButton, QTreeWidgetItem


def sort_key(text):
    """Compare numbers by value and labels case-insensitively."""
    text = text.strip().casefold()
    try:
        number = Decimal(text)
        if number.is_finite():
            return 0, number
    except InvalidOperation:
        pass
    return 1, tuple((0, int(part)) if part.isdecimal() else (1, part)
                    for part in re.split(r"(\d+)", text))


class SortableTreeItem(QTreeWidgetItem):
    def __lt__(self, other):
        column = self.treeWidget().sortColumn()
        return sort_key(self.text(column)) < sort_key(other.text(column))


class SortableListItem(QListWidgetItem):
    def __lt__(self, other):
        return sort_key(self.text()) < sort_key(other.text())


class SortableListWidget(QListWidget):
    def addItem(self, item):
        super().addItem(SortableListItem(item) if isinstance(item, str) else item)

    def addItems(self, labels):
        for label in labels:
            self.addItem(label)


def enable_header_sort(header, sort):
    """Keep the initial order until a sortable header is clicked."""
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(False)
    column = -1
    direction = Qt.SortOrder.AscendingOrder

    def clicked(section):
        nonlocal column, direction
        descending = section == column and direction == Qt.SortOrder.AscendingOrder
        direction = Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        column = section
        header.setSortIndicator(column, direction)
        header.setSortIndicatorShown(True)
        sort(column, direction)

    header.sectionClicked.connect(clicked)


def enable_tree_sort(tree):
    enable_header_sort(tree.header(), tree.sortItems)


class IndexedListWidget(SortableListWidget):
    """Expose source rows to editors while Qt displays the items in sorted order."""

    sourceRowChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_items = []
        model = self.model()
        model.rowsInserted.connect(self._remember_inserted)
        model.rowsAboutToBeRemoved.connect(self._forget_removed)
        model.modelReset.connect(self._source_items.clear)
        self.currentRowChanged.connect(self._emit_source_row)

    def _remember_inserted(self, _parent, first, last):
        for row in range(first, last + 1):
            self._source_items.append(super().item(row))

    def _forget_removed(self, _parent, first, last):
        for row in range(first, last + 1):
            self._source_items.remove(super().item(row))

    def _emit_source_row(self, _row):
        self.sourceRowChanged.emit(self.currentRow())

    def item(self, row):
        return self._source_items[row] if 0 <= row < len(self._source_items) else None

    def row(self, item):
        return self._source_items.index(item) if item in self._source_items else -1

    def currentRow(self):
        return self.row(self.currentItem())

    def setCurrentRow(self, row, *flags):
        item = self.item(row)
        super().setCurrentRow(super().row(item) if item is not None else -1, *flags)

    def takeItem(self, row):
        item = self.item(row)
        return super().takeItem(super().row(item)) if item is not None else None


def add_list_sort(layout, widget, *, label="Name", stretch=0):
    """Add a clickable sort label above a one-column list or icon gallery."""
    button = QToolButton(widget)
    button.setText(label)
    button.setToolTip("Sort ascending or descending")
    direction = None

    def apply_sort():
        if direction is not None:
            with QSignalBlocker(widget):
                widget.sortItems(direction)

    timer = QTimer(widget)
    timer.setSingleShot(True)
    timer.timeout.connect(apply_sort)

    def toggle():
        nonlocal direction
        descending = direction == Qt.SortOrder.AscendingOrder
        direction = Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        button.setText(f"{label} {'▼' if descending else '▲'}")
        apply_sort()

    button.clicked.connect(toggle)
    widget.model().rowsInserted.connect(lambda *_: timer.start(0) if direction is not None else None)
    widget.model().dataChanged.connect(lambda *_: timer.start(0) if direction is not None else None)
    layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
    layout.addWidget(widget, stretch)


def sorted_list_panel(widget, label):
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    panel = QWidget()
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    add_list_sort(layout, widget, label=label, stretch=1)
    return panel
