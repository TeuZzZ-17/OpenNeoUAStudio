from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import bootstrap
from ..core.ldf_model import LdfDocument, briefing_for_export, movie_filename
from ..core.game_installation import (FOLDERS, GameInstallation,
                                      forget_remembered, load_remembered,
                                      save_remembered)
from ..core.resource_catalog import ResourceCatalog, preview_image
from .thumbnail_delegate import ThumbnailDelegate

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


class GameInstallationDialog(QDialog):
    def __init__(self, current: GameInstallation | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Game installation")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the game's Data folder or its parent installation folder.\n"
                                "Folders below are suggested automatically and can be changed."))
        form = QFormLayout()
        layout.addLayout(form)
        self.root = QLineEdit(str(current.data) if current else "")
        form.addRow("Game installation", self._picker(self.root, self._suggest))
        self.edits = {}
        for name in FOLDERS:
            edit = QLineEdit()
            self.edits[name] = edit
            form.addRow(name.capitalize(), self._picker(edit))
            edit.editingFinished.connect(self._refresh_inventory)
        self.root.editingFinished.connect(self._suggest)
        if current:
            for name in FOLDERS:
                self.edits[name].setText(str(current.folder(name)))
        self.inventory_label = QLabel()
        layout.addWidget(self.inventory_label)
        if current:
            self._refresh_inventory()
        self.remember = QCheckBox("Remember these folders on this computer")
        self.remember.setChecked(current is not None and current == load_remembered())
        layout.addWidget(self.remember)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _picker(self, edit: QLineEdit, after=None) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit)
        button = QPushButton("Browse")
        button.clicked.connect(lambda: self._browse(edit, after))
        layout.addWidget(button)
        return row

    def _browse(self, edit: QLineEdit, after=None):
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", edit.text())
        if chosen:
            edit.setText(chosen)
            if after:
                after()

    def _suggest(self):
        if self.root.text().strip():
            install = GameInstallation.suggest(self.root.text().strip())
            for name in FOLDERS:
                self.edits[name].setText(str(install.folder(name)))
            self._refresh_inventory()

    def _refresh_inventory(self):
        if not self.root.text().strip():
            self.inventory_label.setText("")
            return
        counts = self.installation().inventory()
        self.inventory_label.setText(
            f"Found: {counts['sets']} complete sets · {counts['mb']} MB · {counts['db']} DB · "
            f"{counts['skies']} sky archives · {counts['music']} tracks · "
            f"{counts['movies']} movies · {counts['scripts']} scripts · "
            f"{counts.get('levels', 0)} levels · {counts.get('models', 0)} models · "
            f"{counts.get('icons', 0)} icons")

    def installation(self) -> GameInstallation:
        data = GameInstallation.suggest(self.root.text().strip()).data
        folders = {name: Path(edit.text().strip()).expanduser().resolve()
                   for name, edit in self.edits.items()}
        return GameInstallation(data, folders)

    def _accept(self):
        install = self.installation()
        if not install.valid():
            QMessageBox.warning(self, "Game installation",
                                "Select an existing Data folder containing Sets.")
            return
        if self.remember.isChecked():
            save_remembered(install)
        else:
            forget_remembered()
        self.accept()


class ResizeDialog(_Form):
    def __init__(self, doc: LdfDocument, parent=None):
        super().__init__(parent, "Resize map")
        self.doc = doc
        self.width_box, self.height_box = _size_box(doc.mw), _size_box(doc.mh)
        self.form.addRow("Width", self.width_box)
        self.form.addRow("Height", self.height_box)
        self.finish()

    def values(self):
        return self.width_box.value(), self.height_box.value()

    @staticmethod
    def ask(doc: LdfDocument, parent=None):
        """Ritorna (w, h, rimuovi_fuori_mappa) oppure None se annullato."""
        dialog = ResizeDialog(doc, parent)
        if not dialog.exec():
            return None
        w, h = dialog.values()
        counts = doc.count_resize_out_of_bounds(w, h)
        if any(counts.values()):
            labels = {'gate_objects': "gates", 'item_objects': "items", 'gems': "gems",
                      'squads': "squads", 'hosts': "host stations",
                      'gate_keys': "gate keys", 'item_keys': "item keys"}
            text = ", ".join(f"{labels[k]}: {v}" for k, v in counts.items() if v)
            answer = QMessageBox.question(
                parent, "Objects outside map",
                f"The new size leaves these objects outside the map: {text}.\nRemove them?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
            return w, h, True
        return w, h, False


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
        title_form.addRow("Level title", self.title_edit)
        layout.addLayout(title_form)
        self.title_edit.editingFinished.connect(self._emit_values_changed)

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
        sky_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(sky_label)
        self.sky_list = QListWidget()
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
        current_name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
        for name, source in self.catalog.skies.items():
            item = QListWidgetItem(source.name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.sky_list.addItem(item)
            self.sky_items[name] = item
            if name == current_name:
                self.sky_list.setCurrentItem(item)
        self.sky_list.currentItemChanged.connect(self._select_sky)
        layout.addWidget(self.sky_list)
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
        media_form.addRow("Ambience track", self.music_box)
        self.movie_box = QComboBox()
        self.movie_box.setEditable(True)
        self.movie_box.addItem("None")
        self.movie_box.addItems(self.catalog.movies)
        self.movie_box.setCurrentText(movie_filename(doc.lvl_info.get("movie")) or "None")
        self.movie_box.activated.connect(lambda _index: self._emit_values_changed())
        self.movie_box.lineEdit().editingFinished.connect(self._emit_values_changed)
        media_form.addRow("Intro movie", self.movie_box)
        layout.addLayout(media_form)

        for key, prefix in (("mbmap", "mb"), ("dbmap", "db")):
            self._update_art(key, prefix)
        self.sky_list.verticalScrollBar().valueChanged.connect(
            lambda _value: self._schedule_visible_sky_load())
        self._schedule_sky_grid_layout()

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
            else:
                item.setIcon(QIcon(QPixmap.fromImage(image).scaled(
                    self.sky_list.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
                selected_name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
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
        new_value = f"objects/{source.name}.bas"
        if new_value != self.sky_value:
            self.sky_value = new_value
            self._emit_values_changed()
        image = self._image_cache.get(source)
        if image is not None:
            self.previewReady.emit(self.sky_value, QImage(image))
        self._schedule_visible_sky_load()

    def _schedule_visible_sky_load(self):
        if not self._disposed:
            QTimer.singleShot(0, self._load_visible_skies)

    def _schedule_sky_grid_layout(self):
        if not self._disposed:
            QTimer.singleShot(0, self._layout_sky_grid)

    def _layout_sky_grid(self):
        if self._disposed or self.sky_list.viewport().width() <= 0:
            return
        self._layout_artwork()
        width = self.sky_list.viewport().width()
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
        name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
        source = self.catalog.skies.get(name)
        image = self._image_cache.get(source) if source is not None else None
        return QImage(image) if image is not None else None

    def request_selected_sky(self):
        name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
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
                "mbmap": self._art_value("mbmap"),
                "dbmap": self._art_value("dbmap"),
                "music": self.music_value(),
                "movie": movie_filename(self.movie_box.currentText()) or "None"}

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
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
        name = Path(self.panel.sky_value.replace("\\", "/")).stem.casefold()
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
