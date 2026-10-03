"""Compact two-row colored tabs backed by a stacked page widget."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


TAB_COLORS = ((75, 195, 220), (235, 155, 70), (90, 200, 125), (180, 135, 240),
              (225, 120, 180), (235, 210, 80), (120, 155, 245))


class PaletteTabs(QWidget):
    """Small QTabWidget-compatible surface for the map editor's page palette."""

    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tab_texts = []
        self._buttons = []

        self._button_grid = QGridLayout()
        self._button_grid.setContentsMargins(0, 0, 0, 0)
        self._button_grid.setHorizontalSpacing(4)
        self._button_grid.setVerticalSpacing(3)

        self._tab_bar = QWidget(self)
        self._tab_bar.setLayout(self._button_grid)

        self._pages = QStackedWidget(self)
        self._pages.setMinimumWidth(0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self._tab_bar)
        layout.addWidget(self._pages, 1)
        self._pages.currentChanged.connect(self._on_current_changed)

        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def addTab(self, page, label):
        index = len(self._buttons)
        text = str(label)
        button = QPushButton(text, self._tab_bar)
        button.setObjectName(f"palette_tab_{index}")
        button.setCheckable(True)
        button.setMinimumWidth(0)
        button.setSizePolicy(QSizePolicy.Policy.Ignored,
                             QSizePolicy.Policy.Fixed)
        color = TAB_COLORS[index % len(TAB_COLORS)]
        red, green, blue = color
        button.setStyleSheet(
            "QPushButton {"
            f"color: rgb({red}, {green}, {blue});"
            "background-color: #252525;"
            f"border: 1px solid rgba({red}, {green}, {blue}, 105);"
            f"border-bottom: 2px solid rgba({red}, {green}, {blue}, 150);"
            "border-radius: 5px; padding: 4px 2px;"
            "}"
            "QPushButton:hover { background-color: #303030; }"
            "QPushButton:checked {"
            f"background-color: rgba({red}, {green}, {blue}, 42);"
            f"border: 1px solid rgb({red}, {green}, {blue});"
            f"border-bottom: 3px solid rgb({red}, {green}, {blue});"
            "}"
        )
        button.clicked.connect(
            lambda checked=False, tab_index=index: self.setCurrentIndex(tab_index)
        )

        self._tab_texts.append(text)
        self._buttons.append(button)
        self._pages.addWidget(page)
        self._reflow_buttons()
        return index

    def _reflow_buttons(self):
        count = len(self._buttons)
        columns = max(1, (count + 1) // 2)
        for button in self._buttons:
            self._button_grid.removeWidget(button)
        for index, button in enumerate(self._buttons):
            row = 0 if index < columns else 1
            column = index if row == 0 else index - columns
            self._button_grid.addWidget(button, row, column)
        for column in range(columns):
            self._button_grid.setColumnStretch(column, 1)

    def _on_current_changed(self, index):
        for button_index, button in enumerate(self._buttons):
            button.setChecked(button_index == index)
        self.currentChanged.emit(index)

    def count(self):
        return self._pages.count()

    def tabText(self, index):
        if not 0 <= index < len(self._tab_texts):
            return ""
        return self._tab_texts[index]

    def widget(self, index):
        return self._pages.widget(index)

    def currentWidget(self):
        return self._pages.currentWidget()

    def currentIndex(self):
        return self._pages.currentIndex()

    def setCurrentIndex(self, index):
        if 0 <= index < self.count():
            self._pages.setCurrentIndex(index)
