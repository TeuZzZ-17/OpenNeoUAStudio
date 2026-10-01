"""Visual references used by building mounts in original and modern scripts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .building_defs import BUILDING_SUFFIXES, _decode


@dataclass
class VehicleVisual:
    vp_normal: int = 0
    base_normal: str = ""


def parse_vehicle_text(text: str, result=None):
    result = {} if result is None else result
    active = None
    depth = 0
    for raw in text.splitlines():
        line = raw.split(';', 1)[0].strip()
        match = re.fullmatch(r'(new|modify)_vehicle\s+(\d+)', line, re.I)
        if match:
            key = int(match[2])
            active = (VehicleVisual() if match[1].lower() == 'new'
                      else result.get(key, VehicleVisual()))
            result[key] = active
            depth = 0
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
                if name.lower() == 'vp_normal':
                    try:
                        active.vp_normal = int(value, 0)
                    except ValueError:
                        pass
                elif name.lower() == 'base_normal':
                    active.base_normal = value
    return result


def load_vehicle_files(scripts: Path):
    result = {}
    paths = sorted((p for p in scripts.rglob('*')
                    if p.is_file() and p.suffix.lower() in BUILDING_SUFFIXES),
                   key=lambda p: (p.name.lower() == 'vehicles.cfg', str(p).lower()))
    for path in paths:
        parse_vehicle_text(_decode(path.read_bytes()), result)
    return result
