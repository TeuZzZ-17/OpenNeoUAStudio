"""Sets and sectors for the Studio MCP, built on the Sector Editor's own document.

Every table edit goes through ``sector_editor.document.SetDocument``: the same checks, slot allocation, LEGO
override list (Scripts/override.lst) and verified writer as the Sector Editor window. This module adds what an
assistant needs on top of it: readable listings, a batch runner with undo snapshots, installing a loose model
into a set, set textures and new sets.

The Sector Editor is in beta. When its document changes (operations, fields, file layout), update the operations
below, studio_help('sets') and tests/test_studio_mcp_sets.py in the same change.

Where the engine reads set files (verified in the OpenNeoUA source, Nov 2026):
- SET.SDF model rows: the BASE name is only a label; the visual comes from the SET.BAS LEGO table by position,
  or from Scripts/override.lst ``lego <id> <file>`` -> Objects/<file> (.base is read as .bas).
- A BASE in Objects/ loads its skeleton and textures through ``rsrc:`` = the set folder: Skeleton/<name>.sklt,
  <texture name> at the set root. Set1-Set7 also read Loose/ILBM, Loose/ANM and Loose/SKLT first.
- Embedded SET.BAS resources with the same name win over loose files of a new model, except the Loose overrides
  of Set1-Set7. So new skeleton and texture names must be unique in the set.
"""

from __future__ import annotations

import copy
import inspect
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace as with_fields
from pathlib import Path

from studio_mcp import core, history, textures
from studio_mcp.loose import resolve_base

from map_editor import bootstrap  # noqa: E402
from map_editor.core.set_sdf_parser import SdfBuildingModel, decode_sdf, parse_set_sdf_text  # noqa: E402
from sector_editor.document import (  # noqa: E402
    AUTO, BUILDING_LIMIT, MODEL_LIMIT, SECTOR_LIMIT, SetDocument, table_issues,
)
from sector_editor.textures import LOOSE_SETS, set_textures  # noqa: E402
from studio.exchange.portable import DirectoryIndex, find_existing_path  # noqa: E402
from studio.formats.setbas import decode_texture  # noqa: E402

SET_OPS: dict = {}
NAME_TOKEN = re.compile(r"[A-Za-z0-9_\-]{1,32}")
LAYOUTS = {"3x3": False, "single": True}
# MCP tool name of each set operation (the default is studio_set_<op>)
TOOL_NAMES = {"set_sector": "studio_set_sector_properties", "set_cells": "studio_set_fill_cells"}


def tool_name(op_name: str) -> str:
    return TOOL_NAMES.get(op_name, f"studio_set_{op_name}")


def set_op(name: str):
    def register(function):
        SET_OPS[name] = function
        function.op_name = name
        return function
    return register


# --- locating and opening sets ---------------------------------------------------------------


def _prepare() -> None:
    """The Sector Editor modules find the game through NME_GAME_DATA; the MCP knows it as ONUA_DATA."""
    os.environ.setdefault("NME_GAME_DATA", str(core.data_root()))
    DirectoryIndex.clear_cache()


def set_number(set_name) -> int:
    match = re.fullmatch(r"(?:set)?\s*(\d+)", str(set_name).strip(), re.I)
    if not match or not 1 <= int(match[1]) <= 255:
        raise core.McpToolError(f"Unknown set {set_name!r}: use 'set3' or 3 (Set1 ... Set255)")
    return int(match[1])


def set_folder(set_name, *, must_exist: bool = True) -> Path:
    number = set_number(set_name)
    sets_dir = core.under_data("Sets")
    found = find_existing_path(sets_dir, f"Set{number}") if sets_dir.is_dir() else None
    if found is None and must_exist:
        raise core.McpToolError(f"Set{number} does not exist in {sets_dir} (studio_set_list shows the sets)")
    return found or sets_dir / f"Set{number}"


def open_document(set_name) -> SetDocument:
    _prepare()
    folder = set_folder(set_name)
    try:
        return SetDocument.open(folder)
    except (OSError, ValueError) as exc:
        raise core.McpToolError(f"Cannot open {folder.name}: {exc}") from exc


def _embedded_names(doc: SetDocument) -> list[str]:
    if doc.embedded_names is None:
        doc.override_bytes()   # loads SET.BAS once and fills embedded_names
    return doc.embedded_names


def _assets(doc: SetDocument):
    if doc.assets is None:
        from map_editor.core.asset_bridge import SetAssets
        doc.assets = SetAssets(doc.number, set_dir=doc.folder).load()
    return doc.assets


def _stem(name: str) -> str:
    return Path(str(name).replace("\\", "/")).stem.casefold()


# --- references used by the operations -----------------------------------------------------


@dataclass
class SetContext:
    doc: SetDocument
    named: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)      # Path -> bytes written at commit
    notes: list = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return self.doc.folder

    def ref(self, value, kind: str):
        if isinstance(value, dict) and "named" in value:
            if value["named"] not in self.named:
                raise core.McpToolError(f"no earlier step saved a {kind} as {value['named']!r}")
            return self.named[value["named"]]
        return value

    def model(self, value) -> int:
        value = self.ref(value, "model")
        models = self.doc.sdf.models
        if isinstance(value, str) and not value.strip().isdigit():
            wanted = _stem(value)
            ids = [m.id for m in models if _stem(m.base) == wanted]
            if not ids:
                raise core.McpToolError(f"no model row uses BASE {value!r} (studio_set_models lists them)")
            return ids[0]
        if value is None or not 0 <= int(value) < len(models):
            raise core.McpToolError(f"model {value!r} does not exist (0..{len(models) - 1})")
        return int(value)

    def building(self, value) -> int:
        value = self.ref(value, "building")
        if value is None or not 0 <= int(value) < len(self.doc.sdf.buildings):
            raise core.McpToolError(f"building {value!r} does not exist (0..{len(self.doc.sdf.buildings) - 1})")
        return int(value)

    def sector(self, value, *, defined: bool = True) -> int:
        value = self.ref(value, "sector")
        number = int(value, 0) if isinstance(value, str) else int(value)
        if not 0 <= number < SECTOR_LIMIT:
            raise core.McpToolError(f"sector {value!r}: IDs go from 0 to 255")
        if defined and number not in self.doc.sdf.sectors:
            raise core.McpToolError(f"sector {number} is not defined (new_sector creates it)")
        return number

    def sectors(self, values) -> list[int]:
        if values is None:
            raise core.McpToolError("give `sectors` (a list of sector numbers)")
        values = values if isinstance(values, (list, tuple)) else [values]
        return [self.sector(v) for v in values]

    def cells(self, number: int, values=None) -> list[int]:
        sector = self.doc.sdf.sectors[number]
        if sector.single:
            return [4]
        if values is None or values == "all":
            return list(range(9))
        values = values if isinstance(values, (list, tuple)) else [values]
        if any(type(c) is not int or not 0 <= c < 9 for c in values):
            raise core.McpToolError("cells are numbers 0..8: 0 1 2 = far row, 3 4 5 = middle, 6 7 8 = near row")
        return sorted(set(values))


def _field(value):
    """'auto' -> the Sector Editor's Automatic choice; None keeps the value."""
    if isinstance(value, str) and value.casefold() == "auto":
        return AUTO
    return None if value is None else int(value)


def _model_numbers(old: tuple, symbol=None, shield=None, collision=None) -> tuple:
    numbers = list(old)
    for index, value in ((1, symbol), (2, shield), (3, collision)):
        if value is not None:
            numbers[index] = int(value)
    return tuple(numbers)


def _effects(value):
    if value is None:
        return None
    groups = tuple(tuple(int(v) for v in group) for group in value)
    if any(len(g) != 4 for g in groups):
        raise core.McpToolError("effects are groups of four integers: [explosion VP, x, z, height]")
    return groups


def _set_comment(doc: SetDocument, number: int, comment: str) -> None:
    if "\n" in comment or "\r" in comment:
        raise core.McpToolError("comments must use one line")
    doc.sdf.sectors[number] = with_fields(doc.sdf.sectors[number], comment=comment)


def _fill_cells(ctx: SetContext, number: int, models=None, buildings=None, whole: bool = False) -> None:
    """Put a model or building in each cell: one value for all cells, or a list of 9 (1 for a single cell).
    null keeps that cell."""
    sector = ctx.doc.sdf.sectors[number]
    count = 1 if sector.single else 9
    for values, kind in ((buildings, "building"), (models, "model")):
        if values is None:
            continue
        if not isinstance(values, (list, tuple)):
            values = [values] * count
        if len(values) != count:
            raise core.McpToolError(f"{kind}s: give one value or {count} (one per cell, null = keep)")
        for cell, value in zip([4] if sector.single else range(9), values):
            if value is None:
                continue
            if kind == "building":
                ctx.doc.assign_building(number, [cell], ctx.building(value))
            else:
                ctx.doc.assign_model(number, [cell], ctx.model(value), 0, whole=whole)


# --- sector operations -------------------------------------------------------------------------


@set_op("new_sector")
def op_new_sector(ctx, number=None, copy_of=None, layout=None, models=None, buildings=None, ground=None,
                  symbol=None, comment=None, whole: bool = False, save_as=None):
    """Create a sector in a free slot (number, default the first free 1-247), empty or as a copy of `copy_of`
    (a variant). layout '3x3' (9 cells, default) or 'single' (one large cell). models / buildings: one value for
    every cell or a list of 9 (cell 0 = far-left ... 8 = near-right; null = keep / empty). A model brings its own
    damage states. ground = connection family 0-5, symbol = map symbol 0-255; 'auto' (default for a new sector)
    copies the most similar sector, a copy keeps its own. whole=true centres one model over the whole sector."""
    doc = ctx.doc
    source = ctx.sector(copy_of) if copy_of is not None else None
    target = ctx.sector(number, defined=False) if number is not None else None
    if target is not None and target in doc.sdf.sectors:
        raise core.McpToolError(f"sector {target} is already defined (set_cells / set_sector change it)")
    if layout is not None and layout not in LAYOUTS:
        raise core.McpToolError("layout must be '3x3' or 'single'")
    created = doc.add_sector(source=source, number=target)
    if layout is not None and LAYOUTS[layout] != doc.sdf.sectors[created].single:
        doc.set_layout(created, LAYOUTS[layout])
    _fill_cells(ctx, created, models, buildings, whole)
    if source is None:
        ground, symbol = ("auto" if ground is None else ground), ("auto" if symbol is None else symbol)
        if "auto" in (ground, symbol) and not any(n < 248 and n != created for n in doc.sdf.sectors):
            ctx.notes.append(f"sector {created}: no other sector to copy ground/symbol from ('auto'); give "
                             "ground 0-5 and symbol 0-255 (studio_set_info of the source set shows the families)")
    if ground is not None or symbol is not None:
        doc.set_sector_fields([created], ground=_field(ground), symbol=_field(symbol))
    if comment is not None:
        _set_comment(doc, created, str(comment))
    if created >= 248:
        ctx.notes.append(f"sector {created}: slots 248-255 are the map border sectors")
    return {"copy_of": source, **_sector_summary(doc, created)}


@set_op("set_sector")
def op_set_sector(ctx, sectors=None, ground=None, symbol=None, comment=None, layout=None):
    """Change sector properties: ground (connection family 0-5 or 'auto'), symbol (map symbol 0-255 or 'auto'),
    comment, layout ('3x3' / 'single': single keeps the centre cell, 3x3 repeats it in all nine)."""
    numbers = ctx.sectors(sectors)
    doc = ctx.doc
    if layout is not None:
        if layout not in LAYOUTS:
            raise core.McpToolError("layout must be '3x3' or 'single'")
        for number in numbers:
            if doc.sdf.sectors[number].single != LAYOUTS[layout]:
                doc.set_layout(number, LAYOUTS[layout])
    if ground is not None or symbol is not None:
        doc.set_sector_fields(numbers, ground=_field(ground), symbol=_field(symbol))
    if comment is not None:
        for number in numbers:
            _set_comment(doc, number, str(comment))
    return {"sectors": [_sector_summary(doc, n) for n in numbers]}


@set_op("set_cells")
def op_set_cells(ctx, sector=None, models=None, buildings=None, whole: bool = False):
    """Fill the cells of a defined sector: models / buildings = one value for all cells, or 9 values (1 for a
    single cell), null keeps a cell. Models bring their damage states (ST_X -> STxX, STyX, STzX)."""
    number = ctx.sector(sector)
    if models is None and buildings is None:
        raise core.McpToolError("give `models` and/or `buildings`")
    _fill_cells(ctx, number, models, buildings, whole)
    return _sector_summary(ctx.doc, number)


@set_op("place_model")
def op_place_model(ctx, sector=None, cells=None, model=None, state: int = 0, whole: bool = False):
    """Put one model in the chosen cells (default all). state 0 = intact (brings the damage states with it);
    1 damaged, 2 heavy damage, 3 destroyed replace only that state. whole=true: centred over the whole sector."""
    number = ctx.sector(sector)
    if not 0 <= int(state) <= 3:
        raise core.McpToolError("state is 0 intact, 1 damaged, 2 heavy damage, 3 destroyed")
    ctx.doc.assign_model(number, ctx.cells(number, cells), ctx.model(model), int(state), whole=whole)
    return _sector_summary(ctx.doc, number)


@set_op("place_building")
def op_place_building(ctx, sector=None, cells=None, building=None):
    """Put an existing building row (four damage states) in the chosen cells (default all)."""
    number = ctx.sector(sector)
    ctx.doc.assign_building(number, ctx.cells(number, cells), ctx.building(building))
    return _sector_summary(ctx.doc, number)


@set_op("place_states")
def op_place_states(ctx, sector=None, cells=None, states=None, destructible: int = 1):
    """Put four explicit models in the cells: states = [intact, damaged, heavy, destroyed] (ids or BASE names).
    A matching building row is reused, otherwise a free one is made; shared rows are never changed."""
    number = ctx.sector(sector)
    if not isinstance(states, (list, tuple)) or len(states) != 4:
        raise core.McpToolError("states = four models: [intact, damaged, heavy damage, destroyed]")
    ctx.doc.assign_states(number, ctx.cells(number, cells), [ctx.model(s) for s in states], int(destructible))
    return _sector_summary(ctx.doc, number)


@set_op("shift_state")
def op_shift_state(ctx, sector=None, cells=None, state: int = 1):
    """Make cells START damaged: 1 damaged, 2 heavy, 3 destroyed (later states follow, so they can still fall)."""
    number = ctx.sector(sector)
    ctx.doc.shift_state(number, ctx.cells(number, cells), int(state))
    return _sector_summary(ctx.doc, number)


@set_op("rotate_cells")
def op_rotate_cells(ctx, sector=None, cells=None, quarters: int = 1):
    """Turn the chosen cells clockwise by quarter turns around the centre of the selection (the models
    themselves do not turn). Cells left behind become empty (building 0)."""
    number = ctx.sector(sector)
    moved = ctx.doc.rotate_cells(number, ctx.cells(number, cells), int(quarters))
    return {"cells": sorted(moved), **_sector_summary(ctx.doc, number)}


@set_op("move_cells")
def op_move_cells(ctx, sector=None, cells=None, to_cell: int = 4, keep_source: bool = False):
    """Move (or with keep_source=true duplicate) a pattern of cells inside a 3x3 sector; the lowest selected
    cell lands on `to_cell`."""
    number = ctx.sector(sector)
    moved = ctx.doc.relocate_cells(number, ctx.cells(number, cells), int(to_cell), move=not keep_source)
    return {"cells": sorted(moved), **_sector_summary(ctx.doc, number)}


@set_op("copy_cells")
def op_copy_cells(ctx, from_sector=None, cells=None, to_sector=None, to_cells=None):
    """Copy cells (with their four states) from one sector to another: one copied cell fills all to_cells,
    otherwise give as many to_cells as copied cells."""
    source = ctx.sector(from_sector)
    target = ctx.sector(to_sector)
    clipboard = ctx.doc.copy_cells(source, ctx.cells(source, cells))
    ctx.doc.paste_cells(target, ctx.cells(target, to_cells), clipboard)
    return _sector_summary(ctx.doc, target)


@set_op("copy_sectors")
def op_copy_sectors(ctx, sectors=None, to=None, move: bool = False):
    """Copy (or move) whole sector definitions to the slots starting at `to`, keeping their order. Occupied
    target slots are overwritten."""
    numbers = ctx.sectors(sectors)
    target = ctx.sector(to, defined=False)
    copies = [copy.deepcopy(ctx.doc.sdf.sectors[n]) for n in numbers]
    placed = ctx.doc.paste_sectors(copies, target, remove=numbers if move else ())
    return {"placed": placed, "moved": bool(move)}


@set_op("clear_sectors")
def op_clear_sectors(ctx, sectors=None):
    """Keep the sectors but empty every cell (building 0)."""
    numbers = ctx.sectors(sectors)
    ctx.doc.clear_sectors(numbers)
    return {"cleared": numbers}


@set_op("wipe_sectors")
def op_wipe_sectors(ctx, sectors=None):
    """Delete whole sector definitions: their slots become free. Maps using them would show nothing there."""
    numbers = ctx.sectors(sectors)
    ctx.doc.wipe_sectors(numbers)
    return {"wiped": numbers}


@set_op("remove_all_sectors")
def op_remove_all_sectors(ctx, confirm: bool = False):
    """Free every sector slot (models and buildings stay). Needs confirm=true. For a brand-new set built
    from a copy; existing maps of this set would break."""
    if not confirm:
        raise core.McpToolError("remove_all_sectors needs confirm=true")
    count = len(ctx.doc.sdf.sectors)
    ctx.doc.reset_sectors(saved=False)
    return {"removed": count}


@set_op("import_sectors")
def op_import_sectors(ctx, from_set=None, sectors=None, to=None):
    """Copy sector definitions from another set (with their buildings and model rows). The models must exist in
    this set by BASE name: install them first (install_model) in a separate call. `to` = target slots."""
    if from_set is None or sectors is None:
        raise core.McpToolError("give from_set and sectors")
    source = open_document(from_set)
    numbers = [int(n) for n in (sectors if isinstance(sectors, (list, tuple)) else [sectors])]
    missing = [n for n in numbers if n not in source.sdf.sectors]
    if missing:
        raise core.McpToolError(f"{source.folder.name} has no sector(s) {missing}")
    targets = [int(n) for n in (to if isinstance(to, (list, tuple)) else ([] if to is None else [to]))]
    with tempfile.TemporaryDirectory(prefix="onua-sectors-") as folder:
        path = Path(folder) / "sectors.sdf"
        source.export_definitions(numbers, path)
        imported = ctx.doc.import_definitions(path, targets)
    return {"imported": dict(zip(numbers, imported))}


# --- model and building rows -----------------------------------------------------------------


def _default_sklt(doc: SetDocument, base: str) -> str:
    obj = _assets(doc).base_object(base, set_only=True)
    if obj is None or not obj.base_object.skeleton_name:
        raise core.McpToolError(f"BASE {base!r} is not in this set: give sklt, or use install_model for a new model")
    return obj.base_object.skeleton_name.replace("\\", "/")


@set_op("add_model")
def op_add_model(ctx, base=None, sklt=None, like=None, symbol=None, shield=None, collision=None, effects=None,
                 comment=None, save_as=None):
    """Add a model row for a BASE that this set already has (embedded or installed). like = a row to copy the
    numbers, effects and collision from. sklt default: the BASE's own skeleton. symbol (legacy building symbol),
    shield 0-100, collision 0 own SKLT / 1 set sub-collision / 2 compact collision, effects = destruction
    explosions [[VP, x, z, height], ...]."""
    doc = ctx.doc
    if not base:
        raise core.McpToolError("give `base` (an existing BASE of this set, e.g. 'ST_CITY1.base')")
    template = doc.sdf.models[ctx.model(like)] if like is not None else SdfBuildingModel(
        0, "", "", (0, 1, 0, 0), (), "")
    row = with_fields(template, base=str(base), sklt=sklt or (template.sklt if like is not None else _default_sklt(doc, base)),
                  numbers=_model_numbers(template.numbers, symbol, shield, collision),
                  effects=_effects(effects) if effects is not None else template.effects,
                  comment=comment if comment is not None else template.comment)
    model = doc._store_model(row)
    return {"model": model, "base": row.base, "sklt": row.sklt}


@set_op("update_model")
def op_update_model(ctx, model=None, base=None, sklt=None, symbol=None, shield=None, collision=None,
                    effects=None, comment=None):
    """Change a model row. Every building and sector using this row changes with it."""
    doc = ctx.doc
    mid = ctx.model(model)
    row = doc.sdf.models[mid]
    doc.sdf.models[mid] = with_fields(row, base=base or row.base, sklt=sklt or row.sklt,
                                  numbers=_model_numbers(row.numbers, symbol, shield, collision),
                                  effects=_effects(effects) if effects is not None else row.effects,
                                  comment=comment if comment is not None else row.comment)
    return {"model": mid, "used_in_sectors": sorted(doc.sectors_using_model(mid))}


@set_op("add_building")
def op_add_building(ctx, states=None, destructible: int = 1, comment=None, save_as=None):
    """A building row = four models [intact, damaged, heavy, destroyed]. An identical row is reused."""
    if not isinstance(states, (list, tuple)) or len(states) != 4:
        raise core.McpToolError("states = four models: [intact, damaged, heavy damage, destroyed]")
    doc = ctx.doc
    ids = [ctx.model(s) for s in states]
    building = doc._building_slot(ids, int(destructible))
    if comment is not None:
        doc.sdf.buildings[building] = with_fields(doc.sdf.buildings[building], comment=str(comment))
    return {"building": building, "states": ids}


@set_op("update_building")
def op_update_building(ctx, building=None, states=None, destructible=None, comment=None):
    """Change a building row in place. Every sector using it changes too (see used_in_sectors)."""
    doc = ctx.doc
    bid = ctx.building(building)
    row = doc.sdf.buildings[bid]
    ids = [ctx.model(s) for s in states] if states is not None else [row.state(s) for s in range(4)]
    if len(ids) != 4:
        raise core.McpToolError("states = four models")
    doc.sdf.buildings[bid] = with_fields(row, intact=ids[0], damaged=ids[1], heavy=ids[2], flat=ids[3],
                                     destructible=row.destructible if destructible is None else int(destructible),
                                     comment=row.comment if comment is None else str(comment))
    used = sorted(n for n, s in doc.sdf.sectors.items() if bid in s.subsects)
    return {"building": bid, "states": ids, "used_in_sectors": used}


# --- installing new models and textures ------------------------------------------------------


def _loose_folder(ctx: SetContext, kind: str) -> Path:
    """Loose/<kind> of Set1-Set7 (read by the engine before SET.BAS), else the set root (rsrc: path)."""
    if ctx.doc.number not in LOOSE_SETS:
        return ctx.folder
    loose = find_existing_path(ctx.folder, "Loose") or ctx.folder / "Loose"
    return find_existing_path(loose, kind) or loose / kind


def _existing_file(folder: Path, name: str) -> Path:
    current = folder
    for part in Path(name.replace("\\", "/")).parts:
        found = find_existing_path(current, part) if current.is_dir() else None
        current = found or current / part
    return current


def _target_palette(ctx: SetContext):
    return textures.set_palette(f"set{ctx.doc.number}")


def _stage_texture(ctx: SetContext, name: str, image, *, replace_embedded: bool = False) -> Path:
    """Queue an ILBM in the set palette where the engine will find it; returns the file."""
    archive = _assets(ctx.doc).archive
    embedded = set_textures(archive).get(Path(name).stem.casefold())
    if embedded is not None and not replace_embedded:
        raise core.McpToolError(f"{name} already exists in this SET.BAS; use another name, or add_texture with "
                                "replace=true to repaint it")
    if embedded is not None and ctx.doc.number not in LOOSE_SETS:
        raise core.McpToolError(f"Set{ctx.doc.number}: the engine reads replaced textures only in Set1-Set7")
    if embedded is not None:
        name = Path(embedded.resource_name.replace("\\", "/")).name
    target = _existing_file(_loose_folder(ctx, "ILBM"), name)
    png = target.with_suffix(".PNG")
    if find_existing_path(target.parent, png.name) is not None:
        ctx.notes.append(f"{png.name} in {target.parent} wins over the ILBM in game: delete it to use the new one")
    ctx.pending[target] = image
    return target


@set_op("add_texture")
def op_add_texture(ctx, name=None, layers=None, source: str = "", size=None, replace: bool = False):
    """Make a set texture from `layers` (see studio_help('textures')) on top of `source` (a set texture
    'set3:CITY1.ILBM', an ILBM or a PNG such as a generated image). It is mapped to this set's palette and written
    where the engine reads it. A new name (e.g. 'NXWALL.ILBM', at most as long as the texture it will replace on
    a model) adds a texture; replace=true repaints an existing one (Set1-Set7 only, kept at its original size)."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_\-]{1,12}\.(ILBM|ilbm|Ilbm)", str(name)):
        raise core.McpToolError("name: up to 12 letters/digits/_ plus .ILBM, e.g. 'NXWALL.ILBM'")
    palette = _target_palette(ctx)
    archive = _assets(ctx.doc).archive
    embedded = set_textures(archive).get(Path(name).stem.casefold())
    if embedded is not None and replace:
        original = decode_texture(archive, embedded)
        size = (original.width, original.height)
    picture = textures.build_picture(layers, size, f"set{ctx.doc.number}", source)
    pixels = textures.to_indices(picture, palette)
    data = textures.ilbm_bytes(pixels, picture.width, picture.height, palette, name)
    target = _stage_texture(ctx, name, data, replace_embedded=bool(replace))
    return {"texture": target.name, "file": str(target), "size": [picture.width, picture.height],
            "replaced": embedded is not None}


def _texture_bytes(ctx: SetContext, name: str, ref, texture_set: str) -> bytes | None:
    """The texture a model uses, re-mapped to this set's palette, or None when the set already has it."""
    path = getattr(ref, "path", None)
    if getattr(ref, "status", "") == "setbas":
        return None
    if path is not None and Path(path).resolve().is_relative_to(ctx.folder.resolve()):
        return None
    if Path(name).stem.casefold() in set_textures(_assets(ctx.doc).archive):
        # The SET.BAS copy is the one the game loads; a loose file of that name would repaint the whole set.
        ctx.notes.append(f"{name}: the model uses this set's own {name}, not the copy beside the model")
        return None
    if path is not None:
        image = textures.parse_ilbm_file(path)
    elif texture_set:
        image = textures.decode_ilbm(f"{texture_set}:{name}", texture_set)
    else:
        raise core.McpToolError(f"texture {name} was not found beside the model nor in this set: pass "
                                "texture_set='set1' (the set the model was made with)")
    palette = _target_palette(ctx)
    return textures.ilbm_bytes(textures.reindex(image, palette), image.width, image.height, palette, name)


@set_op("install_model")
def op_install_model(ctx, model=None, name=None, replace=None, collision="same", like=None, symbol=None,
                     shield=None, collision_mode=None, effects=None, comment=None, texture_set: str = "",
                     save_as=None):
    """Bring a loose model (built with the model tools, e.g. Models/Base/New/Tower_Intact/NXTOWR1.BASE) into
    this set: the BASE goes to Objects/<name>.bas, its skeleton to Skeleton/, missing textures and animations
    (re-mapped to this set's palette) where the engine reads them, and a model row is added (or `replace` = an
    existing row now shows the new model; buildings and sectors using it follow).
    name: the row's BASE name (default the file name; vanilla damage chains use XX_NAME, XXxNAME, XXyNAME,
    XXzNAME). collision: 'same' (the model's own skeleton), 'keep' (with replace), an SKLT of this set
    ('skeleton/STcCITY2.sklt') or a .sklt file. like = a row to copy symbol, shield, collision mode and effects.
    texture_set = the set whose SET.BAS holds textures the model uses that this set lacks."""
    doc = ctx.doc
    if not model:
        raise core.McpToolError("give `model` (the loose .BASE, e.g. 'Models/Base/New/Tower_Intact/NXTOWR1.BASE')")
    base_path = resolve_base(model)
    family = core.load_family(core.Source(base_path, _assets(doc).setbas_path, base_path.name))
    if family.root_object is None:
        raise core.McpToolError(f"{base_path.name} does not load: {'; '.join(family.warnings[:3])}")
    stem = str(name or base_path.stem)
    stem = stem[:-5] if stem.casefold().endswith(".base") else stem
    if not NAME_TOKEN.fullmatch(stem):
        raise core.McpToolError(f"name {stem!r}: letters, digits, _ and - only (up to 32)")
    row_name = f"{stem}.base"
    target_id = ctx.model(replace) if replace is not None else None
    if _stem(row_name) in {n.casefold() for n in _embedded_names(doc)}:
        raise core.McpToolError(f"{stem} is a SET.BAS model name: choose a new name so the game loads your file")
    for row in doc.sdf.models:
        if _stem(row.base) == stem.casefold() and row.id != target_id:
            raise core.McpToolError(f"model row {row.id} already uses {row.base}: pass replace={row.id} to update it")
    objects = find_existing_path(ctx.folder, "Objects") or ctx.folder / "Objects"
    ctx.pending[_existing_file(objects, f"{stem}.bas")] = base_path.read_bytes()
    archive = _assets(doc).archive
    skeletons = []
    for obj in family.all_objects():
        logical = (obj.base_object.skeleton_name or "").replace("\\", "/")
        ref = obj.skeleton_ref
        if not logical or ref is None or getattr(ref, "path", None) is None:
            if logical and getattr(ref, "status", "") != "setbas":
                raise core.McpToolError(f"skeleton {logical} of {base_path.name} was not found")
            continue
        if archive is not None and archive.find(Path(logical).name, "sklt.class"):
            raise core.McpToolError(f"skeleton {Path(logical).name} already exists in this SET.BAS: export the "
                                    "model again with a new skeleton_stem")
        ctx.pending[_existing_file(ctx.folder, logical)] = Path(ref.path).read_bytes()
        skeletons.append(logical)
    copied = []
    for kind, refs in (("ILBM", family.texture_refs), ("ANM", family.animation_refs)):
        for texture, ref in refs.items():
            if kind == "ILBM":
                data = _texture_bytes(ctx, texture, ref, texture_set)
            elif (getattr(ref, "status", "") == "setbas"
                  or (archive is not None and archive.find(Path(texture).name, "bmpanim.class"))
                  or (getattr(ref, "path", None) is not None
                      and Path(ref.path).resolve().is_relative_to(ctx.folder.resolve()))):
                data = None
            elif getattr(ref, "path", None) is None:
                raise core.McpToolError(f"animation {texture} of {base_path.name} was not found")
            else:
                data = Path(ref.path).read_bytes()
            if data is not None:
                ctx.pending[_existing_file(_loose_folder(ctx, kind), Path(texture).name)] = data
                copied.append(texture)
    template = doc.sdf.models[ctx.model(like)] if like is not None else (
        doc.sdf.models[target_id] if target_id is not None else SdfBuildingModel(0, "", "", (0, 1, 0, 0), (), ""))
    if collision == "same":
        if not skeletons:
            raise core.McpToolError("the model has no skeleton of its own: give collision")
        sklt = skeletons[0]
    elif collision == "keep":
        if target_id is None:
            raise core.McpToolError("collision='keep' only works with replace")
        sklt = doc.sdf.models[target_id].sklt
    elif str(collision).lower().endswith((".sklt", ".skl")) and (Path(str(collision)).is_absolute()
                                                                  or core.under_data(collision).is_file()):
        source = core.resolve_existing(collision)
        sklt = f"Skeleton/{source.name}"
        ctx.pending[_existing_file(ctx.folder, sklt)] = source.read_bytes()
    else:
        sklt = str(collision)
    row = with_fields(template, base=row_name, sklt=sklt,
                  numbers=_model_numbers(template.numbers, symbol, shield, collision_mode),
                  effects=_effects(effects) if effects is not None else template.effects,
                  comment=stem if comment is None else comment)
    if target_id is not None:
        doc.sdf.models[target_id] = with_fields(row, id=target_id)
        mid = target_id
    else:
        mid = doc._store_model(row)
    ctx.notes.append("install_model writes its files when the batch is saved; place the model in the same batch "
                     "with place_model/set_cells (copy_cells and import_sectors need it saved first)")
    return {"model": mid, "base": row_name, "sklt": sklt, "skeletons": skeletons, "copied_resources": copied,
            "replaced": target_id is not None}


# --- running a batch ---------------------------------------------------------------------------


def _call(function, ctx, arguments: dict, number: int):
    allowed = [p for p in inspect.signature(function).parameters if p != "ctx"]
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise core.McpToolError(f"operation #{number} '{function.op_name}': unknown argument(s) {unknown}. "
                                f"Allowed: {allowed}")
    return function(ctx, **arguments)


_SAVED_KEYS = ("model", "building", "sector")


def _commit(ctx: SetContext, label: str) -> dict:
    """Write the queued files and the tables once, after the Sector Editor's checks; undo snapshot first."""
    doc = ctx.doc
    issues = table_issues(doc.sdf)
    if issues:
        raise core.McpToolError("the set tables are not valid, nothing was written:\n" + "\n".join(issues[:20]))
    if not doc.dirty and not ctx.pending:
        return {"written": False, "note": "nothing changed"}
    before = {path: path.read_bytes() if path.is_file() else None
              for path in [*ctx.pending, doc.sdf.path, doc.override_path]}
    for path in before:
        core.ensure_writable(path)

    def restore():
        for path, data in before.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)

    try:
        for path, data in ctx.pending.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        DirectoryIndex.clear_cache()
        doc.assets, doc._asset_check = None, None
        for relative in doc.output_files():
            path = doc.folder / relative
            if path not in before:
                before[path] = path.read_bytes() if path.is_file() else None
        doc.save()
    except (OSError, ValueError) as exc:
        restore()
        raise core.McpToolError(f"the set was not written (files restored): {exc}") from exc
    except Exception:
        restore()
        raise
    history.snapshot(doc.folder, before, label)
    changed = [p for p, data in before.items() if (p.read_bytes() if p.is_file() else None) != data]
    return {"written": True, "files": sorted(str(p) for p in changed)}


def run(set_name, operations: list[dict], label: str = "", dry_run: bool = False) -> dict:
    """Apply set operations in order on one in-memory document, then check and write once."""
    if not operations:
        raise core.McpToolError("give at least one operation")
    ctx = SetContext(open_document(set_name))
    outputs = []
    for number, raw in enumerate(operations, start=1):
        if not isinstance(raw, dict) or "op" not in raw:
            raise core.McpToolError(f"operation #{number} has no 'op' name. Known: {sorted(SET_OPS)}")
        arguments = dict(raw)
        name = arguments.pop("op")
        save_as = arguments.pop("save_as", None)
        if name not in SET_OPS:
            raise core.McpToolError(f"operation #{number}: unknown op {name!r}. Known: {sorted(SET_OPS)}")
        try:
            result = _call(SET_OPS[name], ctx, arguments, number)
        except core.McpToolError as exc:
            raise core.McpToolError(f"operation #{number} ({name}) failed, nothing was written: {exc}") from exc
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise core.McpToolError(f"operation #{number} ({name}) failed, nothing was written: "
                                    f"{type(exc).__name__}: {exc}") from exc
        if save_as:
            key = next((k for k in _SAVED_KEYS if k in result), None)
            if key is None:
                raise core.McpToolError(f"operation #{number} ({name}) has nothing to save_as")
            ctx.named[save_as] = result[key]
        outputs.append({"op": name, **result})
    if dry_run:
        issues = table_issues(ctx.doc.sdf)
        summary = {"written": False, "dry_run": True, "table_issues": issues,
                   "would_write": sorted(str(p) for p in ctx.pending)}
    else:
        summary = _commit(ctx, label or "+".join(o["op"] for o in outputs)[:60])
    response = {"set": ctx.folder.name, "results": outputs, **summary}
    notes = list(dict.fromkeys(ctx.notes))
    if summary.get("written"):
        notes.extend(_warnings(ctx.doc, _touched(outputs)))
        response["undo"] = "studio_set_undo restores the previous state"
    if notes:
        response["notes"] = notes
    return response


def _touched(outputs: list[dict]) -> set[int]:
    numbers = set()
    for output in outputs:
        for key in ("sector", "placed", "sectors"):
            value = output.get(key)
            if isinstance(value, int):
                numbers.add(value)
            elif isinstance(value, list):
                numbers.update(v["sector"] if isinstance(v, dict) else v for v in value)
    return numbers


def _warnings(doc: SetDocument, numbers) -> list[str]:
    from map_editor.core.sector_resolver import SectorResolver
    resolver = SectorResolver(doc.sdf)
    return [f"sector {n}: {w}" for n in sorted(numbers) if n in doc.sdf.sectors
            for w in resolver.resolve_typ(n).warnings]


# --- reading ---------------------------------------------------------------------------------------


def _sector_summary(doc: SetDocument, number: int, detail: bool = False) -> dict:
    sector = doc.sdf.sectors[number]
    sdf = doc.sdf
    cells = []
    for index, bid in enumerate(sector.subsects):
        building = sdf.buildings[bid] if 0 <= bid < len(sdf.buildings) else None
        intact = building.intact if building else -1
        base = sdf.models[intact].base if 0 <= intact < len(sdf.models) else "?"
        if detail:
            cells.append({"cell": 4 if sector.single else index, "building": bid, "base": base,
                          "states": [building.state(s) for s in range(4)] if building else None,
                          "destructible": building.destructible if building else None})
        else:
            cells.append(f"b{bid}:{Path(base).stem}")
    return {"sector": number, "hex": f"{number:02X}", "layout": "single" if sector.single else "3x3",
            "ground": sector.ground, "symbol": sector.symbol, "comment": sector.comment, "cells": cells}


def list_sets() -> list[dict]:
    _prepare()
    rows = []
    sets_dir = core.under_data("Sets")
    for child in sorted(sets_dir.iterdir(), key=lambda p: p.name.casefold()) if sets_dir.is_dir() else []:
        match = re.fullmatch(r"set(\d+)", child.name, re.I)
        if not child.is_dir() or not match:
            continue
        scripts = find_existing_path(child, "Scripts")
        sdf = find_existing_path(scripts, "SET.SDF") if scripts else None
        row = {"set": child.name, "folder": str(child), "has_sdf": sdf is not None,
               "loose_overrides_in_game": int(match[1]) in LOOSE_SETS}
        if sdf is not None:
            tables = parse_set_sdf_text(decode_sdf(sdf.read_bytes()))
            row.update(sectors=len(tables.sectors), models=len(tables.models), buildings=len(tables.buildings))
        rows.append(row)
    return rows


def info(set_name) -> dict:
    doc = open_document(set_name)
    sdf = doc.sdf
    free = [n for n in range(1, 248) if n not in sdf.sectors]
    grounds = {}
    for number, sector in sdf.sectors.items():
        if number < 248:
            grounds.setdefault(sector.ground, []).append(sector.comment or str(number))
    problems = table_issues(sdf) + doc.resource_issues()
    used = {b for s in sdf.sectors.values() for b in s.subsects}
    return {
        "set": doc.folder.name, "folder": str(doc.folder),
        "sectors": len(sdf.sectors), "free_sector_slots": len(free), "first_free": free[:12],
        "border_sectors_248_255": sorted(n for n in sdf.sectors if n >= 248),
        "models": f"{len(sdf.models)} / {MODEL_LIMIT}", "buildings": f"{len(sdf.buildings)} / {BUILDING_LIMIT}",
        "unused_buildings": len([b for b in sdf.buildings if b.id not in used]),
        "embedded_lego_models": len(_embedded_names(doc)),
        "override_lst_entries": sum(1 for line in decode_sdf(doc.override_source or b"").splitlines()
                                    if line.split(";")[0].split()[:1] == ["lego"]),
        "connection_families": {g: {"sectors": len(c), "examples": c[:4]} for g, c in sorted(grounds.items())},
        "loose_overrides_in_game": doc.number in LOOSE_SETS,
        "problems": problems[:30], "warnings": sdf.warnings[:10],
    }


def sectors(set_name, numbers=None, text_filter: str = "", detail: bool = False, model=None) -> list[dict]:
    doc = open_document(set_name)
    ctx = SetContext(doc)
    wanted = None if numbers is None else set(ctx.sectors(numbers))
    using = doc.sectors_using_model(ctx.model(model)) if model is not None else None
    rows = []
    for number in sorted(doc.sdf.sectors):
        if (wanted is not None and number not in wanted) or (using is not None and number not in using):
            continue
        row = _sector_summary(doc, number, detail)
        if text_filter and text_filter.casefold() not in f"{row['comment']} {row['cells']}".casefold():
            continue
        rows.append(row)
    return rows


def models(set_name, text_filter: str = "", ids=None, unused: bool = False) -> list[dict]:
    doc = open_document(set_name)
    embedded = _embedded_names(doc)
    used = Counter(b.state(s) for b in doc.sdf.buildings for s in range(4))
    rows = []
    for model in doc.sdf.models:
        if ids is not None and model.id not in set(ids):
            continue
        if unused and used[model.id]:
            continue
        text = f"{model.base} {model.sklt} {model.comment}"
        if text_filter and text_filter.casefold() not in text.casefold():
            continue
        same = model.id < len(embedded) and _stem(model.base) == embedded[model.id].casefold()
        rows.append({"model": model.id, "base": model.base, "sklt": model.sklt, "symbol": model.numbers[1],
                     "shield": model.numbers[2], "collision": model.numbers[3], "effects": len(model.effects),
                     "comment": model.comment, "in_buildings": used[model.id],
                     "visual": "SET.BAS" if same else "override.lst"})
    return rows


def buildings(set_name, ids=None, model=None, unused: bool = False) -> list[dict]:
    doc = open_document(set_name)
    ctx = SetContext(doc)
    mid = ctx.model(model) if model is not None else None
    used = Counter(b for s in doc.sdf.sectors.values() for b in set(s.subsects))
    rows = []
    for building in doc.sdf.buildings:
        states = [building.state(s) for s in range(4)]
        if (ids is not None and building.id not in set(ids)) or (mid is not None and mid not in states):
            continue
        if unused and used[building.id]:
            continue
        rows.append({"building": building.id, "states": states,
                     "bases": [Path(doc.sdf.models[s].base).stem for s in states],
                     "destructible": building.destructible, "comment": building.comment,
                     "in_sectors": used[building.id]})
    return rows


def set_texture_list(set_name, text_filter: str = "") -> list[dict]:
    doc = open_document(set_name)
    archive = _assets(doc).archive
    rows = {}
    for stem, resource in set_textures(archive).items():
        rows[stem] = {"texture": Path(resource.resource_name.replace("\\", "/")).name, "where": "SET.BAS"}
    for folder in {_loose_folder(SetContext(doc), "ILBM"), doc.folder}:
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in (".ilbm", ".ilb", ".png"):
                key = path.stem.casefold()
                where = "replaced: " + path.name if key in rows else "new: " + path.name
                rows.setdefault(key, {"texture": path.stem.upper() + ".ILBM"})["where"] = where
    return [row for _, row in sorted(rows.items())
            if not text_filter or text_filter.casefold() in row["texture"].casefold()]


# --- whole sets -----------------------------------------------------------------------------------


def create(number, from_set, keep_sectors: bool = False, folders: dict | None = None) -> dict:
    """A new SetN: a full copy of `from_set` (Loose overrides and installed models included), or, with
    `folders`, the Sector Editor's New Set assembly ({'Palette': 'set3', ...}; missing folders from from_set)."""
    from sector_editor.document import SET_FOLDERS
    _prepare()
    target = set_folder(number, must_exist=False)
    if target.exists():
        raise core.McpToolError(f"{target.name} already exists: choose a free set number")
    core.ensure_writable(target)
    source = open_document(from_set)
    notes = []
    try:
        if folders:
            sources = {}
            for name in SET_FOLDERS:
                origin = set_folder(folders.get(name, from_set))
                found = find_existing_path(origin, name)
                if found is None:
                    raise core.McpToolError(f"{origin.name} has no {name} folder")
                sources[name] = found
            document = SetDocument.create(target, sources, keep_sectors=True)
            notes.append("built from the chosen folders: Loose overrides and installed models of the source set "
                         "were not copied")
        else:
            document = source.copy_to(target)
        ordinary = [n for n in document.sdf.sectors if n < 248]
        if not keep_sectors and ordinary:
            document.wipe_sectors(ordinary)
            document.save()
            notes.append("sectors 0-247 removed; the map border sectors 248-255 were kept")
    except (OSError, ValueError) as exc:
        raise core.McpToolError(f"Cannot create {target.name}: {exc}") from exc
    if document.number not in LOOSE_SETS and find_existing_path(target, "Loose") is not None:
        notes.append(f"{target.name}: the engine reads Loose/ overrides only in Set1-Set7; textures and models "
                     "installed with the MCP from now on go to the set root and Objects/ instead")
    notes.append("Maps choose their set in the level file (set = N); nothing uses this set until a map does")
    return {"created": str(target), "sectors": len(document.sdf.sectors), "models": len(document.sdf.models),
            "notes": notes}


def undo(set_name, steps: int = 1) -> dict:
    return history.undo(set_folder(set_name), steps)


def redo(set_name, steps: int = 1) -> dict:
    return history.redo(set_folder(set_name), steps)


def history_listing(set_name) -> dict:
    return history.listing(set_folder(set_name))


def ops_help() -> str:
    lines = ["SET OPERATIONS (studio_set_edit operations=[{\"op\": name, ...}]; add \"save_as\":\"x\" and use "
             "{\"named\":\"x\"} later for the new model / building / sector)"]
    for name, function in sorted(SET_OPS.items()):
        params = []
        for pname, parameter in inspect.signature(function).parameters.items():
            if pname in ("ctx", "save_as"):
                continue
            default = "" if parameter.default is inspect.Parameter.empty else f"={parameter.default!r}"
            params.append(pname + default)
        lines.append(f"- {name} [tool {tool_name(name)}]({', '.join(params)})\n"
                     f"    {' '.join((function.__doc__ or '').split())}")
    return "\n".join(lines)
