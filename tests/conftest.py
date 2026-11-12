import gc

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


@pytest.fixture(autouse=True)
def isolated_studio_session():
    # Session persistence is exercised inside each test, not between tests.
    app = QApplication.instance()
    if app is not None:
        app.setProperty('studioSelectedSets', {})
    before = set(app.topLevelWidgets()) if app is not None else set()
    yield
    # Windows a test leaves behind pile up: every later processEvents() repaints the visible
    # ones and a theme switch restyles ALL of them (minutes and gigabytes after a few thousand
    # tests). Hide each one (close() could ask to save), then let Qt delete it. A workspace that
    # still has a background job (preview, loader) is deleted after the events of the test ran.
    app = QApplication.instance()
    if app is None:
        return
    leftovers = [widget for widget in app.topLevelWidgets()
                 if widget not in before and isValid(widget)]
    for widget in leftovers:
        widget.hide()
    app.processEvents()
    for widget in leftovers:
        if isValid(widget):
            widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    # Finalize this test's Qt wrappers now. Left to the interpreter's exit, the collector destroys
    # half-deleted widgets late and in any order: a random heap-corruption crash after all tests passed.
    gc.collect()
