"""Cursor themes: the mouse cursors every Studio editor and tab uses.

Studio never asks Qt for a cursor shape directly. It asks for a *role* (``studio_cursor("point")``)
and the active cursor theme decides what is shown, so one menu entry (Cursor Theme, next to Color
Theme) changes the cursor of every editor at once. Roles:

    point      idle over views and clickable controls
    open       something can be grabbed (also the slider handle)
    closed     grabbing, rotating or dragging
    place      placing or painting with a click
    move       moving the selection
    copy       copying or adding
    forbidden  an action that is not allowed
    busy       Studio is working

Themes (the first one is the default, and is the plain Windows cursor set):

    classic            the system cursors: the classic white pointing hand of Windows
    precision          system arrow for controls, system hands only for grabbing
    studio_hand        small white glove drawn by Studio, with a badge for the action
    studio_hand_large  the same glove one size up

``cursor_kind`` does the opposite: it names the role of a cursor, in any theme.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QAction, QColor, QCursor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

ROLES = ("point", "open", "closed", "place", "move", "copy", "forbidden", "busy")
DEFAULT_THEME = "classic"
_SETTING = "ui/cursor_theme"

# '#' outline, 'w' glove, 'c' cuff. One grid cell is drawn as _SCALE x _SCALE pixels.
_POINT = (
    ".....##.........",
    "....#ww#........",
    "....#ww#........",
    "....#ww#........",
    "....#ww###......",
    "....#ww#ww##....",
    "....#ww#ww#w##..",
    ".##.#ww#ww#w#w#.",
    "#ww##wwwwwwwwww#",
    "#www#wwwwwwwwww#",
    ".#wwwwwwwwwwwww#",
    "..#wwwwwwwwwwww#",
    "..#wwwwwwwwwww#.",
    "...#wwwwwwwwww#.",
    "....#cccccccc#..",
    "....#cccccccc#..",
    "....##########..",
)
_OPEN = (
    "........##........",
    ".....###ww###.....",
    "....#ww#ww#ww###..",
    "....#ww#ww#ww#ww#.",
    "....#ww#ww#ww#ww#.",
    "....#ww#ww#ww#ww#.",
    ".##.#ww#ww#ww#ww#.",
    "#ww##ww#ww#ww#ww#.",
    "#wwwwwwwwwwwwwww#.",
    ".#wwwwwwwwwwwwww#.",
    "..#wwwwwwwwwwww#..",
    "...#wwwwwwwwww#...",
    "....#cccccccc#....",
    "....#cccccccc#....",
    "....##########....",
)
_CLOSED = (
    "..................",
    "..................",
    "....##.##.##.##...",
    "...#ww#ww#ww#ww#..",
    "...#ww#ww#ww#ww#..",
    "...#wwwwwwwwwwww#.",
    "..#wwwwwwwwwwwww#.",
    "..#wwwwwwwwwwwww#.",
    "..#wwwwwwwwwwww#..",
    "...#wwwwwwwwww#...",
    "....#cccccccc#....",
    "....#cccccccc#....",
    "....##########....",
)
_SCALE = 2  # design size; the themes scale the finished pixmap down
_CANVAS = (22 * _SCALE, 20 * _SCALE)
_COLORS = {"#": QColor(16, 16, 20), "w": QColor(250, 250, 245), "c": QColor(92, 112, 150)}
_BADGE_ORIGIN = (28, 24)
_BADGE_SIZE = 16
# Fingertip of the pointing hand, palm centre of the open and closed hand.
_HOTSPOTS = {"point": (5 * _SCALE + 1, 1), "open": (9 * _SCALE, 8 * _SCALE),
             "closed": (9 * _SCALE, 7 * _SCALE)}
_OUTLINE = QColor(16, 16, 20)
_BADGE_INK = QColor(255, 255, 255)
_BADGE_RED = QColor(235, 70, 60)

def _draw_art(painter: QPainter, art) -> None:
    for y, row in enumerate(art):
        for x, mark in enumerate(row):
            color = _COLORS.get(mark)
            if color is not None:
                painter.fillRect(x * _SCALE, y * _SCALE, _SCALE, _SCALE, color)


def _stroke(painter: QPainter, color: QColor, draw) -> None:
    """Draw a badge shape twice: a dark outline first, then the coloured line on top."""

    for pen_color, width in ((_OUTLINE, 3.4), (color, 1.6)):
        painter.setPen(QPen(pen_color, width, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        draw()


def _draw_badge(painter: QPainter, name: str) -> None:
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.translate(*_BADGE_ORIGIN)
    half = _BADGE_SIZE / 2
    centre = QPointF(half, half)
    if name == "place":
        _stroke(painter, _BADGE_INK, lambda: (
            painter.drawEllipse(centre, 4.0, 4.0),
            painter.drawPoint(centre)))
    elif name == "move":
        def arrows():
            painter.drawLine(QPointF(2, half), QPointF(_BADGE_SIZE - 2, half))
            painter.drawLine(QPointF(half, 2), QPointF(half, _BADGE_SIZE - 2))
            # An arrow head at each end: (tip, direction pointing into the cross).
            for tip, (ux, uy) in (((2, half), (1, 0)), ((_BADGE_SIZE - 2, half), (-1, 0)),
                                  ((half, 2), (0, 1)), ((half, _BADGE_SIZE - 2), (0, -1))):
                for side in (-1, 1):
                    painter.drawLine(QPointF(*tip), QPointF(
                        tip[0] + 3 * ux - 3 * side * uy, tip[1] + 3 * uy + 3 * side * ux))
        _stroke(painter, _BADGE_INK, arrows)
    elif name == "copy":
        _stroke(painter, _BADGE_INK, lambda: (
            painter.drawLine(QPointF(half, 3), QPointF(half, _BADGE_SIZE - 3)),
            painter.drawLine(QPointF(3, half), QPointF(_BADGE_SIZE - 3, half))))
    elif name == "forbidden":
        _stroke(painter, _BADGE_RED, lambda: (
            painter.drawEllipse(centre, 5.5, 5.5),
            painter.drawLine(QPointF(4.5, 4.5), QPointF(_BADGE_SIZE - 4.5, _BADGE_SIZE - 4.5))))
    elif name == "busy":
        def glass():
            painter.drawLine(QPointF(4, 2), QPointF(_BADGE_SIZE - 4, 2))
            painter.drawLine(QPointF(4, _BADGE_SIZE - 2), QPointF(_BADGE_SIZE - 4, _BADGE_SIZE - 2))
            painter.drawLine(QPointF(4, 2), QPointF(_BADGE_SIZE - 4, _BADGE_SIZE - 2))
            painter.drawLine(QPointF(_BADGE_SIZE - 4, 2), QPointF(4, _BADGE_SIZE - 2))
        _stroke(painter, _BADGE_INK, glass)



_SYSTEM_SHAPES = {
    "point": Qt.CursorShape.PointingHandCursor, "open": Qt.CursorShape.OpenHandCursor,
    "closed": Qt.CursorShape.ClosedHandCursor, "place": Qt.CursorShape.CrossCursor,
    "move": Qt.CursorShape.SizeAllCursor, "copy": Qt.CursorShape.DragCopyCursor,
    "forbidden": Qt.CursorShape.ForbiddenCursor, "busy": Qt.CursorShape.WaitCursor,
}

# id -> (menu name, description, order, how a role becomes a cursor).
# ``("system", {role: shape})`` overrides some system shapes; ``("art", factor)`` draws the glove.
THEMES = {
    "classic": ("Classic Windows", "The standard Windows cursors: the classic white pointing hand.",
                0, ("system", {})),
    "precision": ("Arrow and Grab", "Normal arrow over controls; hands only to grab and drag.",
                  1, ("system", {"point": Qt.CursorShape.ArrowCursor})),
    "studio_hand": ("Studio Hand", "Small white glove with a badge for the action.",
                    2, ("art", 0.5)),
    "studio_hand_large": ("Studio Hand Large", "The Studio glove, one size up.",
                          3, ("art", 0.75)),
}

_active = DEFAULT_THEME
_cache: dict[tuple[str, str], QCursor] = {}


def _build_art(name: str, factor: float) -> QCursor:
    art = {"open": _OPEN, "closed": _CLOSED}.get(name, _POINT)
    pixmap = QPixmap(*_CANVAS)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    _draw_art(painter, art)
    if name not in ("point", "open", "closed"):
        _draw_badge(painter, name)
    painter.end()
    hot_x, hot_y = _HOTSPOTS.get(name, _HOTSPOTS["point"])
    size = pixmap.size() * factor
    # Nearest-neighbour keeps the pixel-art edges crisp.
    pixmap = pixmap.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    return QCursor(pixmap, round(hot_x * factor), round(hot_y * factor))


def _make(theme: str, role: str) -> QCursor:
    kind, detail = THEMES[theme][3]
    if kind == "art":
        return _build_art(role, detail)
    return QCursor(detail.get(role, _SYSTEM_SHAPES[role]))


def theme_ids() -> list[str]:
    return sorted(THEMES, key=lambda key: (THEMES[key][2], key))


def active_cursor_theme() -> str:
    return _active


def studio_cursor(role: str = "point") -> QCursor:
    """The cursor of ``role`` in the active theme. Built once, then shared."""

    if role not in ROLES:
        raise ValueError(f"unknown Studio cursor role: {role!r}")
    key = (_active, role)
    cursor = _cache.get(key)
    if cursor is None:
        cursor = _cache[key] = _make(_active, role)
    return cursor


def _themed(theme: str, role: str) -> QCursor:
    key = (theme, role)
    if key not in _cache:
        _cache[key] = _make(theme, role)
    return _cache[key]


def _same(a: QCursor, b: QCursor) -> bool:
    if a.shape() != b.shape():
        return False
    if a.shape() != Qt.CursorShape.BitmapCursor:
        return True
    return a.hotSpot() == b.hotSpot() and a.pixmap().toImage() == b.pixmap().toImage()


def cursor_kind(cursor: QCursor | None, theme: str | None = None) -> str | None:
    """Role of ``cursor`` (checked in ``theme`` first, then in every theme), or None."""

    if cursor is None:
        return None
    for candidate in dict.fromkeys([theme or _active, _active, *theme_ids()]):
        for role in ROLES:
            if _same(cursor, _themed(candidate, role)):
                return role
    return None


def set_cursor_theme(theme: str, *, persist: bool = False) -> str:
    """Switch the cursor theme and re-cursor every open widget. Unknown ids fall back to the default."""

    global _active
    if theme not in THEMES:
        theme = DEFAULT_THEME
    previous, _active = _active, theme
    app = QApplication.instance()
    if app is not None:
        for widget in app.allWidgets():
            if widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor):
                role = cursor_kind(widget.cursor(), previous)
                if role is not None:
                    widget.setCursor(studio_cursor(role))
        app.setProperty("studioCursorTheme", theme)
        for window in app.topLevelWidgets():
            for action in window.findChildren(QAction):
                choice = action.property("studioCursorThemeChoice")
                if choice:
                    action.setChecked(choice == theme)
    if persist:
        QSettings("OpenNeoUA", "Studio").setValue(_SETTING, theme)
    return theme


def restore_cursor_theme() -> str:
    """Apply the saved choice at start-up (the default when nothing was saved)."""

    return set_cursor_theme(str(QSettings("OpenNeoUA", "Studio").value(_SETTING, DEFAULT_THEME)))
