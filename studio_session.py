"""Selected game set shared by tools in the same Studio application."""
from PySide6.QtWidgets import QApplication
from map_editor import bootstrap


def selected_set():
    app, install = QApplication.instance(), bootstrap.installation()
    data = getattr(install, 'data', None)
    if app is None or data is None:
        return 1
    stored = app.property('studioSelectedSets') or {}
    number = stored.get(str(data.resolve()).casefold(), 1)
    available = install.available_sets()
    return number if number in available else (1 if 1 in available or not available else available[0])


def remember_set(number):
    app, install = QApplication.instance(), bootstrap.installation()
    data = getattr(install, 'data', None)
    if app is None or data is None or number not in install.available_sets():
        return
    stored = app.property('studioSelectedSets') or {}
    stored[str(data.resolve()).casefold()] = number
    app.setProperty('studioSelectedSets', stored)
