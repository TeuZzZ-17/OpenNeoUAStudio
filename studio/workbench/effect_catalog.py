"""Shared catalog and animated previews for Model Editor effects and emitters."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from threading import local

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QListView, QAbstractItemView, QPushButton, QVBoxLayout,
)

from studio.assets.family import load_asset_family
from studio.formats.setbas import read_setbas
from studio.rendering.geometry import (
    GeometryClipboardError, build_fx_element_clipboard, detect_fx_elements,
)
from studio.rendering.particles import ParticleEmitter
from studio.rendering.playback import PreviewClock
from studio.ui.widgets import crop_uv_region, qimage_from_ilbm


@dataclass(frozen=True)
class CatalogEntry:
    family: object
    owner: object
    value: object
    label: str


def emitter_blocks(owner):
    if owner is None or owner.skeleton is None:
        return
    for index, block in enumerate(owner.base_object.ades):
        if (block.class_id or "").lower() == "particle.class":
            yield index, block


def emitter_texture_names(block):
    return tuple(dict.fromkeys(
        stage.texture.name for stage in block.particle_stages
        if stage.texture is not None))


def scan_catalog(family, kind, owners=None):
    entries = []
    for owner in family.all_objects() if owners is None else owners:
        if kind == "emitter":
            for index, block in emitter_blocks(owner):
                if block.particle_attributes is None or not block.source_objt_bytes:
                    continue
                names = ", ".join(emitter_texture_names(block)) or "No texture"
                entries.append(CatalogEntry(
                    family, owner, index,
                    f"{owner.display_name} · Emitter {index} | {names}"))
            continue
        for fx in detect_fx_elements(owner, family.animations):
            try:
                build_fx_element_clipboard(owner, fx, family.animations)
            except GeometryClipboardError:
                continue
            names = ", ".join(dict.fromkeys(fx.material_names))
            entries.append(CatalogEntry(
                family, owner, fx, f"{owner.display_name} · {fx.fx_name} | {names}"))
    return entries


def scan_catalog_file(path, roots, active_archive, kind):
    path = Path(path)
    archive = read_setbas(path) if path.suffix.lower() == ".bas" else active_archive
    family = load_asset_family(path, roots, setbas=archive)
    if family.root_object is None:
        raise ValueError(next(iter(family.warnings), "No readable BASE model"))
    return scan_catalog(family, kind)


def _resource(mapping, name):
    key = name.replace("\\", "/").casefold()
    return next((value for alias, value in mapping.items()
                 if alias.replace("\\", "/").casefold() == key), None)


@dataclass
class FrameSequence:
    images: list[QImage]
    ends: tuple[float, ...]

    @classmethod
    def from_frames(cls, frames):
        good = [(image, max(1, duration)) for image, duration in frames
                if image is not None and not image.isNull()]
        return cls([image for image, _ in good],
                   tuple(accumulate(duration for _, duration in good)))

    def at(self, elapsed):
        if not self.images:
            return None
        return self.images[bisect_right(self.ends, elapsed % self.ends[-1])]


_preview_atlases = local()


def _preview_atlas(family, name):
    bitmap = _resource(family.textures, name)
    if bitmap is None:
        return None
    cache = getattr(_preview_atlases, "images", None)
    if cache is None:
        cache = _preview_atlases.images = OrderedDict()
    key = id(bitmap), id(family.external_palette)
    if key not in cache:
        cache[key] = (bitmap, family.external_palette,
                      qimage_from_ilbm(bitmap, family.external_palette))
    cache.move_to_end(key)
    if len(cache) > 24:
        cache.popitem(last=False)
    return cache[key][2]


def _texture_sequence(family, texture, groups=()):
    if texture is None:
        return FrameSequence([], ())
    animation = _resource(family.animations, texture.name) \
        if texture.kind == "bmpanim" else None
    if animation is None:
        samples = [(texture.name, groups, 180)]
    else:
        samples = [
            (animation.bitmap_names[frame.frame_id],
             [animation.texcoord_groups[frame.texcoords_id]], frame.duration_ms)
            for frame in animation.frames
            if 0 <= frame.frame_id < len(animation.bitmap_names)
            and 0 <= frame.texcoords_id < len(animation.texcoord_groups)]
    frames = []
    crops = {}
    for name, uvs, duration in samples:
        image = _preview_atlas(family, name)
        if image is not None and uvs:
            key = name, tuple(tuple(tuple(point) for point in group) for group in uvs)
            if key not in crops:
                crops[key] = crop_uv_region(image, uvs)
            image = crops[key]
        frames.append((image, duration))
    return FrameSequence.from_frames(frames)


def effect_preview(entry, kind):
    if kind == "emitter":
        return emitter_preview(
            entry.family, entry.owner.base_object.ades[entry.value], sample_limit=48)
    for block_index, atts_index in zip(entry.value.block_indices,
                                       entry.value.atts_indices):
        block = entry.owner.base_object.ades[block_index]
        groups = [block.olpl[atts_index]] if atts_index < len(block.olpl) else []
        sequence = _texture_sequence(entry.family, block.texture, groups)
        if sequence.images:
            return sequence
    return FrameSequence([], ())


def emitter_stage_preview(family, stage):
    return _texture_sequence(family, stage.texture, stage.olpl)


def emitter_preview(family, block, *, sample_limit=6000):
    """Render a looping sample of the same particle simulation as the viewport."""
    if block.particle_attributes is None or not block.particle_stages:
        return FrameSequence([], ())
    stages = [emitter_stage_preview(family, stage)
              for stage in block.particle_stages[:8]]
    emitter = ParticleEmitter(
        block.particle_attributes, (0.0, 0.0, 0.0),
        [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        [(i, i, stage) for i, stage in enumerate(stages)], "", 17)
    a = block.particle_attributes
    start = max(0, a.context_start_generation)
    span = min(8000, max(1, emitter.duration_ticks - start))
    step = span / 16
    samples = [list(emitter.samples(start + index * step, limit=sample_limit))
               for index in range(1, 17)]
    extent = max((max(abs(x), abs(y)) + size
                  for frame in samples for (x, y, _z), size, _ in frame),
                 default=0)
    if not extent:
        return next((stage for stage in stages if stage.images), FrameSequence([], ()))
    scale = 58 / extent
    frames = []
    for index, frame in enumerate(samples, 1):
        image = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        for (x, y, _z), size, (_, _, sequence) in frame:
            sprite = sequence.at(start + index * step)
            if sprite is not None:
                radius = size * scale
                painter.drawImage(QRectF(
                    64 + x * scale - radius, 64 + y * scale - radius,
                    2 * radius, 2 * radius), sprite)
        painter.end()
        frames.append((image, max(50, min(350, step))))
    return FrameSequence.from_frames(frames)


class EffectCatalogDialog(QDialog):
    """One scrollable catalog. Only visible previews animate or decode."""

    def __init__(self, current, parent=None, *, kind="fx", active_source=None,
                 external_source=None, preferred=None, local_preview=None,
                 playback_speed=None):
        super().__init__(parent)
        self.setWindowTitle("Add Emitter" if kind == "emitter" else "Add Effect")
        self.resize(870, 690)
        self._kind = kind
        self._current = current
        self._preferred = preferred
        self._local_preview = local_preview
        self._entries = []
        self._cache = OrderedDict()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._thumbnail_executor = ThreadPoolExecutor(max_workers=2)
        self._pending_previews = {}
        self._icons = OrderedDict()
        self._scan = None
        self._clock = PreviewClock(playback_speed)
        self._preview_frames = []
        self._preview_frame = 0
        self._preview_timer = QTimer(self, interval=65)
        self._preview_timer.timeout.connect(self._advance_preview)
        self._poll = QTimer(self, interval=50)
        self._poll.timeout.connect(self._finish_scan)
        layout = QVBoxLayout(self)
        sources = QHBoxLayout()
        for name, callback in (
                ("From Active Set", active_source),
                ("From Current Model", lambda: self._show_current()),
                ("From External Source", external_source)):
            button = QPushButton(name)
            button.setEnabled(callback is not None)
            if callback is not None:
                button.clicked.connect(
                    lambda _checked=False, load=callback: self._choose_source(load))
            sources.addWidget(button)
        layout.addLayout(sources)
        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        layout.addWidget(self.source_label)
        self.search = QLineEdit(placeholderText="Filter by model, effect or texture…")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)
        self.gallery = QListWidget()
        self.gallery.setViewMode(QListView.ViewMode.IconMode)
        self.gallery.setMovement(QListView.Movement.Static)
        self.gallery.setResizeMode(QListView.ResizeMode.Adjust)
        self.gallery.setLayoutMode(QListView.LayoutMode.Batched)
        self.gallery.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.gallery.setGridSize(QSize(198, 123))
        self.gallery.setWordWrap(True)
        self.gallery.setUniformItemSizes(True)
        self.gallery.setIconSize(QSize(128, 92))
        self.gallery.currentItemChanged.connect(self._selection_changed)
        self.gallery.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.gallery, 1)
        self.preview_info = QLabel()
        layout.addWidget(self.preview_info)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_current()
        if active_source is not None:
            self._choose_source(active_source)

    def _show_current(self):
        if self._scan is not None:
            self._scan[1].cancel()
        self._scan = None
        self._poll.stop()
        self._show_entries("Current model", self._current)

    def _choose_source(self, load):
        request = load()
        if request is None:
            return
        label, scanner = request
        if self._scan is not None:
            self._scan[1].cancel()
        self._show_entries(f"{label} · scanning…", [])
        self._scan = (label, self._executor.submit(scanner))
        self._poll.start()

    def _finish_scan(self):
        if self._scan is None or not self._scan[1].done():
            return
        label, future = self._scan
        self._scan = None
        self._poll.stop()
        try:
            self._show_entries(label, future.result())
        except Exception as exc:
            self._show_entries(f"{label} · {exc}", [])

    def _show_entries(self, label, entries):
        self._cache.clear()
        self._icons.clear()
        for future in self._pending_previews.values():
            future.cancel()
        self._pending_previews.clear()
        self._entries = list(entries)
        self.source_label.setText(f"{label} · {len(entries)} available")
        self._filter(self.search.text())

    def _filter(self, query):
        self.gallery.blockSignals(True)
        self.gallery.clear()
        row = 0
        words = query.casefold().split()
        for index, entry in enumerate(self._entries):
            if not all(word in entry.label.casefold() for word in words):
                continue
            item = QListWidgetItem(entry.label)
            item.setToolTip(entry.label)
            item.setSizeHint(self.gallery.gridSize())
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.gallery.addItem(item)
            if getattr(entry.value, "identity", None) == self._preferred \
                    and self._preferred is not None:
                row = self.gallery.count() - 1
        self.gallery.setCurrentRow(row if self.gallery.count() else -1)
        self.gallery.blockSignals(False)
        self._selection_changed()

    def selected_entry(self):
        item = self.gallery.currentItem()
        return self._entries[item.data(Qt.ItemDataRole.UserRole)] if item else None

    def _sequence(self, index):
        if index not in self._cache:
            entry = self._entries[index]
            try:
                if entry.family is None and self._local_preview is not None:
                    images, info = self._local_preview(entry.value)
                    sequence = FrameSequence.from_frames((image, 180) for image in images)
                    self._cache[index] = (sequence, "\n".join(info))
                else:
                    future = self._pending_previews.get(index)
                    if future is None and len(self._pending_previews) < 8:
                        self._pending_previews[index] = self._thumbnail_executor.submit(
                            effect_preview, entry, self._kind)
                    if future is None or not future.done():
                        return FrameSequence([], ()), "Loading preview…"
                    self._pending_previews.pop(index)
                    sequence = future.result()
                    self._cache[index] = (sequence, "" if sequence.images else
                                          "Preview asset missing or unreadable")
            except (ValueError, IndexError, OverflowError, RuntimeError) as exc:
                self._cache[index] = (FrameSequence([], ()), str(exc))
        self._cache.move_to_end(index)
        if len(self._cache) > 96:
            self._cache.popitem(last=False)
        return self._cache[index]

    def _selection_changed(self, *_args):
        item = self.gallery.currentItem()
        self._ok_button.setEnabled(item is not None)
        sequence, info = self._sequence(item.data(Qt.ItemDataRole.UserRole)) \
            if item else (FrameSequence([], ()), "")
        self.preview_info.setText(info)
        self._preview_frames = sequence.images
        self._preview_frame = 0
        if item is not None and (len(sequence.images) > 1 or self.gallery.count() > 1
                                 or self.selected_entry().family is not None):
            self._preview_timer.start()
        else:
            self._preview_timer.stop()
        self._draw_visible()

    def _advance_preview(self, *_args):
        if self._preview_frames:
            self._preview_frame = (self._preview_frame + 1) % len(self._preview_frames)
        self._draw_visible()

    def _draw_visible(self):
        viewport = self.gallery.viewport()
        grid = self.gallery.gridSize()
        first, end = 0, self.gallery.count()
        while first < end:
            mid = (first + end) // 2
            rect = self.gallery.visualItemRect(self.gallery.item(mid))
            if rect.isValid() and rect.bottom() < 0:
                first = mid + 1
            else:
                end = mid
        last = first
        while last < self.gallery.count():
            rect = self.gallery.visualItemRect(self.gallery.item(last))
            if not rect.isValid() or rect.top() >= viewport.height() + grid.height():
                break
            last += 1
        visible = {self.gallery.item(row).data(Qt.ItemDataRole.UserRole)
                   for row in range(first, last)}
        # Retire completed requests even after a fast scroll moved them out of view.
        for index, future in tuple(self._pending_previews.items()):
            if future.done():
                self._sequence(index)
            elif index not in visible and future.cancel():
                self._pending_previews.pop(index)
        for row in range(first, last):
            item = self.gallery.item(row)
            sequence, _info = self._sequence(item.data(Qt.ItemDataRole.UserRole))
            image = sequence.at(self._clock.elapsed())
            frame = image.cacheKey() if image is not None else None
            frame_role = int(Qt.ItemDataRole.UserRole) + 1
            if frame is not None and item.data(frame_role) != frame:
                item.setData(frame_role, frame)
                if frame not in self._icons:
                    self._icons[frame] = QIcon(QPixmap.fromImage(image).scaled(
                        self.gallery.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation))
                self._icons.move_to_end(frame)
                if len(self._icons) > 512:
                    self._icons.popitem(last=False)
                item.setIcon(self._icons[frame])

    def done(self, result):
        self._preview_timer.stop()
        self._poll.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        super().done(result)


class AddFxElementDialog(EffectCatalogDialog):
    """FX catalog with the existing clipboard template interface."""

    def __init__(self, templates, parent=None, *, preferred=None,
                 preview_provider=None, active_source=None, external_source=None,
                 playback_speed=None):
        super().__init__(
            [CatalogEntry(None, None, fx, f"{fx.fx_name} | {', '.join(fx.material_names)}")
             for fx in templates], parent,
            preferred=preferred.identity if preferred is not None else None,
            local_preview=preview_provider, active_source=active_source,
            external_source=external_source, playback_speed=playback_speed)

    def selected_template(self):
        entry = self.selected_entry()
        return entry.value if entry is not None else None

    def source_context(self, element):
        entry = self.selected_entry()
        if entry is not None and entry.value is element and entry.family is not None:
            return entry.family, entry.owner
