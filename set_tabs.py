"""Set helpers shared by the Model Editor, Entity Designer and Snapshot Studio."""

from pathlib import Path

from PySide6.QtCore import QSignalBlocker

from map_editor import bootstrap


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
            from studio_session import remember_set
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
