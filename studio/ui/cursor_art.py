"""Painted cursor shapes for the extra Studio cursor themes (drawn with vectors, no image files).

Every shape draws four looks on a 40 x 40 canvas: ``arrow`` (idle over panels), ``point`` (over
something clickable), ``open`` (can be grabbed) and ``closed`` (grabbing). The action roles
(place, move, copy, forbidden, busy) are the ``point`` look plus the small badge shared with the
glove themes. ``cursor_themes`` registers the shapes as themes; ``make_cursor`` builds a QCursor.

Shapes: ``steel`` (a clean dark steel pointer arrow: the Deetex cursor), ``eye`` (cosmic eye) and ``comet`` (neon comet). The pointer shapes leave the grab
hands to the system (``make_cursor`` returns None for them).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QPainter, QPainterPath, QPen, QPixmap

SIZE = 38
OUTLINE = QColor(14, 14, 22)


def _c(value: str) -> QColor:
    return QColor(value)


def _pen(color: QColor, width: float) -> QPen:
    return QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _outlined(p: QPainter, path: QPainterPath, fill: QColor, line: float = 2.2) -> None:
    """A filled shape with the dark outline every Studio cursor has (readable on any theme)."""
    p.setPen(_pen(OUTLINE, line + 1.6))
    p.setBrush(QBrush(fill))
    p.drawPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(path)


def _stroke(p: QPainter, a: QPointF, b: QPointF, color: QColor, width: float) -> None:
    p.setPen(_pen(OUTLINE, width + 2.4))
    p.drawLine(a, b)
    p.setPen(_pen(color, width))
    p.drawLine(a, b)


def _pt(x: float, y: float) -> QPointF:
    return QPointF(x, y)


# -- pointer: the classic arrow in dark steel (the Deetex cursor) ---------------------------------

_ARROW = ((0.0, 0.0), (0.0, 19.0), (4.6, 14.8), (7.8, 22.0), (11.0, 20.6), (7.8, 13.6), (13.6, 13.6))


def _pointer(fill: str, rim: str, accent: str):
    def draw(p: QPainter, look: str) -> tuple[int, int]:
        p.save()
        p.translate(7, 7)
        path = QPainterPath(_pt(*_ARROW[0]))
        for x, y in _ARROW[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
        p.setPen(_pen(OUTLINE, 3.6))
        p.setBrush(QBrush(_c(fill)))
        p.drawPath(path)
        p.setPen(_pen(_c(rim), 1.5))   # a light rim inside the outline: the "polished" look
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if look == "point":           # something here can be clicked: two small ripples at the tip
            p.setPen(_pen(_c(accent), 1.7))
            p.drawArc(-5, -5, 10, 10, 0, 16 * 90)
            p.drawArc(-8, -8, 16, 16, 0, 16 * 90)
        p.restore()
        return 7, 7
    return draw


# -- eye: a cosmic eye; the pointer is the pupil ------------------------------------------------

def _eye(p: QPainter, look: str) -> tuple[int, int]:
    iris = _c("#7df9ff")
    cx, cy = 20, 16
    lid = QPainterPath()
    if look == "closed":
        lid.moveTo(4, cy)
        lid.quadTo(_pt(cx, cy + 9), _pt(36, cy))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(OUTLINE, 5.0))
        p.drawPath(lid)
        p.setPen(_pen(_c("#f2f0ff"), 2.4))
        p.drawPath(lid)
        for x in (10, 16, 22, 28):
            _stroke(p, _pt(x, cy + 4), _pt(x - 1.5, cy + 9), _c("#f2f0ff"), 1.4)
        return cx, cy
    rise = {"arrow": 6, "point": 8, "open": 11}[look]
    lid.moveTo(3, cy)
    lid.quadTo(_pt(cx, cy - rise * 2), _pt(37, cy))
    lid.quadTo(_pt(cx, cy + rise * 2), _pt(3, cy))
    _outlined(p, lid, _c("#f6f3ff"))
    radius = {"arrow": 4.6, "point": 6.4, "open": 7.4}[look]
    p.setPen(_pen(OUTLINE, 1.6))
    p.setBrush(QBrush(iris))
    p.drawEllipse(_pt(cx, cy), radius, radius)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(OUTLINE))
    p.drawEllipse(_pt(cx, cy), radius * 0.45, radius * 0.45)
    p.setBrush(QBrush(QColor(255, 255, 255)))
    p.drawEllipse(_pt(cx - radius * 0.35, cy - radius * 0.35), 1.3, 1.3)
    if look == "open":
        for angle in (-60, -30, 0, 30, 60):
            rad = math.radians(angle - 90)
            _stroke(p, _pt(cx + 17 * math.cos(rad), cy - 3 + 17 * math.sin(rad) * 0.5),
                    _pt(cx + 21 * math.cos(rad), cy - 3 + 21 * math.sin(rad) * 0.5),
                    _c("#f6f3ff"), 1.4)
    return cx, cy


# -- comet: a neon comet with a trailing tail ---------------------------------------------------

def _comet(p: QPainter, look: str) -> tuple[int, int]:
    glow, tail = _c("#fff36b"), _c("#ff4fd8")
    length = {"arrow": 22, "point": 26, "open": 30, "closed": 14}[look]
    for width, color in ((7.0, tail.darker(160)), (4.4, tail), (2.0, _c("#ffd9f7"))):
        p.setPen(_pen(color, width))
        p.drawLine(_pt(9, 9), _pt(9 + length * 0.78, 9 + length * 0.78))
    for off in (-1, 1):
        p.setPen(_pen(tail, 1.4))
        p.drawLine(_pt(9, 9), _pt(9 + length * 0.78 + off * 6, 9 + length * 0.78 - off * 6))
    p.setPen(_pen(OUTLINE, 1.6))
    p.setBrush(QBrush(glow))
    p.drawEllipse(_pt(7, 7), 5.6, 5.6)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(255, 255, 255)))
    p.drawEllipse(_pt(5.6, 5.6), 2.0, 2.0)
    if look in ("point", "open"):
        p.setPen(_pen(glow, 1.4))
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            p.drawLine(_pt(7 + 8 * math.cos(rad), 7 + 8 * math.sin(rad)),
                       _pt(7 + 11 * math.cos(rad), 7 + 11 * math.sin(rad)))
    return 6, 6


SHAPES = {
    "steel": _pointer("#3a4654", "#aebdcb", "#d7263d"),
    "eye": _eye, "comet": _comet,
}
# Pointer shapes: the grab roles are the system hands, which every user already knows.
SYSTEM_GRAB = {"steel"}
_LOOK = {"arrow": "arrow", "point": "point", "open": "open", "closed": "closed"}


def make_cursor(shape: str, role: str, draw_badge) -> QCursor | None:
    """The cursor of ``role`` for a painted shape (None: use the system one); ``draw_badge(painter, role, origin)`` adds the action sign."""
    if shape in SYSTEM_GRAB and role in ("open", "closed"):
        return None
    pixmap = QPixmap(SIZE, SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    hot = SHAPES[shape](painter, _LOOK.get(role, "arrow"))
    if role not in _LOOK:
        painter.save()
        draw_badge(painter, role, (SIZE - 18, SIZE - 18))
        painter.restore()
    painter.end()
    return QCursor(pixmap, int(hot[0]), int(hot[1]))
