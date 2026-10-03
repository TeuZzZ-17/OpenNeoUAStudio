"""Visual references used by building mounts in original and modern scripts."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .building_defs import GunMount

ROBO_GUN_MAX_COUNT = 20


def _script_int(value: str, default: int | None = 0) -> int | None:
    """Read the integer forms accepted by ScriptParser::stol."""
    parts = value.strip().split()
    if not parts:
        return default
    token = parts[0]
    try:
        return int(token, 0)
    except ValueError:
        # C strtol with base zero also treats a leading zero as octal, while
        # Python's int(..., 0) accepts that form only with an explicit 0o.
        if re.fullmatch(r"[+-]?0[0-7]+", token):
            sign = -1 if token.startswith("-") else 1
            digits = token.lstrip("+-")[1:]
            return sign * int(digits, 8)
        try:
            return int(float(token))
        except (ValueError, OverflowError):
            return default


def _script_float(value: str, default: float = 0.0) -> float:
    parts = value.strip().split()
    if not parts:
        return default
    try:
        return float(parts[0])
    except ValueError:
        return default


@dataclass
class VehicleVisual:
    vp_normal: int = 0
    base_normal: str = ""
    name: str = ""
    model: str = ""
    three_ds_normal: str = ""
    enabled_factions: set = field(default_factory=set)
    energy: int = 0
    guns: list[GunMount] = field(default_factory=list)
    viewer: tuple[float, float, float] = (0.0, 0.0, 0.0)


def parse_vehicle_text(text: str, result=None):
    result = {} if result is None else result
    active = None
    depth = 0
    active_gun = -1
    for raw in text.splitlines():
        line = raw.split(';', 1)[0].strip()
        match = re.fullmatch(r'(new|modify)_vehicle\s+(\d+)', line, re.I)
        if match:
            key = int(match[2])
            active = (VehicleVisual() if match[1].lower() == 'new'
                      else result.get(key, VehicleVisual()))
            result[key] = active
            depth = 0
            active_gun = -1
        elif active is not None:
            if line.lower().startswith('begin_'):
                depth += 1
            elif line.lower() == 'end':
                if depth:
                    depth -= 1
                else:
                    active = None
            elif not depth and '=' in line:
                name, value = (s.strip() for s in line.split('=', 1))
                key = name.lower()
                if key == 'vp_normal':
                    try:
                        active.vp_normal = int(value, 0)
                    except ValueError:
                        pass
                elif key == 'base_normal':
                    active.base_normal = value.strip('"')
                elif key == '3ds_normal':
                    active.three_ds_normal = value.strip('"')
                elif key == 'energy':
                    active.energy = _script_int(value, active.energy)
                elif key in ('enable', 'disable'):
                    faction = _script_int(value, None)
                    if faction is None:
                        continue
                    if key == 'enable':
                        active.enabled_factions.add(faction)
                    else:
                        active.enabled_factions.discard(faction)
                elif key in ('name', 'model'):
                    setattr(active, key, value.strip('"'))
                elif key in ('unit_num_guns', 'robo_num_guns'):
                    count = max(0, min(ROBO_GUN_MAX_COUNT,
                                       _script_int(value)))
                    active.guns[:] = active.guns[:count]
                    active.guns.extend(
                        GunMount(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
                        for _ in range(count - len(active.guns)))
                    if active_gun >= count and (
                            key == 'unit_num_guns' or
                            active.model.casefold() == 'robo'):
                        active_gun = count - 1
                elif key in ('unit_act_gun', 'robo_act_gun'):
                    active_gun = _script_int(value)
                    if key == 'unit_act_gun':
                        active_gun = max(0, min(ROBO_GUN_MAX_COUNT - 1,
                                                active_gun))
                        if active_gun >= len(active.guns):
                            active.guns.extend(
                                GunMount(0, (0.0, 0.0, 0.0),
                                         (0.0, 0.0, 0.0))
                                for _ in range(active_gun + 1 - len(active.guns)))
                elif key.startswith('robo_viewer_') and key[-1] in 'xyz':
                    viewer = list(active.viewer)
                    axis = 'xyz'.index(key[-1])
                    viewer[axis] = _script_float(value, viewer[axis])
                    active.viewer = tuple(viewer)
                elif 0 <= active_gun < len(active.guns):
                    mount = active.guns[active_gun]
                    if key in ('unit_gun_type', 'robo_gun_type'):
                        vehicle = _script_int(value) & 0xFF
                        mount.vehicle = vehicle
                    elif key.startswith(('unit_gun_pos_', 'robo_gun_pos_')):
                        axis = key[-1]
                        if axis in 'xyz':
                            pos = list(mount.pos)
                            pos['xyz'.index(axis)] = _script_float(
                                value, pos['xyz'.index(axis)])
                            mount.pos = tuple(pos)
                    elif key.startswith(('unit_gun_dir_', 'robo_gun_dir_')):
                        axis = key[-1]
                        if axis in 'xyz':
                            direction = list(mount.direction)
                            direction['xyz'.index(axis)] = _script_float(
                                value, direction['xyz'.index(axis)])
                            mount.direction = tuple(direction)
    return result


def load_vehicle_files(scripts: Path, extra: str = ''):
    from .script_catalog import script_texts
    result = {}
    for text in script_texts(scripts, extra):
        parse_vehicle_text(text, result)
    return result
