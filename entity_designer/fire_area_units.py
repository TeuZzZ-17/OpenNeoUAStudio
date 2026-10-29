"""Units that fire a weapon, for the Fire Area preview. Qt-free.

The fire volume is measured from the muzzle of the firing unit, so the tab shows
a vehicle that uses the weapon (``weapon`` / ``weapon_2``) with its fire point.
The sideways muzzle positions follow ``fire_x`` and its modes like the engine
(``ypabact_GetProjectileFireX`` / ``ypabact_GetFireXSpan``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from entity_designer.destruction_fx import (
    FLYING_MODELS,
    script_int,
    split_tuple,
    top_level_values,
)
from entity_designer.script_model import find_script_blocks

_WEAPON_KEYS = ("weapon", "weapon_2")
# Projectiles per firing event of each weapon slot, like the engine's num_weapons keys.
_COUNT_KEYS = {"weapon": "num_weapons", "weapon_2": "num_weapons_2"}
FIRE_X_MAX_SLOTS = 256
MAX_PREVIEW_PROJECTILES = 8
FIRE_X_MODES = frozenset({"sequence", "random", "salve_sequence", "salve_mirror"})


def _numbers(raw: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    parts = split_tuple(raw, len(default))
    if parts is None:
        return default
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        return default
    return values if all(math.isfinite(value) for value in values) else default


def _number(raw: str | None) -> float:
    try:
        value = float(raw.strip().split(maxsplit=1)[0]) if raw else 0.0
    except ValueError:
        return 0.0
    return value if math.isfinite(value) else 0.0


@dataclass(frozen=True)
class FiringUnit:
    """A vehicle that fires the weapon, with what its preview needs."""

    vehicle_id: int
    name: str
    vp: int
    fire: tuple[float, float, float]
    model: str
    overeof: float
    scale: tuple[float, float, float]
    rotation: tuple[float, float, float]
    fire_x_mode: str = "vanilla"
    # (start, step, slots) of a valid fire_x rack, only for sequence / random.
    rack: tuple[float, float, int] | None = None
    projectiles: int = 1

    @property
    def x_span(self) -> tuple[float, float]:
        """Sideways range of every muzzle position the engine can use."""

        side = abs(self.fire[0])
        if self.rack is None:
            return (-side, side)
        start, step, slots = self.rack
        last = start + (slots - 1) * step
        return (min(start, last), max(start, last))

    def muzzle_xs(self) -> list[float]:
        """Sideways muzzle position of each projectile of the first firing event.

        The engine alternates sides from one shot to the next in some modes (and
        draws the order at random in ``random``): the preview shows the first event.
        """

        x, count = self.fire[0], max(1, self.projectiles)
        mode = self.fire_x_mode
        if mode == "salve_sequence":
            return [x] * count
        if mode == "salve_mirror":
            per_side = count // 2
            return [x if i < per_side else -x if i < per_side * 2 else 0.0
                    for i in range(count)]
        if mode in ("sequence", "random"):
            if self.rack is not None:
                start, step, slots = self.rack
                return [start + (i % slots) * step for i in range(count)]
            return [(-x, x)[i & 1] for i in range(count)]
        if count == 1:
            return [x]
        side = abs(x)
        return [i * 2 * side / (count - 1) - side for i in range(count)]

    @property
    def label(self) -> str:
        return f"{self.name} ({self.vehicle_id})"

    @property
    def flies(self) -> bool:
        return self.model.casefold() in FLYING_MODELS


def _rack(values: dict[str, str], mode: str) -> tuple[float, float, int] | None:
    """The advanced fire_x rack when the engine accepts it, else None."""

    if mode not in ("sequence", "random"):
        return None
    try:
        start = float(values["fire_x_start"].strip())
        step = float(values["fire_x_step"].strip())
        slots = int(values["fire_x_slots"].strip(), 0)
    except (KeyError, ValueError):
        return None
    if not (math.isfinite(start) and math.isfinite(step) and 1 <= slots <= FIRE_X_MAX_SLOTS):
        return None
    return (start, step, slots) if math.isfinite(start + (slots - 1) * step) else None


def _projectiles(raw: str | None) -> int:
    """First number of ``N`` or ``min_max`` (the engine rolls inside the range)."""

    try:
        count = int((raw or "").strip().split(maxsplit=1)[0].split("_")[0])
    except (ValueError, IndexError):
        return 1
    return min(max(count, 1), MAX_PREVIEW_PROJECTILES)


def unit_from_values(vehicle_id: int, values: dict[str, str],
                     weapon_id: int) -> FiringUnit | None:
    """The unit, when this vehicle fires ``weapon_id`` and has a model to show."""

    key = next((key for key in _WEAPON_KEYS
                if script_int(values.get(key), -1) == weapon_id), None)
    if key is None:
        return None
    vp = script_int(values.get("vp_normal"), 0)
    if vp <= 0:
        return None
    mode = values.get("fire_x_mode", "").strip().casefold()
    mode = mode if mode in FIRE_X_MODES else "vanilla"
    return FiringUnit(
        vehicle_id=vehicle_id,
        name=values.get("name", "").strip('"') or f"Vehicle {vehicle_id}",
        vp=vp,
        fire=(_number(values.get("fire_x")), _number(values.get("fire_y")),
              _number(values.get("fire_z"))),
        model=values.get("model", ""),
        overeof=_number(values.get("overeof")),
        scale=_numbers(values.get("scale_xyz"), (1.0, 1.0, 1.0)),
        rotation=_numbers(values.get("rotation_xyz"), (0.0, 0.0, 0.0)),
        fire_x_mode=mode,
        rack=_rack(values, mode),
        projectiles=_projectiles(values.get(_COUNT_KEYS[key])),
    )


def find_firing_units(weapon_id: int, game_objects=(), script_text: str = "") -> list[FiringUnit]:
    """Vehicles using the weapon: the game catalog plus the vehicles of this script.

    ``game_objects`` are catalog entries with ``kind``, ``object_id`` and the merged
    top-level ``values``. A vehicle of the script wins over the catalog one.
    """

    units: dict[int, FiringUnit] = {}
    for obj in game_objects:
        if obj.kind == "vehicle":
            unit = unit_from_values(obj.object_id, obj.values, weapon_id)
            if unit is not None:
                units[unit.vehicle_id] = unit
    for block in find_script_blocks(script_text) if script_text else ():
        if block.kind != "new_vehicle" or not block.complete:
            continue
        values = top_level_values(script_text, block.start_line, block.end_line)
        # The engine token of ``name`` stops at the first space; the block keeps it whole.
        values["name"] = block.name or values.get("name", "")
        unit = unit_from_values(block.object_id, values, weapon_id)
        if unit is not None:
            units[unit.vehicle_id] = unit
    return sorted(units.values(), key=lambda unit: (unit.name.casefold(), unit.vehicle_id))
