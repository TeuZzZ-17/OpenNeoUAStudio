"""Editable native set tables and verified set-folder/ZIP output."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import shutil
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

from map_editor import bootstrap
from map_editor.core.set_sdf_parser import (
    SetSdf, SdfBuilding, SdfBuildingModel, SdfSector, decode_sdf,
    parse_set_sdf_text, serialize_set_sdf,
)
from studio.exchange.portable import commit_verified_files
from studio.formats.sector_file import (
    MODEL_ICON_ZOOMS, SECTOR_ICON_ZOOMS, SECTORS_FOLDER, SectorFile, SectorModel, SectorPiece,
    find_sector_file, format_sector, read_sector, resolve_data_path, resolve_sector_file, unique_name,
)

SET_FOLDERS = ("HI", "Objects", "Palette", "Remap", "Scripts")
SECTOR_LIMIT = 256
MODEL_LIMIT = 4096
BUILDING_LIMIT = 4096
# Field value meaning "choose it for me" (connection family, map symbol).
AUTO = -1


@dataclass(frozen=True)
class CellClipboard:
    tables: SetSdf
    cells: tuple[int, ...]


def table_issues(sdf: SetSdf) -> list[str]:
    """Check native references before a set can be written or exported."""
    # Some retail rows contain an incomplete trailing FX group. Retain that
    # original row verbatim; the reader already exposes its warning to the UI.
    issues = [w for w in sdf.warnings if "effect values must be a multiple of 4" not in w]
    if not sdf.models or not sdf.buildings:
        issues.append("A set needs at least one model and one building state row.")
    if len(sdf.models) > MODEL_LIMIT:
        issues.append(f"Too many models: {len(sdf.models)} / {MODEL_LIMIT}.")
    if len(sdf.buildings) > BUILDING_LIMIT:
        issues.append(f"Too many building state rows: {len(sdf.buildings)} / {BUILDING_LIMIT}.")
    for index, model in enumerate(sdf.models):
        if model.id != index or len(model.numbers) != 4:
            issues.append(f"Model {index}: invalid positional ID or numeric fields.")
        elif not (0 <= model.numbers[1] <= 255 and 0 <= model.numbers[2] <= 100 and model.numbers[3] in (0, 1, 2)):
            issues.append(f"Model {index}: symbol must be 0–255, shield 0–100 and collision mode 0/1/2.")
        if any(not value or re.search(r"[\s;#>\x00]", value) for value in (model.base, model.sklt)):
            issues.append(f"Model {index}: BASE and collision names must be single SDF tokens.")
        if any(Path(value.replace("\\", "/")).is_absolute() or ":" in value
               or ".." in Path(value.replace("\\", "/")).parts for value in (model.base, model.sklt)):
            issues.append(f"Model {index}: resource names must stay relative to this set.")
        if any(len(effect) != 4 for effect in model.effects):
            issues.append(f"Model {index}: effects require groups of four integers.")
        if "\n" in model.comment or "\r" in model.comment:
            issues.append(f"Model {index}: comments must use one line.")
    for index, building in enumerate(sdf.buildings):
        if building.id != index:
            issues.append(f"Building {index}: positional ID changed.")
        for state in range(4):
            if not 0 <= building.state(state) < len(sdf.models):
                issues.append(f"Building {index}, state {state}: model does not exist.")
        if building.destructible not in (0, 1):
            issues.append(f"Building {index}: destructible must be 0 or 1.")
    for number, sector in sdf.sectors.items():
        if not 0 <= number < SECTOR_LIMIT or sector.number != number:
            issues.append(f"Sector {number}: ID must be between 0 and 255.")
        if sector.kind not in (0, 1) or len(sector.subsects) != (1 if sector.single else 9):
            issues.append(f"Sector {number}: choose one cell or a 3 × 3 grid.")
        if not 0 <= sector.ground <= 5 or not 0 <= sector.symbol <= 255:
            issues.append(f"Sector {number}: ground must be 0–5 and symbol 0–255.")
        for building in sector.subsects:
            if not 0 <= building < len(sdf.buildings):
                issues.append(f"Sector {number}: building {building} does not exist.")
        if "\n" in sector.comment or "\r" in sector.comment:
            issues.append(f"Sector {number}: comments must use one line.")
    return issues


class SetDocument:
    def __init__(self, folder: Path, number: int, sdf: SetSdf, source: bytes):
        self.folder = Path(folder)
        self.number = number
        self.sdf = sdf
        self.source = source
        scripts = bootstrap.find_ci(self.folder, "Scripts")
        self.override_path = (bootstrap.find_ci(scripts, "override.lst") or scripts / "override.lst") if scripts else self.folder / "Scripts/override.lst"
        self.override_source = self.override_path.read_bytes() if self.override_path.is_file() else None
        if self.override_source:
            for line in decode_sdf(self.override_source).splitlines():
                tokens = line.split(";", 1)[0].split()
                if len(tokens) >= 3 and tokens[0].casefold() == "lego":
                    try:
                        index = int(tokens[1])
                        if 0 <= index < len(sdf.models):
                            sdf.models[index] = replace(sdf.models[index], base=tokens[2])
                    except ValueError:
                        pass
        self.saved = deepcopy(sdf)
        # Rows created by File > Import Sector File. Unused ones are given back before a save;
        # rows whose BASE or collision lives in the sector's own folder (external_own) can be
        # edited and exported again, but never saved into this set.
        self.external_models: set[int] = set()
        self.external_buildings: set[int] = set()
        self.external_own: set[int] = set()
        # Map icons of external sectors (map_icon): ("model", id) / ("sector", number) ->
        # files, 1 for every zoom or one per zoom. Kept for editing and export only;
        # they never enter SET.SDF (the vanilla map_symbol stays there).
        self.map_icons: dict[tuple[str, int], tuple[Path, ...]] = {}
        self._icon_folder: Path | None = None
        self.embedded_names = None
        self.assets = None
        self._asset_check = None
        self._undo = []
        self._redo = []

    @classmethod
    def open(cls, folder: str | Path):
        folder = Path(folder).resolve()
        if folder.is_file():
            folder = folder.parent.parent
        match = re.fullmatch(r"Set(\d+)", folder.name, re.IGNORECASE)
        number = int(match[1]) if match else 1
        scripts = bootstrap.find_ci(folder, "Scripts")
        path = bootstrap.find_ci(scripts, "SET.SDF") if scripts else None
        if path is None:
            raise FileNotFoundError(f"Scripts/SET.SDF is missing in {folder}")
        source = path.read_bytes()
        return cls(folder, number, parse_set_sdf_text(decode_sdf(source), path), source)

    @property
    def dirty(self):
        return self.sdf != self.saved

    def change(self, label, operation):
        """Record one complete table edit; failed edits leave the document intact."""
        before = deepcopy(self.sdf)
        try:
            result = operation()
        except Exception:
            self.sdf = before
            raise
        if before != self.sdf:
            self._undo.append((label, before, deepcopy(self.sdf)))
            self._redo.clear()
        return result

    def undo(self):
        if self._undo:
            edit = self._undo.pop()
            self._redo.append(edit)
            self.sdf = deepcopy(edit[1])

    def redo(self):
        if self._redo:
            edit = self._redo.pop()
            self._undo.append(edit)
            self.sdf = deepcopy(edit[2])

    def free_sector(self):
        # Border types 248–255 have a fixed purpose in native maps.
        return next((i for i in range(1, 248) if i not in self.sdf.sectors), None)

    def add_sector(self, source: int | None = None, number: int | None = None):
        number = self.free_sector() if number is None else number
        if number is None:
            raise ValueError("No free ordinary sector slot remains (1–247).")
        if not 0 <= number < SECTOR_LIMIT or number in self.sdf.sectors:
            raise ValueError("Choose an unused sector ID from 0 to 255.")
        sector = (replace(self.sdf.sectors[source], number=number)
                  if source is not None else SdfSector(number, 0, 0, 1, (0,) * 9))
        self.change("Duplicate sector" if source is not None else "New sector",
                    lambda: self.sdf.sectors.__setitem__(number, sector))
        return number

    def _existing_sectors(self, numbers):
        try:
            selected = tuple(dict.fromkeys(numbers))
        except TypeError as error:
            raise ValueError("Choose existing sector IDs.") from error
        if not selected or any(type(n) is not int or n not in self.sdf.sectors for n in selected):
            raise ValueError("Choose existing sector IDs.")
        return selected

    def wipe_sectors(self, numbers):
        """Remove whole definitions; their slots become free."""
        selected = self._existing_sectors(numbers)
        self.change("Wipe sector", lambda: [self.sdf.sectors.pop(n) for n in selected])

    def clear_sectors(self, numbers):
        """Keep each definition and layout, but put building 0 in every cell."""
        selected = self._existing_sectors(numbers)

        def apply():
            for n in selected:
                sector = self.sdf.sectors[n]
                self.sdf.sectors[n] = replace(sector, subsects=(0,) * len(sector.subsects))
        self.change("Clear sector", apply)

    def paste_sectors(self, sectors, target, *, remove=()):
        """Place copied definitions at ``target``, keeping their relative slot order.

        Occupied slots are overwritten. Cut sources not covered by the paste
        become free slots in the same undo step.
        """
        first = min(sector.number for sector in sectors)
        placed = {target + sector.number - first: sector for sector in sectors}
        if any(not 0 <= n < SECTOR_LIMIT for n in placed):
            raise ValueError("The copied sectors do not fit after this slot (last ID is 255).")
        if any(not 0 <= b < len(self.sdf.buildings) for s in sectors for b in s.subsects):
            raise ValueError("The copied sectors use buildings that no longer exist.")

        def apply():
            for n in remove:
                if n not in placed:
                    self.sdf.sectors.pop(n, None)
            for n, sector in placed.items():
                self.sdf.sectors[n] = replace(sector, number=n)
        self.change("Move sectors" if remove else "Paste sectors", apply)
        return sorted(placed)

    def reset_sectors(self, *, saved=False):
        """Empty the sector list, or return the whole set to its last saved tables."""
        def apply():
            if saved:
                self.sdf = deepcopy(self.saved)
            else:
                self.sdf.sectors.clear()
        self.change("Restore saved set" if saved else "Reset set", apply)

    def suggest_fields(self, number):
        """Pick connection family and map symbol from the most similar sector.

        Similar means the same buildings in the cells, then the same model
        families (the first two letters of the intact BASE, as ST/GR/GS).
        Border slots 248-255 are never used as examples.
        """
        sector = self.sdf.sectors[number]

        def profile(row):
            cells = row.subsects * 9 if row.single else row.subsects
            families = []
            for bid in cells:
                mid = self.sdf.buildings[bid].intact if 0 <= bid < len(self.sdf.buildings) else -1
                families.append(self.sdf.models[mid].base[:2].casefold() if 0 <= mid < len(self.sdf.models) else "")
            return Counter(cells), Counter(families)

        mine = profile(sector)
        scored = []
        for other_number, other in self.sdf.sectors.items():
            if other_number == number or other_number >= 248:
                continue
            theirs = profile(other)
            score = 2 * sum((mine[0] & theirs[0]).values()) + sum((mine[1] & theirs[1]).values())
            if score:
                scored.append((other.kind == sector.kind, score, other))
        if not scored:
            return sector.ground, sector.symbol
        best = max(scored, key=lambda row: row[:2])[:2]
        examples = [row[2] for row in scored if row[:2] == best]
        ground = Counter(s.ground for s in examples).most_common(1)[0][0]
        symbol = Counter(s.symbol for s in examples).most_common(1)[0][0]
        return ground, symbol

    def set_layout(self, number, single):
        sector = self.sdf.sectors[number]
        # Converting to a large cell keeps the center piece; undo restores all nine.
        cells = (sector.subsects[0],) * 9 if sector.single else sector.subsects
        subs = (cells[4],) if single else cells
        self.change("Change layout", lambda: self.sdf.sectors.__setitem__(
            number, replace(sector, kind=int(single), subsects=subs)))

    def set_sector_fields(self, numbers, *, ground=None, symbol=None):
        """Set connection family and/or map symbol of several sectors in one step.

        AUTO picks each sector's own value from the most similar sector
        (suggest_fields). None leaves that field unchanged.
        """
        selected = self._existing_sectors(numbers)
        if ground is not None and ground != AUTO and (type(ground) is not int or not 0 <= ground <= 5):
            raise ValueError("Connection family must be between 0 and 5.")
        if symbol is not None and symbol != AUTO and (type(symbol) is not int or not 0 <= symbol <= 255):
            raise ValueError("Map symbol must be between 0 and 255.")

        def apply():
            for number in selected:
                sector = self.sdf.sectors[number]
                auto = self.suggest_fields(number) if AUTO in (ground, symbol) else None
                self.sdf.sectors[number] = replace(
                    sector,
                    ground=sector.ground if ground is None else auto[0] if ground == AUTO else ground,
                    symbol=sector.symbol if symbol is None else auto[1] if symbol == AUTO else symbol)
        self.change("Sector properties", apply)

    def relocate_cells(self, number, source_cells, target_cell, *, move=False):
        """Move or duplicate a selected cell pattern within one 3x3 sector."""
        if type(number) is not int or number not in self.sdf.sectors:
            raise ValueError("Choose an existing sector.")
        if type(move) is not bool:
            raise ValueError("Choose whether to move or duplicate the cells.")
        if type(target_cell) is not int or not 0 <= target_cell < 9:
            raise ValueError("Choose a target cell in the 3 × 3 sector.")
        try:
            sources = frozenset(source_cells)
        except TypeError as error:
            raise ValueError("Choose one or more valid source cells.") from error
        if (not sources or any(type(cell) is not int or not 0 <= cell < 9
                               for cell in sources)):
            raise ValueError("Choose one or more valid source cells.")

        sector = self.sdf.sectors[number]
        if sector.single:
            if sources != {4} or target_cell != 4:
                raise ValueError("A single-cell sector only allows cell 4.")
            destinations = {4}
            transfers = {0: 0}
        else:
            anchor = min(sources)
            anchor_col, anchor_row = anchor % 3, anchor // 3
            target_col, target_row = target_cell % 3, target_cell // 3
            transfers = {}
            for source in sources:
                col, row = source % 3, source // 3
                dest_col = target_col + col - anchor_col
                dest_row = target_row + row - anchor_row
                if not (0 <= dest_col < 3 and 0 <= dest_row < 3):
                    raise ValueError("The selected cell pattern must fit inside the 3x3 sector.")
                transfers[source] = dest_row * 3 + dest_col
            destinations = set(transfers.values())

        original = tuple(sector.subsects)

        def apply():
            values = list(original)
            if move:
                for source in sources - destinations:
                    values[source] = 0
            for source, destination in transfers.items():
                values[destination] = original[source]
            self.sdf.sectors[number] = replace(sector, subsects=tuple(values))

        self.change("Move sector cells" if move else "Duplicate sector cells", apply)
        return destinations

    def assign_building(self, number, cells, building):
        if not 0 <= building < len(self.sdf.buildings):
            raise ValueError("Choose an existing building state row.")
        sector = self.sdf.sectors[number]
        values = list(sector.subsects)
        for cell in cells:
            values[0 if sector.single else cell] = building
        self.change("Replace cells", lambda: self.sdf.sectors.__setitem__(
            number, replace(sector, subsects=tuple(values))))

    def assign_states(self, number, cells, states, destructible):
        """Assign one independent four-state row without changing shared buildings."""
        states = tuple(states)
        if len(states) != 4 or any(mid is None or not 0 <= mid < len(self.sdf.models) for mid in states):
            raise ValueError("Choose an existing model for each of the four damage states.")
        if destructible not in (0, 1):
            raise ValueError("Destructible must be 0 or 1.")
        def apply():
            row = self._building_slot(states, destructible)
            sector = self.sdf.sectors[number]
            values = list(sector.subsects)
            for cell in cells:
                values[0 if sector.single else cell] = row
            self.sdf.sectors[number] = replace(sector, subsects=tuple(values))
        self.change("Independent building states", apply)

    def sectors_using_model(self, model):
        """Find references in every damage state, including single-cell sectors."""
        buildings = {b.id for b in self.sdf.buildings if any(b.state(s) == model for s in range(4))}
        return {n for n, sector in self.sdf.sectors.items() if buildings.intersection(sector.subsects)}

    def _building_slot(self, states, destructible, reserved=()):
        for building in self.sdf.buildings:
            if tuple(building.state(s) for s in range(4)) == tuple(states) and building.destructible == destructible:
                return building.id
        used = {b for sector in self.sdf.sectors.values() for b in sector.subsects}
        used.update(reserved)
        slot = next((b.id for b in self.sdf.buildings if b.id not in used
                     and all(self.sdf.models[b.state(s)].base.casefold() == "dummy.base"
                             for s in range(4))), len(self.sdf.buildings))
        if slot >= BUILDING_LIMIT:
            raise ValueError(f"All {BUILDING_LIMIT} building state slots are occupied.")
        row = SdfBuilding(slot, *states, destructible, comment="Custom building")
        if slot == len(self.sdf.buildings):
            self.sdf.buildings.append(row)
        else:
            self.sdf.buildings[slot] = row
        return slot

    def state_chain(self, model):
        """Damage states that follow an intact model, or None when nothing fits.

        An existing building row that starts from this model wins (the most
        used one). Otherwise the vanilla names are followed: ST_CITY1 ->
        STxCITY1 -> STyCITY1 -> STzCITY1. A missing step repeats the previous one.
        """
        used = Counter(b for sector in self.sdf.sectors.values() for b in sector.subsects)
        rows = [b for b in self.sdf.buildings
                if b.intact == model and any(b.state(s) != model for s in range(1, 4))]
        if rows:
            row = max(rows, key=lambda b: (used[b.id], -b.id))
            return tuple(row.state(s) for s in range(4)), row.destructible
        name = Path(self.sdf.models[model].base.replace("\\", "/")).name
        if len(name) < 4 or name[2] != "_":
            return None
        by_name = {}
        for row in self.sdf.models:
            by_name.setdefault(Path(row.base.replace("\\", "/")).name.casefold(), row.id)
        states = [model]
        for mark in "xyz":
            states.append(by_name.get((name[:2] + mark + name[3:]).casefold(), states[-1]))
        return (tuple(states), 1) if states != [model] * 4 else None

    def assign_model(self, number, cells, model, state=0, *, whole=False):
        """Make independent cell state rows instead of altering shared buildings.

        A model placed as Intact also brings its damage states (state_chain);
        without a chain it fills all four states. ``whole`` is for a model that
        covers the full sector: the sector becomes one large cell, so the
        model is centered instead of shifted into a 3 x 3 cell.
        """
        if not 0 <= model < len(self.sdf.models):
            raise ValueError("Choose an existing model.")
        chain = self.state_chain(model) if state == 0 else None

        def apply():
            sector = self.sdf.sectors[number]
            values = list(sector.subsects)
            targets = {4} if whole else cells
            for cell in targets:
                index = 0 if sector.single else cell
                old = self.sdf.buildings[values[index]]
                if state == 0:
                    states, destructible = chain or ((model,) * 4, old.destructible)
                else:
                    states = [old.state(s) for s in range(4)]
                    states[state] = model
                    destructible = old.destructible
                values[index] = self._building_slot(states, destructible, values)
            if whole:
                values = (values[0 if sector.single else 4],)
            self.sdf.sectors[number] = replace(sector, kind=int(whole or sector.single),
                                               subsects=tuple(values))
        self.change("Replace cell models", apply)

    def shift_state(self, number, cells, state):
        """Make cells start natively from a damage state (0 intact ... 3 destroyed).

        The new row keeps the later states, so a damaged cell can still be
        destroyed in game: Damaged gives damaged, heavy, destroyed, destroyed.
        """
        if type(state) is not int or not 0 <= state <= 3:
            raise ValueError("Choose a damage state from 0 to 3.")

        def apply():
            sector = self.sdf.sectors[number]
            values = list(sector.subsects)
            for cell in ({4} if sector.single else cells):
                index = 0 if sector.single else cell
                old = self.sdf.buildings[values[index]]
                states = [old.state(min(3, state + s)) for s in range(4)]
                values[index] = self._building_slot(states, old.destructible, values)
            self.sdf.sectors[number] = replace(sector, subsects=tuple(values))
        self.change("Change cell state", apply)

    @staticmethod
    def rotated_cells(cells, quarters):
        """Map each selected cell to its place after clockwise quarter turns.

        The selection turns around the center of its bounding box. Returns
        None when a turn would leave the 3 x 3 grid or fall between cells.
        """
        cells = sorted(set(cells))
        if not cells or any(type(c) is not int or not 0 <= c < 9 for c in cells):
            return None
        rows, cols = [c // 3 for c in cells], [c % 3 for c in cells]
        center_row, center_col = (min(rows) + max(rows)) / 2, (min(cols) + max(cols)) / 2
        mapping = {}
        for cell in cells:
            dr, dc = cell // 3 - center_row, cell % 3 - center_col
            for _ in range(quarters % 4):
                dr, dc = dc, -dr
            row, col = center_row + dr, center_col + dc
            if row != int(row) or col != int(col) or not (0 <= row < 3 and 0 <= col < 3):
                return None
            mapping[cell] = int(row) * 3 + int(col)
        return mapping

    def rotate_cells(self, number, cells, quarters):
        """Turn only the selected cells; cells left behind become building 0."""
        sector = self.sdf.sectors[number]
        mapping = self.rotated_cells(cells, quarters)
        if sector.single or mapping is None:
            raise ValueError("This selection cannot turn that way inside the 3 × 3 sector.")
        original = sector.subsects

        def apply():
            values = list(original)
            for source in mapping:
                values[source] = 0
            for source, destination in mapping.items():
                values[destination] = original[source]
            self.sdf.sectors[number] = replace(sector, subsects=tuple(values))
        self.change("Rotate cells", apply)
        return set(mapping.values())

    def _store_model(self, model, reserved=()):
        """Allocate a positional row without changing existing referenced models."""
        used = {b.state(s) for b in self.sdf.buildings for s in range(4)}
        used.update(reserved)
        slot = next((m.id for m in self.sdf.models if m.id not in used
                     and m.base.casefold() == "dummy.base"), len(self.sdf.models))
        if slot >= MODEL_LIMIT:
            raise ValueError(f"All {MODEL_LIMIT} model slots are occupied.")
        row = replace(model, id=slot)
        if slot == len(self.sdf.models):
            self.sdf.models.append(row)
        else:
            self.sdf.models[slot] = row
        return slot

    def add_model(self, base, sklt, source=None):
        """Reuse unreferenced dummy model rows before extending the native table."""
        model = (self.sdf.models[source] if source is not None else
                 SdfBuildingModel(0, base, sklt, (0, 1, 0, 0), (), "Custom model"))
        return self.change("Add model", lambda: self._store_model(model))

    def _subset_tables(self, building_ids):
        bids = sorted(set(building_ids))
        mids = sorted({self.sdf.buildings[bid].state(s) for bid in bids for s in range(4)})
        model_map = {old: new for new, old in enumerate(mids)}
        building_map = {old: new for new, old in enumerate(bids)}
        subset = SetSdf(models=[replace(self.sdf.models[mid], id=model_map[mid]) for mid in mids])
        for bid in bids:
            building = self.sdf.buildings[bid]
            subset.buildings.append(SdfBuilding(building_map[bid],
                *(model_map[building.state(s)] for s in range(4)), building.destructible, building.comment))
        return subset, building_map

    def _merge_buildings(self, source, needed):
        """Share dependency remapping between native imports and cell paste."""
        def identity(model):
            return (model.base.casefold(), model.sklt.casefold(), model.numbers, model.effects)

        known = {identity(m): m.id for m in self.sdf.models}
        model_map, building_map = {}, {}
        models = sorted({source.buildings[bid].state(s) for bid in needed for s in range(4)})
        reserved = {known[identity(source.models[mid])] for mid in models
                    if identity(source.models[mid]) in known}
        for mid in models:
            model = source.models[mid]
            key = identity(model)
            if key not in known:
                known[key] = self._store_model(model, reserved | set(model_map.values()))
            model_map[mid] = known[key]
        for bid in sorted(needed):
            row = source.buildings[bid]
            building_map[bid] = self._building_slot(
                [model_map[row.state(s)] for s in range(4)], row.destructible, building_map.values())
        return building_map

    def copy_cells(self, number, cells):
        """Keep complete definitions so undo or ID reuse cannot change the clipboard."""
        sector = self.sdf.sectors[number]
        bids = [sector.subsects[0 if sector.single else cell] for cell in sorted(cells)]
        subset, building_map = self._subset_tables(bids)
        return CellClipboard(subset, tuple(building_map[bid] for bid in bids))

    def paste_cells(self, number, cells, clipboard):
        selected = sorted(cells)
        if not selected or len(clipboard.cells) not in (1, len(selected)):
            raise ValueError("Copy one cell to fill the selection, or select the same number of destination cells.")
        def apply():
            building_map = self._merge_buildings(clipboard.tables, set(clipboard.cells))
            sector = self.sdf.sectors[number]
            values = list(sector.subsects)
            for index, cell in enumerate(selected):
                values[0 if sector.single else cell] = building_map[clipboard.cells[index % len(clipboard.cells)]]
            self.sdf.sectors[number] = replace(sector, subsects=tuple(values))
            self.validate_resources()
        self.change("Paste cells", apply)

    def export_definitions(self, numbers, target):
        """Export a native SDF subset, including its four-state dependencies."""
        target = Path(target).resolve()
        if target == self.sdf.path.resolve():
            raise ValueError("Choose a separate file for exported definitions.")
        sectors = [self.sdf.sectors[number] for number in sorted(set(numbers))]
        if not sectors:
            raise ValueError("Select a defined sector first.")
        subset, building_map = self._subset_tables(bid for sector in sectors for bid in sector.subsects)
        subset.sectors = {sector.number: replace(sector, subsects=tuple(building_map[b] for b in sector.subsects))
                          for sector in sectors}
        issues = table_issues(subset)
        if issues:
            raise ValueError("\n".join(issues[:20]))
        data = serialize_set_sdf(subset).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="sector-definitions-") as directory:
            stage = Path(directory) / "sectors.sdf"
            stage.write_bytes(data)
            commit_verified_files([(stage, target)], verify=lambda: self._verify_file(target, data))

    def import_definitions(self, path, targets=()):
        """Merge native definitions using the current set's assets.

        Definitions go into ``targets`` first (overwriting them), then into
        free slots.
        """
        source = parse_set_sdf_text(decode_sdf(Path(path).read_bytes()))
        issues = table_issues(source)
        if issues:
            raise ValueError("\n".join(issues[:20]))
        if not source.sectors:
            raise ValueError("This SDF file contains no sector definitions.")
        targets = list(dict.fromkeys(targets))[:len(source.sectors)]
        if any(type(n) is not int or not 0 <= n < SECTOR_LIMIT for n in targets):
            raise ValueError("Choose sector slots from 0 to 255.")
        free = targets + [n for n in range(1, 248) if n not in self.sdf.sectors and n not in targets]
        if len(free) < len(source.sectors):
            raise ValueError("There are not enough free sector slots.")

        def apply():
            needed = {bid for sector in source.sectors.values() for bid in sector.subsects}
            building_map = self._merge_buildings(source, needed)
            imported = []
            for number, destination in zip(sorted(source.sectors), free):
                sector = source.sectors[number]
                self.sdf.sectors[destination] = replace(sector, number=destination,
                    subsects=tuple(building_map[bid] for bid in sector.subsects))
                imported.append(destination)
            self.validate_resources()
            return imported
        return self.change("Import sector definitions", apply)

    # --- external sectors (.sector folders, see studio/formats/sector_file.py) ----------------

    @staticmethod
    def sectors_folder() -> Path:
        data = bootstrap.game_data_dir()
        return bootstrap.find_ci(data, "/".join(SECTORS_FOLDER)) or data.joinpath(*SECTORS_FOLDER)

    def _load_assets(self):
        if self.assets is None:
            from map_editor.core.asset_bridge import SetAssets
            self.assets = SetAssets(self.number, set_dir=self.folder).load()
        return self.assets

    def _setbas_names(self):
        archive = self._load_assets().archive
        return {Path(r.resource_name.replace("\\", "/")).name.casefold() for r in archive.resources} \
            if archive else set()

    def _sector_model_source(self, model, files):
        """``(visual, collision)`` for one model row; own files are queued in ``files``.

        Copyright rule: anything that exists in SET.BAS is written as a name only and the
        engine takes it from the SET of the map. Only our own loose files are copied.
        """
        from studio.assets.family import load_asset_family
        assets = self._load_assets()
        in_setbas = self._setbas_names()
        key = assets._key(model.base)
        loose = assets._index_loose().get(key)
        visual = model.base
        if loose is not None and assets._by_name.get(key) is None:
            visual = Path(model.base.replace("\\", "/")).stem + ".BASE"
            files[visual] = loose
            family = load_asset_family(loose, [loose.parent, self.folder], {}, assets.archive)
            for dependency in family.dependencies:
                path = dependency.resolved_path
                if (path is not None and Path(path).is_file() and dependency.resolution_source != "SET.BAS"
                        and Path(dependency.raw_ref.replace("\\", "/")).name.casefold() not in in_setbas):
                    files.setdefault(Path(path).name, Path(path))
        collision = model.sklt
        skeleton = self._loose_skeleton(model.sklt)
        if skeleton is not None and Path(model.sklt.replace("\\", "/")).name.casefold() not in in_setbas:
            collision = skeleton.name
            files.setdefault(collision, skeleton)
        return visual, collision

    # --- map icons of external sectors ------------------------------------------------------

    def is_own_model(self, model_id):
        """Our own model: a loose BASE that SET.BAS does not have (the only ones with generated icons)."""
        assets = self._load_assets()
        key = assets._key(self.sdf.models[model_id].base)
        return assets._index_loose().get(key) is not None and assets._by_name.get(key) is None

    def icon_folder(self) -> Path:
        """Scratch folder of generated icons until they are exported with the sector."""
        if self._icon_folder is None or not self._icon_folder.is_dir():
            self._icon_folder = Path(tempfile.mkdtemp(prefix="sector-map-icons-"))
        return self._icon_folder

    def set_map_icon(self, kind, key, files):
        """Assign icon files (PNG/SVG): 1 for every zoom, or one per zoom. Empty files clears."""
        zooms = SECTOR_ICON_ZOOMS if kind == "sector" else MODEL_ICON_ZOOMS
        files = tuple(Path(f) for f in files)
        if not files:
            self.map_icons.pop((kind, key), None)
            return
        if len(files) not in (1, zooms) or any(f.suffix.casefold() not in (".png", ".svg") or not f.is_file()
                                               for f in files):
            raise ValueError(f"Choose 1 or {zooms} existing PNG/SVG files.")
        self.map_icons[(kind, key)] = files

    def model_shapes(self, model_id):
        from studio.assets.sector_map_icons import model_shapes
        obj = self._load_assets().base_object(self.sdf.models[model_id].base, set_only=True)
        return model_shapes(obj) if obj is not None else []

    def generate_map_icons(self, number, *, replace_existing=False):
        """Draw the map icons of a sector's own models (all damage states) and of the sector.

        SET models keep their vanilla map symbol. Returns the number of icon sets written.
        """
        from studio.assets.sector_map_icons import is_empty, model_icons, save_icons, sector_icons
        sector = self.sdf.sectors[number]
        folder = self.icon_folder()
        written, own_cells = 0, []
        models = {self.sdf.buildings[bid].state(s) for bid in sector.subsects for s in range(4)}
        for mid in sorted(models):
            if not self.is_own_model(mid) or (("model", mid) in self.map_icons and not replace_existing):
                continue
            images = model_icons(self.model_shapes(mid), single=sector.single)
            if is_empty(images):
                continue
            stem = f"{Path(self.sdf.models[mid].base).stem}{'_single' if sector.single else ''}"
            self.map_icons[("model", mid)] = tuple(save_icons(images, folder, stem))
            written += 1
        for bid in sector.subsects:
            mid = self.sdf.buildings[bid].state(0)
            own_cells.append(self.model_shapes(mid) if self.is_own_model(mid) else None)
        if any(own_cells) and (("sector", number) not in self.map_icons or replace_existing):
            images = sector_icons(own_cells, single=sector.single)
            if not is_empty(images):
                self.map_icons[("sector", number)] = tuple(save_icons(images, folder, f"sector{number:03d}"))
                written += 1
        return written

    def external_sector(self, number):
        """Build the SectorFile of a sector plus the own files it needs ({file name: source})."""
        if number not in self.sdf.sectors:
            raise ValueError(f"Sector {number} is not defined.")
        sector = self.sdf.sectors[number]
        if sector.ground < 0 or sector.symbol < 0:
            raise ValueError("Choose the connection family and map symbol first.")
        files, models, pieces = {}, {}, {}
        used_models, used_pieces = set(), set()
        result = SectorFile(ground=sector.ground, map_symbol=sector.symbol)

        def model_name(mid):
            if mid not in models:
                row = self.sdf.models[mid]
                visual, collision = self._sector_model_source(row, files)
                models[mid] = unique_name(row.base, used_models)
                result.models.append(SectorModel(
                    models[mid], visual, collision, row.numbers[3], row.numbers[1], row.numbers[2],
                    tuple(tuple(e) for e in row.effects),
                    self._export_icons(("model", mid), models[mid], files)))
            return models[mid]

        for bid in sector.subsects:
            if bid not in pieces:
                row = self.sdf.buildings[bid]
                states = tuple(model_name(row.state(s)) for s in range(4))
                pieces[bid] = unique_name(f"piece{len(pieces) + 1}", used_pieces)
                result.pieces.append(SectorPiece(pieces[bid], states[:1] if len(set(states)) == 1 else states,
                                                 row.destructible == 1))
        result.cells = tuple(pieces[bid] for bid in sector.subsects)
        result.map_icon = self._export_icons(("sector", number), "sector", files)
        return result, files

    def _export_icons(self, key, stem, files):
        """Queue the icon files of ``key`` as map/<stem>[_<n>].<ext>; returns the map_icon value."""
        icons = self.map_icons.get(key, ())
        names = []
        for index, path in enumerate(icons):
            name = f"map/{stem}{'' if len(icons) == 1 else '_' + str(index)}{path.suffix.lower()}"
            files[name] = path
            names.append(name)
        return tuple(names)

    def export_external_sector(self, number, target, *, name=None, generate_icons=False):
        """Write a sector as an external sector folder, or as a .onua archive of that folder.

        ``generate_icons`` first draws the missing map icons of the sector's own models.
        """
        from studio.exchange.onua_archive import ONUA_SUFFIX, write_sector_onua
        target = Path(target).resolve()
        archive = target.suffix.casefold() == ONUA_SUFFIX
        name = name or (target.stem if archive else target.name)
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name):
            raise ValueError("Sector names use letters, digits, '_' and '-' (max 40).")
        if not archive and target.exists() and any(target.iterdir()):
            raise FileExistsError(f"The destination folder is not empty: {target}")
        if generate_icons:
            self.generate_map_icons(number)
        sector, files = self.external_sector(number)
        text = format_sector(sector, f"{name}: sector {number} of Set{self.number}")
        with tempfile.TemporaryDirectory(prefix="sector-export-") as directory:
            stage = Path(directory) / name
            stage.mkdir()
            (stage / f"{name}.sector").write_text(text, encoding="utf-8")
            for file_name, source in files.items():
                (stage / file_name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, stage / file_name)
            read_sector(stage / f"{name}.sector")
            if archive:
                write_sector_onua(stage, target)
                return target
            commit_verified_files([(path, target / path.relative_to(stage))
                                   for path in sorted(stage.rglob("*")) if path.is_file()],
                                  replace_existing=False)
        return target / f"{name}.sector"

    def _external_model_row(self, model, folder, visproto):
        """Model row for one .sector model, resolved like the engine; returns (row, own_files)."""
        assets = self._load_assets()
        visual = model.visual
        base, sklt, numbers, effects, own = None, "", [0, 0, 0, 0], (), False
        if visual.isdigit():
            index = int(visual)
            if index >= len(visproto):
                raise ValueError(f"model '{model.name}': VP {visual} does not exist in this set.")
            base = visproto[index]
        elif visual.casefold().endswith((".base", ".3ds")):
            if visual.casefold().startswith("data/"):
                path = resolve_data_path(bootstrap.game_data_dir(), visual)
            else:
                path = bootstrap.find_ci(folder, visual.replace("\\", "/")) or folder / visual
            if path.is_file():
                if path.suffix.casefold() == ".3ds":
                    raise ValueError(f"model '{model.name}': 3DS visuals are not editable in the Sector Editor.")
                assets.add_external_root(path.parent)
                base, own = path.name, True
        if not own:
            # Not beside the file: the SET model with the same name (its row gives the defaults).
            stem = Path((base or visual).replace("\\", "/")).stem.casefold()
            source = next((m for m in self.sdf.models
                           if Path(m.base.replace("\\", "/")).stem.casefold() == stem), None)
            if source is not None:
                base = source.base
                sklt, numbers, effects = source.sklt, list(source.numbers), source.effects
            elif assets.base_object(base or visual, set_only=True) is not None:
                base = base or visual
            else:
                raise ValueError(f"model '{model.name}': {visual} found neither beside the file nor in this set.")
        if model.collision:
            sklt = model.collision
            if any(bootstrap.find_ci(folder, p) for p in (sklt, Path(sklt).name)):
                own = True
        if not sklt:
            raise ValueError(f"model '{model.name}' needs collision.")
        for index, value in ((3, model.collision_mode), (1, model.map_symbol), (2, model.shield)):
            if value is not None:
                numbers[index] = value
        if model.explosions is not None:
            effects = model.explosions
        row = SdfBuildingModel(0, base, sklt, tuple(numbers), tuple(tuple(e) for e in effects),
                               f"external {model.name}")
        return row, own

    def import_external_sector(self, source, target=None):
        """Open a .sector file, a sector folder or a sector .onua archive into a sector slot.

        An archive is unpacked into Data/Models/Sectors/<name> first. Returns the slot used.
        """
        from studio.exchange.onua_archive import ONUA_SUFFIX, import_sector_onua
        source = Path(source)
        if source.suffix.casefold() == ONUA_SUFFIX:
            path = import_sector_onua(source, self.sectors_folder() / source.stem)
        else:
            path = find_sector_file(source) if source.is_dir() else source
        sector = read_sector(path)
        folder = path.parent.resolve()
        self._load_assets().add_external_root(folder)
        self._asset_check = None
        visproto_path = bootstrap.find_ci(self.folder, "Scripts/VISPROTO.LST")
        lines = decode_sdf(visproto_path.read_bytes()).splitlines() if visproto_path else []
        visproto = [line.split(";", 1)[0].split()[0] for line in lines if line.split(";", 1)[0].split()]
        target = self.free_sector() if target is None else int(target)
        if target is None or not 0 <= target < SECTOR_LIMIT:
            raise ValueError("No free sector slot (1-247).")

        def icon_files(values):
            files = tuple(resolve_sector_file(folder, v) for v in values)
            return files if files and all(f.is_file() for f in files) else ()

        subset, own = SetSdf(), []
        for index, model in enumerate(sector.models):
            row, is_own = self._external_model_row(model, folder, visproto)
            subset.models.append(replace(row, id=index))
            own.append(is_own)
        names = {m.name.casefold(): i for i, m in enumerate(sector.models)}
        for index, piece in enumerate(sector.pieces):
            subset.buildings.append(SdfBuilding(index, *(names[piece.state(s).casefold()] for s in range(4)),
                                                1 if piece.destructible else 0, f"external {piece.name}"))
        pieces = {p.name.casefold(): i for i, p in enumerate(sector.pieces)}
        subset.sectors[0] = SdfSector(0, 1 if sector.single else 0, sector.ground, sector.map_symbol,
                                      tuple(pieces[c.casefold()] for c in sector.cells), path.stem)
        issues = table_issues(subset)
        if issues:
            raise ValueError("\n".join(issues[:20]))

        def identity(model):
            return (model.base.casefold(), model.sklt.casefold(), model.numbers, model.effects)

        def created(before, after):
            return {i for i in range(len(after)) if i >= len(before) or after[i] != before[i]}

        def apply():
            models, buildings = list(self.sdf.models), list(self.sdf.buildings)
            building_map = self._merge_buildings(subset, set(range(len(subset.buildings))))
            self.sdf.sectors[target] = replace(subset.sectors[0], number=target,
                subsects=tuple(building_map[b] for b in subset.sectors[0].subsects))
            ids = {identity(m): m.id for m in self.sdf.models}
            self.external_own |= {ids[identity(m)] for m, is_own in zip(subset.models, own) if is_own}
            self.external_models |= created(models, self.sdf.models)
            self.external_buildings |= created(buildings, self.sdf.buildings)
            for model in sector.models:
                files = icon_files(model.map_icon)
                if files:
                    row = subset.models[names[model.name.casefold()]]
                    self.map_icons[("model", ids[identity(row)])] = files
            if icon_files(sector.map_icon):
                self.map_icons[("sector", target)] = icon_files(sector.map_icon)
            self.validate_resources()
            return target
        return self.change("Import external sector", apply)

    def encoded(self):
        issues = table_issues(self.sdf)
        if issues:
            raise ValueError("\n".join(issues[:30]))
        text = serialize_set_sdf(self.sdf, decode_sdf(self.source))
        try:
            self.source.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "cp1252"
        data = text.encode(encoding)
        check = parse_set_sdf_text(decode_sdf(data))
        if (check.models, check.buildings, check.sectors) != (self.sdf.models, self.sdf.buildings, self.sdf.sectors):
            raise ValueError("The written tables did not reload identically.")
        return data

    def override_bytes(self):
        """Use the game's existing LEGO override list for named model replacements.

        The embedded LEGO child table is positional. SET.SDF alone changes its
        collision data, but does not replace the corresponding visual BASE.
        """
        if self.embedded_names is None:
            from map_editor.core.asset_bridge import SetAssets
            assets = SetAssets(self.number, set_dir=self.folder).load()
            if assets.family is None or len(assets.family.root_object.kids) < 2:
                raise ValueError("SET.BAS has no LEGO model table.")
            self.embedded_names = [obj.base_object.name for obj in assets.family.root_object.kids[1].kids]
        def key(value):
            return Path(value.replace("\\", "/")).stem.casefold()
        overrides = {model.id: model.base for model in self.sdf.models
                     if model.id >= len(self.embedded_names) or key(model.base) != key(self.embedded_names[model.id])}
        if self.override_source is None and not overrides:
            return None
        original = decode_sdf(self.override_source or b"")
        newline = "\r\n" if "\r\n" in original else "\n"
        retained = []
        for line in original.splitlines(keepends=True):
            tokens = line.split(";", 1)[0].split()
            if len(tokens) >= 3 and tokens[0].casefold() == "lego":
                try:
                    if 0 <= int(tokens[1]) < len(self.sdf.models):
                        continue
                except ValueError:
                    pass
            retained.append(line)
        if retained and not retained[-1].endswith(("\n", "\r")):
            retained[-1] += newline
        retained.extend(f"lego {index} {name}{newline}" for index, name in sorted(overrides.items()))
        return "".join(retained).encode("cp1252")

    def resource_issues(self):
        """Check BASE and collision dependencies inside this set, using shared readers."""
        key = tuple((model.base, model.sklt) for model in self.sdf.models)
        if self._asset_check is not None and self._asset_check[0] == key:
            return list(self._asset_check[1])
        if self.assets is None:
            from map_editor.core.asset_bridge import SetAssets
            self.assets = SetAssets(self.number, set_dir=self.folder).load()
        issues = []
        for model in self.sdf.models:
            if self.assets.base_object(model.base, set_only=True) is None:
                issues.append(f"Model {model.id}: BASE {model.base} is missing from this set.")
            embedded = self.assets.archive.find(model.sklt, "sklt.class") if self.assets.archive else []
            if self._loose_skeleton(model.sklt) is None and not embedded:
                issues.append(f"Model {model.id}: collision {model.sklt} is missing from this set.")
        self._asset_check = (key, issues)
        return list(issues)

    def _loose_skeleton(self, name):
        """Loose collision file for ``name``: external sector folders first, then the set."""
        logical = Path(name.replace("\\", "/"))
        for root in (self.assets.external_roots if self.assets else ()):
            for relative in (logical.as_posix(), logical.name):
                path = bootstrap.find_ci(root, relative)
                if path is not None and path.is_file():
                    return path
        # Direct rsrc reads use the legacy three-letter extension. For
        # Set1-Set7 the engine first searches canonical and legacy Loose
        # paths, where the original .sklt spelling is also accepted.
        legacy = logical.with_suffix(".skl") if logical.suffix.casefold() == ".sklt" else logical
        candidates = [legacy.as_posix()]
        if 1 <= self.number <= 7:
            for part in (logical.name, legacy.name):
                candidates.extend((f"Loose/SKLT/{part}", f"Loose/Skeleton/{part}"))
        return next((path for relative in candidates
                     if (path := bootstrap.find_ci(self.folder, relative)) is not None and path.is_file()), None)

    def validate_resources(self):
        issues = self.resource_issues()
        if issues:
            raise ValueError("\n".join(issues[:30]))

    def _generated_base_files(self, overrides):
        """Extract required inline BASE objects with the Studio's verified writer."""
        from studio.assets.mapping import export_base_object_bytes
        generated = {}
        if not overrides:
            return generated
        objects = bootstrap.find_ci(self.folder, "Objects")
        for line in decode_sdf(overrides).splitlines():
            tokens = line.split(";", 1)[0].split()
            if len(tokens) < 3 or tokens[0].casefold() != "lego":
                continue
            name = tokens[2].replace("\\", "/")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or ":" in name:
                raise ValueError(f"Invalid set model path: {name}")
            # Native Objects lookups normalize BASE to the legacy BAS suffix.
            native_name = relative.with_suffix(".bas")
            if bootstrap.find_ci(objects, native_name.as_posix()) is not None:
                continue
            target = objects.relative_to(self.folder) / native_name
            if target in generated:
                continue
            if self.assets is None:
                from map_editor.core.asset_bridge import SetAssets
                self.assets = SetAssets(self.number, set_dir=self.folder).load()
            loose = self.assets._index_loose().get(self.assets._key(name))
            if loose is not None and loose.is_relative_to(self.folder):
                generated[target] = loose.read_bytes()
                continue
            model = self.assets.base_object(name, set_only=True)
            if model is None or self.assets.archive is None:
                raise ValueError(f"Cannot extract the runtime BASE for {name}.")
            generated[target] = export_base_object_bytes(self.assets.archive.data, model.base_object)
        return generated

    def _drop_external_rows(self):
        """Give back the rows of imported external sectors before writing the set.

        A sector that still uses the sector's own files stops the save: export it instead.
        """
        if not self.external_models and not self.external_buildings:
            return
        buildings = self.sdf.buildings
        own = {bid for bid in range(len(buildings))
               if any(buildings[bid].state(s) in self.external_own for s in range(4))}
        blocked = sorted(n for n, sector in self.sdf.sectors.items() if own & set(sector.subsects))
        if blocked:
            raise ValueError(
                f"Sector(s) {', '.join(map(str, blocked))} use models of an external sector. "
                "Export them with File > Export Sector File, then clear or wipe them before saving the set.")
        used = {bid for sector in self.sdf.sectors.values() for bid in sector.subsects}
        drop_buildings = {bid for bid in self.external_buildings if bid < len(buildings) and bid not in used}
        kept = [row for bid, row in enumerate(buildings) if bid not in drop_buildings]
        drop_models = {mid for mid in self.external_models
                       if not any(row.state(s) == mid for row in kept for s in range(4))}
        for table, rows, saved in ((buildings, drop_buildings, self.saved.buildings),
                                   (self.sdf.models, drop_models, self.saved.models)):
            for index in sorted(rows, reverse=True):
                if index >= len(table):      # already removed (undo)
                    continue
                if index < len(saved):
                    table[index] = saved[index]
                elif index == len(table) - 1:
                    table.pop()
                else:
                    raise ValueError("Rows added after an external sector import stop the save: "
                                     "undo them or reopen the set.")
        self.external_models.clear()
        self.external_buildings.clear()
        self.external_own.clear()
        # Older undo steps still hold the imported rows; they must not come back untracked.
        self._undo.clear()
        self._redo.clear()

    def output_files(self):
        """One output plan shared by Save, set copies, new sets and ZIP packages."""
        self._drop_external_rows()
        data = self.encoded()
        self.validate_resources()
        overrides = self.override_bytes()
        files = {self.sdf.path.relative_to(self.folder): data}
        if overrides is not None:
            files[self.override_path.relative_to(self.folder)] = overrides
            files.update(self._generated_base_files(overrides))
        return files

    def save(self):
        outputs = self.output_files()
        data = outputs[self.sdf.path.relative_to(self.folder)]
        overrides = outputs.get(self.override_path.relative_to(self.folder))
        target = self.sdf.path
        if target is None:
            raise ValueError("Choose a destination set first.")
        if target.read_bytes() != self.source:
            raise ValueError("SET.SDF changed outside this editor. Reopen it before saving.")
        current_override = self.override_path.read_bytes() if self.override_path.is_file() else None
        if current_override != self.override_source:
            raise ValueError("override.lst changed outside this editor. Reopen the set before saving.")
        with tempfile.TemporaryDirectory(prefix="sector-save-") as directory:
            files = []
            for relative, payload in outputs.items():
                stage = Path(directory) / relative
                stage.parent.mkdir(parents=True, exist_ok=True)
                stage.write_bytes(payload)
                files.append((stage, self.folder / relative))
            def verify():
                for relative, payload in outputs.items():
                    self._verify_file(self.folder / relative, payload)
            commit_verified_files(files, verify=verify)
        self.source = data
        self.override_source = overrides
        self.saved = deepcopy(self.sdf)

    @staticmethod
    def _verify_file(path, data):
        if path.read_bytes() != data:
            raise OSError(f"Verification failed: {path}")

    def _resource_files(self):
        for name in SET_FOLDERS:
            folder = bootstrap.find_ci(self.folder, name)
            if folder is None or not folder.is_dir():
                raise FileNotFoundError(f"{name} folder is missing in {self.folder}")
        for path in sorted(self.folder.rglob("*")):
            if path.is_file():
                yield path, path.relative_to(self.folder)

    def export_zip(self, target):
        """Package all set resources and the current unsaved SDF in native layout."""
        target = Path(target).resolve()
        if target.is_relative_to(self.folder.resolve()):
            raise ValueError("Choose a ZIP destination outside the set folder.")
        outputs = self.output_files()
        data = outputs[self.sdf.path.relative_to(self.folder)]
        files = list(self._resource_files())
        replacements = {path.as_posix().casefold(): (path, payload) for path, payload in outputs.items()}
        prefix = f"Data/Sets/Set{self.number}/"
        with tempfile.TemporaryDirectory(prefix="sector-zip-") as directory:
            stage = Path(directory) / "set.zip"
            with ZipFile(stage, "w", ZIP_DEFLATED) as archive:
                for path, relative in files:
                    name = prefix + relative.as_posix()
                    planned = replacements.pop(relative.as_posix().casefold(), None)
                    if planned is not None:
                        archive.writestr(name, planned[1])
                    else:
                        archive.write(path, name)
                for relative, payload in replacements.values():
                    archive.writestr(prefix + relative.as_posix(), payload)
            with ZipFile(stage) as archive:
                if archive.testzip() is not None:
                    raise OSError("ZIP verification failed.")
                if archive.read(prefix + self.sdf.path.relative_to(self.folder).as_posix()) != data:
                    raise OSError("ZIP contains an incorrect SET.SDF.")
            commit_verified_files([(stage, target)])
        return len(files) + len(replacements)

    def copy_to(self, target):
        """Create a complete independent set; an existing folder is never replaced."""
        target = Path(target).resolve()
        if target.exists():
            raise FileExistsError("The destination already exists. Choose a new Set<number> folder.")
        if target.is_relative_to(self.folder.resolve()):
            raise ValueError("Choose a destination outside the source set.")
        match = re.fullmatch(r"Set(\d+)", target.name, re.IGNORECASE)
        if not match or not 1 <= int(match[1]) <= 255:
            raise ValueError("The destination must be named Set1 … Set255.")
        # A direct copy cannot preserve Loose overrides in Set8 or later;
        # the engine enables that resolver only for Set1-Set7.
        if 1 <= self.number <= 7 and int(match[1]) > 7:
            loose = bootstrap.find_ci(self.folder, "Loose")
            if loose is not None and any(path.is_file() for path in loose.rglob("*")):
                raise ValueError(
                    f"Cannot copy {self.folder.name} with Loose resources to {target.name}: "
                    "the engine only reads Loose overrides in Set1-Set7. Choose a Set1-Set7 destination."
                )
        outputs = self.output_files()
        files = list(self._resource_files())
        with tempfile.TemporaryDirectory(prefix="sector-copy-", dir=target.parent) as directory:
            stage = Path(directory) / target.name
            stage.mkdir()
            for path, relative in files:
                dest = stage / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            for relative, payload in outputs.items():
                dest = bootstrap.find_ci(stage, relative.as_posix()) or stage / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
            os.rename(stage, target)
        return self.open(target)

    @classmethod
    def create(cls, target, sources, *, keep_sectors=False):
        """Assemble selected resource folders, optionally starting with no sectors."""
        target = Path(target).resolve()
        match = re.fullmatch(r"Set(\d+)", target.name, re.IGNORECASE)
        if target.exists() or not match or not 1 <= int(match[1]) <= 255:
            raise ValueError("Choose a new destination named Set1 … Set255.")
        for name in SET_FOLDERS:
            if name not in sources or not Path(sources[name]).is_dir():
                raise ValueError(f"Choose the {name} source folder.")
            if target.is_relative_to(Path(sources[name]).resolve()):
                raise ValueError("Choose a destination outside all selected source folders.")
        objects = Path(sources["Objects"])
        if bootstrap.find_ci(objects, "SET.BAS") is None:
            raise ValueError("The Objects folder must contain SET.BAS.")
        with tempfile.TemporaryDirectory(prefix="sector-new-", dir=target.parent) as directory:
            stage = Path(directory) / target.name
            for name in SET_FOLDERS:
                shutil.copytree(sources[name], stage / name)
            sdf_path = bootstrap.find_ci(stage / "Scripts", "SET.SDF")
            if sdf_path is None:
                sdf_path = stage / "Scripts" / "SET.SDF"
                sdf = SetSdf(models=[SdfBuildingModel(0, "dummy.base", "skeleton/dummy.sklt", (0, 1, 0, 0), ())],
                             buildings=[SdfBuilding(0, 0, 0, 0, 0, 0)])
                sdf_path.write_text(serialize_set_sdf(sdf), encoding="utf-8")
            document = cls.open(stage)
            if not keep_sectors:
                document.sdf.sectors.clear()
            for relative, payload in document.output_files().items():
                dest = stage / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
            from map_editor.core.asset_bridge import SetAssets
            from studio.rendering.indexed import IndexedFamilyAdapter
            assets = SetAssets(int(match[1]), set_dir=stage).load()
            adapter, reason = IndexedFamilyAdapter.try_create(assets.family)
            if adapter is None:
                raise ValueError(f"The selected palette/remap resources cannot render this set: {reason}")
            document.assets = assets
            document.validate_resources()
            os.rename(stage, target)
        return cls.open(target)
