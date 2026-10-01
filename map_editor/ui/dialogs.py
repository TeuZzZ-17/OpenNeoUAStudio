from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
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
        button = QPushButton("Browse...")
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


class LevelInfoDialog(QDialog):
    musicChanged = Signal(str)

    def __init__(self, doc: LdfDocument, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Level Info")
        self.resize(630, 760)
        self.catalog = ResourceCatalog(bootstrap.installation() or
                                       GameInstallation.suggest(bootstrap.game_data_dir()),
                                       doc.set_number)
        self.sky_value = str(doc.lvl_info.get("sky", ""))
        self._preview_jobs = {}
        self._art_requests = {}
        layout = QVBoxLayout(self)
        title_form = QFormLayout()
        self.title_edit = QLineEdit(str(doc.lvl_info.get("title", "")))
        title_form.addRow("Level title", self.title_edit)
        layout.addLayout(title_form)

        art_form = QFormLayout()
        self.art_boxes = {}
        self.art_previews = {}
        self.art_status = {}
        self._original_art = {}
        self._original_art_labels = {}
        art_row = QHBoxLayout()
        for key, label, prefix in (("mbmap", "Briefing art (MB)", "mb"),
                                   ("dbmap", "Debriefing art (DB)", "db")):
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
            art_form.addRow(label, box)
            preview = QLabel("No preview")
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setFixedSize(205, 165)
            preview.setStyleSheet("background:#171717; border:1px solid #454545")
            self.art_previews[key] = preview
            column = QVBoxLayout()
            column.addWidget(preview)
            status = QLabel()
            status.setWordWrap(True)
            status.setFixedWidth(205)
            self.art_status[key] = status
            column.addWidget(status)
            art_row.addLayout(column)
            box.currentTextChanged.connect(lambda _text, k=key, p=prefix: self._update_art(k, p))
        layout.addLayout(art_form)
        layout.addLayout(art_row)
        layout.addWidget(QLabel("Artwork is read from your selected game folders."))

        layout.addWidget(QLabel("Select sky"))
        self.sky_list = QListWidget()
        self.sky_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.sky_list.setFlow(QListWidget.Flow.LeftToRight)
        self.sky_list.setWrapping(True)
        self.sky_list.setUniformItemSizes(True)
        self.sky_list.setSpacing(6)
        self.sky_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.sky_list.setIconSize(QSize(150, 82))
        self.sky_list.setGridSize(QSize(174, 118))
        self.sky_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.sky_list.setMovement(QListWidget.Movement.Static)
        self.sky_list.setMinimumHeight(245)
        self.sky_items = {}
        self._failed_skies = set()
        current_name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
        for name, source in self.catalog.skies.items():
            item = QListWidgetItem(source.name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            self.sky_list.addItem(item)
            self.sky_items[name] = item
            if name == current_name:
                self.sky_list.setCurrentItem(item)
        self.sky_list.currentItemChanged.connect(self._select_sky)
        layout.addWidget(self.sky_list)
        if not self.catalog.skies:
            layout.addWidget(QLabel("No sky archives found in the selected Skies folder."))

        media_form = QFormLayout()
        self.music_box = QComboBox()
        self.music_box.setEditable(True)
        self.music_box.addItem("None", "None")
        for filename in self.catalog.music:
            self.music_box.addItem(filename, Path(filename).stem)
        self._set_media(self.music_box, str(doc.lvl_info.get("music", "None")))
        self.music_box.currentTextChanged.connect(
            lambda _text: self.musicChanged.emit(self.music_value()))
        media_form.addRow("Ambience track", self.music_box)
        self.movie_box = QComboBox()
        self.movie_box.setEditable(True)
        self.movie_box.addItem("None")
        self.movie_box.addItems(self.catalog.movies)
        self.movie_box.setCurrentText(movie_filename(doc.lvl_info.get("movie")) or "None")
        media_form.addRow("Intro movie", self.movie_box)
        layout.addLayout(media_form)
        layout.addWidget(QLabel("Choose from the installation or type a custom value."))
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                        QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for key, prefix in (("mbmap", "mb"), ("dbmap", "db")):
            self._update_art(key, prefix)
        QTimer.singleShot(0, self._load_visible_skies)
        self.sky_list.verticalScrollBar().valueChanged.connect(lambda _value: self._load_visible_skies())

    @staticmethod
    def _set_media(box: QComboBox, value: str):
        index = next((i for i in range(box.count()) if str(box.itemData(i)) == value), -1)
        if index >= 0:
            box.setCurrentIndex(index)
        else:
            box.setCurrentText(value)

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
        label.setText("Loading preview…" if path else "No preview")
        if path:
            self._request_preview(('art', key, path), path)

    def _request_preview(self, key, source):
        if key in self._preview_jobs:
            return
        job = _PreviewJob(key, source, self.catalog.palette)
        self._preview_jobs[key] = job
        job.signals.finished.connect(self._preview_finished)
        QThreadPool.globalInstance().start(job)

    def _preview_finished(self, key, image):
        self._preview_jobs.pop(key, None)
        if key[0] == 'sky':
            item = self.sky_items[key[1]]
            if image is None or image.isNull():
                self._failed_skies.add(key[1])
            else:
                item.setIcon(QIcon(QPixmap.fromImage(image).scaled(
                    self.sky_list.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
            if self.isVisible():
                self._load_visible_skies()
            return
        if self._art_requests.get(key[1]) != key[2]:
            return
        label = self.art_previews[key[1]]
        if image is None or image.isNull():
            label.setPixmap(QPixmap())
            label.setText("No preview")
        else:
            label.setText("")
            label.setPixmap(QPixmap.fromImage(image).scaled(
                label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _select_sky(self, item, _previous=None):
        if item is not None:
            self.sky_value = f"objects/{self.catalog.skies[item.data(Qt.ItemDataRole.UserRole)].name}.bas"
            self._load_visible_skies()

    def _load_visible_skies(self):
        if any(key[0] == 'sky' for key in self._preview_jobs):
            return
        rect = self.sky_list.viewport().rect()
        for name, item in self.sky_items.items():
            if (name not in self._failed_skies and item.icon().isNull() and
                    self.sky_list.visualItemRect(item).intersects(rect)):
                self._request_preview(('sky', name), self.catalog.skies[name])
                break

    def selected_sky_image(self):
        name = Path(self.sky_value.replace("\\", "/")).stem.casefold()
        source = self.catalog.skies.get(name)
        try:
            return preview_image(source, self.catalog.palette) if source else None
        except Exception:
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
