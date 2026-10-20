"""Hand cursors on the clickable Studio controls that have no cursor of their own.

One application-wide filter covers every editor: buttons, check and radio boxes, drop-down
lists and tab bars get the pointing hand; sliders get the open hand, which closes while the
handle is held. A cursor set by a widget itself (the 3D views, the code drawer tab, ...) is
never touched, and a disabled control gets the normal arrow back.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QSlider, QTabBar, QWidget

POINTER_TYPES = (QAbstractButton, QComboBox, QSlider, QTabBar)
# Marks a cursor that this module set, so it can be told apart from one a widget chose itself.
_OWN_CURSOR = "studioPointerCursor"
_INSTALLED = "studioPointerCursorsInstalled"


def hover_cursor(widget: QWidget) -> Qt.CursorShape:
    return (Qt.CursorShape.OpenHandCursor if isinstance(widget, QSlider)
            else Qt.CursorShape.PointingHandCursor)


def update_pointer_cursor(widget: QWidget) -> None:
    """Hand while the control can be used, arrow when it is disabled."""

    ours = bool(widget.property(_OWN_CURSOR))
    if widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor) and not ours:
        return
    if widget.isEnabled():
        widget.setCursor(hover_cursor(widget))
        widget.setProperty(_OWN_CURSOR, True)
    elif ours:
        widget.unsetCursor()
        widget.setProperty(_OWN_CURSOR, False)


class PointerCursorFilter(QObject):
    """Gives each new or re-enabled control its cursor."""

    def eventFilter(self, watched, event):  # noqa: N802
        if not isinstance(watched, POINTER_TYPES):
            return False
        kind = event.type()
        if kind in (QEvent.Type.Polish, QEvent.Type.EnabledChange):
            update_pointer_cursor(watched)
        elif isinstance(watched, QSlider) and watched.property(_OWN_CURSOR) and watched.isEnabled() \
                and getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton:
            # The hand closes while a slider handle is held.
            if kind == QEvent.Type.MouseButtonPress:
                watched.setCursor(Qt.CursorShape.ClosedHandCursor)
            elif kind == QEvent.Type.MouseButtonRelease:
                watched.setCursor(Qt.CursorShape.OpenHandCursor)
        return False


def install_pointer_cursors(app: QApplication | None = None) -> None:
    """Install the filter once and cover the controls that already exist."""

    app = app or QApplication.instance()
    if app is None or app.property(_INSTALLED):
        return
    app.setProperty(_INSTALLED, True)
    app.installEventFilter(PointerCursorFilter(app))
    for widget in app.allWidgets():
        if isinstance(widget, POINTER_TYPES):
            update_pointer_cursor(widget)
