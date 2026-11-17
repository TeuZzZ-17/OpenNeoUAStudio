"""Shared transport appearance for every Studio editor."""
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton, QDoubleSpinBox

from studio.rendering.playback import DEFAULT_ANIMATION_SPEED


class PlaybackButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("Play", parent)
        self.setCheckable(True)
        self.setProperty("studioRole", "play")
        self.setFixedWidth(104)
        self.setMinimumHeight(32)
        self.setToolTip("Play / pause animations")
        self.toggled.connect(self._update_label)

    def _update_label(self, playing):
        self.setIcon(QIcon())
        self.setText("Pause" if playing else "Play")


class PlaybackStepButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(">>", parent)
        self.setFixedWidth(44)
        self.setMinimumHeight(32)
        self.setToolTip("Next animation frame")


class PlaybackSpeedSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0.05, 8.0)
        self.setSingleStep(0.25)
        self.setValue(DEFAULT_ANIMATION_SPEED)
        self.setFixedWidth(112)
        self.setMinimumHeight(32)
