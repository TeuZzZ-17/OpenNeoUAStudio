"""Dirty-resource text shared by the entity catalog and archive browser."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle

MODIFIED_ROLE = int(Qt.ItemDataRole.UserRole) + 90
ORIGINAL_COLORS_ROLE = MODIFIED_ROLE + 1
MODIFIED_COLOR = QColor(255, 80, 80)
LIST_BASE = "#2d2d2d"
LIST_ALTERNATE = "#383838"
LIST_SELECTED = "#006d96"
LIST_HOVER = "#234e64"
LIST_DEPENDENCY_SELECTED = "#31483a"
# One selection look for every resource list (game catalog, BAS Manager,
# entity lists). Without it some lists took the Windows accent color and
# others the native grey.
SELECTION_STYLE = (
    f"QTreeView, QListView {{ background-color: {LIST_BASE}; alternate-background-color: {LIST_ALTERNATE}; }}"
    f"QTreeView::item:selected, QListView::item:selected, QTreeView::item:selected:!active, QListView::item:selected:!active {{ background-color: {LIST_SELECTED}; color: #ffffff; }}"
    f"QTreeView::item:hover:!selected, QListView::item:hover:!selected {{ background-color: {LIST_HOVER}; color: #ffffff; }}"
)


def use_standard_list_style(view, *, selected_color: str | None = None) -> None:
    """Apply the shared list colors, with an optional selection accent."""

    view.setAlternatingRowColors(True)
    view.setMouseTracking(True)
    if SELECTION_STYLE not in view.styleSheet():
        view.setStyleSheet(view.styleSheet() + SELECTION_STYLE)
    if selected_color is not None:
        view.setStyleSheet(
            view.styleSheet()
            + f"QTreeWidget::item:selected, QTreeWidget::item:selected:!active {{ background-color: {selected_color}; color: #ffffff; }}"
            + f"QTreeWidget::item:selected:hover {{ background-color: {LIST_HOVER}; color: #ffffff; }}"
        )


def use_resource_delegate(tree) -> None:
    """Shared dirty-row delegate and selection colors of a resource list."""

    use_standard_list_style(tree)
    tree.setItemDelegate(ResourceStatusDelegate(tree))


class ResourceStatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # Keep modified text red while preserving distinct selection and hover bands.
        highlighted = QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver
        if index.siblingAtColumn(0).data(MODIFIED_ROLE) and option.state & highlighted:
            visible = QStyleOptionViewItem(option)
            visible.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver)
            self.initStyleOption(visible, index)
            visible.backgroundBrush = QBrush(QColor(
                "#15435b" if option.state & QStyle.StateFlag.State_Selected
                else "#213e50"))
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
