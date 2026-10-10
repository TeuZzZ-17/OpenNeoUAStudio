"""Generate a preview first, then save a matching MB/DB pair."""
from __future__ import annotations

import copy
from PySide6.QtCore import QObject, QRunnable, Qt, Signal
from PySide6.QtGui import QImage, QOffscreenSurface, QPixmap
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout,
                              QLabel, QLineEdit, QMessageBox, QVBoxLayout)
from ..render.briefing_art import available_stem, render_briefing, write_briefing_pair


class _BriefingSignals(QObject):
    finished = Signal(QImage, str)


class _BriefingJob(QRunnable):
    def __init__(self, doc, lib, surface):
        super().__init__()
        self.signals = _BriefingSignals()
        self.doc, self.lib, self.surface = copy.deepcopy(doc), lib, surface

    def run(self):
        try:
            image, error = render_briefing(self.doc, self.lib, self.surface), ''
        except Exception as exc:
            image, error = QImage(), str(exc)
        self.signals.finished.emit(image, error)


class BriefingArtDialog(QDialog):
    def __init__(self, doc, lib, directory, preferred, pool, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Generate briefing / debriefing artwork')
        self.resize(430, 510)
        self.directory = directory
        self.paths = None
        self.image = QImage()
        self.finished_rendering = False
        self._cancelled = False
        layout = QVBoxLayout(self)
        self.preview = QLabel('Rendering the current map')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(302, 302)
        self.preview.setStyleSheet('background:#080808; border:1px solid #454545')
        layout.addWidget(self.preview)
        self.status = QLabel('MB and DB preserve the map colours; DB is darker. Existing artwork is preserved.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        form = QFormLayout()
        self.name = QLineEdit(available_stem(directory, preferred))
        form.addRow('Map image name', self.name)
        layout.addLayout(form)
        destination = QLabel(f'Destination: {directory}')
        destination.setWordWrap(True)
        layout.addWidget(destination)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText('Save MB / DB')
        self.save_button.setEnabled(False)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        # Offscreen surfaces must be created on the GUI thread; the job owns its context.
        self.surface = None
        if QApplication.platformName() not in ('offscreen', 'minimal'):
            from ..render.gpu_viewport import gl_format
            self.surface = QOffscreenSurface()
            self.surface.setFormat(gl_format())
            self.surface.create()
        self._job = _BriefingJob(doc, lib, self.surface)
        self._job.signals.finished.connect(self._rendered)
        pool.start(self._job)

    def _rendered(self, image, error):
        self.finished_rendering = True
        if self.surface is not None:
            self.surface.destroy()
            self.surface = None
        if self._cancelled:
            self.deleteLater()
            return
        if error or image.isNull():
            self.preview.setText('Unable to generate artwork')
            self.status.setText(error or 'No image was generated.')
            return
        self.image = image
        self.preview.setPixmap(QPixmap.fromImage(image))
        self.save_button.setEnabled(True)

    def _save(self):
        try:
            self.paths = write_briefing_pair(self.image, self.directory, self.name.text().strip())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Briefing artwork', str(exc))
            return
        self.accept()

    def reject(self):
        self._cancelled = True
        super().reject()
