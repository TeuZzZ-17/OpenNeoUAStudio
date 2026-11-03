"""Sector and model browsers: zoomable tile grids, model drag and sector copy."""

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QFontMetrics, QImage, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QListView, QPushButton, QStyle,
    QStyledItemDelegate, QWidget,
)
from map_editor.ui.preview_cards import COLOR_ROLE, DETAIL_ROLE, PreviewList, paint_card_background
from studio.ui.cursor_themes import studio_cursor

# Smallest tile width of each zoom level. A wider panel fits more tiles per row.
TILE_WIDTHS = (110, 150, 205, 280)
DEFAULT_TILE_LEVEL = 2


class TileDelegate(QStyledItemDelegate):
    """Preview on top, title and detail below, in the shared card colors."""

    def sizeHint(self, option, index):
        # Every tile fills one grid cell, so the card uses the whole cell.
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        view = self.parent()
        card = option.rect.adjusted(3, 3, -3, -3)
        painter.save()
        color = index.data(COLOR_ROLE) or QColor(175, 175, 175)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        paint_card_background(painter, card, color, selected,
                              bool(option.state & QStyle.StateFlag.State_MouseOver))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRect(card.left() + 7, card.top(), card.width() - 14, 3), 1, 1)
        side = view.image_side
        image = QRect(card.center().x() - side // 2, card.top() + 7, side, side)
        painter.fillRect(image, QColor(23, 23, 23))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            ratio = view.devicePixelRatioF()
            pixmap = icon.pixmap(QSize(round((side - 8) * ratio), round((side - 8) * ratio)))
            fitted = pixmap.size().scaled(image.adjusted(4, 4, -4, -4).size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(image.center().x() - fitted.width() // 2, image.center().y() - fitted.height() // 2,
                           fitted.width(), fitted.height())
            painter.drawPixmap(target, pixmap, pixmap.rect())
        else:
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(image, Qt.AlignmentFlag.AlignCenter, "—")
        text = QRect(card.left() + 6, image.bottom() + 5, card.width() - 12, card.bottom() - image.bottom() - 6)
        title = QFont(view.font())
        title.setBold(True)
        painter.setFont(title)
        painter.setPen(option.palette.color(option.palette.ColorRole.HighlightedText) if selected else color)
        line = QFontMetrics(title).height()
        painter.drawText(QRect(text.left(), text.top(), text.width(), line), Qt.AlignmentFlag.AlignHCenter,
                         QFontMetrics(title).elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, text.width()))
        painter.setFont(view.font())
        painter.setPen(option.palette.color(option.palette.ColorRole.HighlightedText if selected
                                            else option.palette.ColorRole.Text))
        metrics = QFontMetrics(view.font())
        for row, detail in enumerate(str(index.data(DETAIL_ROLE) or "").splitlines()[:2]):
            painter.drawText(QRect(text.left(), text.top() + line + 2 + row * metrics.height(), text.width(), metrics.height()),
                             Qt.AlignmentFlag.AlignHCenter,
                             metrics.elidedText(detail, Qt.TextElideMode.ElideRight, text.width()))
        painter.restore()


class KeyboardList(PreviewList):
    """Tile grid: widening the panel puts more tiles on each row."""
    applyRequested = Signal()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tile_level = DEFAULT_TILE_LEVEL
        self.image_side = 96
        self.setItemDelegate(TileDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(0)

    @property
    def columns(self):
        return max(1, (self.viewport().width() - 2) // TILE_WIDTHS[self.tile_level])

    def _layout_tiles(self):
        width = max(60, self.viewport().width() - 2)
        cell = width // self.columns
        self.image_side = max(40, cell - 20)
        lines = QFontMetrics(self.font()).height() * 3 + 16
        self.setGridSize(QSize(cell, self.image_side + lines))

    def resizeEvent(self, event):
        self._layout_tiles()
        super().resizeEvent(event)

    def set_tile_level(self, level):
        self.tile_level = max(0, min(len(TILE_WIDTHS) - 1, level))
        self._layout_tiles()
        self.doItemsLayout()
        self.previewsRequested.emit()

    def zoom_controls(self):
        """Compact − / + row; the same zoom steps as the Map Editor palettes."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Zoom"))
        layout.addStretch()
        minus, plus = QPushButton("−"), QPushButton("+")
        label = QLabel()
        for control in (minus, plus):
            control.setFixedWidth(32)

        def step(delta):
            self.set_tile_level(self.tile_level + delta)
            label.setText(f"{self.tile_level + 1}/{len(TILE_WIDTHS)}")
            minus.setEnabled(self.tile_level > 0)
            plus.setEnabled(self.tile_level < len(TILE_WIDTHS) - 1)
        minus.clicked.connect(lambda: step(-1))
        plus.clicked.connect(lambda: step(1))
        for widget in (minus, label, plus):
            layout.addWidget(widget)
        step(0)
        return row

    def keyPressEvent(self, event):
        sideways = self.columns > 1
        keys = {Qt.Key.Key_W: Qt.Key.Key_Up, Qt.Key.Key_S: Qt.Key.Key_Down,
                Qt.Key.Key_A: Qt.Key.Key_Left if sideways else Qt.Key.Key_Up,
                Qt.Key.Key_D: Qt.Key.Key_Right if sideways else Qt.Key.Key_Down}
        if event.key() in keys:
            event = QKeyEvent(event.type(), keys[event.key()], event.modifiers(),
                              "", event.isAutoRepeat(), event.count())
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.applyRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SectorList(KeyboardList):
    """Sector slots; a copied sector follows the mouse until it is released on a slot."""
    carryDropped = Signal(int)
    carryCancelled = Signal()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._carry = None

    @property
    def carrying(self):
        return self._carry is not None

    def start_carry(self, icon, count):
        self.stop_carry()
        pixmap = QPixmap(72, 72)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setOpacity(.75)
        painter.fillRect(pixmap.rect(), QColor(40, 42, 46))
        if icon is not None and not icon.isNull():
            image = icon.pixmap(64, 64).toImage().convertToFormat(QImage.Format.Format_Grayscale8)
            painter.drawImage(QRect(4, 4, 64, 64), image)
        painter.setOpacity(1)
        if count > 1:
            painter.setPen(QColor(235, 235, 235))
            painter.drawText(pixmap.rect().adjusted(0, 0, -4, -2),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, f"×{count}")
        painter.end()
        self._carry = QLabel(self.viewport())
        self._carry.setPixmap(pixmap)
        self._carry.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._carry.resize(pixmap.size())
        self._move_carry(self.viewport().mapFromGlobal(QCursor.pos()))
        self._carry.show()
        self.viewport().setCursor(studio_cursor("copy"))
        self.setFocus()

    def stop_carry(self):
        if self._carry is not None:
            self._carry.deleteLater()
            self._carry = None
            self.viewport().setCursor(studio_cursor("point"))

    def _move_carry(self, point):
        self._carry.move(point + QPoint(14, 14))

    def mousePressEvent(self, event):
        if self.carrying:
            if event.button() != Qt.MouseButton.LeftButton:
                self.carryCancelled.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.carrying:
            self._move_carry(event.position().toPoint())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.carrying and event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is None:
                self.carryCancelled.emit()
            else:
                self.carryDropped.emit(self.row(item))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if self.carrying and event.key() == Qt.Key.Key_Escape:
            self.carryCancelled.emit()
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
        self.setCursor(studio_cursor("open"))

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
