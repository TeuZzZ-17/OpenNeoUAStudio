"""Compact tab accents; labels keep their native Qt sizing and scrolling."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QRegion
from PySide6.QtWidgets import QTabBar, QStyleOptionTab, QToolButton


TAB_COLORS = ((75, 195, 220), (235, 155, 70), (90, 200, 125), (180, 135, 240),
              (225, 120, 180), (235, 210, 80), (120, 155, 245))


class ColoredTabBar(QTabBar):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        visible = QRegion(self.rect())
        for button in self.findChildren(QToolButton):
            if button.isVisible():
                visible = visible.subtracted(QRegion(button.geometry().adjusted(-2, 0, 2, 0)))
        painter.setClipRegion(visible)
        for i in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, i)
            rect = option.rect.adjusted(1, 2, -1, -1)
            color = QColor(*TAB_COLORS[i % len(TAB_COLORS)])
            selected = i == self.currentIndex()
            background = QColor(color)
            background.setAlpha(85 if selected else 25)
            painter.setPen(QPen(color if selected else QColor(65, 65, 65), 1))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(color.lighter(125) if selected else color)
            painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignCenter, self.tabText(i))
            painter.setPen(QPen(color, 3 if selected else 1))
            painter.drawLine(rect.bottomLeft(), rect.bottomRight())
