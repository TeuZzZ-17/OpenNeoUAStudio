"""Vanilla compatibility level of every row of the Visuals & FX effects list.

Levels are the Studio-wide ones of ``studio.ui.shell``:
green = fully vanilla, orange = vanilla row carrying OpenNeoUA extras (the vanilla
engine stops at keys it does not know), red = the vanilla engine has no such effect.
"""

from __future__ import annotations

import re

from entity_designer import destruction_fx as fx
from studio.ui.shell import OPENNEOUA, RISKY, VANILLA

# Events and sub-fields documented for vanilla Vehicles.cfg / Weapons.cfg.
_VANILLA_EVENTS = {
    fx.CONTEXT_VEHICLE: frozenset({
        "cockpit", "normal", "wait", "fire", "goingdown", "explode", "genesis",
        "crashland", "crashvhcl", "build", "beamin", "beamout"}),
    fx.CONTEXT_WEAPON: frozenset({"launch", "normal", "hit"}),
}
_VANILLA_FIELDS = {
    "sound": frozenset({"sample", "volume", "pitch"}),
    "palette": frozenset({"slot", "mag0", "mag1", "time"}),
    "shake": frozenset({"slot", "mag0", "mag1", "time", "mute", "x", "y", "z"}),
}
_PREFIX = {"sound": "snd", "palette": "pal", "shake": "shk"}
# "~" (random range) and "|" (field separator) are OpenNeoUA value syntax.
_EXTENDED_VALUE = re.compile(r"[~|]")


def key_group_level(group: fx.FxKeyGroup, context: str) -> str:
    """Compatibility of one snd_ / pal_ / shk_ package."""

    kind, event = group.group
    if event not in _VANILLA_EVENTS.get(context, frozenset()):
        return OPENNEOUA
    head = f"{_PREFIX[kind]}_{event}_"
    for _index, key, value in group.rows():
        field = key[len(head):] if key.startswith(head) else ""
        if field not in _VANILLA_FIELDS[kind] or _EXTENDED_VALUE.search(value):
            return RISKY
    return VANILLA


def dest_line_level(line: fx.DestFxLine) -> str:
    """Only the plain ``dest_fx`` key exists in vanilla (not ext_ / extended_)."""

    tokens = fx.engine_tokens(line.line)
    return VANILLA if tokens and tokens[0].casefold() == "dest_fx" else OPENNEOUA


def model_level(values: dict[str, str]) -> str:
    """vp_ keys are vanilla; base_ / 3ds_ keys are not, but fall back to the vp_ key."""

    extended = [key for key, value in values.items()
                if value and key.casefold().startswith(("base_", "3ds_"))]
    if not extended:
        return VANILLA
    plain = [key for key, value in values.items()
             if value and key.casefold().startswith("vp_")]
    return RISKY if plain else OPENNEOUA


def decal_level(values: dict[str, str]) -> str:
    """ground_decal_id / _size are OpenNeoUA keys; unset they write nothing and stay vanilla."""

    return OPENNEOUA if values.get("ground_decal_id") or values.get("ground_decal_size") else VANILLA
