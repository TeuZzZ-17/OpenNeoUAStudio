"""Startup tool selector for OpenUAStudio.

The selector deliberately imports only the lightweight Qt widgets it needs.
The selected editor is loaded by :mod:`main` after this dialog is closed, so
opening OpenUAStudio does not initialize every editor up front.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class ToolOption:
    """One workspace offered by the startup selector."""

    key: str
    title: str
    description: str


TOOL_OPTIONS = (
    ToolOption(
        "main_suite",
        "Main Suite",
        "General asset workbench for BASE, SKLT, SET.BAS, textures, "
        "animations, and integrated tools.",
    ),
    ToolOption(
        "map_editor",
        "Map Editor",
        "Create and edit LDF maps, terrain sectors, buildings, vehicles, "
        "and level layouts.",
    ),
    ToolOption(
        "collision_editor",
        "Collision Editor",
        "Create and edit collision spheres, then export collision scripts "
        "for vehicles and models.",
    ),
    ToolOption(
        "wireframe_editor",
        "Wireframe Editor",
        "Inspect and edit 2D wireframe geometry stored in SKLT files.",
    ),
)


class _ToolCard(QWidget):
    """Clickable item widget used inside the tool list."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class StartupToolSelector(QDialog):
    """Ask which OpenUAStudio workspace should be opened."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenUAStudio - Select Tool")
        self.setModal(True)
        self.setMinimumSize(620, 470)
        self.resize(700, 540)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(12)

        title = QLabel("Choose a workspace")
        title.setObjectName("selectorTitle")
        title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(title)

        subtitle = QLabel(
            "Select the tool that matches the task you want to perform."
        )
        subtitle.setObjectName("selectorSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.tool_list = QListWidget()
        self.tool_list.setObjectName("toolList")
        self.tool_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection)
        self.tool_list.setSpacing(5)
        self.tool_list.setUniformItemSizes(True)
        self.tool_list.setAlternatingRowColors(False)
        self.tool_list.itemDoubleClicked.connect(
            lambda _item: self.accept())
        layout.addWidget(self.tool_list, 1)

        note = QLabel(
            "Additional utilities remain available from the Main Suite."
        )
        note.setObjectName("selectorNote")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        buttons_layout.addWidget(self.cancel_button)
        self.open_button = QPushButton("Open Tool")
        self.open_button.setObjectName("openToolButton")
        self.open_button.setDefault(True)
        self.open_button.clicked.connect(self.accept)
        buttons_layout.addWidget(self.open_button)
        layout.addLayout(buttons_layout)

        self._populate_tools()
        self.setStyleSheet(
            """
            QDialog {
                background: #20262b;
            }
            QLabel#selectorTitle {
                color: #f2f5f7;
                font-size: 21px;
                font-weight: 600;
            }
            QLabel#selectorSubtitle, QLabel#selectorNote {
                color: #aeb9c1;
                font-size: 11px;
            }
            QLabel#selectorNote {
                color: #8f9ca5;
            }
            QListWidget#toolList {
                background: #171c20;
                border: 1px solid #3b464d;
                border-radius: 6px;
                outline: none;
                padding: 6px;
            }
            QListWidget#toolList::item {
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 5px;
            }
            QListWidget#toolList::item:selected {
                background: #28566a;
                border: 1px solid #69c9e8;
            }
            QLabel#toolTitle {
                color: #f2f5f7;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#toolDescription {
                color: #b9c4ca;
                font-size: 11px;
            }
            QPushButton {
                min-width: 92px;
                min-height: 30px;
                padding: 4px 14px;
            }
            QPushButton#openToolButton {
                background: #347e9a;
                border: 1px solid #69c9e8;
                border-radius: 4px;
                color: white;
                font-weight: 600;
            }
            QPushButton#openToolButton:hover {
                background: #4197b5;
            }
            """)

    def _populate_tools(self) -> None:
        for option in TOOL_OPTIONS:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, option.key)
            item.setToolTip(option.description)
            item.setSizeHint(QSize(0, 72))

            card = _ToolCard()
            card.setAttribute(
                Qt.WidgetAttribute.WA_TranslucentBackground, True)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 5, 8, 5)
            card_layout.setSpacing(3)

            title = QLabel(option.title)
            title.setObjectName("toolTitle")
            title.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            description = QLabel(option.description)
            description.setObjectName("toolDescription")
            description.setWordWrap(True)
            description.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            card_layout.addWidget(title)
            card_layout.addWidget(description)

            self.tool_list.addItem(item)
            self.tool_list.setItemWidget(item, card)
            card.clicked.connect(
                lambda item=item: self.tool_list.setCurrentItem(item))

        self.tool_list.setCurrentRow(0)

    def selected_tool(self) -> str | None:
        """Return the selected tool key, if a row is selected."""

        item = self.tool_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value is not None else None
