"""Briefing framing with installed backgrounds and the shared UA renderer."""

import math
import re
import time
from pathlib import Path
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtWidgets import QCheckBox, QComboBox, QFileDialog, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget

from map_editor import bootstrap
from map_editor.core.game_installation import IMAGE_SUFFIXES, save_remembered
from map_editor.core.resource_catalog import preview_image
from game_browser import game_files


# The engine frames the briefing model at a camera distance of 7 * radius, so the
# on-screen size follows 1 / radius. briefing_size = REFERENCE / radius makes a
# larger number mean a larger model (keep equal to TVhclProto::BRIEFING_SIZE_REFERENCE).
BRIEFING_SIZE_REFERENCE = 4000.0
BRIEFING_SIZE_MIN = 1
BRIEFING_SIZE_MAX = 10000
_SIZE_SLIDER_STEPS = 10000


def radius_to_briefing_size(radius):
    """Closest valid integer size for a framing radius."""
    return max(BRIEFING_SIZE_MIN, min(BRIEFING_SIZE_MAX, round(BRIEFING_SIZE_REFERENCE / max(radius, 1e-6))))


def briefing_size_to_slider(size):
    ratio = math.log10(max(BRIEFING_SIZE_MIN, size)) / math.log10(BRIEFING_SIZE_MAX)
    return round(max(0.0, min(1.0, ratio)) * _SIZE_SLIDER_STEPS)


def slider_to_briefing_size(value):
    ratio = max(0.0, min(1.0, value / _SIZE_SLIDER_STEPS))
    return max(BRIEFING_SIZE_MIN, min(BRIEFING_SIZE_MAX, round(10 ** (ratio * math.log10(BRIEFING_SIZE_MAX)))))


def briefing_layout(install):
    """Read the engine's rectangle when its source accompanies the game."""
    rectangle = (-0.98750001, 0.34999999, -0.003125, 0.85416669)
    path = install.data / "src" / "yw_funcs.cpp"
    if path.is_file():
        return _source_layout(path, path.stat().st_mtime_ns)
    return rectangle


@lru_cache(maxsize=4)
def _source_layout(path, mtime):
    rectangle = (-0.98750001, 0.34999999, -0.003125, 0.85416669)
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"ViewingObjectRect\s*=\s*Common::FRect\(([^)]+)\)", text)
    if match:
        values = tuple(float(value.strip()) for value in match[1].split(","))
        if len(values) == 4 and all(math.isfinite(value) for value in values):
            rectangle = values
    return rectangle

def find_background(install, mode):
    manual = install.preview_backgrounds.get(mode)
    if manual is not None and manual.is_file():
        return manual
    stem = "brief" if mode == "brief" else "debrief"
    preferred = install.folder("levels") / "Bg"
    if preferred.is_dir():
        found = next((path for path in preferred.iterdir() if path.is_file() and
                      path.stem.casefold() == stem and path.suffix.casefold() in IMAGE_SUFFIXES), None)
        if found is not None:
            return found
    return _search_background(str(install.data), tuple(sorted((k, str(v)) for k, v in install.folders.items())), stem)


@lru_cache(maxsize=16)
def _search_background(data, folders, stem):
    from map_editor.core.game_installation import GameInstallation
    install = GameInstallation(Path(data), {key: Path(value) for key, value in folders})
    candidates = [path for path in game_files(install, IMAGE_SUFFIXES) if path.stem.casefold() == stem]
    return candidates[0] if candidates else None


from gpu_widget import AcceleratedWidget


class BriefingPreview(AcceleratedWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        from .editor import CollisionViewport

        class PreviewRenderer(CollisionViewport):
            def _camera_vertex(renderer, point, camera=None):
                angle = math.radians(self.angle)
                pitch = math.radians(10.0)
                x, y, z = point
                rx = x * math.cos(angle) + z * math.sin(angle)
                rz = -x * math.sin(angle) + z * math.cos(angle)
                ry = y * math.cos(pitch) - rz * math.sin(pitch)
                rz = y * math.sin(pitch) + rz * math.cos(pitch)
                distance = self.radius * 7.0
                left, top, right, bottom = self.rectangle
                return (rx + (left + right) * .5 * distance,
                        -ry - (top + bottom) * .5 * distance,
                        4.0 - (rz + distance))

            def _indexed_view_cache_signature(renderer, width, height, camera):
                return super()._indexed_view_cache_signature(width, height, camera) + (self.angle, self.radius, self.rectangle)

        self.renderer = PreviewRenderer(self)
        self.renderer.hide()
        self.renderer.set_cockpit_preview_active(True)
        self.renderer.set_ground_alignment(False, False, 0)
        self.angle = 0.0
        self.radius = 25.0
        self.rectangle = (-0.98750001, 0.34999999, -0.003125, 0.85416669)
        self.background = QImage()
        self._family_key = None
        self._background_key = None
        self._last_frame = time.monotonic()
        self.error = ""
        self._asked_modes = set()
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)

    def showEvent(self, event):
        super().showEvent(event)
        self._last_frame = time.monotonic()
        self.timer.start()
        self.refresh()
        QTimer.singleShot(0, self._request_missing_background)

    def _request_missing_background(self):
        install = bootstrap.installation()
        mode = self.editor.briefing_tab.mode.currentData()
        if install is not None and mode not in self._asked_modes and find_background(install, mode) is None:
            self._asked_modes.add(mode)
            self.editor.briefing_tab.choose_background()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def _tick(self):
        now = time.monotonic()
        self.angle = (self.angle + (now - self._last_frame) * 200.0) % 360.0
        self._last_frame = now
        self.update()

    def refresh(self):
        install = bootstrap.installation()
        project = self.editor.project
        self.radius = BRIEFING_SIZE_REFERENCE / project.briefing_size if (
            project.briefing_size_enabled and project.briefing_size > 0) else (
            project.legacy.radius if project.legacy is not None else 25.0)
        self.radius = max(.001, self.radius)
        family_key = id(self.editor.family), self.editor._current_owner
        if family_key != self._family_key:
            self._family_key = family_key
            self.renderer.load_family(self.editor.family,
                {self.editor._current_owner} if self.editor._current_owner else None,
                primary_owner=self.editor._current_owner)
        self.renderer.set_model_preview_scale(project.model_scale_x, project.model_scale_y, project.model_scale_z)
        if install is not None:
            self.rectangle = briefing_layout(install)
            mode = self.editor.briefing_tab.mode.currentData()
            path = find_background(install, mode)
            key = path, path.stat().st_mtime_ns if path is not None else None
            if key != self._background_key:
                self._background_key = key
                self.background = preview_image(path) if path is not None else QImage()
            self.editor.briefing_tab.background_label.setText(str(path) if path else "Background missing — choose its file below.")
        self.update()

    def _paint_viewport(self, painter):
        painter.fillRect(self.rect(), QColor("#171c20"))
        aspect = self.editor.briefing_tab.aspect.currentData()
        width = min(self.width(), self.height() * aspect)
        height = width / aspect
        target = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
        if not self.background.isNull():
            painter.drawImage(target, self.background)
        if self.renderer.has_model and self.editor.project.target_category == "vehicle":
            try:
                self.renderer.set_cockpit_runtime_aspect(aspect)
                # Bound preview resolution; camera proportions still match the displayed screen.
                size = QSize(max(1, int(min(width, 960))), max(1, int(min(width, 960) / aspect)))
                image = self.renderer.render_snapshot(size, None)
                painter.drawImage(target, image)
                self.error = ""
            except RuntimeError as exc:
                self.error = str(exc)
        if self.background.isNull() or self.error:
            painter.setPen(QColor("#e6c587"))
            painter.drawText(self.rect().adjusted(15, 15, -15, -15), Qt.TextFlag.TextWordWrap,
                self.error or "Choose the installed Brief / Debrief background in the Briefing Model Size tab.")


class BriefingControls(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        layout = QVBoxLayout(self)
        label = QLabel("OpenNeoUA only. A larger size makes the vehicle larger in briefing and debriefing.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.enabled = QCheckBox("Include briefing_size")
        self.value = QSpinBox()
        self.value.setRange(BRIEFING_SIZE_MIN, BRIEFING_SIZE_MAX)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, _SIZE_SLIDER_STEPS)
        self.slider.sliderPressed.connect(editor._push_undo)
        self.enabled.toggled.connect(self._enabled)
        self.value.valueChanged.connect(self._value)
        self.slider.valueChanged.connect(lambda value: self.value.setValue(slider_to_briefing_size(value)))
        layout.addWidget(self.enabled)
        layout.addWidget(self.value)
        layout.addWidget(self.slider)
        self.mode = QComboBox()
        self.mode.addItem("Brief", "brief")
        self.mode.addItem("Debrief", "debrief")
        self.mode.currentIndexChanged.connect(self._mode)
        layout.addWidget(self.mode)
        self.aspect = QComboBox()
        for text, ratio in (("16:10", 16/10), ("16:9", 16/9), ("4:3", 4/3)):
            self.aspect.addItem(text, ratio)
        self.aspect.currentIndexChanged.connect(lambda: editor.briefing_preview.update())
        layout.addWidget(self.aspect)
        self.background_label = QLabel()
        self.background_label.setWordWrap(True)
        layout.addWidget(self.background_label)
        choose = QPushButton("Choose Background")
        choose.clicked.connect(self.choose_background)
        layout.addWidget(choose)
        self.output = QLabel()
        self.output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.output)
        layout.addStretch(1)

    def sync(self):
        project = self.editor.project
        with QSignalBlocker(self.enabled), QSignalBlocker(self.value), QSignalBlocker(self.slider):
            self.enabled.setChecked(project.briefing_size_enabled)
            size = round(project.briefing_size) or radius_to_briefing_size(
                project.legacy.radius if project.legacy else 25.0)
            self.value.setValue(size)
            self.slider.setValue(briefing_size_to_slider(size))
        self.setEnabled(project.target_category == "vehicle")
        self.output.setText(f"briefing_size = {round(project.briefing_size)}" if project.briefing_size_enabled else "Uses the vehicle's radius.")

    def _enabled(self, enabled):
        if self.editor._syncing:
            return
        self.editor._push_undo()
        self.editor.project.briefing_size_enabled = enabled
        self.editor.project.briefing_size = self.value.value()
        self.editor._set_modified()
        self.editor._sync_all()

    def _value(self, value):
        if self.editor._syncing:
            return
        if not self.slider.isSliderDown():
            self.editor._push_undo()
        self.editor.project.briefing_size_enabled = True
        self.editor.project.briefing_size = value
        self.editor._set_modified()
        self.editor._sync_all()

    def _mode(self):
        self.editor.briefing_preview.refresh()
        install = bootstrap.installation()
        if install is not None and find_background(install, self.mode.currentData()) is None:
            self.choose_background()

    def choose_background(self):
        install = bootstrap.installation()
        if install is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose installed briefing background", str(install.folder("levels")), "Images (*.png *.jpg *.bmp *.iff *.ilbm *.lbm *.vbmp)")
        if path:
            install.preview_backgrounds[self.mode.currentData()] = Path(path)
            save_remembered(install)
            self.editor.briefing_preview.refresh()
