"""Official all-model snapshot batch exporter for Snapshot Studio.

The batch uses the active SET.BAS and the existing AssetViewport renderer.
It exports every renderable VP slot plus every additional embedded SKLT model
that is not present in the VISPROTO table. Source assets remain read-only.
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from studio.exchange.sevenzip import write_7z
from studio.ui.widgets import CollapsibleSection
from studio.rendering.snapshot_export import (
    AnimationExportCancelled,
    export_animation_atomic,
    is_animated_format,
    output_extension,
    write_static_image_atomic,
)
from studio.rendering.asset_viewport import AssetViewport, VIEW_PRESET_ANGLES
from studio.assets.family import AssetFamily, FamilyObject, load_asset_family
from studio.formats.base_format import BaseObject
from studio.formats.setbas import decode_skeleton
from studio.assets.visproto import normalize_skeleton_name


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass
class SnapshotSource:
    source_kind: str
    source_id: int
    vp_id: int
    base_name: str
    comment: str
    resource_index: int
    resource_name: str
    folder_name: str
    owner_path: str
    skeleton_name: str
    mapped_object: object | None
    decoded_skeleton: object | None
    geometry_only: bool
    skip_status: str = ""
    skip_message: str = ""


@dataclass
class BatchManifestRow:
    source_kind: str
    source_id: int
    vp_id: int
    base_name: str
    resource_index: int
    resource_name: str
    owner_path: str
    skeleton_name: str
    view: str
    relative_file: str
    status: str
    message: str
    requested_renderer: str = ""
    effective_renderer: str = ""
    render_error: str = ""
    palette_sha256: str = ""
    shadermp_sha256: str = ""
    tracyrmp_sha256: str = ""
    index_buffer_sha256: str = ""


def safe_component(value: str, fallback: str = "unnamed",
                   limit: int = 80) -> str:
    """Return a deterministic Windows-safe path component."""

    text = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    text = _INVALID_FILENAME.sub("_", text).strip(" ._")
    if not text:
        text = fallback
    return text[:limit].rstrip(" ._") or fallback


def view_slug(value: str) -> str:
    return safe_component(value.lower().replace(" ", "_"), "view", 48)


def assign_source_folder_names(
        sources: list[SnapshotSource], mode: str) -> None:
    """Use the selected source name and add IDs only for collisions."""

    candidates = []
    for source in sources:
        if mode == "vp_id" and source.vp_id >= 0:
            candidate = f"VP_{source.vp_id:04d}"
        elif mode == "vp_id" and source.source_kind == "SKLT":
            candidate = f"SKLT_{source.source_id:04d}"
        else:
            resource = str(source.base_name or source.resource_name or "").replace("\\", "/")
            candidate = safe_component(
                Path(resource).stem or source.base_name or source.folder_name,
                f"{source.source_kind}_{source.source_id:04d}")
        candidates.append(safe_component(candidate, "snapshot"))

    counts: dict[str, int] = {}
    for candidate in candidates:
        key = candidate.casefold()
        counts[key] = counts.get(key, 0) + 1

    used = set()
    for source, candidate in zip(sources, candidates):
        base_candidate = candidate
        if counts[candidate.casefold()] > 1 \
                or candidate.casefold() in used:
            suffix = f"{source.source_kind}_{source.source_id:04d}"
            candidate = _append_name_suffix(candidate, suffix)
        key = candidate.casefold()
        counter = 2
        while key in used:
            candidate = _append_name_suffix(
                base_candidate, f"{suffix}_{counter}")
            key = candidate.casefold()
            counter += 1
        source.folder_name = candidate
        used.add(key)


def _append_name_suffix(candidate: str, suffix: str,
                        limit: int = 80) -> str:
    """Reserve path length for a collision suffix and its counter."""

    suffix = safe_component(suffix, "2", limit)
    prefix = candidate[:max(0, limit - len(suffix) - 1)].rstrip(" ._")
    value = f"{prefix}_{suffix}" if prefix else suffix
    return safe_component(value, "snapshot", limit)


def drawable_polygon_count(skeleton) -> int:
    """Count polygons that the static Snapshot renderer can actually draw."""

    if skeleton is None:
        return 0
    return sum(1 for polygon in skeleton.polygons if len(polygon) >= 3)


def batch_renderable_sources(
        sources: list[SnapshotSource]) -> list[SnapshotSource]:
    """Include mapped particle-only models supported by AssetViewport."""

    return [
        source for source in sources
        if not source.skip_status
        or (source.skip_status == "PARTICLE_ONLY"
            and source.mapped_object is not None)
    ]


DEFAULT_COMPRESSION_LEVEL = 1
COMPRESSION_LEVEL_NAMES = {
    1: "Fastest", 2: "Fast", 3: "Fast", 4: "Normal", 5: "Normal",
    6: "Balanced", 7: "Strong", 8: "Strong", 9: "Maximum"}
SEVENZIP_METHODS = (
    ("LZMA2 (7-Zip default)", "lzma2"), ("LZMA", "lzma"),
    ("BZip2", "bzip2"), ("Deflate", "deflate"), ("Store (no compression)", "store"))


class VPSnapshotBatchPanel(QWidget):
    """Compact batch controls embedded in the shared Export group."""

    def __init__(self, window) -> None:
        super().__init__(window._snapshot_export_box)
        self.window = window
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "Export all renderable models in the active SET.BAS using the "
            "shared export settings.")

        self._running = False
        self._cancel_requested = False
        self._temporarily_disabled_widgets = None
        self._sources: list[SnapshotSource] = []
        self._queue: list[tuple[SnapshotSource, int, str]] = []
        self._queue_index = 0
        self._rows: list[BatchManifestRow] = []
        self._warnings: list[str] = []
        self._family = None
        self._root: Path | None = None
        self._target_size = QSize(1024, 1024)
        self._zoom = 100
        self._format_id = "png"
        self._quality = 95
        self._fps = 25
        self._speed = 1.0
        self._duration_seconds: float | None = None
        self._name_mode = "resource"
        self._background = None
        self._written = 0
        self._existing = 0
        self._skipped_models = 0
        self._skipped_animations = 0
        self._failed = 0
        self._started_at = 0.0
        self._last_setbas_path = ""
        self._renderer_mode = "textured"
        self._batch_viewport: AssetViewport | None = None
        self._active_source_key: tuple[str, int] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("Batch VP Snapshots")
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)

        output_row = QHBoxLayout()
        output_row.setSpacing(5)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Output folder")
        self.output_edit.setAccessibleName("Output folder")
        output_row.addWidget(self.output_edit, 1)
        self.output_button = QPushButton("...")
        self.output_button.setFixedWidth(34)
        self.output_button.setToolTip("Choose the output folder")
        self.output_button.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_button)
        layout.addLayout(output_row)

        options_row = QHBoxLayout()
        options_row.setSpacing(18)
        self.skip_existing_check = QCheckBox("Skip Existing")
        self.skip_existing_check.setChecked(True)
        self.skip_existing_check.setToolTip(
            "Keep existing non-empty image files, allowing safe resume.")
        options_row.addWidget(self.skip_existing_check)
        self.zip_check = QCheckBox("Create ZIP")
        self.zip_check.setChecked(True)
        options_row.addWidget(self.zip_check)
        self.sevenzip_check = QCheckBox("Create 7z")
        self.sevenzip_check.setToolTip(
            "Also write a 7z archive next to the folder (smaller than ZIP).")
        options_row.addWidget(self.sevenzip_check)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        # Compression settings appear only while an archive is requested.
        self.compression_rows = CollapsibleSection(self)
        compression_grid = self.compression_rows.grid
        compression_grid.setHorizontalSpacing(8)
        compression_grid.addWidget(QLabel("Compression level:"), 0, 0)
        self.level_slider = QSlider(Qt.Orientation.Horizontal)
        self.level_slider.setRange(1, 9)
        self.level_slider.setValue(DEFAULT_COMPRESSION_LEVEL)
        self.level_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.level_slider.setTickInterval(1)
        self.level_slider.setToolTip(
            "1 is the fastest, 9 gives the smallest archive. Images are already "
            "compressed, so high levels are slow and save little.")
        compression_grid.addWidget(self.level_slider, 0, 1)
        self.level_label = QLabel()
        self.level_label.setMinimumWidth(78)
        compression_grid.addWidget(self.level_label, 0, 2)
        compression_grid.addWidget(QLabel("7z algorithm:"), 1, 0)
        self.method_combo = QComboBox()
        for label, method in SEVENZIP_METHODS:
            self.method_combo.addItem(label, method)
        self.method_combo.setToolTip(
            "LZMA2 is the 7-Zip default. The ZIP archive always uses Deflate "
            "so it opens everywhere.")
        compression_grid.addWidget(self.method_combo, 1, 1, 1, 2)
        compression_grid.setColumnStretch(1, 1)
        layout.addWidget(self.compression_rows)
        self.level_slider.valueChanged.connect(self._level_changed)
        self.zip_check.toggled.connect(self._archive_options_changed)
        self.sevenzip_check.toggled.connect(self._archive_options_changed)
        self._level_changed(self.level_slider.value())
        self._archive_options_changed()

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(5)
        self.export_button = QPushButton("Export All VP")
        self.export_button.clicked.connect(self.start)
        buttons_row.addWidget(self.export_button, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.request_cancel)
        buttons_row.addWidget(self.cancel_button)
        layout.addLayout(buttons_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        self.refresh()

    @property
    def is_running(self) -> bool:
        return self._running

    def refresh(self) -> None:
        """Refresh source counts without disturbing a running job."""

        if self._running:
            return

        table = getattr(self.window, "_vp_table", None)
        archive = getattr(self.window, "_setbas", None)
        entries = tuple(table.entries) if table is not None else ()
        sklt_resources = (
            tuple(
                resource for resource in archive.resources
                if resource.class_id.lower() == "sklt.class"
                and not resource.error
            )
            if archive is not None else ()
        )

        if archive is None or not sklt_resources:
            self.export_button.setEnabled(False)
            self.export_button.setToolTip("Load SET.BAS in BAS Manager first.")
            return

        embedded_entries = tuple(getattr(
            getattr(self.window, "_vp_embedded", None), "entries", ()))
        assigned_skeletons = {
            normalize_skeleton_name(entry.skeleton_name)
            for entry in embedded_entries
        }
        extra_count = sum(
            1 for resource in sklt_resources
            if normalize_skeleton_name(resource.resource_name)
            not in assigned_skeletons
        )
        potential_sources = len(entries) + extra_count
        views = len(VIEW_PRESET_ANGLES)

        self.export_button.setEnabled(bool(entries or extra_count))
        format_id = self.window.snapshot_format_combo.currentData()
        extension = output_extension(format_id)
        self.export_button.setToolTip(
            f"Export {potential_sources} models, up to "
            f"{potential_sources * views:,} {extension} image(s) using "
            "the shared settings.")

        archive_path = str(Path(archive.path).resolve())
        if archive_path != self._last_setbas_path:
            self._last_setbas_path = archive_path
            set_name = safe_component(
                Path(archive.path).parent.name or Path(archive.path).stem,
                "SET",
            )
            base_dir = Path(
                getattr(self.window, "_last_directory", Path.home()))
            self.output_edit.setText(
                str(base_dir / f"{set_name}_All_Models_Snapshot_Corpus"))

    def _level_changed(self, level: int) -> None:
        self.level_label.setText(f"{level} · {COMPRESSION_LEVEL_NAMES[level]}")

    def _archive_options_changed(self, *_args) -> None:
        wanted = self.zip_check.isChecked() or self.sevenzip_check.isChecked()
        self.compression_rows.set_expanded(wanted)
        self.method_combo.setEnabled(
            self.sevenzip_check.isChecked() and not self._running)

    def _choose_output(self) -> None:
        current = self.output_edit.text().strip()
        path = QFileDialog.getExistingDirectory(
            self,
            "Choose complete snapshot corpus folder",
            current or str(Path.home()),
        )
        if path:
            self.output_edit.setText(path)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.progress.setVisible(running)
        self.status_label.setVisible(running)
        self.export_button.setEnabled(not running)
        self.output_edit.setEnabled(not running)
        self.output_button.setEnabled(not running)
        self.skip_existing_check.setEnabled(not running)
        self.zip_check.setEnabled(not running)
        self.sevenzip_check.setEnabled(not running)
        self.level_slider.setEnabled(not running)
        self.method_combo.setEnabled(
            not running and self.sevenzip_check.isChecked())
        self.cancel_button.setEnabled(running)
        self.cancel_button.setVisible(running)

        if running:
            if self._temporarily_disabled_widgets is None:
                widgets = []
                photo_box = getattr(
                    self.window, "_snapshot_studio_box", None)
                export_box = getattr(
                    self.window, "_snapshot_export_box", None)
                if photo_box is not None:
                    widgets.extend(
                        widget for widget in photo_box.findChildren(QWidget)
                        if widget.parentWidget() is photo_box
                        and widget is not export_box)
                if export_box is not None:
                    widgets.extend(
                        widget for widget in export_box.findChildren(QWidget)
                        if widget.parentWidget() is export_box
                        and widget is not self)
                bas_panel = getattr(self.window, "_bas_panel", None)
                if bas_panel is not None:
                    widgets.append(bas_panel)
                self._temporarily_disabled_widgets = [
                    (widget, widget.isEnabled()) for widget in widgets]
                for widget, _was_enabled in self._temporarily_disabled_widgets:
                    widget.setEnabled(False)
        elif self._temporarily_disabled_widgets is not None:
            for widget, was_enabled in self._temporarily_disabled_widgets:
                widget.setEnabled(was_enabled)
            self._temporarily_disabled_widgets = None

    def request_cancel(self) -> None:
        if not self._running:
            return
        self._cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling")

    def start(self) -> None:
        if self._running:
            return

        table = getattr(self.window, "_vp_table", None)
        archive = getattr(self.window, "_setbas", None)
        if archive is None:
            QMessageBox.information(
                self,
                "No SET.BAS loaded",
                "Open a SET.BAS archive in BAS Manager first.",
            )
            return
        entries = tuple(table.entries) if table is not None else ()

        output_text = self.output_edit.text().strip()
        if not output_text:
            self._choose_output()
            output_text = self.output_edit.text().strip()
        if not output_text:
            return

        self._cancel_requested = False
        self._sources.clear()
        self._queue.clear()
        self._rows.clear()
        self._warnings.clear()
        self._queue_index = 0
        self._written = 0
        self._existing = 0
        self._skipped_models = 0
        self._skipped_animations = 0
        self._failed = 0
        self._started_at = time.time()
        self._root = Path(output_text).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._target_size = self.window._snapshot_output_size()
        self._zoom = int(self.window.snapshot_zoom_spin.value())
        options = self.window._snapshot_export_options()
        self._format_id = str(options["format_id"])
        self._quality = int(options["quality"])
        self._fps = int(options["fps"])
        self._speed = float(options["speed"])
        self._duration_seconds = options["duration_seconds"]
        self._name_mode = str(options["name_mode"])
        self._background = self.window._snapshot_background()
        self._renderer_mode = "textured"

        self._set_running(True)
        self.status_label.setText("Scanning SET.BAS")
        QApplication.processEvents()

        try:
            archive_path = Path(archive.path)
            current_family = getattr(self.window, "_family", None)
            if current_family is not None \
                    and current_family.base_path == archive_path:
                self._family = current_family
            else:
                self._family = load_asset_family(
                    archive_path,
                    self.window._extra_roots,
                    {},
                    archive,
                )

            self._sources = self._build_sources(
                entries, archive, self._family)
            assign_source_folder_names(self._sources, self._name_mode)
        except Exception as exc:
            self._set_running(False)
            QMessageBox.critical(
                self,
                "Snapshot batch preparation failed",
                f"The SET.BAS and source assets were not modified.\n\n{exc}",
            )
            self.status_label.setText("Preparation failed.")
            return

        renderable = batch_renderable_sources(self._sources)
        renderable_ids = {id(source) for source in renderable}
        skipped = [
            source for source in self._sources
            if id(source) not in renderable_ids
        ]
        self._skipped_models = len(skipped)
        for source in skipped:
            self._record(
                source, "", "",
                source.skip_status, source.skip_message)

        views = tuple(VIEW_PRESET_ANGLES.keys())
        self._queue = [
            (source, view_index, view)
            for source in renderable
            for view_index, view in enumerate(views, 1)
        ]

        if not self._queue:
            self._write_manifests(cancelled=False)
            self._set_running(False)
            QMessageBox.information(
                self,
                "Nothing renderable",
                "The archive contains no polygonal models or mapped effects "
                "that the Snapshot renderer can draw.",
            )
            self.status_label.setText("Nothing renderable.")
            return

        self.progress.setRange(0, len(self._queue))
        self.progress.setValue(0)

        self._batch_viewport = AssetViewport(self)
        self._batch_viewport.begin_snapshot_mode(self._background)
        self._batch_viewport.set_mode(self._renderer_mode)
        self._batch_viewport.play_animation(False)
        self._batch_viewport.set_snapshot_guides_visible(False)
        self._active_source_key = None

        self.window.statusBar().showMessage(
            f"Complete model batch started: {len(renderable)} models × "
            f"{len(views)} views.")
        self.status_label.setText("Starting")
        QTimer.singleShot(0, self._step)

    def _build_sources(self, vp_entries, archive, family) -> list[SnapshotSource]:
        """Build one deterministic corpus entry per VP plus extra SKLT."""

        sklt_resources = [
            resource for resource in archive.resources
            if resource.class_id.lower() == "sklt.class"
            and not resource.error
        ]
        resources_by_name: dict[str, list[object]] = {}
        for resource in sklt_resources:
            resources_by_name.setdefault(
                normalize_skeleton_name(resource.resource_name), []
            ).append(resource)

        objects = list(family.all_objects())
        objects_by_offset: dict[int, object] = {}
        objects_by_skeleton: dict[str, list[object]] = {}
        for obj in objects:
            base_object = getattr(obj, "base_object", None)
            if base_object is None:
                continue
            offset = int(getattr(base_object, "source_objt_offset", -1))
            if offset >= 0:
                objects_by_offset[offset] = obj
            skeleton_name = normalize_skeleton_name(
                getattr(base_object, "skeleton_name", ""))
            if skeleton_name:
                objects_by_skeleton.setdefault(
                    skeleton_name, []).append(obj)

        embedded_entries = tuple(getattr(
            getattr(self.window, "_vp_embedded", None), "entries", ()))
        embedded_by_index = {
            entry.index: entry for entry in embedded_entries
        }
        assigned_skeletons = {
            normalize_skeleton_name(entry.skeleton_name)
            for entry in embedded_entries
        }

        decoded_cache: dict[int, object] = {}
        sources: list[SnapshotSource] = []

        def decode(resource):
            cached = decoded_cache.get(resource.index)
            if cached is not None:
                return cached
            skeleton = decode_skeleton(archive, resource)
            decoded_cache[resource.index] = skeleton
            return skeleton

        def classification(skeleton) -> tuple[str, str]:
            if skeleton is None:
                return "ERROR", "Skeleton could not be decoded"
            drawable = drawable_polygon_count(skeleton)
            if drawable:
                return "", ""
            if not skeleton.points and not skeleton.polygons:
                return "EMPTY", "Skeleton contains no points or polygons"
            if skeleton.points and not skeleton.polygons:
                return (
                    "PARTICLE_ONLY",
                    "Point/particle-only VP: no static polygon mesh exists. "
                    "A mapped effect source is required for rendering.",
                )
            return (
                "NON_MESH",
                "Skeleton contains no polygon with at least three vertices",
            )

        for entry in vp_entries:
            embedded = embedded_by_index.get(entry.index)
            skeleton_key = (
                normalize_skeleton_name(embedded.skeleton_name)
                if embedded is not None else ""
            )
            candidates = resources_by_name.get(skeleton_key, ())
            resource = candidates[0] if candidates else None
            if resource is None:
                sources.append(SnapshotSource(
                    "VP", entry.index, entry.index,
                    entry.base_name, entry.comment,
                    -1, embedded.skeleton_name if embedded else "",
                    f"VP_{entry.index:04d}_{safe_component(Path(entry.base_name).stem)}",
                    "", embedded.skeleton_name if embedded else "",
                    None, None, False,
                    "MISSING_RESOURCE",
                    "The VP skeleton resource is not present in SET.BAS",
                ))
                continue

            try:
                skeleton = decode(resource)
                skip_status, skip_message = classification(skeleton)
            except Exception as exc:
                skeleton = None
                skip_status = "DECODE_ERROR"
                skip_message = str(exc)

            mapped = None
            if embedded is not None and embedded.source_objt_offset >= 0:
                mapped = objects_by_offset.get(
                    embedded.source_objt_offset)
            if mapped is None:
                mapped_candidates = objects_by_skeleton.get(
                    skeleton_key, ())
                mapped = mapped_candidates[0] if mapped_candidates else None

            sources.append(SnapshotSource(
                "VP", entry.index, entry.index,
                entry.base_name, entry.comment,
                resource.index, resource.resource_name,
                f"VP_{entry.index:04d}_{safe_component(Path(entry.base_name).stem)}",
                str(getattr(mapped, "owner_path", "") if mapped else ""),
                resource.resource_name,
                mapped, skeleton, mapped is None,
                skip_status, skip_message,
            ))

        for resource in sklt_resources:
            skeleton_key = normalize_skeleton_name(
                resource.resource_name)
            if skeleton_key in assigned_skeletons:
                continue

            try:
                skeleton = decode(resource)
                skip_status, skip_message = classification(skeleton)
            except Exception as exc:
                skeleton = None
                skip_status = "DECODE_ERROR"
                skip_message = str(exc)

            mapped_candidates = objects_by_skeleton.get(
                skeleton_key, ())
            mapped = mapped_candidates[0] if mapped_candidates else None
            stem = safe_component(Path(
                resource.resource_name.replace("\\", "/")).stem)

            sources.append(SnapshotSource(
                "SKLT", resource.index, -1,
                "", "", resource.index, resource.resource_name,
                f"SKLT_{resource.index:04d}_{stem}",
                str(getattr(mapped, "owner_path", "") if mapped else ""),
                resource.resource_name,
                mapped, skeleton, mapped is None,
                skip_status, skip_message,
            ))

            if len(sources) % 40 == 0:
                QApplication.processEvents()
                if self._cancel_requested:
                    raise RuntimeError("Batch cancelled during archive scan")

        return sources

    def _family_descendants(self, owner: str) -> set[str]:
        prefix = owner + "/"
        return {
            obj.owner_path for obj in self._family.all_objects()
            if obj.owner_path == owner
            or obj.owner_path.startswith(prefix)
        }

    def _geometry_family(self, source: SnapshotSource) -> AssetFamily:
        """Create a read-only geometry-only family for an unmapped SKLT."""

        family = AssetFamily()
        family.base_path = None
        family.setbas_archive = getattr(
            self.window, "_setbas", None)
        family.setbas_path = Path(
            getattr(family.setbas_archive, "path", ""))

        base_object = BaseObject()
        base_object.name = Path(
            source.resource_name.replace("\\", "/")).stem
        base_object.skeleton_name = source.resource_name

        fam_obj = FamilyObject(
            base_object=base_object,
            skeleton=source.decoded_skeleton,
            owner_path="root",
        )
        family.root_object = fam_obj
        family.warnings.append(
            f"Geometry-only snapshot for {source.resource_name}: "
            "no BASE material mapping exists in this SET.BAS."
        )
        return family

    def _load_source(self, source: SnapshotSource) -> None:
        key = (source.source_kind, source.source_id)
        if self._active_source_key == key:
            return
        self._active_source_key = key

        viewport = self._batch_viewport
        if viewport is None:
            raise RuntimeError("Batch viewport is unavailable")

        if source.mapped_object is not None:
            owner = source.owner_path
            viewport.load_family(
                self._family,
                visible_owners=self._family_descendants(owner),
                primary_owner=owner,
            )
            viewport.set_selected_owner(owner)
        else:
            geometry_family = self._geometry_family(source)
            viewport.load_family(
                geometry_family,
                visible_owners={"root"},
                primary_owner="root",
            )
            viewport.set_selected_owner("root")

        viewport.begin_snapshot_mode(self._background)
        viewport.set_mode(self._renderer_mode)
        viewport.play_animation(False)
        viewport.reset_animation()
        viewport.set_snapshot_guides_visible(False)
        if not viewport.has_model:
            raise RuntimeError(
                "The selected source produced no renderable geometry or effects")

    def _step(self) -> None:
        if not self._running:
            return
        if self._cancel_requested:
            self._finish(cancelled=True)
            return
        if self._queue_index >= len(self._queue):
            self._finish(cancelled=False)
            return

        source, view_index, view = self._queue[self._queue_index]
        self._queue_index += 1
        self.progress.setValue(self._queue_index)
        self.status_label.setText(
            f"{source.folder_name} • {view_index}/{len(VIEW_PRESET_ANGLES)}")

        folder = self._root / source.folder_name
        folder.mkdir(parents=True, exist_ok=True)
        extension = output_extension(self._format_id)
        output_path = folder / (
            f"{view_index:02d}_{view_slug(view)}.{extension}")
        relative = str(
            output_path.relative_to(self._root)).replace("\\", "/")

        try:
            self._load_source(source)
        except Exception as exc:
            message = str(exc)
            self._record(source, view, "", "ERROR", message)
            self._warnings.append(
                f"{source.folder_name} [{view}]: {message}")
            self._failed += 1
            QTimer.singleShot(0, self._step)
            return

        if is_animated_format(self._format_id) \
                and not self._batch_viewport.has_animation:
            self._record(
                source, view, "", "SKIPPED_NO_ANIMATION",
                "This source has no animated material to export")
            self._skipped_animations += 1
            QTimer.singleShot(0, self._step)
            return

        if self.skip_existing_check.isChecked() \
                and output_path.is_file() \
                and output_path.stat().st_size > 0:
            self._record(
                source, view, relative,
                "EXISTS", "Existing non-empty image retained")
            self._existing += 1
            QTimer.singleShot(0, self._step)
            return

        try:
            self._batch_viewport.apply_snapshot_preset(
                view, self._target_size, self._zoom)
            if is_animated_format(self._format_id):
                def cancelled() -> bool:
                    QApplication.processEvents()
                    return self._cancel_requested

                frame_count = export_animation_atomic(
                    self._batch_viewport,
                    output_path,
                    self._format_id,
                    self._target_size,
                    self._background,
                    include_guides=False,
                    fps=self._fps,
                    speed=self._speed,
                    quality=self._quality,
                    duration_seconds=self._duration_seconds,
                    cancelled=cancelled,
                    progress=lambda current, total: self.status_label.setText(
                        f"{source.folder_name} • {view_index}/"
                        f"{len(VIEW_PRESET_ANGLES)} • "
                        f"frame {current}/{total}"),
                )
                width = self._target_size.width()
                height = self._target_size.height()
            else:
                background = self._background
                if self._format_id == "jpg" and background is None:
                    background = QColor(96, 96, 96)
                image = self._batch_viewport.render_snapshot(
                    self._target_size,
                    background,
                    include_guides=False,
                )
                write_static_image_atomic(
                    image, output_path, self._format_id, self._quality)
                width = image.width()
                height = image.height()
                frame_count = 1
        except AnimationExportCancelled:
            self._record(
                source, view, "", "CANCELLED",
                "Animation export cancelled before the file was written",
                include_renderer=True)
            QTimer.singleShot(0, self._step)
            return
        except Exception as exc:
            message = str(exc)
            self._record(
                source, view, "", "ERROR", message, include_renderer=True)
            self._warnings.append(
                f"{source.folder_name} [{view}]: {message}")
            self._failed += 1
        else:
            mode = "geometry-only" if source.geometry_only else "retail-indexed"
            animation = (
                f", {frame_count} frames at {self._fps} FPS, "
                f"speed {self._speed:g}x"
                if is_animated_format(self._format_id) else "")
            self._record(
                source, view, relative, "WRITTEN",
                f"{width}x{height} {extension.upper()}{animation} ({mode})",
                include_renderer=True)
            self._written += 1

        QTimer.singleShot(0, self._step)

    def _record(
            self, source: SnapshotSource, view: str,
            relative_file: str, status: str, message: str,
            *, include_renderer: bool = False) -> None:
        renderer = {}
        viewport = self._batch_viewport
        if include_renderer and viewport is not None and view:
            try:
                renderer = viewport.indexed_renderer_info()
            except Exception:
                renderer = {}
        sources = renderer.get("sources", {}) if isinstance(renderer, dict) else {}
        stats = renderer.get("last_render_stats", {}) if isinstance(renderer, dict) else {}

        def source_hash(name: str) -> str:
            value = sources.get(name, {}) if isinstance(sources, dict) else {}
            return str(value.get("sha256", "")) if isinstance(value, dict) else ""

        requested = "retail_indexed_reconstructed"
        effective = str(renderer.get("effective_renderer", ""))
        self._rows.append(BatchManifestRow(
            source.source_kind,
            source.source_id,
            source.vp_id,
            source.base_name,
            source.resource_index,
            source.resource_name,
            source.owner_path,
            source.skeleton_name,
            view,
            relative_file,
            status,
            message,
            requested,
            effective,
            str(renderer.get("render_error", "")),
            source_hash("palette"),
            source_hash("shader"),
            source_hash("tracy"),
            str(stats.get("index_buffer_sha256", ""))
            if isinstance(stats, dict) else "",
        ))

    def _write_manifests(self, cancelled: bool) -> None:
        if self._root is None:
            return
        effective_background = self._background
        if self._format_id == "jpg" and effective_background is None:
            effective_background = QColor(96, 96, 96)
        rows = [asdict(row) for row in self._rows]
        (self._root / "manifest.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (self._root / "manifest.csv").open(
                "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(
                    BatchManifestRow.__dataclass_fields__),
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        (self._root / "warnings.log").write_text(
            "\n".join(self._warnings)
            + ("\n" if self._warnings else ""),
            encoding="utf-8",
        )
        run_info = {
            "feature": "OpenNeoUA Studio Snapshot Studio complete model batch",
            "setbas": str(getattr(
                getattr(self.window, "_setbas", None),
                "path", "",
            )),
            "vp_source": str(getattr(
                self.window, "_vp_source", "")),
            "vp_source_path": str(getattr(
                self.window, "_vp_source_path", "") or ""),
            "source_count": len(self._sources),
            "renderable_source_count": len({
                (row.source_kind, row.source_id)
                for row in self._rows
                if row.view
            }),
            "views": list(VIEW_PRESET_ANGLES),
            "width": self._target_size.width(),
            "height": self._target_size.height(),
            "zoom_percent": self._zoom,
            "format": output_extension(self._format_id).upper(),
            "format_id": self._format_id,
            "animated": is_animated_format(self._format_id),
            "quality": self._quality,
            "name_mode": self._name_mode,
            "background": (
                "transparent" if effective_background is None
                else effective_background.name()),
            "transparent_background": effective_background is None,
            "guides": False,
            "animation_fps": (
                self._fps if is_animated_format(self._format_id) else None),
            "animation_speed": (
                self._speed if is_animated_format(self._format_id) else None),
            "animation_duration_seconds": (
                self._duration_seconds
                if is_animated_format(self._format_id) else None),
            "animation_duration_source": (
                "fixed seconds" if self._duration_seconds is not None
                else "viewport cycle")
            if is_animated_format(self._format_id) else None,
            "animation_frame": (
                "deterministic playback from time zero"
                if is_animated_format(self._format_id) else "initial/reset"),
            "renderer_mode": self._renderer_mode,
            "renderer_requested": "retail_indexed_reconstructed",
            "retail_indexed_policy": (
                "fail_closed_on_unsupported_or_ambiguous_sources"),
            "written": self._written,
            "existing": self._existing,
            "skipped_models": self._skipped_models,
            "skipped_animations": self._skipped_animations,
            "failed_images": self._failed,
            "cancelled": cancelled,
            "elapsed_seconds": round(
                time.time() - self._started_at, 3),
            "scope": (
                "Every VISPROTO VP slot plus every additional embedded "
                "SKLT resource not assigned to VISPROTO"
            ),
            "non_mesh_policy": (
                "Mapped effect sources use the viewport particle renderer; "
                "unmapped point-only resources are skipped"
            ),
        }
        (self._root / "run_info.json").write_text(
            json.dumps(
                run_info, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _archive_members(self) -> list[tuple[Path, str]]:
        """Files that belong to the corpus, with their names inside an archive."""

        authorized = {
            (self._root / row.relative_file).resolve()
            for row in self._rows
            if row.relative_file and row.status in ("WRITTEN", "EXISTS")
        }
        authorized.update(
            (self._root / name).resolve()
            for name in (
                "manifest.json", "manifest.csv", "warnings.log",
                "run_info.json"))
        return [
            (path, f"{self._root.name}/{path.relative_to(self._root)}")
            for path in sorted(authorized)
            if path.is_file() and self._root in path.parents]

    def _tick(self) -> None:
        """Keep the window responsive while an archive is written."""

        if self._cancel_requested:
            raise InterruptedError
        QApplication.processEvents()

    def _write_archive(self, suffix: str, write) -> Path | None:
        """Write to a .part file and rename it, so a cancel leaves nothing half-made."""

        if self._root is None:
            return None
        target = self._root.parent / f"{self._root.name}{suffix}"
        partial = target.with_name(target.name + ".part")
        if partial.exists():
            partial.unlink()
        try:
            write(partial)
            os.replace(partial, target)
        except InterruptedError:
            if partial.exists():
                partial.unlink()
            return None
        return target

    def _create_zip(self) -> Path | None:
        members = self._archive_members()

        def write(partial: Path) -> None:
            with zipfile.ZipFile(
                    partial, "w", compression=zipfile.ZIP_DEFLATED,
                    compresslevel=self.level_slider.value(),
                    allowZip64=True) as archive:
                for path, name in members:
                    self._tick()
                    archive.write(path, arcname=name)

        return self._write_archive(".zip", write)

    def _create_7z(self) -> Path | None:
        members = self._archive_members()
        return self._write_archive(".7z", lambda partial: write_7z(
            partial, members, method=self.method_combo.currentData(),
            level=self.level_slider.value(), progress=self._tick))

    def _dispose_batch_viewport(self) -> None:
        viewport = self._batch_viewport
        self._batch_viewport = None
        self._active_source_key = None
        if viewport is None:
            return
        try:
            viewport.play_animation(False)
            viewport.end_snapshot_mode()
        except Exception:
            pass
        viewport.deleteLater()

    def _finish(self, cancelled: bool) -> None:
        self._write_manifests(cancelled)
        zip_path = sevenzip_path = None
        if not cancelled and self.zip_check.isChecked():
            self.status_label.setText("Creating ZIP")
            QApplication.processEvents()
            zip_path = self._create_zip()
            cancelled = self._cancel_requested
        if not cancelled and self.sevenzip_check.isChecked():
            self.status_label.setText("Creating 7z")
            QApplication.processEvents()
            sevenzip_path = self._create_7z()
            cancelled = self._cancel_requested

        self._dispose_batch_viewport()
        self._set_running(False)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(self._queue_index)

        if cancelled:
            self.status_label.setText("Cancelled safely.")
            self.window.statusBar().showMessage(
                "Snapshot batch cancelled; completed image files were kept.",
                8000)
            return

        self.status_label.setText(
            f"{self._written} new • {self._existing} existing • "
            f"{self._skipped_animations} static skipped • "
            f"{self._failed} errors")
        self.window.statusBar().showMessage(
            f"Complete snapshot batch: {self._written} written, "
            f"{self._existing} existing, {self._skipped_models} "
            f"non-mesh/empty models documented, "
            f"{self._skipped_animations} static sources skipped for animation, "
            f"{self._failed} failed images.",
            12000,
        )
        message = (
            f"Output:\n{self._root}\n\n"
            f"Written: {self._written}\n"
            f"Existing: {self._existing}\n"
            f"Non-mesh/empty models documented: {self._skipped_models}\n"
            f"Static sources skipped for animation: "
            f"{self._skipped_animations}\n"
            f"Failed images: {self._failed}"
        )
        if zip_path is not None:
            message += f"\n\nZIP:\n{zip_path}"
        if sevenzip_path is not None:
            message += f"\n\n7z:\n{sevenzip_path}"
        QMessageBox.information(
            self, "Snapshot batch complete", message)
