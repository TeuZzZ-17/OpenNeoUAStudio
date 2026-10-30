"""Map Editor dialogs: new map, resize, and the Level Info panel (title, briefing art, sky, music, ambient sound, movie, clouds)."""

from __future__ import annotations

from pathlib import Path

import random

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
                               QStyle, QVBoxLayout, QWidget)

from .. import bootstrap
from studio.exchange.portable import find_existing_path
from ..core.ambient_sound import (AUDIO_SUFFIXES, DEFAULT_FOLDER, ambient_candidates, ambient_sound_value,
                                  default_folder, import_into_data, relative_to_data)
from ..core.clouds import load_cloud_profiles
from ..core.ldf_model import (EDITOR_HEIGHT_MAX, LdfDocument, briefing_for_export, cloud_id_value,
                              movie_filename)
from ..core.game_installation import (GameInstallation)
from ..core.resource_catalog import ResourceCatalog, preview_image, sky_key
from studio.ui.shell import ResourcesDialog as GameInstallationDialog  # noqa: F401 - used by map_editor.editor, replaced by tests
from studio.ui.shell import COMPAT_STYLES, OPENNEOUA
from .thumbnail_delegate import PLACEHOLDER_ROLE, ThumbnailDelegate

MIN_SIZE, MAX_SIZE = 5, 255


def _size_box(value: int) -> QSpinBox:
    box = QSpinBox()
    box.setRange(MIN_SIZE, MAX_SIZE)
    box.setValue(value)
    return box


class _Form(QDialog):
    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.form = QFormLayout(self)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

    def finish(self):
        self.form.addRow(self.buttons)


class NewMapDialog(_Form):
    def __init__(self, parent=None, sets=(1, 2, 3, 4, 5, 6)):
        super().__init__(parent, "New map")
        self.width_box, self.height_box = _size_box(15), _size_box(15)
        self.set_box = QComboBox()
        for number in sets:
            self.set_box.addItem(f"Set {number}", number)
        self.form.addRow("Width", self.width_box)
        self.form.addRow("Height", self.height_box)
        self.form.addRow("Set", self.set_box)
        self.finish()

    def values(self):
        return (self.width_box.value(), self.height_box.value(),
                self.set_box.currentData())




class _SectorHeightBox(QSpinBox):
    """Terrain height 0-60 (30 = original ground); the lowest step reads "Default" and means "keep".

    -1 is the "Default" step: the map keeps its terrain and new sectors get the standard ground.
    """

    def __init__(self):
        super().__init__()
        self.setRange(-1, EDITOR_HEIGHT_MAX)
        self.setSpecialValueText("Default")
        self.setValue(-1)
        self.setToolTip("Default keeps the current terrain and gives new sectors the standard ground height. "
                        "A number (0-60, 30 = original ground) sets every sector of the map to that height.")

    def height(self) -> int | None:
        return None if self.value() < 0 else self.value()


class ResizeDialog(_Form):
    def __init__(self, doc: LdfDocument, parent=None):
        super().__init__(parent, "Resize map")
        self.doc = doc
        self.width_box, self.height_box = _size_box(doc.mw), _size_box(doc.mh)
        self.link_box = QCheckBox("Change width and height together")
        self.link_box.setChecked(True)
        self.link_box.setToolTip("While checked, a new width is also used as the height, and the other way round.")
        self.sector_height_box = _SectorHeightBox()
        self.form.addRow(self.link_box)
        self.form.addRow("Width", self.width_box)
        self.form.addRow("Height", self.height_box)
        self.form.addRow("Sector height", self.sector_height_box)
        self.width_box.valueChanged.connect(lambda value: self._follow(self.height_box, value))
        self.height_box.valueChanged.connect(lambda value: self._follow(self.width_box, value))
        self.finish()

    def _follow(self, other: QSpinBox, value: int):
        if self.link_box.isChecked() and other.value() != value:
            other.setValue(value)

    def values(self):
        return self.width_box.value(), self.height_box.value()

    @staticmethod
    def ask(doc: LdfDocument, parent=None):
        """Return (w, h, remove_out_of_bounds, sector_height) or None if cancelled."""
        dialog = ResizeDialog(doc, parent)
        if not dialog.exec():
            return None
        w, h = dialog.values()
        sector_height = dialog.sector_height_box.height()
        counts = doc.count_resize_out_of_bounds(w, h)
        if any(counts.values()):
            labels = {'gate_objects': "gates", 'item_objects': "items", 'gems': "gems",
                      'squads': "squads", 'hosts': "host stations",
                      'gate_keys': "gate keys", 'item_keys': "item keys", 'locations': "locations"}
            text = ", ".join(f"{labels[k]}: {v}" for k, v in counts.items() if v)
            answer = QMessageBox.question(
                parent, "Objects outside map",
                f"The new size leaves these objects outside the map: {text}.\nRemove them?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
            return w, h, True, sector_height
        return w, h, False, sector_height


class _PreviewSignals(QObject):
    finished = Signal(object, object)


class _PreviewJob(QRunnable):
    def __init__(self, key, source, palette):
        super().__init__()
        self.signals = _PreviewSignals()
        self.key, self.source, self.palette = key, source, palette

    def run(self):
        try:
            image = preview_image(self.source, self.palette)
        except Exception:
            image = None
        self.signals.finished.emit(self.key, image)


class LevelInfoPanel(QWidget):
    valuesChanged = Signal(dict)
    generateArtRequested = Signal()
    previewReady = Signal(str, QImage)
    musicChanged = Signal(str)

    def __init__(self, doc: LdfDocument, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.catalog = ResourceCatalog(bootstrap.installation() or
                                       GameInstallation.suggest(bootstrap.game_data_dir()),
                                       doc.set_number)
        self.sky_value = str(doc.lvl_info.get("sky", ""))
        self.catalog.include_sky(self.sky_value)
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(2)
        self._preview_jobs = {}
        self._preview_sources = {}
        self._image_cache = {}
        self._art_requests = {}
        self._disposed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        title_form = QFormLayout()
        self.title_edit = QLineEdit(str(doc.lvl_info.get("title", "")))
        title_form.addRow("Level Title", self.title_edit)
        layout.addLayout(title_form)
        self.title_edit.editingFinished.connect(self._emit_values_changed)

        briefing_header = QHBoxLayout()
        briefing_header.setSpacing(6)
        briefing_header.addWidget(QLabel("Briefing text"))
        self.briefing_marker = QLabel()
        self.briefing_marker.setObjectName("briefingCompatibilityMarker")
        self.briefing_marker.setFixedSize(8, 8)
        self.briefing_marker.setStyleSheet(
            f"QLabel#briefingCompatibilityMarker {{ background:{COMPAT_STYLES[OPENNEOUA].color}; border-radius:4px; }}")
        self.briefing_marker.setToolTip("OpenNeoUA only: optional briefing stored in the level. Overrides the language file.")
        briefing_header.addWidget(self.briefing_marker)
        briefing_header.addStretch()
        layout.addLayout(briefing_header)
        self.briefing_edit = QPlainTextEdit()
        self.briefing_edit.setPlaceholderText("Optional mission briefing…\nLeave empty to use the language file.")
        self.briefing_edit.setMinimumHeight(120)
        self.briefing_edit.setMaximumHeight(190)
        self.briefing_edit.setTabChangesFocus(True)
        self.briefing_edit.setPlainText(str(doc.lvl_info.get("briefing_text", "")))
        self.briefing_edit.setToolTip(self.briefing_marker.toolTip())
        self.briefing_edit.textChanged.connect(self._emit_values_changed)
        layout.addWidget(self.briefing_edit)

        self.art_boxes = {}
        self.art_previews = {}
        self.art_status = {}
        self._original_art = {}
        self._original_art_labels = {}
        art_row = QHBoxLayout()
        art_row.setSpacing(8)
        self._art_row = art_row
        for key, label, prefix in (("mbmap", "Briefing art (MB)", "mb"),
                                   ("dbmap", "Debriefing art (DB)", "db")):
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(QLabel(label))
            box = QComboBox()
            box.setEditable(True)
            box.addItem("None")
            for path in sorted(self.catalog.briefings[prefix].values(), key=lambda p: p.stem.casefold()):
                box.addItem(path.name, path.name)
            original = str(doc.lvl_info.get(key, ""))
            original_label = Path(original.replace("\\", "/")).name or "None"
            box.setCurrentText(original_label)
            self._original_art[key] = original
            self._original_art_labels[key] = original_label
            self.art_boxes[key] = box
            column.addWidget(box)

            preview = QLabel("No preview")
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setMinimumSize(100, 62)
            preview.setMaximumSize(300, 188)
            preview.setStyleSheet("background:#171717; border:1px solid #454545")
            self.art_previews[key] = preview
            column.addWidget(preview, alignment=Qt.AlignmentFlag.AlignHCenter)
            status = QLabel()
            status.setWordWrap(True)
            status.setMaximumWidth(300)
            self.art_status[key] = status
            column.addWidget(status)
            art_row.addLayout(column, 1)

            box.currentTextChanged.connect(lambda _text, k=key, p=prefix: self._update_art(k, p))
            box.activated.connect(lambda _index, k=key: self._emit_values_changed())
            box.lineEdit().editingFinished.connect(self._emit_values_changed)
        layout.addLayout(art_row)
        self.generate_art_button = QPushButton('Generate MB / DB')
        self.generate_art_button.setToolTip('Create matching briefing and debriefing images from the current map')
        self.generate_art_button.clicked.connect(self.generateArtRequested.emit)
        layout.addWidget(self.generate_art_button)

        sky_label = QLabel("Select Sky:")
        # Centred between the button above and the list below, not stuck to the top of its box.
        sky_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sky_label.setMinimumHeight(30)
        layout.addWidget(sky_label)
        self.sky_list = QListWidget()
        from studio.ui.widgets import use_standard_list_style
        use_standard_list_style(self.sky_list)
        self.sky_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.sky_list.setFlow(QListWidget.Flow.LeftToRight)
        self.sky_list.setWrapping(True)
        self.sky_list.setUniformItemSizes(True)
        self.sky_list.setSpacing(4)
        self.sky_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.sky_list.setItemDelegate(ThumbnailDelegate(self.sky_list))
        self.sky_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.sky_list.setMovement(QListWidget.Movement.Static)
        self.sky_list.setMinimumHeight(166)
        self.sky_list.setMaximumHeight(278)
        self.sky_items = {}
        self._failed_skies = set()
        current_name = sky_key(self.sky_value)
        for name, source in self.catalog.skies.items():
            item = QListWidgetItem(source.name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.sky_list.addItem(item)
            self.sky_items[name] = item
            if name == current_name:
                self.sky_list.setCurrentItem(item)
        self.sky_list.currentItemChanged.connect(self._select_sky)
        # The grid depends on the list's own width, which changes when the
        # scroll bar appears or the panel is laid out after loading a map.
        self.sky_list.installEventFilter(self)
        layout.addWidget(self.sky_list)
        self.custom_sky_button = QPushButton("Custom sky…")
        self.custom_sky_button.setToolTip("Pick a sky archive (.bas) or complete sky file (.base) inside the game's Data folder")
        self.custom_sky_button.clicked.connect(self._choose_custom_sky)
        layout.addWidget(self.custom_sky_button)
        self.no_skies_label = QLabel("No sky archives found in the selected Skies folder.")
        self.no_skies_label.setVisible(not self.catalog.skies)
        layout.addWidget(self.no_skies_label)

        media_form = QFormLayout()
        self.music_box = QComboBox()
        self.music_box.setEditable(True)
        self.music_box.addItem("None", "None")
        for filename in self.catalog.music:
            self.music_box.addItem(filename, Path(filename).stem)
        self._set_media(self.music_box, str(doc.lvl_info.get("music", "None")))
        self.music_box.currentTextChanged.connect(self._music_value_changed)
        self.music_box.activated.connect(lambda _index: self._emit_values_changed())
        self.music_box.lineEdit().editingFinished.connect(self._music_editing_finished)
        media_form.addRow("Ambience Track", self.music_box)
        self._build_ambient_sound(media_form, doc)
        self.movie_box = QComboBox()
        self.movie_box.setEditable(True)
        self.movie_box.addItem("None")
        self.movie_box.addItems(self.catalog.movies)
        self.movie_box.setCurrentText(movie_filename(doc.lvl_info.get("movie")) or "None")
        self.movie_box.activated.connect(lambda _index: self._emit_values_changed())
        self.movie_box.lineEdit().editingFinished.connect(self._emit_values_changed)
        # Row order: Intro Movie, Clouds, Ambience Track, Ambient sound.
        media_form.insertRow(0, "Intro Movie", self.movie_box)

        self.cloud_profiles = load_cloud_profiles(self.catalog.installation.folder("scripts"),
                                                  doc.script_content)
        self.cloud_box = QComboBox()
        # The editable combos around it inset their text by 2 px more than a plain combo; match them.
        self.cloud_box.setStyleSheet("QComboBox { padding-left: 10px; }")
        self.cloud_box.addItem("None", -1)
        for profile in self.cloud_profiles.values():
            self.cloud_box.addItem(profile.label, profile.id)
        cloud_id = cloud_id_value(doc.lvl_info.get("cloud_id", -1))
        if cloud_id >= 0 and cloud_id not in self.cloud_profiles:
            # Keep an ID that no script defines instead of silently dropping it on save.
            self.cloud_box.addItem(f"{cloud_id} - not defined", cloud_id)
        self.cloud_box.setCurrentIndex(max(0, self.cloud_box.findData(cloud_id)))
        self.cloud_box.activated.connect(lambda _index: self._emit_values_changed())
        cloud_tip = ("OpenNeoUA only: floating-mesh atmosphere (new_cloud profile) for the whole level. "
                     "Previewed on the map.")
        self.cloud_box.setToolTip(cloud_tip)
        cloud_label = QWidget()
        cloud_row = QHBoxLayout(cloud_label)
        cloud_row.setContentsMargins(0, 0, 0, 0)
        cloud_row.setSpacing(6)
        cloud_row.addWidget(QLabel("Clouds"))
        self.cloud_marker = QLabel()
        self.cloud_marker.setObjectName("cloudCompatibilityMarker")
        self.cloud_marker.setFixedSize(8, 8)
        self.cloud_marker.setStyleSheet(
            f"QLabel#cloudCompatibilityMarker {{ background:{COMPAT_STYLES[OPENNEOUA].color}; border-radius:4px; }}")
        self.cloud_marker.setToolTip(cloud_tip)
        cloud_row.addWidget(self.cloud_marker)
        media_form.insertRow(1, cloud_label, self.cloud_box)
        layout.addLayout(media_form)

        for key, prefix in (("mbmap", "mb"), ("dbmap", "db")):
            self._update_art(key, prefix)
        self.sky_list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_visible_sky_load())
        self._schedule_sky_grid_layout()

    def _build_ambient_sound(self, form: QFormLayout, doc: LdfDocument):
        """Row under Ambience Track: a sound file, or a folder the game picks from at random."""
        tip = ("OpenNeoUA only: ambient sound looped for the whole level, overriding the game's default "
               "ambient sound. A file plays as it is; a folder plays one of its sounds, chosen at random "
               "each time the level starts. It must be inside the game's Data folder. Empty = game default.")
        label = QWidget()
        label_row = QHBoxLayout(label)
        label_row.setContentsMargins(0, 0, 0, 0)
        label_row.setSpacing(6)
        label_row.addWidget(QLabel("Ambient sound"))
        marker = QLabel()
        marker.setObjectName("ambientCompatibilityMarker")
        marker.setFixedSize(8, 8)
        marker.setStyleSheet(
            f"QLabel#ambientCompatibilityMarker {{ background:{COMPAT_STYLES[OPENNEOUA].color}; border-radius:4px; }}")
        marker.setToolTip(tip)
        self.ambient_marker = marker
        label_row.addWidget(marker)

        self.ambient_edit = QLineEdit(ambient_sound_value(doc.lvl_info.get("ambient_sound", "")))
        self.ambient_edit.setPlaceholderText("Sounds/... file or folder (empty = game default)")
        self.ambient_edit.setToolTip(tip)
        self.ambient_edit.editingFinished.connect(self._ambient_edited)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for text, handler in (("File...", self._choose_ambient_file), ("Folder...", self._choose_ambient_folder),
                              ("Listen", self._toggle_ambient_listen)):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, h=handler: h())
            buttons.addWidget(button)
            if text == "Listen":
                self.ambient_listen = button
        self.ambient_status = QLabel()
        self.ambient_status.setWordWrap(True)
        column = QWidget()
        box = QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        box.addWidget(self.ambient_edit)
        box.addLayout(buttons)
        box.addWidget(self.ambient_status)
        form.addRow(label, column)
        self._ambient_audio = QAudioOutput(self)
        self._ambient_audio.setVolume(0.35)
        self._ambient_player = QMediaPlayer(self)
        self._ambient_player.setAudioOutput(self._ambient_audio)
        self._ambient_player.playbackStateChanged.connect(
            lambda state: self.ambient_listen.setText(
                "Stop" if state == QMediaPlayer.PlaybackState.PlayingState else "Listen"))
        self._update_ambient_status()

    def _ambient_data(self) -> Path:
        return self.catalog.installation.data

    def _update_ambient_status(self):
        value = ambient_sound_value(self.ambient_edit.text())
        candidates = ambient_candidates(self._ambient_data(), value) if value else []
        if not value:
            text = ""
        elif not candidates:
            text = "No sound found at this path inside Data."
        elif Path(value).suffix.casefold() in AUDIO_SUFFIXES:
            text = ""
        else:
            text = (f"Folder with {len(candidates)} sound{'s' if len(candidates) != 1 else ''}: "
                    "one is picked at random each time the level starts.")
        self.ambient_status.setText(text)

    def _set_ambient(self, value: str):
        self.ambient_edit.setText(value)
        self._ambient_edited()

    def _ambient_edited(self):
        self.ambient_edit.setText(ambient_sound_value(self.ambient_edit.text()))
        self._update_ambient_status()
        self._emit_values_changed()

    def _ambient_start_dir(self) -> str:
        data = self._ambient_data()
        folder = default_folder(data) or find_existing_path(data, DEFAULT_FOLDER[0]) or data
        return str(folder)

    def _choose_ambient_file(self):
        # Linux matches name filters with case, so each extension is offered in both spellings.
        suffixes = " ".join(f"*{s} *{s.upper()}" for s in sorted(AUDIO_SUFFIXES))
        path, _ = QFileDialog.getOpenFileName(self, "Choose ambient sound", self._ambient_start_dir(),
                                              f"Sounds ({suffixes});;All files (*)")
        if not path:
            return
        data = self._ambient_data()
        value = relative_to_data(data, Path(path))
        if value is None:
            # The engine only reads sounds from Data, so an outside file has to be copied in first.
            target = "/".join(DEFAULT_FOLDER)
            answer = QMessageBox.question(
                self, "Ambient sound",
                f"The game can only read sounds inside its Data folder.\nCopy {Path(path).name} to {target}?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                value = import_into_data(data, Path(path))
            except FileExistsError as exc:
                QMessageBox.warning(self, "Ambient sound",
                                    f"A different file named {exc} is already in {target}. Rename it first.")
                return
            except OSError as exc:
                QMessageBox.warning(self, "Ambient sound", f"Could not copy the file: {exc}")
                return
        self._set_ambient(value)

    def _choose_ambient_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose ambient sound folder", self._ambient_start_dir())
        if not path:
            return
        value = relative_to_data(self._ambient_data(), Path(path))
        if value is None or value == ".":
            QMessageBox.warning(self, "Ambient sound", "Choose a folder inside the game's Data folder.")
            return
        self._set_ambient(value)

    def _toggle_ambient_listen(self):
        if self._ambient_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._ambient_player.stop()
            return
        candidates = ambient_candidates(self._ambient_data(), self.ambient_edit.text())
        if not candidates:
            self.ambient_status.setText("No sound found at this path inside Data.")
            return
        self._ambient_player.setSource(QUrl.fromLocalFile(str(random.choice(candidates))))
        self._ambient_player.play()

    @staticmethod
    def _set_media(box: QComboBox, value: str):
        index = next((i for i in range(box.count()) if str(box.itemData(i)) == value), -1)
        if index >= 0:
            box.setCurrentIndex(index)
        else:
            box.setCurrentText(value)

    def _emit_values_changed(self):
        if not self._disposed:
            self.valuesChanged.emit(self.values())

    def _music_value_changed(self, _text: str):
        if self._disposed:
            return
        self.musicChanged.emit(self.music_value())

    def _music_editing_finished(self):
        self._emit_values_changed()

    def _update_art(self, key: str, prefix: str):
        path = self.catalog.briefing(prefix, self.art_boxes[key].currentText())
        label = self.art_previews[key]
        selected_name = Path(self.art_boxes[key].currentText().replace("\\", "/")).name
        if path is not None and path.name.casefold() != selected_name.casefold():
            self.art_status[key].setText(
                f"Preview: {path.name}. Referenced file {selected_name} is missing; "
                "select the available file to use it.")
        else:
            self.art_status[key].setText("")
        self._art_requests[key] = path
        label.setPixmap(QPixmap())
        label.setText("Loading preview" if path else "No preview")
        if path:
            self._request_preview(('art', key, path), path)

    def _request_preview(self, key, source):
        if self._disposed or key in self._preview_jobs:
            return
        cached = self._image_cache.get(source)
        if cached is not None:
            self._preview_sources[key] = source
            self._preview_finished(key, QImage(cached))
            return
        job = _PreviewJob(key, source, self.catalog.palette)
        self._preview_jobs[key] = job
        self._preview_sources[key] = source
        job.signals.finished.connect(self._preview_finished, Qt.ConnectionType.QueuedConnection)
        self._preview_pool.start(job)

    def _preview_finished(self, key, image):
        job = self._preview_jobs.pop(key, None)
        source = self._preview_sources.pop(key, None)
        if self._disposed:
            return
        if source is None and job is not None:
            source = job.source
        if image is not None and not image.isNull() and source is not None:
            image = QImage(image)
            self._image_cache[source] = image
        if key[0] == 'sky':
            item = self.sky_items.get(key[1])
            if item is None:
                return
            if image is None or image.isNull():
                self._failed_skies.add(key[1])
                item.setData(PLACEHOLDER_ROLE, self._missing_sky_text(key[1]))
            else:
                item.setIcon(QIcon(QPixmap.fromImage(image).scaled(
                    self.sky_list.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
                selected_name = sky_key(self.sky_value)
                if selected_name == key[1]:
                    self.previewReady.emit(self.sky_value, QImage(image))
            if self.isVisible():
                self._schedule_visible_sky_load()
            return
        if self._art_requests.get(key[1]) != key[2]:
            return
        label = self.art_previews[key[1]]
        if image is None or image.isNull():
            label.setPixmap(QPixmap())
            label.setText("No preview")
        else:
            label.setText("")
            self._scale_art_preview(key[1], image)

    def _missing_sky_text(self, name: str) -> str:
        """An archive without any resource is the "no sky" entry; others just have no picture."""
        try:
            bootstrap.ensure_studio_on_path()
            from studio.formats.setbas import read_setbas
            return "No sky" if not read_setbas(self.catalog.skies[name].archive).resources else "No preview"
        except Exception:
            return "No preview"

    def _scale_art_preview(self, key: str, image: QImage):
        label = self.art_previews[key]
        label.setText("")
        label.setPixmap(QPixmap.fromImage(image).scaled(
            label.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _select_sky(self, item, _previous=None):
        if item is None or self._disposed:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        source = self.catalog.skies.get(name)
        if source is None:
            return
        new_value = source.level_value
        if new_value != self.sky_value:
            self.sky_value = new_value
            self._emit_values_changed()
        image = self._image_cache.get(source)
        if image is not None:
            self.previewReady.emit(self.sky_value, QImage(image))
        self._schedule_visible_sky_load()

    def _choose_custom_sky(self):
        from .sky_dialog import CustomSkyDialog
        dialog = CustomSkyDialog(self.catalog, self)
        if dialog.exec() and dialog.value:
            self.add_custom_sky(dialog.value)

    def add_custom_sky(self, value: str):
        """Show the validated sky and keep its exact engine-compatible level reference."""
        self.catalog.refresh_skies()
        name = self.catalog.include_sky(value)
        if name not in self.catalog.skies:
            return
        item = self.sky_items.get(name)
        if item is None:
            item = QListWidgetItem(self.catalog.skies[name].name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.sky_list.insertItem(sorted(self.catalog.skies).index(name), item)
            self.sky_items[name] = item
        self.sky_list.setCurrentItem(item)
        self.no_skies_label.setVisible(False)
        self.sky_list.scrollToItem(item)
        self._schedule_visible_sky_load()

    def _schedule_visible_sky_load(self):
        if not self._disposed:
            QTimer.singleShot(0, self._load_visible_skies)

    def _schedule_sky_grid_layout(self):
        if not self._disposed:
            QTimer.singleShot(0, self._layout_sky_grid)

    def eventFilter(self, watched, event):
        if watched is self.sky_list and event.type() == QEvent.Type.Resize:
            self._schedule_sky_grid_layout()
        return super().eventFilter(watched, event)

    def _layout_sky_grid(self):
        if self._disposed or self.sky_list.viewport().width() <= 0:
            return
        self._layout_artwork()
        # Width without the scroll bar, always reserved: the column count must
        # not flip when the bar appears and shrinks the viewport.
        bar = self.sky_list.verticalScrollBar()
        width = (self.sky_list.viewport().width()
                 + (bar.width() if bar.isVisible() else 0)
                 - self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
        spacing = self.sky_list.spacing()
        columns = 2 if width >= 2 * 135 + spacing * 3 else 1
        cell_width = max(1, (width - spacing * (columns + 1)) // columns)
        self.sky_list.setGridSize(QSize(cell_width, 166))
        self.sky_list.setIconSize(QSize(max(1, cell_width - 18), 124))
        self._schedule_visible_sky_load()

    def _layout_artwork(self):
        available_width = self.width() - 20
        direction = (QHBoxLayout.Direction.LeftToRight if available_width >= 288
                     else QHBoxLayout.Direction.TopToBottom)
        if self._art_row.direction() != direction:
            self._art_row.setDirection(direction)
        columns = 2 if direction == QHBoxLayout.Direction.LeftToRight else 1
        cell_width = (available_width - self._art_row.spacing() * (columns - 1)) // columns
        preview_width = max(100, min(300, cell_width - 16))
        preview_height = round(preview_width * 0.62)
        for key, preview in self.art_previews.items():
            preview.setFixedSize(preview_width, preview_height)
            self.art_status[key].setMaximumWidth(preview_width)
            source = self._art_requests.get(key)
            image = self._image_cache.get(source)
            if image is not None and not image.isNull():
                self._scale_art_preview(key, image)

    def _load_visible_skies(self):
        if self._disposed or not self.isVisible():
            return
        rect = self.sky_list.viewport().rect()
        for name, item in self.sky_items.items():
            source = self.catalog.skies[name]
            if (name not in self._failed_skies and item.icon().isNull() and
                    source not in self._image_cache and
                    self.sky_list.visualItemRect(item).intersects(rect)):
                self._request_preview(('sky', name), source)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_sky_grid_layout()
        self._schedule_visible_sky_load()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_sky_grid_layout()

    def selected_sky_image(self):
        name = sky_key(self.sky_value)
        source = self.catalog.skies.get(name)
        image = self._image_cache.get(source) if source is not None else None
        return QImage(image) if image is not None else None

    def request_selected_sky(self):
        name = sky_key(self.sky_value)
        source = self.catalog.skies.get(name)
        if self._disposed:
            return None
        if source is None:
            self.previewReady.emit(self.sky_value, QImage())
            return None
        image = self._image_cache.get(source)
        if image is not None and not image.isNull():
            ready = QImage(image)
            self.previewReady.emit(self.sky_value, ready)
            return ready
        self._request_preview(('sky', name), source)
        return None

    def _art_value(self, key: str) -> str:
        box = self.art_boxes[key]
        text = box.currentText().strip()
        if text == self._original_art_labels[key]:
            return self._original_art[key]
        index = box.currentIndex()
        if index >= 0 and text == box.itemText(index) and box.itemData(index):
            return str(box.itemData(index))
        return briefing_for_export(text)

    def music_value(self) -> str:
        index = self.music_box.currentIndex()
        value = (self.music_box.itemData(index) if index >= 0 and
                 self.music_box.currentText() == self.music_box.itemText(index)
                 else self.music_box.currentText().strip())
        return str(value)

    def values(self) -> dict:
        return {"title": self.title_edit.text().strip(), "sky": self.sky_value,
                "briefing_text": self.briefing_edit.toPlainText(),
                "mbmap": self._art_value("mbmap"),
                "dbmap": self._art_value("dbmap"),
                "music": self.music_value(),
                "movie": movie_filename(self.movie_box.currentText()) or "None",
                "cloud_id": cloud_id_value(self.cloud_box.currentData()),
                "ambient_sound": ambient_sound_value(self.ambient_edit.text())}

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._ambient_player.stop()
        self._preview_pool.waitForDone()
        self._preview_jobs.clear()
        self._preview_sources.clear()

    def closeEvent(self, event):
        self.dispose()
        super().closeEvent(event)


class LevelInfoDialog(QDialog):
    musicChanged = Signal(str)

    def __init__(self, doc: LdfDocument, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Level Info")
        self.setFixedWidth(350)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = LevelInfoPanel(doc, self)
        self.panel.generate_art_button.hide()
        self.catalog = self.panel.catalog
        self.panel.musicChanged.connect(self.musicChanged.emit)
        layout.addWidget(self.panel)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def sky_value(self):
        return self.panel.sky_value

    def values(self) -> dict:
        return self.panel.values()

    def __getattr__(self, name):
        panel = self.__dict__.get("panel")
        if panel is not None:
            try:
                return getattr(panel, name)
            except AttributeError:
                pass
        raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")

    def selected_sky_image(self):
        image = self.panel.selected_sky_image()
        if image is not None and not image.isNull():
            return image
        name = sky_key(self.panel.sky_value)
        source = self.catalog.skies.get(name)
        try:
            return preview_image(source, self.catalog.palette) if source else None
        except Exception:
            return None

    def done(self, result):
        self.panel.dispose()
        super().done(result)

    def closeEvent(self, event):
        self.panel.dispose()
        super().closeEvent(event)
