"""Tab widget with the shared game-set selector directly under its tab bar."""

from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTabWidget, QWidget

from map_editor import bootstrap

_ROW_HEIGHT = 30


class SetSelectorTabs(QTabWidget):
    """A QTabWidget whose pane starts below a "Set:" row.

    The row sits between the tab bar and the pages, so the set that drives
    every page is visible on all of them. Everything else is plain QTabWidget,
    which keeps existing ``addTab`` / ``setCurrentWidget`` callers unchanged.
    """

    def __init__(self, set_combo, parent=None):
        super().__init__(parent)
        self.set_combo = set_combo
        self._row = QWidget(self)
        layout = QHBoxLayout(self._row)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.addWidget(QLabel("Set:"))
        layout.addWidget(set_combo, 1)
        # Leave room for the row: the pane is pushed down by its height.
        self.setStyleSheet(f"QTabWidget::pane {{ margin-top: {_ROW_HEIGHT}px; border: none; }}")
        self.tabBar().installEventFilter(self)
        self._place_row()

    def _place_row(self):
        bar = self.tabBar().geometry()
        self._row.setGeometry(0, bar.bottom() + 1, self.width(), _ROW_HEIGHT)
        self._row.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_row()

    def eventFilter(self, watched, event):
        if watched is self.tabBar() and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._place_row()
        return super().eventFilter(watched, event)


def build_set_tabs(old_tabs, set_combo):
    """Copy ``old_tabs`` into a SetSelectorTabs with the same look and pages."""
    tabs = SetSelectorTabs(set_combo)
    tabs.setDocumentMode(old_tabs.documentMode())
    tabs.setUsesScrollButtons(old_tabs.usesScrollButtons())
    tabs.tabBar().setExpanding(old_tabs.tabBar().expanding())
    tabs.tabBar().setStyleSheet(old_tabs.tabBar().styleSheet())
    while old_tabs.count():
        title, page = old_tabs.tabText(0), old_tabs.widget(0)
        old_tabs.removeTab(0)
        tabs.addTab(page, title)
    return tabs


def fill_set_combo(combo, install, selected=1):
    """List the complete sets of ``install`` in ``combo`` without emitting changes."""
    with QSignalBlocker(combo):
        combo.clear()
        for number in install.available_sets() if install else []:
            combo.addItem(f"Set {number}", number)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)


def bind_entity_set_selector(tabs, global_row, entity_panel, controls, combo):
    """Move the same selector between the compact entity row and BAS context."""
    def route(*_args):
        if tabs.currentWidget() is entity_panel:
            global_row.layout().removeWidget(combo)
            controls.insertWidget(1, combo)
            global_row.hide()
            if isinstance(tabs, SetSelectorTabs):
                tabs.setStyleSheet('QTabWidget::pane { margin-top: 0px; border: none; }')
        else:
            controls.removeWidget(combo)
            global_row.layout().addWidget(combo, 1)
            global_row.show()
            if isinstance(tabs, SetSelectorTabs):
                tabs.setStyleSheet(f'QTabWidget::pane {{ margin-top: {_ROW_HEIGHT}px; border: none; }}')
    tabs.currentChanged.connect(route)
    route()


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
            from studio_session import remember_set
            remember_set(number)
            return number
    return None


class SetBasMixin:
    """Keeps the Bas Manager on the SET.BAS of the set chosen in ``_set_combo``.

    The host window sets ``_set_combo`` and calls ``_set_selector_changed`` when
    the selection changes. The first load always uses set 1.
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
            from loading_feedback import loading_resources
            with loading_resources(self, f'Loading Set {number}. Please wait…'):
                self.open_setbas(path)
        if self._setbas is not None and Path(self._setbas.path) == path:
            self._bas_set = number
            from studio_session import remember_set
            remember_set(number)
            return True
        return False

    def _load_default_bas(self):
        """Open the shared selected set, using set 1 for a fresh session."""
        from studio_session import selected_set
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
