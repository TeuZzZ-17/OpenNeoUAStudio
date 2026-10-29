import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(autouse=True)
def isolated_studio_session():
    # Session persistence is exercised inside each test, not between tests.
    app = QApplication.instance()
    if app is not None:
        app.setProperty('studioSelectedSets', {})
    before = set(app.topLevelWidgets()) if app is not None else set()
    yield
    # A window a test leaves on screen would be repainted by every later
    # processEvents(), so a long run slows down and grows without limit. Hide it:
    # close() could ask to save, and deleting a workspace can crash a background
    # job (preview, loader) that still reports to it.
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if widget not in before and widget.isVisible():
            widget.hide()
