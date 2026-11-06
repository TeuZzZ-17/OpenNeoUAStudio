"""Guided import of a complete model: BASE plus its resource family.

The dialog lists every skeleton, texture and animation referenced by the
selected BASE.  Each row resolves live against loose files and the optional
SET.BAS archive, so the user sees what is ready before importing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from studio.assets.family import (
    FamilyRequirementProbe,
    probe_family_requirements,
)
from studio.exchange.portable import normalize_logical_name
from studio.formats.setbas import (
    read_setbas,
    SetBasArchive,
    SetBasError,
)
from studio.ui.widgets import STATUS_COLORS, loading_resources

_REQUIREMENT_ROLE = Qt.ItemDataRole.UserRole + 71

_KIND_LABELS = {
    "skeleton": "Skeleton",
    "texture": "Texture",
    "animation": "Animation",
}

_RESOURCE_FILTER = (
    "Model resources (*.sklt *.skl *.ilbm *.ilb *.lbm *.iff *.vbmp *.png "
    "*.anm *.vanm *.SKLT *.SKL *.ILBM *.ILB *.LBM *.IFF *.VBMP *.PNG "
    "*.ANM *.VANM);;All files (*)"
)

_STATUS_LABELS = {
    "loose": "Ready (loose)",
    "manual": "Ready (chosen)",
    "setbas": "Ready (SET.BAS)",
    "ambiguous": "Ambiguous",
    "missing": "Missing",
}


@dataclass(frozen=True)
class ModelImportSelection:
    """Everything the workbench needs to load the assembled model."""

    base_path: Path
    archive: SetBasArchive | None
    overrides: dict[str, str]
    search_roots: list[Path]


class ModelImportDialog(QDialog):
    """Choose a BASE and inspect its skeleton, textures and animations.

    Rows turn green when a resource is resolved (loose file, manual choice or
    SET.BAS), yellow when the loose search is ambiguous and red when nothing
    can supply it.
    """

    def __init__(
            self,
            parent=None,
            *,
            initial_base: str | Path | None = None,
            initial_archive: SetBasArchive | None = None,
            initial_directory: str | Path | None = None,
            extra_roots: list[str | Path] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Model")
        self.resize(980, 640)
        self.setMinimumSize(760, 480)
        self._directory = Path(initial_directory or Path.home())
        self._base_path: Path | None = None
        self._base_error = ""
        self._archive = initial_archive
        self._probes: list[FamilyRequirementProbe] = []
        # logical name (casefold) -> Path or "setbas"
        self._choices: dict[str, Path | str] = {}
        # Root folders already used by the workbench, plus the ones the user
        # adds for this import only.
        self._extra_roots = [Path(root) for root in (extra_roots or [])]
        self._search_roots: list[Path] = []

        self._build_ui()
        if initial_archive is not None:
            self.archive_edit.setText(str(initial_archive.path))
            self.archive_edit.setToolTip(str(initial_archive.path))
            self._update_archive_summary()
        if initial_base is not None:
            self.set_base_path(initial_base)

    # -- public ---------------------------------------------------------------

    def set_base_path(self, path: str | Path) -> bool:
        """Parse *path* and rebuild the resource table."""

        base = Path(path)
        self._base_path = base
        self.base_edit.setText(str(base))
        self.base_edit.setToolTip(str(base))
        self._directory = base.parent
        self._choices.clear()
        self._search_roots.clear()
        return self._reprobe()

    def set_archive(self, archive: SetBasArchive | None) -> None:
        if archive is not self._archive:
            # "Use SET.BAS" choices belong to the archive they were made on.
            self._choices = {
                name: choice for name, choice in self._choices.items()
                if choice != "setbas"}
        self._archive = archive
        if archive is None:
            self.archive_edit.clear()
            self.archive_edit.setToolTip("")
        else:
            self.archive_edit.setText(str(archive.path))
            self.archive_edit.setToolTip(str(archive.path))
        self._update_archive_summary()
        self._reprobe()

    def selection(self) -> ModelImportSelection:
        if self._base_path is None:
            raise ValueError("no model BASE selected")
        overrides = {
            name: (f"setbas:{name}" if choice == "setbas" else str(choice))
            for name, choice in self._choices.items()
        }
        return ModelImportSelection(
            base_path=self._base_path,
            archive=self._archive,
            overrides=overrides,
            search_roots=list(self._search_roots),
        )

    # -- UI -------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Choose the model BASE, then check the resources below. Loose "
            "files beside the BASE win; anything missing can come from the "
            "selected SET.BAS archive.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        base_row = QHBoxLayout()
        base_label = QLabel("Model BASE")
        base_label.setMinimumWidth(112)
        self.base_edit = QLineEdit()
        self.base_edit.setReadOnly(True)
        self.base_edit.setPlaceholderText("Select a .BASE / .BAS entry point")
        self.base_button = QPushButton("Browse…")
        self.base_button.clicked.connect(self._browse_base)
        self.base_state = QLabel("—")
        self.base_state.setMinimumWidth(70)
        base_row.addWidget(base_label)
        base_row.addWidget(self.base_edit, 1)
        base_row.addWidget(self.base_button)
        base_row.addWidget(self.base_state)
        layout.addLayout(base_row)

        archive_row = QHBoxLayout()
        archive_label = QLabel("SET.BAS resources")
        archive_label.setMinimumWidth(112)
        self.archive_edit = QLineEdit()
        self.archive_edit.setReadOnly(True)
        self.archive_edit.setPlaceholderText(
            "Optional: select a SET.BAS archive to load missing resources (skeletons, textures, animations)")
        self.archive_button = QPushButton("Browse…")
        self.archive_button.clicked.connect(self._browse_archive)
        self.archive_clear_button = QPushButton("Clear")
        self.archive_clear_button.clicked.connect(self._clear_archive)
        archive_row.addWidget(archive_label)
        archive_row.addWidget(self.archive_edit, 1)
        archive_row.addWidget(self.archive_button)
        archive_row.addWidget(self.archive_clear_button)
        layout.addLayout(archive_row)

        self.archive_summary = QLabel(
            "No SET.BAS archive selected. If external resources are missing, "
            "select a SET.BAS archive above to load them automatically.")
        self.archive_summary.setWordWrap(True)
        layout.addWidget(self.archive_summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            ["Component", "Resource", "Status", "Source", "Path"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 100)
        self.tree.setColumnWidth(1, 250)
        self.tree.setColumnWidth(2, 120)
        self.tree.setColumnWidth(3, 135)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(
            lambda _item, _column: self._choose_source())
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.source_button = QPushButton("Choose source…")
        self.source_button.clicked.connect(self._choose_source)
        self.files_button = QPushButton("Add loose files…")
        self.files_button.clicked.connect(self._add_loose_files)
        self.folder_button = QPushButton("Add search folder…")
        self.folder_button.clicked.connect(self._add_search_folder)
        self.reset_button = QPushButton("Reset choices")
        self.reset_button.clicked.connect(self._reset_choices)
        for button in (
                self.source_button, self.files_button,
                self.folder_button, self.reset_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.summary = QLabel("Select a model BASE to inspect its resources.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.import_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Ok)
        self.import_button.setText("Import Model")
        self.import_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._sync_buttons()

    def _browse_base(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select the model BASE", str(self._directory),
            "Urban Assault BASE (*.base *.bas *.BASE *.BAS);;All files (*)")
        if not path:
            return
        if Path(path).name.casefold() == "set.bas":
            QMessageBox.information(
                self, "SET.BAS is not a loose model",
                "SET.BAS is a resource archive. Use File > Import BAS "
                "Archive to browse it, or select the standalone BASE file "
                "of the model.")
            return
        self.set_base_path(path)

    def _browse_archive(self) -> None:
        start = (self._archive.path.parent
                 if self._archive is not None else self._directory)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a SET.BAS resource archive", str(start),
            "SET.BAS archives (SET.BAS *.bas *.BAS);;All files (*)")
        if path:
            self._load_archive(Path(path))

    def _load_archive(self, path: Path) -> None:
        with loading_resources(
                self, "Loading SET.BAS resources. Please wait…"):
            try:
                archive = read_setbas(path)
            except SetBasError as exc:
                QMessageBox.warning(
                    self, "SET.BAS not loaded",
                    f"The archive was not loaded.\n\n{exc}")
                return
        self._directory = path.parent
        self.set_archive(archive)

    def _clear_archive(self) -> None:
        self.set_archive(None)

    def _update_archive_summary(self) -> None:
        archive = self._archive
        if archive is None:
            self.archive_summary.setText(
                "No SET.BAS archive selected. If external resources are missing, "
                "select a SET.BAS archive above to load them automatically.")
            return
        census = archive.census()
        parts = ", ".join(
            f"{count} {class_id}" for class_id, count in census.items())
        self.archive_summary.setText(
            f"{archive.path.name}: {len(archive.resources)} resource(s)"
            + (f" ({parts})" if parts else "")
            + ". Loose files still win; missing resources will be loaded from this archive.")

    # -- resolution -----------------------------------------------------------

    def _all_search_roots(self) -> list[Path]:
        return [*self._extra_roots, *self._search_roots]

    def _override_map(self) -> dict[str, Path | str]:
        return {
            name: (f"setbas:{name}" if choice == "setbas" else Path(choice))
            for name, choice in self._choices.items()
        }

    @staticmethod
    def _choice_key(name: str) -> str:
        return normalize_logical_name(name).casefold()

    def _reprobe(self) -> bool:
        if self._base_path is None:
            self._probes = []
            self._base_error = ""
            self._fill_tree()
            return False
        try:
            _base_asset, probes = probe_family_requirements(
                self._base_path,
                self._all_search_roots(),
                self._override_map(),
                self._archive,
            )
        except Exception as exc:  # parse errors must stay inside the dialog
            self._probes = []
            self._base_error = str(exc)
            self._fill_tree()
            return False
        self._probes = probes
        self._base_error = ""
        self._fill_tree()
        return True

    def _fill_tree(self) -> None:
        selected = {
            item.data(0, _REQUIREMENT_ROLE).requirement.logical_name
            for item in self.tree.selectedItems()
            if item.data(0, _REQUIREMENT_ROLE) is not None
        }
        self.tree.clear()

        if self._base_path is None:
            self._set_base_state("—", None)
            self.summary.setText(
                "Select a model BASE to inspect its resources.")
            self.summary.setStyleSheet("")
            self.import_button.setEnabled(False)
            self._sync_buttons()
            return

        if self._base_error:
            self._set_base_state("Invalid", "missing")
            self.summary.setText(
                f"{self._base_path.name}: {self._base_error}")
            self.summary.setStyleSheet(
                f"color: {STATUS_COLORS['missing'].name()};")
            self.import_button.setEnabled(False)
            self._sync_buttons()
            return

        self._set_base_state("Ready", "found")
        for probe in self._probes:
            requirement = probe.requirement
            source, path_text = self._source_text(probe)
            item = QTreeWidgetItem([
                _KIND_LABELS.get(requirement.kind, requirement.kind),
                requirement.logical_name,
                _STATUS_LABELS.get(probe.status, probe.status),
                source,
                path_text,
            ])
            item.setData(0, _REQUIREMENT_ROLE, probe)
            item.setToolTip(2, probe.detail)
            item.setToolTip(4, path_text)
            self._decorate(item, probe)
            self.tree.addTopLevelItem(item)
            if requirement.logical_name in selected:
                item.setSelected(True)

        total = len(self._probes)
        ready = sum(1 for probe in self._probes if probe.resolved)
        missing = sum(
            1 for probe in self._probes if probe.status == "missing")
        ambiguous = sum(
            1 for probe in self._probes if probe.status == "ambiguous")
        from_setbas = sum(
            1 for probe in self._probes if probe.status == "setbas")
        text = (
            f"{ready}/{total} resources ready"
            f" · {from_setbas} from SET.BAS"
            f" · {missing} missing · {ambiguous} ambiguous")
        if missing and self._archive is None:
            text += " — Select a SET.BAS archive above to load missing resources."
        if missing or ambiguous:
            key = "ambiguous" if ambiguous else "missing"
            self.summary.setStyleSheet(
                f"color: {STATUS_COLORS[key].name()};")
        else:
            self.summary.setStyleSheet(
                f"color: {STATUS_COLORS['found'].name()};")
        self.summary.setText(text)
        self.import_button.setEnabled(self._base_path.is_file())
        self._sync_buttons()

    def _set_base_state(self, text: str, status: str | None) -> None:
        self.base_state.setText(text)
        if status is None:
            self.base_state.setStyleSheet("")
        else:
            self.base_state.setStyleSheet(
                f"color: {STATUS_COLORS[status].name()}; font-weight: 600;")

    @staticmethod
    def _source_text(probe: FamilyRequirementProbe) -> tuple[str, str]:
        if probe.status == "loose":
            return "Loose file", str(probe.path or "")
        if probe.status == "manual":
            return "File chosen", str(probe.path or "")
        if probe.status == "setbas":
            return (
                "SET.BAS",
                probe.embedded_names[0] if probe.embedded_names else "")
        if probe.status == "ambiguous":
            return (
                "Several matches",
                "; ".join(probe.embedded_names) or probe.detail)
        return "Not found", ""

    @staticmethod
    def _decorate(item: QTreeWidgetItem,
                  probe: FamilyRequirementProbe) -> None:
        # Every resolved row is green, whichever source supplies it; the
        # Source column keeps loose files, manual choices and SET.BAS apart.
        color_key = {
            "loose": "found",
            "manual": "found",
            "setbas": "found",
            "ambiguous": "ambiguous",
            "missing": "missing",
        }.get(probe.status, "missing")
        color = QColor(STATUS_COLORS[color_key])
        item.setForeground(2, color)
        background = QColor(color)
        background.setAlpha(42)
        for column in range(5):
            item.setBackground(column, background)

    # -- row actions ----------------------------------------------------------

    def _selected_probes(self) -> list[FamilyRequirementProbe]:
        return [
            item.data(0, _REQUIREMENT_ROLE)
            for item in self.tree.selectedItems()
            if item.data(0, _REQUIREMENT_ROLE) is not None
        ]

    def _sync_buttons(self) -> None:
        has_selection = bool(self.tree.selectedItems())
        self.source_button.setEnabled(has_selection)

    def _choose_source(self) -> None:
        probes = self._selected_probes()
        if not probes:
            return
        menu = QMenu(self)
        automatic = menu.addAction("Automatic (loose file, then SET.BAS)")
        use_setbas = menu.addAction("Use SET.BAS")
        choose_file = menu.addAction(
            "Choose file…" if len(probes) == 1 else "Choose files…")
        use_setbas.setEnabled(
            self._archive is not None
            and any(probe.embedded_names for probe in probes))
        action = menu.exec(
            self.source_button.mapToGlobal(
                self.source_button.rect().bottomLeft()))
        if action is automatic:
            for probe in probes:
                self._choices.pop(
                    self._choice_key(probe.requirement.logical_name), None)
            self._reprobe()
        elif action is use_setbas:
            for probe in probes:
                if probe.embedded_names:
                    self._choices[self._choice_key(
                        probe.requirement.logical_name)] = "setbas"
            self._reprobe()
        elif action is choose_file:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Choose model resources", str(self._directory),
                _RESOURCE_FILTER)
            if paths:
                self._directory = Path(paths[0]).parent
                self._assign_paths([Path(path) for path in paths], probes)
                self._reprobe()

    def _assign_paths(
            self, paths: list[Path],
            probes: list[FamilyRequirementProbe]) -> list[str]:
        """Bind files to rows by name, then to the selected rows in order."""

        remaining = list(paths)
        targets = list(probes)
        unmatched: list[str] = []

        if len(remaining) == 1 and targets:
            self._set_file_choice(targets[0], remaining.pop(0))
            return unmatched

        for path in list(remaining):
            match = self._probe_for_file(path, self._probes)
            if match is not None:
                self._set_file_choice(match, path)
                remaining.remove(path)
                if match in targets:
                    targets.remove(match)

        for path in list(remaining):
            if not targets:
                unmatched.append(path.name)
                continue
            self._set_file_choice(targets.pop(0), path)
            remaining.remove(path)
        return unmatched

    def _set_file_choice(
            self, probe: FamilyRequirementProbe, path: Path) -> None:
        self._choices[self._choice_key(
            probe.requirement.logical_name)] = Path(path)

    @staticmethod
    def _probe_for_file(
            path: Path,
            probes: list[FamilyRequirementProbe]) -> FamilyRequirementProbe | None:
        name = path.name.casefold()
        stem = path.stem.casefold()
        bare_names = [
            normalize_logical_name(probe.requirement.logical_name)
            .rsplit("/", 1)[-1].casefold()
            for probe in probes]
        for probe, bare in zip(probes, bare_names):
            if bare == name:
                return probe
        for probe, bare in zip(probes, bare_names):
            if Path(bare).stem.casefold() == stem:
                return probe
        return None

    def _add_loose_files(self) -> None:
        if self._base_path is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add loose model resources", str(self._directory),
            _RESOURCE_FILTER)
        if not paths:
            return
        unmatched: list[str] = []
        for raw in paths:
            path = Path(raw)
            probe = self._probe_for_file(path, self._probes)
            if probe is None:
                unmatched.append(path.name)
            else:
                self._set_file_choice(probe, path)
        self._directory = Path(paths[0]).parent
        self._reprobe()
        if unmatched:
            QMessageBox.information(
                self, "Resources not linked",
                "These files do not match any resource referenced by the "
                "selected BASE, so they were ignored:\n\n"
                + "\n".join(sorted(unmatched)))

    def _add_search_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Add a search folder", str(self._directory))
        if not directory:
            return
        path = Path(directory)
        if path not in self._search_roots:
            self._search_roots.append(path)
        self._directory = path
        self._reprobe()

    def _reset_choices(self) -> None:
        self._choices.clear()
        self._search_roots.clear()
        self._reprobe()

    # -- accept ---------------------------------------------------------------

    def accept(self) -> None:
        if self._base_path is None or not self._base_path.is_file():
            QMessageBox.information(
                self, "Select a model BASE",
                "Choose the .BASE entry point of the model before importing.")
            return
        if self._base_error:
            QMessageBox.warning(
                self, "Invalid model BASE",
                f"{self._base_path.name} cannot be parsed:\n\n"
                f"{self._base_error}")
            return
        super().accept()
