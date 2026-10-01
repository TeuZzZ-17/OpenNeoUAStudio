"""Looping preview of the selected local ambience track."""

from __future__ import annotations

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..core.game_installation import GameInstallation
from ..core.resource_catalog import music_path


class MusicPreview(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.35)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.enabled = True
        self._path = None

    def set_track(self, installation: GameInstallation, value: str):
        path = music_path(installation, value)
        if path != self._path:
            self._path = path
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
        if self.enabled and path:
            self.player.play()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if enabled and self._path:
            self.player.play()
        else:
            self.player.stop()

    def stop(self):
        self.player.stop()
