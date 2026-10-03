"""Resource cards with a fitted model, faction accent and readable details."""
from PySide6.QtCore import QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextLayout, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QListWidget, QLayout, QScrollArea,
                              QPushButton, QStyledItemDelegate, QStyle, QStyleOptionButton)

DETAIL_ROLE = int(Qt.ItemDataRole.UserRole) + 1
COLOR_ROLE = DETAIL_ROLE + 1
RESOURCE_ROLE = DETAIL_ROLE + 2
PREVIEW_PIXELS = (80, 112, 160)


def actor_scroll_area(panel):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    # Expanded forms must grow inside the scroll area instead of squeezing fields.
    panel.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(panel)
    return scroll


class ResourceCardDelegate(QStyledItemDelegate):
    @staticmethod
    def wrapped_text(text, font, width):
        layout = QTextLayout(text, font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        height = 0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, height))
            height += line.height()
        layout.endLayout()
        return layout, height

    def text_layout(self, index, width):
        view = self.parent()
        size = min(view.preview_size, max(48, (width - 6) // 3))
        text_width = max(20, width - size - 36 -
                         (30 if index.data(Qt.ItemDataRole.CheckStateRole) is not None else 0))
        font = QFont(view.font())
        title = QFont(font)
        title.setBold(True)
        title_layout, title_height = self.wrapped_text(str(index.data() or ''), title, text_width)
        detail_layout, detail_height = self.wrapped_text(str(index.data(DETAIL_ROLE) or '').replace('\n', '\u2028'), font, text_width)
        return size, text_width, title_layout, detail_layout, title_height, detail_height

    def sizeHint(self, option, index):
        size, _, _, _, title, detail = self.text_layout(index, self.parent().viewport().width())
        return QSize(0, round(max(size + 24, title + detail + 32)))

    def paint(self, painter, option, index):
        view = self.parent()
        card = option.rect.adjusted(3, 3, -3, -3)
        painter.save()
        color = index.data(COLOR_ROLE) or QColor(175, 175, 175)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(62, 62, 62) if option.state & QStyle.StateFlag.State_Selected
                         else QColor(43, 43, 43))
        painter.drawRoundedRect(card, 5, 5)
        painter.setBrush(color)
        painter.drawRoundedRect(QRect(card.left(), card.top() + 7, 3, card.height() - 14), 1, 1)
        size, width, title_layout, detail_layout, title_height, detail_height = self.text_layout(index, option.rect.width())
        image_rect = QRect(card.left() + 10, card.center().y() - size // 2, size, size)
        painter.fillRect(image_rect, QColor(23, 23, 23))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            ratio = view.devicePixelRatioF()
            pix = icon.pixmap(QSize(round((size - 4) * ratio), round((size - 4) * ratio)))
            fitted = pix.size().scaled(image_rect.adjusted(2, 2, -2, -2).size(),
                                       Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(image_rect.center().x() - fitted.width() // 2,
                           image_rect.center().y() - fitted.height() // 2,
                           fitted.width(), fitted.height())
            painter.drawPixmap(target, pix, pix.rect())
        else:
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, '—')
        left = image_rect.right() + 12
        right = card.right() - 8
        checked = index.data(Qt.ItemDataRole.CheckStateRole)
        if checked is not None:
            box = QStyleOptionButton()
            box.rect = QRect(right - 20, card.center().y() - 10, 20, 20)
            box.state = QStyle.StateFlag.State_Enabled | (QStyle.StateFlag.State_On
                         if checked == Qt.CheckState.Checked.value else QStyle.StateFlag.State_Off)
            view.style().drawControl(QStyle.ControlElement.CE_CheckBox, box, painter, view)
            right -= 30
        painter.setPen(color)
        top = card.center().y() - (title_height + detail_height + 8) // 2
        title_layout.draw(painter, QPointF(left, top))
        painter.setPen(QColor(195, 195, 195))
        detail_layout.draw(painter, QPointF(left, top + title_height + 8))
        painter.restore()


class PreviewList(QListWidget):
    previewsRequested = Signal()

    def __init__(self, large_text=True):
        super().__init__()
        self.preview_level = 0
        if large_text:
            font = self.font()
            font.setPointSizeF(max(11, font.pointSizeF() + 2))
            self.setFont(font)
        self.setItemDelegate(ResourceCardDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(3)
        self.verticalScrollBar().valueChanged.connect(lambda _value: self.previewsRequested.emit())
        self.setMinimumHeight(170)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.doItemsLayout()
        self.previewsRequested.emit()

    @property
    def preview_size(self):
        return PREVIEW_PIXELS[self.preview_level]

    def keyPressEvent(self, event):
        item = self.currentItem()
        if (event.key() == Qt.Key.Key_Space and item is not None
                and item.data(Qt.ItemDataRole.CheckStateRole) is not None):
            self.itemClicked.emit(item)
            event.accept()
            return
        super().keyPressEvent(event)

    def preview_controls(self):
        row = QHBoxLayout()
        row.addWidget(QLabel('Preview size'))
        row.addStretch()
        minus, plus, label = QPushButton('−'), QPushButton('+'), QLabel(f'{self.preview_level + 1}/3')
        minus.setEnabled(self.preview_level > 0)
        for button in (minus, plus):
            button.setFixedWidth(32)
        def step(delta):
            self.preview_level = max(0, min(2, self.preview_level + delta))
            label.setText(f'{self.preview_level + 1}/3')
            minus.setEnabled(self.preview_level > 0)
            plus.setEnabled(self.preview_level < 2)
            self.doItemsLayout()
            self.previewsRequested.emit()
        minus.clicked.connect(lambda: step(-1))
        plus.clicked.connect(lambda: step(1))
        for widget in (minus, label, plus):
            row.addWidget(widget)
        return row


def set_card(item, title, detail, color, resource):
    item.setText(title)
    item.setData(DETAIL_ROLE, detail)
    item.setData(COLOR_ROLE, QColor(*color))
    item.setData(RESOURCE_ROLE, resource)
    item.setToolTip(f'{title}\n{detail}')


def faction_style(combo, colors):
    color = colors.get(combo.currentData(), (180, 180, 180))
    combo.setStyleSheet(f'QComboBox {{ color: rgb{color}; }}')


def form_vehicle(combo):
    index = combo.currentIndex()
    value = (combo.itemData(index) if index >= 0 and combo.currentText() == combo.itemText(index)
             else int(combo.currentText().strip()))
    if value is None or value <= 0:
        raise ValueError('Choose a model or enter a positive vehicle ID.')
    return value
