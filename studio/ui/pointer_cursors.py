"""Cursors of the whole Studio: hands on the clickable controls, the theme arrow everywhere else.

One application-wide filter covers every editor: buttons, check and radio boxes, drop-down
lists and tab bars get the pointing hand; sliders get the open hand, which closes while the
handle is held. Every window, dialog and menu also gets the theme's ``arrow`` cursor, which
all its child widgets inherit, so a painted cursor theme is visible over panels, lists and forms
too, not only in the 3D views (a system theme leaves the windows alone). The hands are the
shared set of ``studio.ui.cursor_themes``. A cursor set by a
widget itself (the 3D views, the code drawer tab, ...) is never touched, and a disabled
control gets the normal arrow back.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QSlider, QTabBar, QWidget

from studio.ui.cursor_themes import arrow_is_custom, studio_cursor

POINTER_TYPES = (QAbstractButton, QComboBox, QSlider, QTabBar)
# Marks a window whose cursor is the theme arrow set by this module.
_WINDOW_CURSOR = "studioWindowCursor"
# Marks a cursor that this module set, so it can be told apart from one a widget chose itself.
_OWN_CURSOR = "studioPointerCursor"
_INSTALLED = "studioPointerCursorsInstalled"


def hover_cursor(widget: QWidget) -> QCursor:
    return studio_cursor("open" if isinstance(widget, QSlider) else "point")


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


def update_window_cursor(window: QWidget) -> None:
    """The theme arrow on a window (inherited by its widgets); nothing for system themes."""

    ours = bool(window.property(_WINDOW_CURSOR))
    if window.testAttribute(Qt.WidgetAttribute.WA_SetCursor) and not ours:
        return  # the window chose its own cursor
    if arrow_is_custom():
        window.setCursor(studio_cursor("arrow"))
        window.setProperty(_WINDOW_CURSOR, True)
    elif ours:
        window.unsetCursor()
        window.setProperty(_WINDOW_CURSOR, False)


def refresh_window_cursors(app: QApplication | None = None) -> None:
    """After a cursor theme change: every open window takes the new arrow."""

    app = app or QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        update_window_cursor(widget)
    for widget in app.allWidgets():
        if isinstance(widget, POINTER_TYPES) and widget.property(_OWN_CURSOR):
            widget.setCursor(hover_cursor(widget))


class PointerCursorFilter(QObject):
    """Gives each new or re-enabled control its cursor, and each new window the theme arrow."""

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow() \
                and watched.windowType() != Qt.WindowType.ToolTip:
            update_window_cursor(watched)
        if not isinstance(watched, POINTER_TYPES):
            return False
        kind = event.type()
        if kind in (QEvent.Type.Polish, QEvent.Type.EnabledChange):
            update_pointer_cursor(watched)
        elif isinstance(watched, QSlider) and watched.property(_OWN_CURSOR) and watched.isEnabled() \
                and getattr(event, "button", lambda: None)() == Qt.MouseButton.LeftButton:
            # The hand closes while a slider handle is held.
            if kind == QEvent.Type.MouseButtonPress:
                watched.setCursor(studio_cursor("closed"))
            elif kind == QEvent.Type.MouseButtonRelease:
                watched.setCursor(studio_cursor("open"))
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
    refresh_window_cursors(app)
