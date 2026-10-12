"""Shared busy feedback for synchronous resource loads on the GUI thread."""
from contextlib import contextmanager
from functools import wraps
from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout


DIALOG_STYLE = (
    "QDialog { background: #15141b; }"
    "QLabel { color: #ece9f7; background: transparent; font-size: 14px; }"
)


@contextmanager
def loading_resources(parent, label):
    if getattr(parent, '_resource_load_active', False):
        yield
        return
    parent._resource_load_active = True
    dialog = QDialog(parent)
    dialog.setWindowTitle('Loading Resources')
    # Explicit colours: without them the dialog can paint as a blank white box.
    dialog.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 22)
    message = QLabel(label)
    message.setWordWrap(True)
    message.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(message)
    dialog.setMinimumWidth(320)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    dialog.show()
    # Paint the feedback without allowing another edit or set change mid-load.
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    dialog.repaint()
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    try:
        yield
    finally:
        dialog.close()
        dialog.deleteLater()
        parent._resource_load_active = False


def loading_set(method):
    @wraps(method)
    def wrapped(self, number, *args, **kwargs):
        with loading_resources(self, f'Loading Set {number}. Please wait…'):
            return method(self, number, *args, **kwargs)
    return wrapped
