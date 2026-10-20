"""Wireframe tab of the Entity Designer.

Shows the wireframe the vehicle or weapon has now, or a new one generated from its model,
and exports the new one.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from studio.rendering.asset_viewport import VIEW_PRESET_ANGLES

from .wireframe_detail import DEFAULT_DETAIL, DETAIL_LEVELS

# Slider steps from the first to the last detail level (fine, so the slider feels smooth).
SLIDER_STEPS = 100
# Pause after the last movement of the handle before the preview is rebuilt.
PREVIEW_DELAY_MS = 90
# How often the open tab checks whether the camera or the model has moved.
LIVE_CHECK_MS = 50
# The camera must stay still this long before the preview follows it: while it moves, only the
# model view is drawn, so the wireframe never slows the camera down.
CAMERA_SETTLE_S = 0.25

# Height of the action buttons, like the other tabs' buttons.
ACTION_BUTTON_HEIGHT = 30

# Title shared by the Entity Designer and Model Editor tabs.
TAB_TITLE = "Wireframe"

# Smallest contour simplification, in pixels of the 1024 px source view.
DEFAULT_SIMPLIFICATION = 3.0

# Border and glow of the active "Show current wireframe" button, per Studio theme.
CURRENT_BUTTON_ACCENTS = {
    "dark": ("#5ecbff", QColor(94, 203, 255, 190)),
    "light": ("#005f85", QColor(0, 95, 133, 120)),
}


def _load_wireframe_generator():
    """Import the generator only when a preview is requested."""

    from .wireframe_generator import generate_wireframe

    return generate_wireframe


def resolve_wireframe_path(value, *source_paths) -> Path | None:
    """Resolve a definition ``wireframe`` value to its .SKL/.SKLT file.

    Scripts often write ``.sklt`` while the files on disk are ``.SKL``; the game
    treats the two spellings alike, so both are tried before giving up.
    """

    if not value:
        return None
    name = str(value).split()[0].strip('"')
    if not name:
        return None
    stem, dot, extension = name.rpartition(".")
    spellings = [name]
    if dot and extension.casefold() in ("sklt", "skl"):
        other = "skl" if extension.casefold() == "sklt" else "sklt"
        spellings.append(f"{stem}.{other}")

    from .script_model import _resolve_data_asset_path

    for spelling in spellings:
        path = _resolve_data_asset_path(spelling, *source_paths)
        if path is not None:
            return path
    return None


class _ImagePreview(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(240, 240)
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


class WireframePanel(QWidget):
    """Let the user inspect and save a generated SKLT wireframe."""

    PREVIEW_SIZE = QSize(900, 900)

    def __init__(self, viewport, parent=None) -> None:
        super().__init__(parent)
        self.viewport = viewport
        self.projection = None
        # True when the preview may no longer match the model or view (rebuilt when shown).
        self._stale = True

        # The wireframe the definition has now (read from its script), shown instead of the new one.
        self.current_projection = None
        self.current_path = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)
        settings = QFormLayout()

        self.view_preset_combo = QComboBox(self)
        self.view_preset_combo.addItem("Current View")
        self.view_preset_combo.setToolTip(
            "Use the visible model view and its current animation frame.")
        for preset in VIEW_PRESET_ANGLES:
            self.view_preset_combo.addItem(preset)
        settings.addRow("View:", self.view_preset_combo)

        # Detail: from every line of the model (left) to the fewest, straightest lines (right).
        detail_row = QWidget(self)
        detail_layout = QHBoxLayout(detail_row)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_slider = QSlider(Qt.Orientation.Horizontal, detail_row)
        self.detail_slider.setRange(0, SLIDER_STEPS)
        self.detail_slider.setValue(self._slider_value(DEFAULT_DETAIL))
        self.detail_slider.setSingleStep(1)
        self.detail_slider.setPageStep(SLIDER_STEPS//(len(DETAIL_LEVELS)-1))
        self.detail_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.detail_slider.setTickInterval(SLIDER_STEPS//(len(DETAIL_LEVELS)-1))
        self.detail_slider.setToolTip(
            "Level of detail. Left keeps every line of the model, right gives the "
            "fewest and straightest lines, like the original game outlines.")
        detail_layout.addWidget(self.detail_slider, 1)
        self.detail_label = QLabel(self._level_label(self.detail_slider.value()), detail_row)
        self.detail_label.setMinimumWidth(70)
        detail_layout.addWidget(self.detail_label)
        settings.addRow("Detail:", detail_row)

        layout.addLayout(settings)

        preview_box = QGroupBox("Preview", self)
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(8, 6, 8, 8)
        preview_layout.setSpacing(6)
        # Full path of the definition's current SKL, in the empty band above the picture.
        self.path_label = QLabel("", preview_box)
        self.path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.path_label.setWordWrap(True)
        self.path_label.setTextFormat(Qt.TextFormat.PlainText)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setVisible(False)
        preview_layout.addWidget(self.path_label)
        self.preview = _ImagePreview(preview_box)
        self.preview.setText("No preview yet.")
        preview_layout.addWidget(self.preview, 1)

        self.stats_label = QLabel("", preview_box)
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.stats_label)
        layout.addWidget(preview_box, 1)

        self.error_label = QLabel("", self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff9b86;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        # Same arrangement as the action buttons of the other tabs: wide buttons sharing the row.
        buttons = QHBoxLayout()
        buttons.setSpacing(5)
        self.overwrite_button = QPushButton("Overwrite", self)
        self.overwrite_button.setEnabled(False)
        self.overwrite_button.setToolTip(
            "Replace the wireframe file this definition uses now with the new one. "
            "A .bak copy of the old file is kept.")
        self.overwrite_button.setMinimumHeight(ACTION_BUTTON_HEIGHT)
        self.overwrite_button.clicked.connect(self._overwrite_current)
        buttons.addWidget(self.overwrite_button, 1)
        self.current_button = QPushButton("Show current wireframe", self)
        self.current_button.setCheckable(True)
        self.current_button.setEnabled(False)
        self.current_button.setToolTip(
            "Show the wireframe this definition has now. Turn it off to see, adjust and "
            "export a new one generated from the model.")
        self.current_button.setMinimumHeight(ACTION_BUTTON_HEIGHT)
        # Keep the full label visible when the checked border and glow change its size hint.
        self.current_button.setMinimumWidth(
            self.current_button.fontMetrics().horizontalAdvance(
                self.current_button.text()) + 24)
        self.current_button.toggled.connect(self._current_toggled)
        buttons.addWidget(self.current_button, 1)
        self.save_button = QPushButton("Export", self)
        self.save_button.setEnabled(False)
        self.save_button.setToolTip("Save the new wireframe as a new .SKL file.")
        self.save_button.setMinimumHeight(ACTION_BUTTON_HEIGHT)
        self.save_button.clicked.connect(self._save_projection)
        buttons.addWidget(self.save_button, 1)
        layout.addLayout(buttons)

        # The checked button glows: a shadow effect draws the halo around it.
        self._glow_color = QColor(CURRENT_BUTTON_ACCENTS["dark"][1])
        self._current_glow = QGraphicsDropShadowEffect(self.current_button)
        self._current_glow.setOffset(0, 0)
        self.current_button.setGraphicsEffect(self._current_glow)
        self._style_current_button()
        self._style_path_label()

        self.view_preset_combo.currentTextChanged.connect(self.refresh)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._refresh_for_detail)
        self.detail_slider.valueChanged.connect(self._detail_changed)
        self.detail_slider.sliderReleased.connect(self._refresh_for_detail)
        self._shown_detail = None
        # The preview follows the camera and the model while the tab is open (Current View only).
        self._built_view = None
        # Camera state last seen and since when it has not changed (injectable for tests).
        self._clock = time.monotonic
        self._seen_view = None
        self._seen_since = 0.0
        self._new_image = None
        self._new_stats = ""
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_CHECK_MS)
        self._live_timer.timeout.connect(self._live_tick)

    @staticmethod
    def _slider_value(position: float) -> int:
        return int(round(position/(len(DETAIL_LEVELS)-1)*SLIDER_STEPS))

    @staticmethod
    def _level_label(value: int) -> str:
        last = len(DETAIL_LEVELS)-1
        return DETAIL_LEVELS[min(last, int(value/SLIDER_STEPS*last+0.5))].label

    def detail_position(self) -> float:
        """Detail the slider asks for, from 0 to the last level."""

        return self.detail_slider.value()/SLIDER_STEPS*(len(DETAIL_LEVELS)-1)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The model or its view may have changed while the tab was hidden.
        self._stale = True
        QTimer.singleShot(0, self._refresh_if_stale)
        self._live_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._live_timer.stop()
        super().hideEvent(event)

    def _view_signature(self):
        """What the preview depends on besides its own settings: the model and the camera."""

        camera = getattr(self.viewport, "_camera_state", None)
        if camera is None:
            return None
        faces = getattr(self.viewport, "_faces", None)
        return (id(faces), len(faces) if faces is not None else 0, repr(camera()))

    def _live_tick(self) -> None:
        """Rebuild the preview once the camera or the model has changed and stopped moving."""

        if (not self.isVisible() or self.detail_slider.isSliderDown()
                or self._showing_current()
                or self.view_preset_combo.currentText() != "Current View"):
            return
        signature = self._view_signature()
        if signature is None:
            return
        now = self._clock()
        if signature != self._seen_view:
            # Still moving: remember when it last changed and wait.
            self._seen_view = signature
            self._seen_since = now
            return
        if signature != self._built_view and now-self._seen_since >= CAMERA_SETTLE_S:
            self.refresh()

    # -- current wireframe ------------------------------------------------------------

    def refresh_studio_theme(self) -> None:
        """Keep the checked button's accent in step with the Studio theme."""

        self._style_current_button()
        self._style_path_label()

    def _style_path_label(self) -> None:
        """Use the theme's muted text color for the current file path."""

        palette = self.path_label.palette()
        palette.setColor(
            QPalette.ColorRole.WindowText,
            palette.color(QPalette.ColorRole.PlaceholderText))
        self.path_label.setPalette(palette)

    def _style_current_button(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        theme = str(app.property("studioThemeMode") or "dark")
        border, glow = CURRENT_BUTTON_ACCENTS.get(theme, CURRENT_BUTTON_ACCENTS["dark"])
        background = app.palette().color(QPalette.ColorRole.Highlight).name()
        self.current_button.setStyleSheet(
            "QPushButton:checked {"
            f" background-color: {background}; color: #ffffff;"
            f" border: 2px solid {border}; padding: 2px 6px;"
            "}")
        self._glow_color = QColor(glow)
        self._update_current_glow()

    def _update_current_glow(self) -> None:
        """Draw the halo only while the current wireframe is actually shown."""

        active = self.current_button.isChecked() and self.current_button.isEnabled()
        self._current_glow.setBlurRadius(18 if active else 0)
        glow = QColor(self._glow_color)
        glow.setAlpha(glow.alpha() if active else 0)
        self._current_glow.setColor(glow)

    def _showing_current(self) -> bool:
        return self.current_button.isChecked() and self.current_projection is not None

    def _sync_save_buttons(self) -> None:
        """Export and Overwrite apply to the new wireframe only; Overwrite also needs the file to replace."""

        can_save = self.projection is not None and not self._showing_current()
        self.save_button.setEnabled(can_save)
        self.overwrite_button.setEnabled(
            can_save and self.current_path is not None and self.current_path.is_file())

    def set_current_wireframe(self, path) -> None:
        """Set the wireframe file the definition uses now (None = it has none)."""

        self.current_path = Path(path) if path else None
        self.current_projection = None
        self.path_label.setText(
            str(self.current_path) if self.current_path is not None else "")
        self.path_label.setToolTip(
            f"Current wireframe file:\n{self.current_path}"
            if self.current_path is not None else "")
        self.path_label.setVisible(self.current_path is not None)
        if self.current_path is not None:
            try:
                self.current_projection = self._load_projection(self.current_path)
            except Exception as exc:
                self._show_error(f"Could not read the current wireframe: {exc}")
        with QSignalBlocker(self.current_button):
            self.current_button.setEnabled(self.current_projection is not None)
            self.current_button.setChecked(self.current_projection is not None)
        self._stale = True
        self._current_toggled(self.current_button.isChecked())
        self._sync_save_buttons()

    @staticmethod
    def _load_projection(path: Path):
        from studio.formats.sklt import parse_sklt_file
        from .wireframe_generator import WireframeProjection

        model = parse_sklt_file(path)
        segments = [[a, b] for polygon in model.polygons
                    for a, b in zip(polygon, polygon[1:])]
        if not model.points or not segments:
            raise ValueError("the file has no lines")
        return WireframeProjection([tuple(p) for p in model.points], segments)

    def _current_toggled(self, _checked: bool = False) -> None:
        showing = self._showing_current()
        self._update_current_glow()
        # The new wireframe's settings only matter while the new wireframe is shown.
        for widget in (self.view_preset_combo, self.detail_slider, self.detail_label):
            widget.setEnabled(not showing)
        if showing:
            self._show_error("")
            self._sync_save_buttons()
            image = self.current_projection.preview_image(size=self.PREVIEW_SIZE)
            self.preview.set_image(image)
            self.stats_label.setText(
                f"{len(self.current_projection.points)} points · "
                f"{len(self.current_projection.segments)} segments · "
                f"{self.current_path.name if self.current_path else ''}")
        elif self.isVisible():
            self.refresh()
        elif self.projection is not None and self._new_image is not None:
            self.preview.set_image(self._new_image)
            self.stats_label.setText(self._new_stats)
            self._sync_save_buttons()

    def _refresh_if_stale(self) -> None:
        if self._stale and self.isVisible():
            self.refresh()

    def _detail_changed(self, value: int) -> None:
        self.detail_label.setText(self._level_label(value))
        if self.detail_slider.isSliderDown():
            # Dragging: the preview follows after a short pause, so the handle never waits for it.
            self._preview_timer.start()
        else:
            self._refresh_for_detail()

    def _refresh_for_detail(self) -> None:
        self._preview_timer.stop()
        # A click and its release (or an unchanged value) must not build the same preview twice.
        key = (self.view_preset_combo.currentText(), self.detail_slider.value())
        if key != self._shown_detail or self.projection is None:
            self.refresh()

    def refresh(self, *_args) -> None:
        """Build the preview again from the model and view shown now."""

        if self._showing_current():
            # The new wireframe is built when the current one is switched off.
            self._stale = True
            return
        self._stale = False
        self._built_view = self._view_signature()
        self.projection = None
        self._sync_save_buttons()
        self.stats_label.clear()
        self._show_error("")
        # Keep the last picture while the next one is built: a moving camera must not make it flicker.
        if self.preview._image.isNull():
            self.preview.setText("Generating preview...")

        # The animation frame stays fixed while the wireframe is built.
        timer = getattr(self.viewport, "_anim_timer", None)
        animation_was_active = timer is not None and timer.isActive()
        if animation_was_active:
            timer.stop()
        try:
            generate_wireframe = _load_wireframe_generator()
            projection = generate_wireframe(
                self.viewport,
                preset=self.view_preset_combo.currentText(),
                respect_transparency=(
                    bool(self.viewport.indexed_renderer_info().get("available"))),
                simplification=DEFAULT_SIMPLIFICATION,
                detail=self.detail_position(),
            )
            image = projection.preview_image(size=self.PREVIEW_SIZE)
            if image is None or image.isNull():
                raise ValueError("The generated wireframe preview is empty.")
            stats_text = (
                f"{len(projection.points)} points · "
                f"{len(projection.segments)} segments")
        except Exception as exc:
            self.preview.clear_image()
            self.preview.setText("Preview unavailable")
            self._show_error(str(exc))
            return
        finally:
            if animation_was_active:
                timer.start()

        self.projection = projection
        self._shown_detail = (self.view_preset_combo.currentText(), self.detail_slider.value())
        self._new_image, self._new_stats = image, stats_text
        self.preview.set_image(image)
        self.stats_label.setText(stats_text)
        self._sync_save_buttons()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def _overwrite_current(self) -> None:
        """Replace the wireframe file the definition uses with the new wireframe (keeps a .bak)."""

        target = self.current_path
        if (self.projection is None or self._showing_current()
                or target is None or not target.is_file()):
            return
        answer = QMessageBox.question(
            self, "Overwrite wireframe",
            f"Overwrite the current wireframe?\n\n{target}\n\n"
            "Every definition that uses this file will change. A .bak copy is created first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from studio.workbench.window import _next_backup_path

        backup = _next_backup_path(target)
        try:
            shutil.copy2(target, backup)
            # Written beside the original and swapped in at the end: a failure leaves the old file intact.
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".wireframe_") as folder:
                staged = Path(folder) / target.name
                self.projection.save(staged)
                self._load_projection(staged)          # the new file must read back
                staged.replace(target)
        except Exception as exc:
            self._show_error(f"Could not overwrite the wireframe (the old file is unchanged): {exc}")
            return
        self._show_error("")
        # The wireframe the definition has now is the one just written.
        try:
            self.current_projection = self._load_projection(target)
        except Exception:
            self.current_projection = None
        host = self.window()
        notify = getattr(host, "_notify", None)
        if callable(notify):
            notify(f"Wireframe overwritten: {target.name}. Backup: {backup.name}.", 8000)

    def _save_projection(self) -> None:
        if self.projection is None or self._showing_current():
            return

        suggested = Path("Wireframe.SKL")
        host = self.window()
        last_directory = getattr(host, "_last_directory", None)
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
            self._show_error(f"Could not save the wireframe: {exc}")
            return

        self._show_error("")
        if host is not None and hasattr(host, "_last_directory"):
            host._last_directory = target.parent
        notify = getattr(host, "_notify", None)
        if callable(notify):
            notify(f"Wireframe exported to {target}", 6000)
