"""OpenNeoUA external sector files (``.sector``): one complete sector in its own folder.

A building points at it with ``sec_type = Data/Models/Sectors/<Name>/<Name>.sector`` (optionally
``|<SET sector>`` as fallback). The engine reader is ``game/yw_external_sector.cpp``; this module
follows the same rules so Studio never writes a file the game would reject.

Layout of a file (ScriptParser syntax, ``;`` starts a comment)::

    begin_sector
        cells = G|G|G|G|CORE|G|G|G|G   ; 9 piece names (SET.SDF order) or 1 (one large cell)
        ground = 0                     ; connection family 0-5
        map_symbol = 3                 ; 0-255, default 0
        map_icon = map/sector.png      ; optional PNG/SVG map icon, 1 file or zoom 0|1
    end
    begin_piece CORE                   ; fills a cell
        states = ok|hit|heavy|flat     ; intact|damaged|heavy|destroyed, or 1 model for all
        destructible = 1               ; default 0
    end
    begin_model ok
        visual = KRAFT_OK.BASE         ; VP number, .BASE/.3ds file, or SET model name
        collision = KRAFT_OK.sklt      ; default: the SET model's collision
        collision_mode = 0             ; 0 own skeleton, 1 simple, 2 complex
        map_symbol = 12                ; 0-255
        shield = 60                    ; 0-100
        explosion = 49|0|0|100         ; repeatable ID|x|z|height (SET.SDF effect group), or none
        map_icon = map/ok.png          ; optional cell icon, 1 file or zoom 2|3|4
    end

Files are searched beside the ``.sector`` first; a missing BASE/3DS uses the SET model with the same
name and everything a local BASE needs falls back to the SET of the map being played.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re

SECTOR_SUFFIX = ".sector"
SECTORS_FOLDER = ("Models", "Sectors")    # inside the game Data folder
STATE_COUNT = 4
SECTOR_ICON_ZOOMS = 2      # map zoom 0 and 1: one icon per sector
MODEL_ICON_ZOOMS = 3       # map zoom 2, 3 and 4: one icon per building cell
ICON_SUFFIXES = (".png", ".svg")


class SectorFileError(ValueError):
    pass


@dataclass
class SectorModel:
    name: str
    visual: str
    collision: str = ""
    collision_mode: int | None = None     # None = inherited from the SET model (or 0)
    map_symbol: int | None = None
    shield: int | None = None
    explosions: tuple[tuple[int, int, int, int], ...] | None = None
    map_icon: tuple[str, ...] = ()      # 1 file, or one per map zoom 2|3|4


@dataclass
class SectorPiece:
    name: str
    states: tuple[str, ...]
    destructible: bool = False

    def state(self, index: int) -> str:
        return self.states[0 if len(self.states) == 1 else index]


@dataclass
class SectorFile:
    cells: tuple[str, ...] = ()
    ground: int = 0
    map_symbol: int = 0
    map_icon: tuple[str, ...] = ()      # 1 file, or one per map zoom 0|1
    pieces: list[SectorPiece] = field(default_factory=list)
    models: list[SectorModel] = field(default_factory=list)
    path: Path | None = None

    @property
    def single(self) -> bool:
        return len(self.cells) == 1

    def piece(self, name: str) -> SectorPiece | None:
        return next((p for p in self.pieces if p.name.casefold() == name.casefold()), None)

    def model(self, name: str) -> SectorModel | None:
        return next((m for m in self.models if m.name.casefold() == name.casefold()), None)


# ----------------------------------------------------------------------------
# sec_type values in new_building / modify_building
# ----------------------------------------------------------------------------

def parse_sec_type(raw: str) -> tuple[str, int]:
    """Return ``(sector_file, fallback)``; ``sector_file`` is ``""`` for a vanilla number.

    Raises SectorFileError for values the engine ignores (it keeps the previous sector).
    """
    file, fallback = "", 0
    words = str(raw).split()
    # Like the engine: only the first word after "=" counts.
    for token in (words[0].split("|") if words else []):
        if not token:
            continue
        try:
            number = int(token, 0)
        except ValueError:
            number = None
        if number is not None:
            if not 0 <= number <= 255:
                raise SectorFileError(f"Sector number {number} is outside 0-255.")
            fallback = number
        elif not file and token.casefold().endswith(SECTOR_SUFFIX):
            file = normalize_data_path(token)
        else:
            raise SectorFileError(f"Invalid sec_type value: {raw}")
    return file, fallback


def sector_reference(raw: str) -> tuple[str, int]:
    """Like parse_sec_type, but an invalid value gives ``("", 0)`` (readers that only preview)."""
    try:
        return parse_sec_type(raw)
    except SectorFileError:
        return "", 0


def format_sec_type(sector_file: str, fallback: int | None) -> str:
    if not sector_file:
        return str(fallback or 0)
    return f"{sector_file}|{fallback}" if fallback else sector_file


def normalize_data_path(value: str) -> str:
    """Engine rule (uaNormalizeDataAssetPath without legacy paths): Data/... only, no traversal."""
    value = value.replace("\\", "/")
    parts = value.split("/")
    if (not value or value.startswith("/") or ":" in value or parts[0].casefold() != "data"
            or len(parts) < 2 or any(p in ("", ".", "..") for p in parts)):
        raise SectorFileError(f"Sector files must be Data/... paths: {value}")
    return "/".join(["Data", *parts[1:]])


def resolve_data_path(data_dir: Path, value: str) -> Path:
    """Map a ``Data/...`` path to the file system (case-insensitive like the engine)."""
    from map_editor import bootstrap
    parts = normalize_data_path(value).split("/")[1:]
    found = bootstrap.find_ci(Path(data_dir), "/".join(parts))
    return found if found is not None else Path(data_dir).joinpath(*parts)


def data_relative(data_dir: Path, path: Path) -> str:
    return "Data/" + Path(path).resolve().relative_to(Path(data_dir).resolve()).as_posix()


# ----------------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------------

def _int(value: str, low: int, high: int, where: str) -> int:
    try:
        number = int(value, 0)
    except ValueError:
        raise SectorFileError(f"{where}: '{value}' is not a whole number.") from None
    if not low <= number <= high:
        raise SectorFileError(f"{where}: {number} is outside {low}-{high}.")
    return number


def _icons(value: str, zooms: int, where: str) -> tuple[str, ...]:
    files = tuple(part.strip() for part in value.split("|"))
    if len(files) not in (1, zooms) or any(not f.casefold().endswith(ICON_SUFFIXES) for f in files):
        raise SectorFileError(f"{where}: map_icon needs 1 or {zooms} PNG/SVG files.")
    return files


def _split_line(line: str) -> tuple[str, str]:
    body = line.split(";", 1)[0].strip()
    if not body:
        return "", ""
    match = re.match(r"([^\s=]+)\s*(?:=\s*|\s+)?(.*)$", body)
    return match[1], match[2].strip()


def parse_sector_text(text: str, path: Path | None = None) -> SectorFile:
    sector = SectorFile(path=Path(path) if path else None)
    seen_sector = False
    scope, current = None, None
    ground = None
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        key, value = _split_line(raw)
        if not key:
            continue
        where = f"line {number}"
        word = key.casefold()
        if scope is None:
            if word == "begin_sector" and not seen_sector:
                scope, seen_sector = "sector", True
            elif word in ("begin_model", "begin_piece") and value and " " not in value:
                pool = sector.models if word == "begin_model" else sector.pieces
                if any(item.name.casefold() == value.casefold() for item in pool):
                    raise SectorFileError(f"{where}: '{value}' is defined twice.")
                current = SectorModel(value, "") if word == "begin_model" else SectorPiece(value, ())
                pool.append(current)
                scope = word[6:]
            else:
                raise SectorFileError(f"{where}: unknown keyword {key}.")
            continue
        if word == "end":
            scope, current = None, None
            continue
        if scope == "sector":
            if word == "cells":
                cells = tuple(value.split("|"))
                if len(cells) not in (1, 9) or "" in cells:
                    raise SectorFileError(f"{where}: cells needs 1 or 9 piece names.")
                sector.cells = cells
            elif word == "ground":
                ground = _int(value, 0, 5, where)
            elif word == "map_symbol":
                sector.map_symbol = _int(value, 0, 255, where)
            elif word == "map_icon":
                sector.map_icon = _icons(value, SECTOR_ICON_ZOOMS, where)
            else:
                raise SectorFileError(f"{where}: unknown keyword {key}.")
        elif scope == "piece":
            if word == "states":
                states = tuple(value.split("|"))
                if len(states) not in (1, STATE_COUNT) or "" in states:
                    raise SectorFileError(f"{where}: states needs 1 or 4 model names.")
                current.states = states
            elif word == "destructible":
                current.destructible = _int(value, 0, 1, where) == 1
            else:
                raise SectorFileError(f"{where}: unknown keyword {key}.")
        else:
            if word == "visual":
                current.visual = value
            elif word == "collision":
                current.collision = value
            elif word == "collision_mode":
                current.collision_mode = _int(value, 0, 2, where)
            elif word == "map_symbol":
                current.map_symbol = _int(value, 0, 255, where)
            elif word == "shield":
                current.shield = _int(value, 0, 100, where)
            elif word == "map_icon":
                current.map_icon = _icons(value, MODEL_ICON_ZOOMS, where)
            elif word == "explosion":
                # The first explosion line replaces inherited ones; "none" keeps the list empty.
                effects = list(current.explosions or ())
                if value.casefold() != "none":
                    parts = value.split("|")
                    if len(parts) != 4:
                        raise SectorFileError(f"{where}: explosion needs ID|x|z|height.")
                    effects.append((_int(parts[0], 0, 65535, where),
                                    *(_int(p, -100000, 100000, where) for p in parts[1:])))
                current.explosions = tuple(effects)
            else:
                raise SectorFileError(f"{where}: unknown keyword {key}.")
    if scope is not None:
        raise SectorFileError("Unexpected end of file: a block is not closed with end.")
    if not seen_sector or not sector.cells:
        raise SectorFileError("begin_sector with cells is missing.")
    if ground is None:
        raise SectorFileError("begin_sector needs ground = 0-5.")
    sector.ground = ground
    issues = sector_issues(sector)
    if issues:
        raise SectorFileError(issues[0])
    return sector


def read_sector(path: str | Path) -> SectorFile:
    path = Path(path)
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    return parse_sector_text(text, path)


def find_sector_file(folder: str | Path) -> Path:
    """The single ``.sector`` file of a sector folder."""
    found = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.casefold() == SECTOR_SUFFIX]
    if len(found) != 1:
        raise SectorFileError(f"{folder} must contain exactly one {SECTOR_SUFFIX} file.")
    return found[0]


def sector_issues(sector: SectorFile) -> list[str]:
    issues = []
    if len(sector.cells) not in (1, 9):
        issues.append("cells needs 1 or 9 piece names.")
    if not 0 <= sector.ground <= 5 or not 0 <= sector.map_symbol <= 255:
        issues.append("ground must be 0-5 and map_symbol 0-255.")
    for cell in sector.cells:
        if sector.piece(cell) is None:
            issues.append(f"cells uses an unknown piece '{cell}'.")
    for piece in sector.pieces:
        if len(piece.states) not in (1, STATE_COUNT):
            issues.append(f"piece '{piece.name}' needs states.")
        for state in piece.states:
            if sector.model(state) is None:
                issues.append(f"piece '{piece.name}' uses an unknown model '{state}'.")
    for model in sector.models:
        if not model.visual:
            issues.append(f"model '{model.name}' needs visual.")
        for value in (model.name, model.visual, model.collision):
            if ";" in value or "\n" in value:
                issues.append(f"model '{model.name}': names cannot contain ';' or line breaks.")
    for owner, icons, zooms in (("sector", sector.map_icon, SECTOR_ICON_ZOOMS),
                                *((f"model '{m.name}'", m.map_icon, MODEL_ICON_ZOOMS) for m in sector.models)):
        if icons and (len(icons) not in (1, zooms) or
                      any(not f.casefold().endswith(ICON_SUFFIXES) or "|" in f for f in icons)):
            issues.append(f"{owner}: map_icon needs 1 or {zooms} PNG/SVG files.")
    for item in (*sector.pieces, *sector.models):
        if not re.fullmatch(r"[^\s|;=]+", item.name):
            issues.append(f"'{item.name}' is not a valid name (no spaces, '|', ';' or '=').")
    return issues


# ----------------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------------

def format_sector(sector: SectorFile, title: str = "") -> str:
    issues = sector_issues(sector)
    if issues:
        raise SectorFileError("\n".join(issues))
    lines = [f"; {title}" if title else "; OpenNeoUA external sector",
             "; Files are searched beside this file first, then in the SET of the map.", "",
             "begin_sector",
             f"    cells = {'|'.join(sector.cells)}",
             f"    ground = {sector.ground}",
             f"    map_symbol = {sector.map_symbol}"]
    if sector.map_icon:
        lines.append(f"    map_icon = {'|'.join(sector.map_icon)}")
    lines.append("end")
    for piece in sector.pieces:
        lines += ["", f"begin_piece {piece.name}", f"    states = {'|'.join(piece.states)}"]
        if piece.destructible:
            lines.append("    destructible = 1")
        lines.append("end")
    for model in sector.models:
        lines += ["", f"begin_model {model.name}", f"    visual = {model.visual}"]
        if model.collision:
            lines.append(f"    collision = {model.collision}")
        for key in ("collision_mode", "map_symbol", "shield"):
            value = getattr(model, key)
            if value is not None:
                lines.append(f"    {key} = {value}")
        if model.explosions is not None:
            lines += [f"    explosion = {'|'.join(map(str, fx))}" for fx in model.explosions] \
                or ["    explosion = none"]
        if model.map_icon:
            lines.append(f"    map_icon = {'|'.join(model.map_icon)}")
        lines.append("end")
    return "\n".join(lines) + "\n"


def resolve_sector_file(folder: Path, value: str) -> Path:
    """A file named in a .sector: Data/... as written, anything else beside the .sector."""
    from map_editor import bootstrap
    if value.replace("\\", "/").casefold().startswith("data/"):
        return resolve_data_path(bootstrap.game_data_dir(), value)
    relative = value.replace("\\", "/")
    return bootstrap.find_ci(Path(folder), relative) or Path(folder) / relative


def unique_name(text: str, used: set[str]) -> str:
    """A valid block name derived from ``text`` that is not in ``used`` (case-insensitive)."""
    base = re.sub(r"[^\w.-]+", "_", PurePosixPath(text.replace("\\", "/")).stem) or "item"
    name, index = base, 2
    while name.casefold() in used:
        name, index = f"{base}_{index}", index + 1
    used.add(name.casefold())
    return name
