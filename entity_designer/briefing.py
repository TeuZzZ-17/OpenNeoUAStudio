"""Briefing framing with installed backgrounds and the shared UA renderer."""

import math
import re
import time
from contextlib import ExitStack
from pathlib import Path
from functools import lru_cache

from PySide6.QtCore import QRectF, QSize, Qt, QSignalBlocker, QTimer
from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QLabel, QPushButton,
    QSizePolicy, QSlider, QSpinBox, QDoubleSpinBox, QHBoxLayout, QVBoxLayout, QWidget,
)

from map_editor import bootstrap
from map_editor.core.game_installation import IMAGE_SUFFIXES, save_remembered
from map_editor.core.resource_catalog import preview_image
from studio.ui.browsers import game_files


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
    path = bootstrap.find_ci(install.data, "src/game/yw_funcs.cpp", "src/yw_funcs.cpp")
    if path is not None and path.is_file():
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
    preferred = bootstrap.find_ci(install.folder("levels"), "Bg")
    if preferred is not None and preferred.is_dir():
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


from studio.rendering.gpu import AcceleratedWidget


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
                offset = self.editor.project
                ox = offset.briefing_x if offset.briefing_xyz_enabled else 0.0
                oy = offset.briefing_y if offset.briefing_xyz_enabled else 0.0
                oz = offset.briefing_z if offset.briefing_xyz_enabled else 0.0
                # Keep the projected position independent of the size camera distance.
                base_radius = offset.legacy.radius if offset.legacy is not None else 25.0
                position_scale = self.radius / max(.001, base_radius)
                return (rx + (left + right) * .5 * distance + ox * position_scale,
                        -ry - (top + bottom) * .5 * distance - oy * position_scale,
                        4.0 - (rz + distance + oz * position_scale))

            def _indexed_view_cache_signature(renderer, width, height, camera):
                return super()._indexed_view_cache_signature(width, height, camera) + (self.angle, self.radius, self.rectangle, self.editor.project.briefing_xyz_enabled, self.editor.project.briefing_x, self.editor.project.briefing_y, self.editor.project.briefing_z)

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
                self.error or "Choose the installed Brief / Debrief background in the Briefing Model Settings tab.")


class BriefingControls(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(3)
        group = QGroupBox("Briefing Model Settings")
        group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(6, 4, 6, 4)
        group_layout.setSpacing(3)
        layout.addWidget(group)
        layout.addStretch(1)
        label = QLabel("A larger size makes the vehicle larger in briefing and debriefing.")
        label.setWordWrap(True)
        group_layout.addWidget(label)
        self.enabled = QCheckBox("Include briefing_size")
        self.value = QSpinBox()
        self.value.setRange(BRIEFING_SIZE_MIN, BRIEFING_SIZE_MAX)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, _SIZE_SLIDER_STEPS)
        self.slider.sliderPressed.connect(editor._push_undo)
        self.enabled.toggled.connect(self._enabled)
        self.value.valueChanged.connect(self._value)
        self.slider.valueChanged.connect(lambda value: self.value.setValue(slider_to_briefing_size(value)))
        group_layout.addWidget(self.enabled)
        group_layout.addWidget(self.value)
        group_layout.addWidget(self.slider)
        xyz_label = QLabel("Position offset (camera axes; x+ right, y+ down, z+ away)")
        xyz_label.setWordWrap(True)
        group_layout.addWidget(xyz_label)
        self.xyz_enabled = QCheckBox("Include briefing_xyz")
        self.xyz_enabled.toggled.connect(self._xyz_enabled)
        group_layout.addWidget(self.xyz_enabled)
        self.xyz_step = QDoubleSpinBox()
        self.xyz_step.setRange(0.01, 10000.0)
        self.xyz_step.setValue(1.0)
        self.xyz_step.setSingleStep(0.5)
        self.xyz_step.setDecimals(2)
        self.xyz_step.setSuffix(" units")
        step_label = QLabel("Movement step")
        group_layout.addWidget(step_label)
        group_layout.addWidget(self.xyz_step)
        self.xyz_step_slider = QSlider(Qt.Orientation.Horizontal)
        self.xyz_step_slider.setRange(0, 600)
        self.xyz_step_slider.setValue(200)
        self.xyz_step_slider.setToolTip("Arrow movement step: 0.01 to 10000 model units")
        self.xyz_step_slider.valueChanged.connect(self._step_slider)
        self.xyz_step.valueChanged.connect(self._step_value)
        group_layout.addWidget(self.xyz_step_slider)
        self.xyz_values = {}
        row = QHBoxLayout()
        for axis, title in (("x", "X"), ("y", "Y"), ("z", "Z")):
            spin = QDoubleSpinBox()
            spin.setRange(-100000.0, 100000.0)
            spin.setDecimals(2)
            spin.setSingleStep(1.0)
            spin.setPrefix(f"{title}: ")
            self.xyz_values[axis] = spin
            row.addWidget(spin)
            spin.valueChanged.connect(lambda value, key=axis: self._xyz_value(key, value))
        group_layout.addLayout(row)
        self.xyz_buttons = {}
        arrows = QHBoxLayout()
        for axis, title, sign in (("y", "↑", -1), ("y", "↓", 1), ("x", "←", -1), ("x", "→", 1)):
            button = QPushButton(title)
            button.setToolTip(f"Move {axis} by the current step")
            button.setAutoRepeat(True)
            self.xyz_buttons[(axis, sign)] = button
            button.clicked.connect(lambda _checked=False, key=axis, amount=sign: self._nudge(key, amount))
            arrows.addWidget(button)
        reset = QPushButton("Reset position")
        reset.clicked.connect(self._reset_xyz)
        arrows.addWidget(reset)
        group_layout.addLayout(arrows)
        self.mode = QComboBox()
        self.mode.addItem("Brief", "brief")
        self.mode.addItem("Debrief", "debrief")
        self.mode.currentIndexChanged.connect(self._mode)
        group_layout.addWidget(self.mode)
        self.aspect = QComboBox()
        for text, ratio in (("16:10", 16/10), ("16:9", 16/9), ("4:3", 4/3)):
            self.aspect.addItem(text, ratio)
        self.aspect.currentIndexChanged.connect(lambda: editor.briefing_preview.update())
        group_layout.addWidget(self.aspect)
        self.background_label = QLabel()
        self.background_label.setWordWrap(True)
        group_layout.addWidget(self.background_label)
        choose = QPushButton("Choose Background")
        choose.clicked.connect(self.choose_background)
        group_layout.addWidget(choose)
        self.output = QLabel()
        self.output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        group_layout.addWidget(self.output)

    def sync(self):
        project = self.editor.project
        with QSignalBlocker(self.enabled), QSignalBlocker(self.value), QSignalBlocker(self.slider):
            self.enabled.setChecked(project.briefing_size_enabled)
            size = round(project.briefing_size) or radius_to_briefing_size(
                project.legacy.radius if project.legacy else 25.0)
            self.value.setValue(size)
            self.slider.setValue(briefing_size_to_slider(size))
        with ExitStack() as blockers:
            blockers.enter_context(QSignalBlocker(self.xyz_enabled))
            for spin in self.xyz_values.values():
                blockers.enter_context(QSignalBlocker(spin))
            self.xyz_enabled.setChecked(project.briefing_xyz_enabled)
            for axis, value in (("x", project.briefing_x), ("y", project.briefing_y), ("z", project.briefing_z)):
                self.xyz_values[axis].setValue(value)
        self.setEnabled(project.target_category == "vehicle")
        size_text = f"briefing_size = {round(project.briefing_size)}" if project.briefing_size_enabled else "Uses the vehicle's radius."
        position_text = (f"briefing_xyz = {project.briefing_x:g}_{project.briefing_y:g}_{project.briefing_z:g}"
                         if project.briefing_xyz_enabled else "Classic model position.")
        self.output.setText(size_text + "\n" + position_text)

    def _xyz_value(self, axis, value):
        if self.editor._syncing:
            return
        self.editor._push_undo()
        setattr(self.editor.project, f"briefing_{axis}", value)
        self.editor.project.briefing_xyz_enabled = True
        with QSignalBlocker(self.xyz_enabled):
            self.xyz_enabled.setChecked(True)
        self.editor._set_modified()
        self.editor.briefing_preview.refresh()

    def _xyz_enabled(self, enabled):
        if self.editor._syncing:
            return
        self.editor._push_undo()
        self.editor.project.briefing_xyz_enabled = enabled
        self.editor._set_modified()
        self.editor.briefing_preview.refresh()

    def _nudge(self, axis, amount):
        self.xyz_values[axis].setValue(self.xyz_values[axis].value() + amount * self.xyz_step.value())

    def _reset_xyz(self):
        self.editor._push_undo()
        self.editor.project.briefing_xyz_enabled = False
        with ExitStack() as blockers:
            blockers.enter_context(QSignalBlocker(self.xyz_enabled))
            self.xyz_enabled.setChecked(False)
            for axis, spin in self.xyz_values.items():
                blockers.enter_context(QSignalBlocker(spin))
                spin.setValue(0.0)
                setattr(self.editor.project, f"briefing_{axis}", 0.0)
        self.editor._set_modified()
        self.editor.briefing_preview.refresh()

    def _step_slider(self, value):
        with QSignalBlocker(self.xyz_step):
            self.xyz_step.setValue(10 ** (value / 100.0 - 2))

    def _step_value(self, value):
        with QSignalBlocker(self.xyz_step_slider):
            self.xyz_step_slider.setValue(round((math.log10(max(.01, value)) + 2) * 100))

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
