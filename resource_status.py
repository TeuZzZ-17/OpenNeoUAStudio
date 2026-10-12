"""Dirty-resource text shared by the entity catalog and archive browser."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle

MODIFIED_ROLE = int(Qt.ItemDataRole.UserRole) + 90
ORIGINAL_COLORS_ROLE = MODIFIED_ROLE + 1
MODIFIED_COLOR = QColor(255, 80, 80)


class ResourceStatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # The purple selection and hover bands hide the red text, so a modified
        # row gets a dark band in both states.
        highlighted = QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver
        if index.siblingAtColumn(0).data(MODIFIED_ROLE) and option.state & highlighted:
            visible = QStyleOptionViewItem(option)
            visible.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
            self.initStyleOption(visible, index)
            visible.backgroundBrush = QBrush(QColor(30, 32, 38))
            style = visible.widget.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, visible, painter, visible.widget)
            return
        super().paint(painter, option, index)

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.siblingAtColumn(0).data(MODIFIED_ROLE):
            option.palette.setColor(QPalette.ColorRole.HighlightedText, MODIFIED_COLOR)


def mark_resource_modified(item, modified):
    previous = bool(item.data(0, MODIFIED_ROLE))
    if previous == modified:
        return
    if modified:
        item.setData(0, ORIGINAL_COLORS_ROLE, [item.foreground(i) for i in range(item.columnCount())])
    colors = item.data(0, ORIGINAL_COLORS_ROLE) or []
    for column in range(item.columnCount()):
        item.setForeground(column, QBrush(MODIFIED_COLOR) if modified else colors[column])
    item.setData(0, MODIFIED_ROLE, modified)
