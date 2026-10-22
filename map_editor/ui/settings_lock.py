"""A shared, reversible lock for editor fields and placement palettes."""
from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


class SettingsLockButton(QToolButton):
    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(28,28)
        self.setIconSize(QSize(20,20))
        self._palettes = set()
        self._icons = {}
        for closed in (False,True):
            pixmap=QPixmap(48,48);pixmap.fill(Qt.GlobalColor.transparent)
            painter=QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(220,225,235),3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(11,23,26,20),3,3)
            painter.drawArc(QRectF(15 if closed else 23,5,18,34),0,180*16)
            painter.drawLine(24,31,24,36)
            painter.end()
            self._icons[closed]=QIcon(pixmap)
        self.toggled.connect(self._show_state)
        self._show_state(False)

    def _show_state(self, closed):
        self.setIcon(self._icons[closed])
        self.setToolTip('Unlock settings' if closed else 'Lock settings against accidental edits')
        self.setAccessibleName('Settings locked' if closed else 'Settings unlocked')

    def wrap(self, widget):
        # A disabled parent stays disabled even when a panel refresh enables individual fields.
        area=QWidget()
        layout=QVBoxLayout(area);layout.setContentsMargins(0,0,0,0)
        layout.addWidget(widget)
        self.toggled.connect(area.setDisabled)
        area.setDisabled(self.isChecked())
        return area

    def bind_palette(self, view):
        self._palettes.update((view,view.viewport()))
        view.installEventFilter(self);view.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if self.isChecked() and obj in self._palettes:
            kind=event.type()
            if kind in (QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick,QEvent.Type.KeyPress):
                return True
            if kind==QEvent.Type.MouseMove and event.buttons()!=Qt.MouseButton.NoButton:
                return True
        return super().eventFilter(obj,event)
