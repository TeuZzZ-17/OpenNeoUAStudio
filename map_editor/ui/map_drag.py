"""Drag of a palette entry (sector, building or special object) onto the map."""

from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QObject, QEvent, Qt
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtWidgets import QApplication

MIME_TYPE = 'application/x-openneoua-map-item'
DRAG_ICON = 32


def encode_payload(payload: dict) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME_TYPE, json.dumps(payload).encode())
    return mime


def decode_payload(mime: QMimeData) -> dict | None:
    """The dict a drag source stored, or None for any other drag (files, text, ...)."""
    if mime is None or not mime.hasFormat(MIME_TYPE):
        return None
    try:
        payload = json.loads(bytes(mime.data(MIME_TYPE)).decode())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) and payload.get('kind') else None


class MapDragSource(QObject):
    """Starts a drag from a list item once the mouse moves far enough with the left button held.

    It only watches the list: the click still selects the item as before, and a short click
    never becomes a drag. ``payload_for`` returns the dict to drag, or None for items that
    cannot be placed on the map.
    """

    def __init__(self, list_widget, payload_for):
        super().__init__(list_widget)
        self.list = list_widget
        self.payload_for = payload_for
        self._item = None
        self._origin = None
        list_widget.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            point = event.position().toPoint()
            item = self.list.itemAt(point)
            self._item = item if item is not None and not item.isHidden() else None
            self._origin = point
        elif kind == QEvent.Type.MouseButtonRelease:
            self._item = None
        elif (kind == QEvent.Type.MouseMove and self._item is not None
              and event.buttons() & Qt.MouseButton.LeftButton
              and (event.position().toPoint() - self._origin).manhattanLength() >= QApplication.startDragDistance()):
            item, self._item = self._item, None
            payload = self.payload_for(item)
            if payload is not None:
                self._start(item, payload)
        return False

    def _start(self, item, payload):
        drag = QDrag(self.list)
        drag.setMimeData(encode_payload(payload))
        pixmap = item.icon().pixmap(DRAG_ICON, DRAG_ICON)
        if not pixmap.isNull():
            # The map shows the grey preview, so this small icon stays off the cursor tip.
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().topLeft())
        drag.exec(Qt.DropAction.CopyAction)
