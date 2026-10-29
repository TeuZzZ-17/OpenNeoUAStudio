import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(autouse=True)
def isolated_studio_session():
    # Session persistence is exercised inside each test, not between tests.
    app = QApplication.instance()
    if app is not None:
        app.setProperty('studioSelectedSets', {})
    before = set(app.topLevelWidgets()) if app is not None else set()
    yield
    # Windows a test leaves open would live on for the whole run: a later theme
    # change refreshes them (with their fake data) and every processEvents()
    # repaints all of them. Delete them without close(), which could ask to save.
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if widget not in before:
            widget.hide()
            widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
