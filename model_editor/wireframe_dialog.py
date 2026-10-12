"""Preview and export a wireframe generated from the active viewport model."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from assembly_viewer import VIEW_PRESET_ANGLES


def _load_wireframe_generator():
    """Import the generator only when a preview is requested."""

    from .wireframe_generator import generate_wireframe

    return generate_wireframe


class _ImagePreview(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(360, 360)
        self.setMaximumSize(640, 640)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFrameShape(QFrame.Shape.StyledPanel)

    def set_image(self, image) -> None:
        self._image = QPixmap.fromImage(image)
        self._update_scaled_image()

    def clear_image(self) -> None:
        self._image = QPixmap()
        self.clear()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scaled_image()

    def _update_scaled_image(self) -> None:
        if self._image.isNull():
            return
        self.setPixmap(self._image.scaled(
            self.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class WireframeGeneratorDialog(QDialog):
    """Let the user inspect and save a generated SKLT wireframe."""

    PREVIEW_SIZE = QSize(640, 640)

    def __init__(self, viewport, parent=None) -> None:
        super().__init__(parent)
        self.viewport = viewport
        self.projection = None

        self.setWindowTitle("Generate Wireframe")
        self.setModal(True)
        self.resize(760, 760)

        layout = QVBoxLayout(self)
        settings = QFormLayout()

        self.view_preset_combo = QComboBox(self)
        self.view_preset_combo.addItem("Current View")
        self.view_preset_combo.setToolTip(
            "Use the visible model view and its current animation frame.")
        for preset in VIEW_PRESET_ANGLES:
            self.view_preset_combo.addItem(preset)
        settings.addRow("View:", self.view_preset_combo)

        layout.addLayout(settings)

        self.preview = _ImagePreview(self)
        self.preview.setText("Preparing preview...")
        layout.addWidget(self.preview, 1)

        self.stats_label = QLabel("", self)
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.stats_label)

        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff9b86;")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_button = QPushButton("Export", self)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_projection)
        buttons.addWidget(self.save_button)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.view_preset_combo.currentTextChanged.connect(
            self._refresh_preview)
        self._refresh_preview()

    def _refresh_preview(self, *_args) -> None:
        self.projection = None
        self.save_button.setEnabled(False)
        self.stats_label.clear()
        self.error_label.clear()
        self.preview.clear_image()
        self.preview.setText("Generating preview...")

        try:
            generate_wireframe = _load_wireframe_generator()
            projection = generate_wireframe(
                self.viewport,
                preset=self.view_preset_combo.currentText(),
                respect_transparency=(
                    bool(self.viewport.indexed_renderer_info().get("available"))),
                simplification=3.0,
            )
            image = projection.preview_image(size=self.PREVIEW_SIZE)
            if image is None or image.isNull():
                raise ValueError("The generated wireframe preview is empty.")
            stats_text = (
                f"{len(projection.points)} points Â· "
                f"{len(projection.segments)} segments")
        except Exception as exc:
            self.preview.setText("Preview unavailable")
            self.error_label.setText(str(exc))
            return

        self.projection = projection
        self.preview.set_image(image)
        self.stats_label.setText(stats_text)
        self.save_button.setEnabled(True)

    def _save_projection(self) -> None:
        if self.projection is None:
            return

        suggested = Path("Wireframe.SKL")
        parent = self.parent()
        last_directory = getattr(parent, "_last_directory", None)
        if last_directory:
            suggested = Path(last_directory) / suggested.name
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export Generated Wireframe", str(suggested),
            "Urban Assault skeleton (*.SKL *.skl);;All files (*)")
        if not filename:
            return

        target = Path(filename)
        if target.suffix.casefold() not in (".skl", ".sklt"):
            target = target.with_suffix(".SKL")
        try:
            self.projection.save(target)
        except Exception as exc:
            self.error_label.setText(f"Could not save the wireframe: {exc}")
            return

        if parent is not None and hasattr(parent, "_last_directory"):
            parent._last_directory = target.parent
        self.accept()
