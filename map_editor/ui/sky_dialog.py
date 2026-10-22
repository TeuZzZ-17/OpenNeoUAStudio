"""Pick a custom sky inside the game's Data folder, check it, preview it and apply it."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QLabel,
                               QPushButton, QVBoxLayout)

from ..core.custom_sky import CustomSky, inspect_custom_sky, install_custom_sky
from ..core.resource_catalog import SkyResource, preview_image


class CustomSkyDialog(QDialog):
    """Choose a ``.bas`` archive or ``.base`` file; Apply installs the chosen sky."""

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom sky")
        self.catalog = catalog
        self.data_dir = catalog.installation.data
        self.objects_dir = catalog.installation.folder("skies")
        self.sky = CustomSky()
        self.stem = ""
        self.value = ''
        layout = QVBoxLayout(self)
        self.file_button = QPushButton("Choose sky file")
        self.file_button.clicked.connect(self._choose_file)
        layout.addWidget(self.file_button)
        self.path_label = QLabel("Nothing chosen")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        self.preview = QLabel("No preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedSize(256, 256)
        self.preview.setStyleSheet("background:#171717; border:1px solid #454545")
        layout.addWidget(self.preview, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(320)
        layout.addWidget(self.status)
        self.box = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.box.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setEnabled(False)
        self.box.clicked.connect(self._clicked)
        layout.addWidget(self.box)

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a sky file", str(self.objects_dir),
                                              "Sky files (*.bas *.base *.BAS *.BASE)",
                                              options=QFileDialog.Option.DontUseNativeDialog)
        if path:
            self.inspect(path)

    def inspect(self, path):
        """Validate ``path`` and show the result; Apply is enabled only for a valid sky."""
        self.sky = inspect_custom_sky(path, self.data_dir, self.objects_dir)
        self.path_label.setText(str(path))
        lines = [f"<span style='color:#ff8787'>{text}</span>" for text in self.sky.problems]
        lines += [text for text in self.sky.notes]
        self.status.setText("<br>".join(lines))
        self.apply_button.setEnabled(self.sky.valid)
        self.preview.setPixmap(QPixmap())
        self.preview.setText("No preview")
        if self.sky.archive is not None:
            self._show_preview()

    def _show_preview(self):
        try:
            image = preview_image(SkyResource(self.sky.stem, self.sky.archive), self.catalog.palette)
        except Exception:
            return
        if not image.isNull():
            self.preview.setText("")
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _clicked(self, button):
        role = self.box.buttonRole(button)
        if role == QDialogButtonBox.ButtonRole.ApplyRole:
            self.apply()
        elif role == QDialogButtonBox.ButtonRole.RejectRole:
            self.reject()

    def apply(self):
        if not self.sky.valid:
            return
        try:
            install_custom_sky(self.sky, self.objects_dir)
        except OSError as exc:
            self.status.setText(f"<span style='color:#ff8787'>{exc}</span>")
            return
        self.stem = self.sky.stem
        self.value = self.sky.level_value
        self.accept()
