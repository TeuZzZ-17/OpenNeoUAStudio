"""Fitted thumbnails and selection cards shared by map resource palettes."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QStyledItemDelegate, QStyle
from .preview_cards import COLOR_ROLE, paint_card_background

# Text shown in place of a missing picture, e.g. "No sky".
PLACEHOLDER_ROLE = int(Qt.ItemDataRole.UserRole) + 11


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
            image_rect = image_rect.adjusted(6, 6, -6, -6)
            ratio = view.devicePixelRatioF()
            size = QSize(max(4, round(image_rect.width() * ratio)), max(4, round(image_rect.height() * ratio)))
            pix = icon.pixmap(size)
            pix.setDevicePixelRatio(ratio)
            logical = pix.deviceIndependentSize()
            painter.drawPixmap(round(image_rect.center().x() - logical.width() / 2),
                               round(image_rect.center().y() - logical.height() / 2), pix)
        elif index.data(PLACEHOLDER_ROLE):
            font = painter.font()
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() + 2)
            painter.setFont(font)
            painter.setPen(QColor(150, 160, 170))
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, str(index.data(PLACEHOLDER_ROLE)))
        text_rect = QRect(card.left() + 6, card.bottom() - 26, card.width() - 12, 24)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(option.palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text))
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
                              bool(option.state & QStyle.StateFlag.State_Selected),
                              bool(option.state & QStyle.StateFlag.State_MouseOver))
        if self.parent().itemWidget(self.parent().item(index.row())) is None:
            content = type(option)(option)
            content.rect = option.rect.adjusted(10, 5, -10, -5)
            if option.state & QStyle.StateFlag.State_Selected:
                content.palette.setColor(QPalette.ColorRole.Text, option.palette.highlightedText().color())
            # The card already shows hover and selection; letting the style repaint them
            # turned a hovered selected card grey.
            content.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus
                               | QStyle.StateFlag.State_MouseOver)
            super().paint(painter, content, index)
        painter.restore()
