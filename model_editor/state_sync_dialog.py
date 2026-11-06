"""Apply the changes made on one state of a model to its other states.

The dialog finds the other states by itself, shows each one with the changes already
applied, and writes only the states the person ticks when Apply is pressed. Previews
happen in memory: nothing reaches the disk before Apply.
"""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from map_editor import bootstrap
from model_editor.state_discovery import (
    discover_states,
    FOLDER,
    merge_states,
    nearby_models,
    SIMILAR,
    state_word,
    StateCandidate,
)
from studio.assets.family import load_asset_family
from studio.assets.mapping import MappingEditError
from studio.exchange.portable import backup_existing_file, DirectoryIndex
from studio.rendering.asset_viewport import AssetViewport
from studio.ui.widgets import loading_resources
from studio_mcp.core import McpToolError
from studio_mcp.loose import LooseModel
from studio_mcp.state_sync import apply_plan, build_plan, SyncOptions, texture_files

# A folder neighbour counts as the same model from this share of identical faces.
SIMILAR_SHARE = 0.6
_SLOT_ROLE = Qt.ItemDataRole.UserRole + 81
_FAILURES = (OSError, ValueError, RuntimeError, MappingEditError, McpToolError)


def first_skeleton_object(family):
    return next((obj for obj in family.all_objects() if obj.skeleton is not None), None)


class StateSlot:
    """One destination state: its model in memory, and how to put it back as it was."""

    def __init__(self, candidate, family, fam_obj):
        self.candidate = candidate
        self.family = family
        self.fam_obj = fam_obj
        self.plan = None
        self._pristine = self._capture()

    def _capture(self):
        model = self.fam_obj.skeleton
        return copy.deepcopy((model.points, model.polygons, model.parsed_polygon_count,
                              self.fam_obj.base_object.ades))

    def reset(self):
        """Back to the state on disk, keeping the same objects the viewport knows."""
        points, polygons, count, blocks = copy.deepcopy(self._pristine)
        model = self.fam_obj.skeleton
        model.points[:] = points
        model.polygons[:] = polygons
        model.parsed_polygon_count = count
        self.fam_obj.base_object.ades[:] = blocks

    def edited_model(self, source_family, source_obj, options, *, writable=False):
        self.reset()
        lm = LooseModel.from_family(self.family, self.fam_obj, writable=writable)
        apply_plan(lm, self.plan, source_family, source_obj, options)
        return lm


class StateSyncDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.source_family = window._family
        self.source_obj = (window._owner_to_obj.get(window._selected_owner)
                           or window._workbench_obj)
        if self.source_family is None or self.source_obj is None \
                or self.source_obj.skeleton is None:
            raise ValueError("Open a model first.")
        self.setWindowTitle("Apply Changes to Other States")
        self.resize(1120, 700)
        self.slots = []
        self._saved_source = None
        self._archives = {}
        self._build_ui()
        with loading_resources(self, "Looking for the other states. Please wait…"):
            self._saved_source = self._saved_source_object()
            self._add_states(self._discover())
        if self.states.topLevelItemCount():
            self.states.setCurrentItem(self.states.topLevelItem(0))
        else:
            self.preview_status.setText("No other state found. Use Add State to choose one.")
        self._sync_apply()

    # -- layout --------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        self.source_label = QLabel(f"From: {self._source_title()}")
        self.source_label.setObjectName("stateSyncSource")
        root.addWidget(self.source_label)

        splitter = QSplitter()
        controls = QWidget()
        column = QVBoxLayout(controls)
        column.setContentsMargins(0, 0, 6, 0)
        self.states = QTreeWidget()
        self.states.setColumnCount(4)
        self.states.setHeaderLabels(["State", "Match", "Changes", "Found in"])
        self.states.setRootIsDecorated(False)
        self.states.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self.states.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index in (1, 2, 3):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        self.states.currentItemChanged.connect(lambda *_: self._show_current())
        self.states.itemChanged.connect(lambda *_: self._sync_apply())
        column.addWidget(self.states, 1)

        self.add_button = QPushButton("Add State")
        self.add_button.setToolTip("Choose a BASE file Studio did not find by itself.")
        self.add_button.clicked.connect(self._choose_state)
        column.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)

        box = QGroupBox("Copy")
        options = QVBoxLayout(box)
        self.textures_check = QCheckBox()
        self.state_textures_check = QCheckBox()
        self.shape_check = QCheckBox()
        self.added_check = QCheckBox()
        self.state_faces_check = QCheckBox()
        self.deleted_check = QCheckBox()
        self.textures_check.setToolTip("Texture, UV and shading of the faces both states have.")
        self.state_textures_check.setToolTip(
            "Faces whose look already differed from this state before the current edits.")
        self.shape_check.setToolTip("Points moved on the faces both states have.")
        self.added_check.setToolTip("Faces added to the open model since it was last saved.")
        self.state_faces_check.setToolTip(
            "Faces saved in the open model that this state never had. They are often "
            "what makes the state different, so check the preview.")
        self.deleted_check.setToolTip("Faces deleted from the open model since it was last saved.")
        for check, checked in ((self.textures_check, True), (self.state_textures_check, False),
                               (self.shape_check, True),
                               (self.added_check, True), (self.state_faces_check, False),
                               (self.deleted_check, True)):
            check.setChecked(checked)
            check.toggled.connect(lambda *_: self._options_changed())
            options.addWidget(check)
        column.addWidget(box)
        splitter.addWidget(controls)

        preview_side = QWidget()
        preview_column = QVBoxLayout(preview_side)
        preview_column.setContentsMargins(6, 0, 0, 0)
        bar = QHBoxLayout()
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        bar.addWidget(self.preview_status, 1)
        self.original_button = QPushButton("Show Original")
        self.original_button.setCheckable(True)
        self.original_button.setToolTip("Compare with the state as it is on disk.")
        self.original_button.toggled.connect(lambda *_: self._show_current())
        bar.addWidget(self.original_button)
        preview_column.addLayout(bar)
        self.preview = AssetViewport()
        self.preview.setMinimumSize(420, 380)
        preview_column.addWidget(self.preview, 1)
        splitter.addWidget(preview_side)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply
                                        | QDialogButtonBox.StandardButton.Close)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

    def _source_title(self):
        path = self.source_family.base_path
        name = getattr(self.source_obj.base_object, "name", "") or ""
        if path is None or Path(path).name.casefold() == "set.bas":
            return name or self.source_obj.display_name
        return Path(path).stem

    # -- finding and loading states ------------------------------------------------

    def _load_object(self, candidate):
        """A fresh copy from disk: the destination is never the open model's memory."""
        window = self.window
        if not candidate.base_name:
            family = load_asset_family(candidate.path, window._extra_roots, setbas=window._setbas)
            return family, first_skeleton_object(family)
        key = str(candidate.path).casefold()
        if key not in self._archives:
            self._archives[key] = load_asset_family(
                candidate.path, window._extra_roots, {},
                window._setbas if window._setbas is not None else candidate.path)
        family = self._archives[key]
        return family, window._resolve_family_base_object(family, candidate.base_name)

    def _saved_source_object(self):
        path = self.source_family.base_path
        if path is None:
            return None
        name = getattr(self.source_obj.base_object, "name", "") or ""
        archive = Path(path).name.casefold() == "set.bas"
        try:
            return self._load_object(StateCandidate("", Path(path), name if archive else ""))[1]
        except _FAILURES:
            return None

    def _discover(self):
        window = self.window
        browser = getattr(window, "entity_browser", None)
        return discover_states(self.source_family, self.source_obj, bootstrap.installation(),
                               getattr(browser, "set_number", None) or 1)

    def _add_states(self, candidates):
        known = {slot.candidate.key for slot in self.slots}
        for candidate in candidates:
            if candidate.key not in known:
                known.add(candidate.key)
                self._add_slot(candidate)
        path = self.source_family.base_path
        if path is None or Path(path).name.casefold() == "set.bas":
            return
        for neighbour in nearby_models(Path(path), known):
            candidate = StateCandidate(state_word(neighbour.stem) or neighbour.stem, neighbour,
                                       found_by=[SIMILAR])
            self._add_slot(candidate, least_share=SIMILAR_SHARE)

    def _add_slot(self, candidate, least_share=0.0):
        try:
            family, fam_obj = self._load_object(candidate)
            if fam_obj is None or fam_obj.skeleton is None:
                return None
            slot = StateSlot(candidate, family, fam_obj)
            slot.plan = build_plan(self.source_obj, fam_obj, self._saved_source)
        except _FAILURES:
            return None
        if slot.plan.match_share < least_share:
            return None
        self.slots.append(slot)
        item = QTreeWidgetItem([candidate.state or candidate.label,
                                f"{slot.plan.match_share:.0%}", "",
                                ", ".join(candidate.found_by)])
        item.setData(0, _SLOT_ROLE, slot)
        item.setToolTip(0, f"{candidate.label}\n{candidate.path}" + (
            f" : {candidate.base_name}" if candidate.base_name else ""))
        self.states.blockSignals(True)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        self.states.addTopLevelItem(item)
        self.states.blockSignals(False)
        self._refresh_row(item)
        return item

    def _choose_state(self):
        window = self.window
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a state of this model", str(window._game_folder()),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)")
        if not path:
            return
        candidate = merge_states([StateCandidate(
            state_word(Path(path).stem) or Path(path).stem, Path(path), found_by=[FOLDER])])[0]
        if any(slot.candidate.key == candidate.key for slot in self.slots):
            return
        item = self._add_slot(candidate)
        if item is None:
            QMessageBox.information(self, "Add State", "This file has no model to compare.")
        else:
            self.states.setCurrentItem(item)

    # -- options and preview -------------------------------------------------------

    def options(self):
        return SyncOptions(textures=self.textures_check.isChecked(),
                           state_textures=self.state_textures_check.isChecked(),
                           shape=self.shape_check.isChecked(),
                           added_faces=self.added_check.isChecked(),
                           state_faces=self.state_faces_check.isChecked(),
                           deleted_faces=self.deleted_check.isChecked())

    def _current_slot(self):
        item = self.states.currentItem()
        return item.data(0, _SLOT_ROLE) if item is not None else None

    def _refresh_row(self, item):
        slot = item.data(0, _SLOT_ROLE)
        counts = slot.plan.counts(self.options())
        parts = []
        if counts["textures"]:
            parts.append(f"{counts['textures']} textures")
        if counts["points"]:
            parts.append(f"{counts['points']} points")
        if counts["added"]:
            parts.append(f"+{counts['added']} faces")
        if counts["removed"]:
            parts.append(f"-{counts['removed']} faces")
        item.setText(2, ", ".join(parts) or (
            "Already the same" if slot.plan.links else "Different model"))
        problem = "" if slot.candidate.writable else "Inside SET.BAS: preview only"
        item.setToolTip(2, problem)
        locked = not slot.candidate.writable or not slot.plan.has_work(self.options())
        self.states.blockSignals(True)
        if locked:
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        else:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.states.blockSignals(False)

    def _refresh_option_labels(self):
        slot = self._current_slot()
        plan = slot.plan if slot is not None else None
        def count(name):
            return len(getattr(plan, name)) if plan is not None else 0
        self.textures_check.setText(f"Textures and UV ({count('material_changes')})")
        self.state_textures_check.setText(f"Other texture differences ({count('state_materials')})")
        self.shape_check.setText(f"Moved points ({count('moved_points')})")
        self.added_check.setText(f"Faces added here ({count('added_faces')})")
        self.state_faces_check.setText(f"Faces only this model has ({count('state_faces')})")
        self.deleted_check.setText(f"Faces deleted here ({count('deleted_faces')})")

    def _options_changed(self):
        for index in range(self.states.topLevelItemCount()):
            self._refresh_row(self.states.topLevelItem(index))
        self._show_current()
        self._sync_apply()

    def _show_current(self):
        slot = self._current_slot()
        self._refresh_option_labels()
        if slot is None:
            self.preview.clear()
            return
        try:
            if self.original_button.isChecked():
                slot.reset()
                note = "Original"
            else:
                lm = slot.edited_model(self.source_family, self.source_obj, self.options())
                note = "With changes"
                if lm.warnings:
                    note += f" · {lm.warnings[0]}"
            scope = ({slot.fam_obj.owner_path} if slot.candidate.base_name else None)
            self.preview.load_family(slot.family, visible_owners=scope, keep_camera=True,
                                     primary_owner=slot.fam_obj.owner_path)
            self.preview_status.setText(f"{slot.candidate.label} · {note}")
        except _FAILURES as exc:
            self.preview.clear()
            self.preview_status.setText(f"Preview unavailable: {exc}")

    # -- apply ---------------------------------------------------------------------

    def _chosen_slots(self):
        items = (self.states.topLevelItem(i) for i in range(self.states.topLevelItemCount()))
        return [item.data(0, _SLOT_ROLE) for item in items
                if item.checkState(0) == Qt.CheckState.Checked]

    def _sync_apply(self):
        chosen = self._chosen_slots()
        self.apply_button.setEnabled(bool(chosen))
        self.apply_button.setText(f"Apply to {len(chosen)} State{'s' if len(chosen) != 1 else ''}"
                                  if chosen else "Apply")

    def _apply(self):
        chosen = self._chosen_slots()
        options = self.options()
        written = []
        try:
            # Every state is prepared and checked first: one bad state stops them all
            # before anything has been written.
            models = []
            for slot in chosen:
                lm = slot.edited_model(self.source_family, self.source_obj, options, writable=True)
                lm.extra_files.update(texture_files(lm, slot.plan, self.source_family,
                                                    self.source_obj, options))
                lm._final_checks()
                models.append((slot, lm))
            for slot, lm in models:
                backup_existing_file(lm.base_path)
                backup_existing_file(lm.sklt_path)
                lm.commit("Apply changes from another state")
                written.append(slot.candidate.label)
        except _FAILURES as exc:
            done = f"\n\nAlready written: {', '.join(written)}" if written else ""
            QMessageBox.critical(self, "Apply failed", f"{exc}{done}")
            return
        finally:
            DirectoryIndex.clear_cache()
        self.window.statusBar().showMessage(
            f"Changes applied to {', '.join(written)}.", 10000)
        self.accept()
