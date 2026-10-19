"""Direct model placement using the existing Map Editor preview list."""

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QKeyEvent
from PySide6.QtWidgets import QAbstractItemView
from map_editor.ui.preview_cards import PreviewList


class KeyboardList(PreviewList):
    applyRequested = Signal()

    def keyPressEvent(self, event):
        keys = {Qt.Key.Key_W: Qt.Key.Key_Up, Qt.Key.Key_A: Qt.Key.Key_Up,
                Qt.Key.Key_S: Qt.Key.Key_Down, Qt.Key.Key_D: Qt.Key.Key_Down}
        if event.key() in keys:
            event = QKeyEvent(event.type(), keys[event.key()], event.modifiers(),
                              "", event.isAutoRepeat(), event.count())
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.applyRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ModelList(KeyboardList):
    dragFinished = Signal()

    def __init__(self, parent=None, **kwargs):
        super().__init__(**kwargs)
        if parent is not None:
            self.setParent(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData("application/x-openneoua-sector-model",
                     str(item.data(Qt.ItemDataRole.UserRole)).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if not item.icon().isNull():
            drag.setPixmap(item.icon().pixmap(80, 80))
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.dragFinished.emit()
            drag.deleteLater()
