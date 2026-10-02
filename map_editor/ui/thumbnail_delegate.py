"""Fitted thumbnails and selection cards shared by map resource palettes."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate, QStyle


class ThumbnailDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        view = self.parent()
        card = option.rect.adjusted(3, 3, -3, -3)
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(65, 65, 65))
            painter.drawRoundedRect(card, 5, 5)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        image_rect = card.adjusted(6, 6, -6, -30)
        if icon is not None and not icon.isNull():
            # Fit the image to the same rectangle as its selection card.
            pix = icon.pixmap(view.iconSize())
            pix.setDevicePixelRatio(1.0)
            pix = pix.scaled(image_rect.size(), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(image_rect.center().x() - pix.width() // 2,
                               image_rect.center().y() - pix.height() // 2, pix)
        text_rect = QRect(card.left() + 6, card.bottom() - 26, card.width() - 12, 24)
        painter.setPen(option.palette.text().color())
        text = option.fontMetrics.elidedText(str(index.data() or ''),
                                             Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
