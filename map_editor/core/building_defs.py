"""Script building definitions (new_building blocks).

The modern installation stores them in ``Scripts/Buildings.cfg`` while the
original game used ``.scr`` files with the same block syntax. Both suffixes
(plus ``.ini``) are parsed so custom installations keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BUILDING_SUFFIXES = {".cfg", ".scr", ".ini"}

_BEGIN = re.compile(r"(?im)^\s*(new|modify)_building\s+(\d+)\b")
_ASSIGN = re.compile(r"(?im)^\s*([A-Za-z_0-9]+)\s*=\s*([^\n;]+)")


@dataclass
class GunMount:
    vehicle: int
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    direction: tuple[float, float, float] = (0.0, 0.0, 1.0)


@dataclass
class BuildingDef:
    id: int
    name: str = ""
    model: str = ""
    sec_type: int = 0
    power: int = 0
    energy: int = 0
    production_cost: int = 0
    type_icon: str = ""
    source: str = ""
    debug_note: str = ""
    guns: list[GunMount] = field(default_factory=list)
    enabled_factions: set = field(default_factory=set)

    @property
    def is_radar(self) -> bool:
        return self.model.casefold() == "radar"

    @property
    def has_power(self) -> bool:
        return self.model.casefold() == "kraftwerk" and self.power > 0

    @property
    def has_guns(self) -> bool:
        return bool(self.guns)

    @property
    def is_debug(self) -> bool:
        if "debug" in self.debug_note.casefold():
            return True
        upper = self.name.upper()
        if upper.startswith(("GEM", "GATE")) and self.model.casefold() == "building":
            return True
        return (self.model.casefold() == "building" and self.power == 0
                and not self.guns and self.energy == 0)

    @property
    def energy_levels(self) -> int:
        """Number of glowing energy icons above the building (1-4)."""
        if not self.has_power:
            return 0
        if self.power >= 150:
            return 4
        if self.power >= 100:
            return 3
        if self.power >= 40:
            return 2
        return 1

    @property
    def kinds(self) -> tuple[str, ...]:
        out = []
        if self.has_power:
            out.append("power")
        if self.has_guns:
            out.append("flak")
        if self.is_radar:
            out.append("radar")
        if not out:
            out.append("system" if self.is_debug else "structure")
        return tuple(out)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _number(raw: str, default: int = 0) -> int:
    try:
        value = raw.strip().split()[0]
        return int(value, 0) if value.lower().startswith(('0x', '-0x', '+0x')) else int(float(value))
    except (ValueError, OverflowError, IndexError):
        return default


def parse_building_text(text: str, source: str = "", result=None) -> dict[int, BuildingDef]:
    result = {} if result is None else result
    for match in _BEGIN.finditer(text):
        bid = int(match.group(2))
        previous = result.get(bid) if match.group(1).casefold() == 'modify' else None
        tail = text[match.end():]
        lines = []
        depth = 0
        for raw in tail.splitlines():
            code = raw.split(';', 1)[0].strip().lower()
            if code.startswith('begin_'):
                depth += 1
            elif code == 'end':
                if depth:
                    depth -= 1
                else:
                    break
            elif not depth:
                lines.append(raw)
        body = '\n'.join(lines)
        values: dict[str, str] = {}
        debug_note = ""
        for assign in _ASSIGN.finditer(body):
            key = assign.group(1).strip().lower()
            raw = assign.group(2).strip()
            comment = ""
            semi = raw.find(";")
            if semi >= 0:
                comment = raw[semi + 1:]
                raw = raw[:semi]
            values.setdefault(key, raw.strip())
            if "debug" in comment.casefold():
                debug_note += comment.strip() + " "
        guns: dict[int, dict] = {}
        for line in body.splitlines():
            code = line.split(";", 1)[0]
            if "=" not in code:
                continue
            key, _, raw = code.partition("=")
            key, raw = key.strip().lower(), raw.strip()
            if not key.startswith("sbact_"):
                continue
            # sbact_act selects the current gun slot.
            if key == "sbact_act":
                values["_act"] = str(_number(raw))
                continue
            slot = _number(values.get("_act", "0"))
            entry = guns.setdefault(slot, {})
            entry[key[6:]] = raw
        mounts: list[GunMount] = []
        inherited = previous.guns if previous is not None else []
        for slot in range(max(max(guns, default=-1), len(inherited) - 1) + 1):
            entry = guns.get(slot, {})
            old = inherited[slot] if slot < len(inherited) else GunMount(0)
            vehicle = _number(str(entry.get("vehicle", old.vehicle)))
            if vehicle <= 0:
                break

            def triplet(prefix: str, fallback: tuple[float, float, float]):
                try:
                    return (float(str(entry.get(f"{prefix}_x", fallback[0])).split()[0]),
                            float(str(entry.get(f"{prefix}_y", fallback[1])).split()[0]),
                            float(str(entry.get(f"{prefix}_z", fallback[2])).split()[0]))
                except (ValueError, IndexError):
                    return fallback

            mounts.append(GunMount(
                vehicle, triplet("pos", old.pos),
                triplet("dir", old.direction)))
        values.pop("_act", None)
        definition = BuildingDef(
            id=bid,
            name=values.get("name", f"Building {bid}"),
            model=values.get("model", ""),
            sec_type=_number(values.get("sec_type", "0")),
            power=_number(values.get("power", "0")),
            energy=_number(values.get("energy", "0")),
            production_cost=_number(values.get("production_cost", "0")),
            type_icon=values.get("type_icon", "").strip(),
            source=source,
            debug_note=debug_note.strip(),
            guns=mounts,
        )
        if previous is not None:
            for key in ('name', 'model', 'sec_type', 'power', 'energy', 'production_cost', 'type_icon'):
                if key not in values:
                    setattr(definition, key, getattr(previous, key))
            if not guns:
                definition.guns = previous.guns
            definition.enabled_factions = set(previous.enabled_factions)
        for key, raw in _ASSIGN.findall(body):
            if key.casefold() == 'enable':
                definition.enabled_factions.add(_number(raw))
            elif key.casefold() == 'disable':
                definition.enabled_factions.discard(_number(raw))
        result[bid] = definition
    return result


def load_building_files(scripts_dir: Path, extra: str = '') -> dict[int, BuildingDef]:
    """Merge prototypes in the installation's manifest/include order."""
    from .script_catalog import script_texts
    merged: dict[int, BuildingDef] = {}
    for text, source in script_texts(scripts_dir, extra, with_source=True):
        parse_building_text(text, source, merged)
    return merged


def sec_type_index(definitions: dict[int, BuildingDef]) -> dict[int, list[int]]:
    index: dict[int, list[int]] = {}
    for bid, definition in definitions.items():
        if definition.sec_type:
            index.setdefault(definition.sec_type, []).append(bid)
    return {key: sorted(value) for key, value in index.items()}
