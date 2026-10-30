"""Shared pieces of the asset workbench window: item roles, small widgets,
path helpers and the Add FX Element dialog."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QImage,
    QPixmap,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QVBoxLayout,
)

from studio.ui.widgets import (
    configure_stable_image_preview,
    crop_uv_region,
    qimage_from_ilbm as _qimage_from_ilbm,
)
from studio.rendering.geometry import (
    FxElement,
)


WINDOW_TITLE = "OpenNeoUA Studio"


_BAS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 1


_BAS_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class _TransformStepSpinBox(QDoubleSpinBox):
    """Keep full spin-box precision while presenting compact step text."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.display_mode = "move"

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt override
        if self.display_mode == "move":
            return f"{value:.2f}"
        return f"{value:g}"


class _BasResourceTree(QTreeWidget):
    """Keep right-click purely contextual for every BAS resource class."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _display_path(path: str | Path) -> Path:
    """Return a stable absolute path for window-title display."""

    return Path(path).expanduser().resolve(strict=False)


def _reveal_in_file_manager(path: str | Path) -> None:
    """Show a file in the system file manager: selected on Windows, its folder elsewhere."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


class AddFxElementDialog(QDialog):
    """Choose one compatible FX element to clone without reconstruction."""

    def __init__(self, templates: list[FxElement], parent=None,
                 preferred: FxElement | None = None,
                 preview_provider=None, source_loader=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add FX Element")
        self._preview_provider = preview_provider
        self._source_loader = source_loader
        self._sources = {}
        self._preview_frames: list[QImage] = []
        self._preview_frame = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self._advance_preview)
        self._current_templates = list(templates)
        self._preferred_identity = (
            preferred.identity if preferred is not None else None)
        layout = QVBoxLayout(self)

        # Keep the proven current-model initialization path used by the old
        # dialog.  Source switching only replaces the combo contents after the
        # dialog already exists, which avoids doing external/source plumbing
        # during construction.
        self.source_label = QLabel("Source: Current model")
        self.source_label.setWordWrap(True)
        self.source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(QLabel("FX Element List:"))
        self.material_combo = QComboBox()
        for element in self._current_templates:
            self.material_combo.addItem(
                self._source_item_text(None, element), element)
            if self._preferred_identity is not None \
                    and element.identity == self._preferred_identity:
                self.material_combo.setCurrentIndex(
                    self.material_combo.count() - 1)
        layout.addWidget(self.material_combo)
        layout.addWidget(self.source_label)

        source_row = QHBoxLayout()
        self.current_source_button = QPushButton("From Current Model")
        self.current_source_button.setEnabled(False)
        self.current_source_button.clicked.connect(
            lambda _checked=False: self._restore_current_source())
        source_row.addWidget(self.current_source_button)
        self.other_model_source_button = QPushButton("From Another Model")
        self.other_model_source_button.setEnabled(source_loader is not None)
        self.other_model_source_button.clicked.connect(
            lambda _checked=False: self._load_source("model"))
        source_row.addWidget(self.other_model_source_button)
        self.bas_source_button = QPushButton("From BAS Archive")
        self.bas_source_button.setEnabled(source_loader is not None)
        self.bas_source_button.clicked.connect(
            lambda _checked=False: self._load_source("bas"))
        source_row.addWidget(self.bas_source_button)
        layout.addLayout(source_row)
        self.preview_label = QLabel("Preview unavailable")
        configure_stable_image_preview(self.preview_label, 360)
        self.preview_label.setStyleSheet(
            "background: palette(base); border: 1px solid palette(mid);")
        layout.addWidget(self.preview_label)
        self.preview_info = QLabel()
        self.preview_info.setWordWrap(True)
        layout.addWidget(self.preview_info)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setEnabled(bool(templates))
        layout.addWidget(buttons)
        self.material_combo.currentIndexChanged.connect(
            self._refresh_preview)
        self._refresh_preview()

    @staticmethod
    def _source_item_text(obj, element: FxElement) -> str:
        kind = "VANM" if element.source_kind == "VANM" else "direct"
        material = ", ".join(element.material_names)
        if obj is None:
            return f"{element.fx_name} | {kind} | {material}"
        return f"{obj.display_name} | {element.fx_name} | {kind} | {material}"

    def _set_source_elements(
            self, sources, label: str, *, current_model: bool = False) -> None:
        self._preview_timer.stop()
        self._sources.clear()
        self.material_combo.blockSignals(True)
        self.material_combo.clear()
        preferred_index = -1
        for index, source in enumerate(sources):
            if len(source) == 3:
                family, obj, element = source
                self._sources[id(element)] = (family, obj)
            else:
                family = obj = None
                element = source[0]
            self.material_combo.addItem(
                self._source_item_text(obj, element), element)
            if self._preferred_identity is not None \
                    and element.identity == self._preferred_identity:
                preferred_index = index
        if self.material_combo.count():
            self.material_combo.setCurrentIndex(
                preferred_index if preferred_index >= 0 else 0)
        self.material_combo.blockSignals(False)
        self.source_label.setText(f"Source: {label}")
        self.current_source_button.setEnabled(not current_model)
        self._ok_button.setEnabled(self.material_combo.count() > 0)
        self._refresh_preview()

    def _restore_current_source(self) -> None:
        self._set_source_elements(
            [(element,) for element in self._current_templates],
            "Current model", current_model=True)

    def _load_source(self, kind):
        try:
            loaded = self._source_loader(kind)
            if not loaded:
                return
            if isinstance(loaded, tuple) and len(loaded) == 2 \
                    and isinstance(loaded[0], str):
                label, sources = loaded
            else:
                label, sources = (
                    "Another model" if kind == "model" else "BAS Archive",
                    loaded)
            if sources:
                self._set_source_elements(sources, label)
        except Exception as exc:
            QMessageBox.warning(self, "FX source unavailable", str(exc))

    def source_context(self, element):
        return self._sources.get(id(element))

    def selected_template(self) -> FxElement | None:
        value = self.material_combo.currentData()
        return value if isinstance(value, FxElement) else None

    def _show_preview_frame(self) -> None:
        if not self._preview_frames:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Missing or unreadable asset")
            return
        image = self._preview_frames[
            self._preview_frame % len(self._preview_frames)]
        if image.isNull():
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Missing or unreadable asset")
            return
        pixmap = QPixmap.fromImage(image).scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation)
        self.preview_label.setText("")
        self.preview_label.setPixmap(pixmap)

    def _refresh_preview(self, _index: int = 0) -> None:
        self._preview_timer.stop()
        self._preview_frames = []
        self._preview_frame = 0
        element = self.selected_template()
        info = []
        try:
            context = (
                self.source_context(element) if element is not None else None)
            if context is not None:
                family, obj = context
                for index, name in enumerate(element.material_names):
                    animation = family.animations.get(name)
                    if animation is not None:
                        samples = [
                            (animation.bitmap_names[frame.frame_id],
                             animation.texcoord_groups[frame.texcoords_id])
                            for frame in animation.frames
                            if 0 <= frame.frame_id < len(animation.bitmap_names)
                            and 0 <= frame.texcoords_id
                            < len(animation.texcoord_groups)
                        ]
                    else:
                        if index >= len(element.block_indices) \
                                or index >= len(element.atts_indices):
                            continue
                        block_index = element.block_indices[index]
                        if not 0 <= block_index < len(obj.base_object.ades):
                            continue
                        block = obj.base_object.ades[block_index]
                        atts = element.atts_indices[index]
                        samples = (
                            [(name, block.olpl[atts])]
                            if 0 <= atts < len(block.olpl) else [])
                    for bitmap_name, uvs in samples:
                        bitmap = family.textures.get(bitmap_name)
                        if bitmap is None:
                            continue
                        image = _qimage_from_ilbm(
                            bitmap, family.external_palette)
                        frame = crop_uv_region(image, [uvs])
                        if not frame.isNull():
                            self._preview_frames.append(frame)
                info = [
                    f"{element.source_kind} | "
                    f"{len(element.poly_ids)} polygon(s)",
                    "Clone preserves geometry, materials and all UV phases.",
                ]
            elif element is not None and self._preview_provider is not None:
                frames, info = self._preview_provider(element)
                self._preview_frames = [
                    frame for frame in frames
                    if isinstance(frame, QImage) and not frame.isNull()]
        except Exception as exc:
            # A bad/missing source asset must never take down the editor just
            # because the Add FX picker tried to render its preview.
            self._preview_frames = []
            info = [f"Preview diagnostic: {exc}"]
        self.preview_info.setText("\n".join(info))
        self._show_preview_frame()
        if len(self._preview_frames) > 1:
            self._preview_timer.start()

    def _advance_preview(self) -> None:
        if len(self._preview_frames) < 2:
            self._preview_timer.stop()
            return
        self._preview_frame = (self._preview_frame + 1) % len(
            self._preview_frames)
        self._show_preview_frame()

    def done(self, result: int) -> None:
        self._preview_timer.stop()
        super().done(result)
