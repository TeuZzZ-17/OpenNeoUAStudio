"""Fitted thumbnails and selection cards shared by map resource palettes."""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate, QStyle
from .preview_cards import COLOR_ROLE, paint_card_background


class ThumbnailDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        view = self.parent()
        rect = option.rect
        if view.gridSize().width() * 2 > view.viewport().width():
            rect = QRect(0, rect.top(), view.viewport().width(), rect.height())
        card = rect.adjusted(3, 3, -3, -3)
        painter.save()
        color = index.data(COLOR_ROLE) or QColor(75, 195, 220)
        paint_card_background(painter, card, color,
                              bool(option.state & QStyle.StateFlag.State_Selected))
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


class PaletteCardDelegate(QStyledItemDelegate):
    """Keep existing row contents inside the shared selection card."""
    def paint(self, painter, option, index):
        painter.save()
        paint_card_background(painter, option.rect.adjusted(3, 3, -3, -3),
                              index.data(COLOR_ROLE) or QColor(240, 153, 75),
                              bool(option.state & QStyle.StateFlag.State_Selected))
        if self.parent().itemWidget(self.parent().item(index.row())) is None:
            content = type(option)(option)
            content.rect = option.rect.adjusted(10, 5, -10, -5)
            content.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
            super().paint(painter, content, index)
        painter.restore()
