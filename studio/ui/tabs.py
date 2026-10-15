"""Workspace tab strips in the Map Editor palette style and the Set tab helpers.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRect, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from map_editor import bootstrap

# ----------------------------------------------------------------------------
# Studio-wide workspace tabs in the Map Editor palette style.
# (formerly studio_tabs.py)
# ----------------------------------------------------------------------------

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

    light = QApplication.instance().property("studioTheme") == "light"
    red, green, blue = color
    if light:
        red, green, blue = (int(channel * 0.55) for channel in color)
    palette = QApplication.instance().palette()
    background = palette.color(QPalette.ColorRole.Button).name()
    hover = palette.color(QPalette.ColorRole.AlternateBase).name()
    checked_text = palette.color(QPalette.ColorRole.WindowText).name() if light else "#ffffff"
    return (
        "QPushButton {"
        f"color: rgb({red}, {green}, {blue});"
        f"background-color: {background};"
        f"border: 1px solid rgba({red}, {green}, {blue}, 105);"
        f"border-bottom: 2px solid rgba({red}, {green}, {blue}, 150);"
        "border-radius: 5px; padding: 4px 2px;"
        "}"
        f"QPushButton:hover {{ background-color: {hover}; }}"
        "QPushButton:checked {"
        f"background-color: rgba({red}, {green}, {blue}, 85);"
        f"color: {checked_text}; padding: 3px 1px;"
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
        self._tab_color = color
        self.marker = marker
        self.setCheckable(True)
        self.setMinimumWidth(0)
        self.setFixedHeight(_BUTTON_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(palette_button_style(color))

    def refresh_studio_theme(self) -> None:
        self.setStyleSheet(palette_button_style(self._tab_color))

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
            # deleteLater() is deferred: hide now so the old button is never
            # painted over the new one.
            button.hide()
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


# ----------------------------------------------------------------------------
# Set helpers shared by the Model Editor, Entity Designer and Snapshot Studio.
# (formerly set_tabs.py)
# ----------------------------------------------------------------------------

def fill_set_combo(combo, install, selected=1):
    """List the complete sets of ``install`` in ``combo`` without emitting changes."""
    with QSignalBlocker(combo):
        combo.clear()
        for number in install.available_sets() if install else []:
            combo.addItem(f"Set {number}", number)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)


def sync_archive_set_selector(combo, path):
    """Use the installation's actual SET.BAS paths, never a guessed set ID."""
    install = bootstrap.installation()
    if install is None:
        return None
    path = Path(path).resolve()
    for number in install.available_sets():
        candidate = SetBasMixin._set_bas_path(number)
        if candidate is not None and candidate.resolve() == path:
            with QSignalBlocker(combo):
                combo.setCurrentIndex(combo.findData(number))
            from studio.ui.shell import remember_set
            remember_set(number)
            return number
    return None


class SetBasMixin:
    """Keeps the Bas Manager on the SET.BAS of the set chosen in ``_set_combo``.

    ``_set_combo`` is the hidden combo driven by the Change Set menu; the host
    window calls ``_set_selector_changed`` when it changes. The first load
    always uses set 1.
    """

    _set_combo = None
    _bas_set = None

    def _sync_imported_set(self, path):
        if self._set_combo is None:
            return
        number = sync_archive_set_selector(self._set_combo, path)
        if number is not None:
            self._bas_set = number
            browser = getattr(self, 'entity_browser', None)
            if browser is not None:
                browser.enable_state_column()
        browser = getattr(self, 'entity_browser', None)
        if browser is not None and not getattr(self, '_entity_loading', False):
            browser.select_object(None)
            self._current_entity = None
            with QSignalBlocker(self.entity_state):
                self.entity_state.clear()

    @staticmethod
    def _set_bas_path(number):
        """SET.BAS of a set from the configured game resources, or None."""
        if bootstrap.installation() is None:
            return None
        objects = bootstrap.find_ci(bootstrap.set_dir(number), "objects")
        return bootstrap.find_ci(objects, "set.bas") if objects else None

    def _load_bas_for_set(self, number):
        """Load the set's SET.BAS into the Bas Manager; True when it is active."""
        path = self._set_bas_path(number)
        if path is None:
            return False
        if self._setbas is None or Path(self._setbas.path) != path:
            from studio.ui.widgets import loading_resources
            with loading_resources(self, f'Loading Set {number}. Please wait…'):
                self.open_setbas(path)
        if self._setbas is not None and Path(self._setbas.path) == path:
            self._bas_set = number
            from studio.ui.shell import remember_set
            remember_set(number)
            return True
        return False

    def _load_default_bas(self):
        """Open the shared selected set, using set 1 for a fresh session."""
        from studio.ui.shell import selected_set
        combo = self._set_combo
        index = combo.findData(selected_set())
        if index >= 0:
            with QSignalBlocker(combo):
                combo.setCurrentIndex(index)
        self._load_bas_for_set(combo.currentData() or 1)

    def _set_selector_changed(self, number):
        """Load the new set; when refused (unsaved edits) put the selector back."""
        if self._load_bas_for_set(number):
            return True
        combo = self._set_combo
        index = combo.findData(self._bas_set)
        if index >= 0 and combo.currentIndex() != index:
            with QSignalBlocker(combo):
                combo.setCurrentIndex(index)
        return False
