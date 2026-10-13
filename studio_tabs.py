"""Studio-wide workspace tabs in the Map Editor palette style.

``PaletteTabStrip.install(tabs)`` replaces the single-row tab bar of a
QTabWidget with a grid of colored buttons. When a page is itself a
QTabWidget (a category of sub-tabs), its pages become buttons of the same
grid: Studio shows one flat level of tabs and no sub-tabs. The QTabWidgets
keep owning pages, current indices and enabled states, so existing editor
code is unchanged; the strip only mirrors and drives them.

The strip sits in a small container that takes the QTabWidget's place in
its parent layout (strip above, tabs below). No style sheet is set on the
tabs, so the controls of every page keep their native size and look.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QGridLayout, QPushButton, QSizePolicy, QSplitter, QTabWidget, QVBoxLayout,
    QWidget,
)

# Twelve contrasting colors, one per tab position (Map Editor palette).
TAB_COLORS = (
    (75, 195, 220),    # cyan
    (240, 153, 75),    # orange
    (90, 210, 105),    # green
    (169, 120, 238),   # violet
    (238, 106, 159),   # pink
    (235, 210, 75),    # yellow
    (100, 146, 245),   # blue
    (70, 208, 178),    # turquoise
    (242, 105, 92),    # red
    (169, 219, 82),    # lime
    (225, 105, 230),   # magenta
    (190, 165, 120),   # sand
)

_BUTTON_HEIGHT = 34
# Narrowest button that still shows a full tab title.
_MIN_BUTTON_WIDTH = 128
_SPACING = 4
_REFRESH_MS = 400


def palette_button_style(color: tuple[int, int, int]) -> str:
    """Style of one palette tab button (shared with the Map Editor)."""

    red, green, blue = color
    return (
        "QPushButton {"
        f"color: rgb({red}, {green}, {blue});"
        "background-color: #252525;"
        f"border: 1px solid rgba({red}, {green}, {blue}, 105);"
        f"border-bottom: 2px solid rgba({red}, {green}, {blue}, 150);"
        "border-radius: 5px; padding: 4px 2px;"
        "}"
        "QPushButton:hover { background-color: #303030; }"
        "QPushButton:checked {"
        f"background-color: rgba({red}, {green}, {blue}, 85);"
        "color: #ffffff; padding: 3px 1px;"
        f"border: 2px solid rgb({red}, {green}, {blue});"
        f"border-bottom: 3px solid rgb({red}, {green}, {blue});"
        "}"
        "QPushButton:disabled { color: #5a5a5a; border-color: #3a3a3a; }"
    )


def palette_columns(count: int) -> int:
    """Balanced grid columns: one row up to four tabs, then two rows."""

    if count <= 4:
        return max(1, count)
    if count <= 10:
        return (count + 1) // 2
    return 4


class _PaletteButton(QPushButton):
    """Palette tab with an optional compatibility dot in its corner."""

    def __init__(self, text: str, color, marker: QColor | None, parent=None):
        super().__init__(text, parent)
        self.marker = marker
        self.setCheckable(True)
        self.setMinimumWidth(0)
        self.setFixedHeight(_BUTTON_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(palette_button_style(color))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.marker is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self.marker)
        if not self.isEnabled():
            color.setAlpha(80)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRect(self.width() - 12, 5, 7, 7))
        painter.end()


class PaletteTabStrip(QWidget):
    """Flat palette of every page of a QTabWidget and its sub-tab widgets."""

    leafActivated = Signal(object)

    def __init__(self, root: QTabWidget, *, markers=None) -> None:
        super().__init__()
        self.root = root
        self.container: QWidget | None = None
        # page widget -> QColor of its compatibility dot
        self._markers = dict(markers or {})
        self._buttons: list[_PaletteButton] = []
        self._leaves: list[tuple[QTabWidget, int, QTabWidget | None, int]] = []
        self._signature = None
        self._columns = 0
        self._connected: set[int] = set()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, _SPACING)
        self._grid.setHorizontalSpacing(_SPACING)
        self._grid.setVerticalSpacing(3)
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        root.installEventFilter(self)
        self.refresh()
        self._dock()

    @classmethod
    def install(cls, root: QTabWidget, *, markers=None) -> "PaletteTabStrip":
        existing = getattr(root, "_palette_strip", None)
        if existing is not None:
            existing.set_markers(markers or {})
            return existing
        strip = cls(root, markers=markers)
        root._palette_strip = strip
        return strip

    def set_markers(self, markers) -> None:
        self._markers = dict(markers)
        self._signature = None
        self.refresh()

    # -- model -----------------------------------------------------------------

    def _collect(self):
        leaves = []
        root = self.root
        for index in range(root.count()):
            if not root.isTabVisible(index):
                continue
            page = root.widget(index)
            if isinstance(page, QTabWidget):
                page.tabBar().hide()
                self._watch(page)
                for inner in range(page.count()):
                    if page.isTabVisible(inner):
                        leaves.append((root, index, page, inner))
            else:
                leaves.append((root, index, None, -1))
        return leaves

    def _watch(self, tabs: QTabWidget) -> None:
        if id(tabs) not in self._connected:
            self._connected.add(id(tabs))
            tabs.currentChanged.connect(self.refresh)
            tabs.currentChanged.connect(self._sync_checked)

    def _leaf_text(self, leaf) -> str:
        root, index, inner_tabs, inner = leaf
        return inner_tabs.tabText(inner) if inner_tabs is not None else root.tabText(index)

    def _leaf_page(self, leaf):
        root, index, inner_tabs, inner = leaf
        return inner_tabs.widget(inner) if inner_tabs is not None else root.widget(index)

    def _leaf_enabled(self, leaf) -> bool:
        root, index, inner_tabs, inner = leaf
        enabled = root.isTabEnabled(index)
        if inner_tabs is not None:
            enabled = enabled and inner_tabs.isTabEnabled(inner)
        return enabled

    def refresh(self, *_args) -> None:
        """Rebuild after tabs were added, removed, renamed or toggled."""

        self.root.tabBar().hide()
        self._watch(self.root)
        leaves = self._collect()
        signature = tuple(
            (self._leaf_text(leaf), id(self._leaf_page(leaf)),
             self._leaf_enabled(leaf)) for leaf in leaves)
        if signature == self._signature:
            return
        self._signature = signature
        self._leaves = leaves
        for button in self._buttons:
            self._grid.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        for position, leaf in enumerate(leaves):
            page = self._leaf_page(leaf)
            button = _PaletteButton(
                self._leaf_text(leaf), TAB_COLORS[position % len(TAB_COLORS)],
                self._markers.get(page), self)
            button.setEnabled(self._leaf_enabled(leaf))
            button.clicked.connect(
                lambda _checked=False, slot=position: self.activate(slot))
            self._buttons.append(button)
        self._columns = 0
        self._sync_checked()
        self._place()

    def _fit_columns(self) -> int:
        """Palette columns, reduced when the area is too narrow."""

        count = len(self._buttons)
        width = max(1, self.root.width())
        return max(1, min(palette_columns(count), width // _MIN_BUTTON_WIDTH))

    def _regrid(self) -> None:
        columns = self._fit_columns()
        if columns == self._columns:
            return
        self._columns = columns
        for button in self._buttons:
            self._grid.removeWidget(button)
        for position, button in enumerate(self._buttons):
            row, column = divmod(position, columns)
            self._grid.addWidget(button, row, column)
        for column in range(max(self._grid.columnCount(), columns)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def activate(self, slot: int) -> None:
        if not 0 <= slot < len(self._leaves):
            return
        root, index, inner_tabs, inner = self._leaves[slot]
        if inner_tabs is not None:
            inner_tabs.setCurrentIndex(inner)
        root.setCurrentIndex(index)
        self._sync_checked()
        self.leafActivated.emit(self._leaf_page(self._leaves[slot]))

    def _sync_checked(self, *_args) -> None:
        current_root = self.root.currentIndex()
        for button, leaf in zip(self._buttons, self._leaves):
            root, index, inner_tabs, inner = leaf
            checked = index == current_root and (
                inner_tabs is None or inner_tabs.currentIndex() == inner)
            button.setChecked(checked)

    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def row_count(self) -> int:
        columns = self._columns or self._fit_columns()
        return -(-len(self._buttons) // columns) if self._buttons else 0

    # -- placement above the QTabWidget ------------------------------------------

    def _dock(self) -> bool:
        """Put strip and tabs in one container at the tabs' layout slot.

        Called again (deferred) when the tabs get a parent later, because
        Qt adds the layout item only after reparenting the widget.
        """

        if self.container is not None:
            return True
        parent = self.root.parentWidget()
        if parent is None:
            return False
        was_hidden = self.root.isHidden()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        if isinstance(parent, QSplitter):
            index = parent.indexOf(self.root)
            if index < 0:
                return False
            parent.replaceWidget(index, container)
        else:
            parent_layout = parent.layout()
            if parent_layout is None or parent_layout.indexOf(self.root) < 0:
                container.deleteLater()
                return False
            parent_layout.replaceWidget(self.root, container)
        layout.addWidget(self)
        layout.addWidget(self.root, 1)
        container.setSizePolicy(self.root.sizePolicy())
        # Reparenting hides a widget: restore the tabs' own visibility.
        self.root.setVisible(not was_hidden)
        container.setVisible(not was_hidden)
        self.container = container
        self._place()
        return True

    def strip_height(self) -> int:
        rows = self.row_count()
        return rows * _BUTTON_HEIGHT + max(0, rows - 1) * 3 + _SPACING if rows else 0

    def _place(self) -> None:
        self._regrid()
        self.setFixedHeight(self.strip_height())

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802
        if watched is self.root:
            kind = event.type()
            if kind == QEvent.Type.Resize:
                self._place()
            elif kind == QEvent.Type.Show:
                # Pick up pages added while hidden before the first paint.
                self.refresh()
            elif kind == QEvent.Type.ParentChange and self.container is None:
                QTimer.singleShot(0, self._dock)
        return super().eventFilter(watched, event)
