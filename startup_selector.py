"""Startup tool selector for OpenNeoUA Studio.

The selector deliberately imports only the lightweight Qt widgets it needs.
The selected editor is loaded by :mod:`main` after this dialog is closed, so
opening OpenNeoUA Studio does not initialize every editor up front.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)


@dataclass(frozen=True)
class ToolOption:
    """One workspace offered by the startup selector."""

    key: str
    title: str
    description: str


TOOL_OPTIONS = (
    ToolOption(
        "model_editor",
        "Model Editor",
        "View, inspect, and edit textured Urban Assault 3D models, asset "
        "families, UVs, materials, and animations.",
    ),
    ToolOption(
        "snapshot_studio",
        "Snapshot Studio",
        "Load textured models and create clean presentation snapshots with "
        "camera presets, animation controls, and export options.",
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


class _ToolCard(QFrame):
    """One fixed workspace card inside the static launcher panel."""

    clicked = Signal(str)
    double_clicked = Signal(str)

    def __init__(self, option: ToolOption, parent=None) -> None:
        super().__init__(parent)
        self._key = option.key
        self.setObjectName("toolCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(option.description)
        self.setFixedHeight(72)

        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(13, 8, 13, 8)
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

    @property
    def key(self) -> str:
        return self._key

    def set_selected(self, selected: bool) -> None:
        """Refresh the card's selected-state stylesheet."""

        if bool(self.property("selected")) == selected:
            return
        self.setProperty("selected", selected)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._key)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._key)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            self.clicked.emit(self._key)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.double_clicked.emit(self._key)
            event.accept()
            return
        super().keyPressEvent(event)


class _StaticToolPanel(QFrame):
    """True static workspace panel with no item-view scrolling machinery."""

    double_activated = Signal(str)

    def __init__(self, options: tuple[ToolOption, ...], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolList")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._cards: list[_ToolCard] = []
        self._current_index = -1

        panel_layout = QVBoxLayout(self)
        panel_layout.setContentsMargins(6, 6, 6, 6)
        panel_layout.setSpacing(5)

        for option in options:
            card = _ToolCard(option, self)
            card.clicked.connect(self.set_current_key)
            card.double_clicked.connect(self._activate_key)
            panel_layout.addWidget(card)
            self._cards.append(card)

        card_height = 72
        spacing = panel_layout.spacing() * max(0, len(self._cards) - 1)
        margins = (
            panel_layout.contentsMargins().top()
            + panel_layout.contentsMargins().bottom()
        )
        self.setFixedHeight(card_height * len(self._cards) + spacing + margins)
        self.set_current_row(0)

    def count(self) -> int:
        """Compatibility helper retained for selector tests."""

        return len(self._cards)

    def current_row(self) -> int:
        return self._current_index

    def set_current_row(self, row: int) -> None:
        if not 0 <= row < len(self._cards):
            return
        self._set_current_index(row)

    def current_key(self) -> str | None:
        if not 0 <= self._current_index < len(self._cards):
            return None
        return self._cards[self._current_index].key

    def set_current_key(self, key: str) -> None:
        for index, card in enumerate(self._cards):
            if card.key == key:
                self._set_current_index(index)
                return

    def _set_current_index(self, index: int) -> None:
        self._current_index = index
        for card_index, card in enumerate(self._cards):
            card.set_selected(card_index == index)

    def _activate_key(self, key: str) -> None:
        self.set_current_key(key)
        self.double_activated.emit(key)


class StartupToolSelector(QDialog):
    """Ask which OpenNeoUA Studio workspace should be opened."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenNeoUA Studio - Select Tool")
        self.setModal(True)
        self.setMinimumSize(720, 720)
        self.resize(760, 760)
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

        # This is a plain fixed QWidget layout, not a QListWidget/QAbstractItemView.
        # Selection can therefore never scroll, reposition or hide another card.
        self.tool_list = _StaticToolPanel(TOOL_OPTIONS, self)
        self.tool_list.double_activated.connect(
            lambda _key: self.accept())
        layout.addWidget(self.tool_list)

        note = QLabel(
            "Each workspace shares the same OpenNeoUA Studio asset pipeline."
        )
        note.setObjectName("selectorNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

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
            QFrame#toolList {
                background: #171c20;
                border: 1px solid #3b464d;
                border-radius: 6px;
            }
            QFrame#toolCard {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 5px;
            }
            QFrame#toolCard:hover {
                background: #202b31;
            }
            QFrame#toolCard[selected="true"] {
                background: #28566a;
                border: 1px solid #69c9e8;
            }
            QLabel#toolTitle {
                color: #f2f5f7;
                font-size: 13px;
                font-weight: 600;
                background: transparent;
                border: none;
            }
            QLabel#toolDescription {
                color: #b9c4ca;
                font-size: 11px;
                background: transparent;
                border: none;
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

    def selected_tool(self) -> str | None:
        """Return the selected tool key, if a card is selected."""

        return self.tool_list.current_key()
