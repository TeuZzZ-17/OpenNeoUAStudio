"""Small Qt helpers shared by the tests."""

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication


def dispose_window(window) -> None:
    """Close a window filled with fake test data and really delete it.

    A closed window still lives on, and a later theme change refreshes every
    widget: with its fake data such a window would fail in another test.
    """
    window.close()
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
