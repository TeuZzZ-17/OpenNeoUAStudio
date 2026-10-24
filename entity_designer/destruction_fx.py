"""Destruction FX preview: script reading, engine rules and simulation.

The Visuals & FX tab previews effects and provides live script-text editing
through the same parser. Two effect systems are read:

* OpenNeoUA ``begin_fx ... end`` blocks (Chain FX). Parsing and validation
  mirror ``ParseChainFXBlock`` in ``world/parsers.cpp``; the visual timeline
  mirrors ``SpawnChainFX`` and the transient VP update in ``yw_game.cpp``.
* Vanilla ``dest_fx = type_vehicle_x_y_z[_accel]`` lines. Each line spawns an
  FX *vehicle*; that vehicle either runs its own ``scale_fx`` animation
  (``CrashOrLand__sub0``) or falls with the dead-state physics of
  ``NC_STACK_ypabact::Move`` before its megadeth VP.

The preview replays the whole death of the definition: unit or projectile
models (VP, BASE or 3DS, as the engine resolves them), chain effects,
fragments, decals, FX vehicles and their particle emitters. The module has
no Qt dependency: the tab and the viewport only consume its plain data.
"""

from __future__ import annotations

from studio.rendering.particles import (
    EmitterSpec, ParticleSprite, _emit_particles, _rotate_vector,
)

import math
import random
import re
from dataclasses import dataclass, field, replace

from entity_designer import decal_smoke

CONTEXT_VEHICLE = "vehicle"
CONTEXT_WEAPON = "weapon"

MODE_VISUAL = "visual"
MODE_PHYSICAL = "physical"
# Ground decals are not a begin_fx mode: they come from the reusable
# ``new_decal`` profiles that vehicles/weapons link with ground_decal_id.
MODE_DECAL = "ground_decal"
MODES = (MODE_VISUAL, MODE_PHYSICAL)

TRIGGERS = {
    CONTEXT_VEHICLE: ("destroyed", "air_destroyed"),
    CONTEXT_WEAPON: ("detonate", "impact_world", "launch"),
}
LEGACY_TRIGGERS = ("death", "megadeth", "create", "beam")
DEST_FX_KEYS = ("dest_fx", "ext_dest_fx", "extended_dest_fx")

# Runtime limits shared with the engine parser.
MAX_COUNT = 32
MAX_SCALE_FX_VPS = 32
SPIN_MAX = 10.0

# Dead-state gravity and position factor of NC_STACK_ypabact::Move.
DEAD_GRAVITY = 39.2266
MOVE_POSITION_FACTOR = 6.0
# Non-inline dead vehicles end after 5 s without ground contact
# (ChangeSectorEnergy deadLifetimeMs).
LEGACY_DEAD_LIFETIME_MS = 5000
# The engine shows the impact/megadeth VP on the terminal frame of a dead
# vehicle. The preview keeps it visible briefly so it can be inspected.
IMPACT_PREVIEW_MS = 600
# Decal smoke lasts minutes in the engine; the preview plays only its first part
# so the timeline stays usable.
SMOKE_PREVIEW_MS = 20000.0
SIMULATION_STEP_MS = 10.0
# The engine keeps the four strongest shakes; the preview draws a new random
# turn of the view about 60 times a second.
MAX_SHAKES = 4
SHAKE_FRAME_MS = 16.0
# Most frames a painted preview frame looks back over for the strongest shake.
SHAKE_SCAN_FRAMES = 12
# Source id of the profile decal in timeline rows (not an item of the document).
PROFILE_DECAL_SOURCE = -5
LEGACY_NESTING_LIMIT = 3

_SOUND_PREFIXES = ("snd_", "pal_", "shk_")  # shk_ is read by the game, never previewed
_VISUAL_KEYS = ("vp_model", "base_model", "3ds_model", "tint")

SEVERITY_FATAL = "fatal"      # the whole script file stops loading
SEVERITY_IGNORED = "ignored"  # the engine silently drops this block/line
SEVERITY_WARNING = "warning"  # loads, but with a fallback value
SEVERITY_UNKNOWN = "unknown"  # key not known to Studio (maybe a newer engine)


@dataclass(frozen=True)
class FxIssue:
    severity: str
    message: str


# -- engine tokenizer --------------------------------------------------------

def engine_tokens(line: str) -> tuple[str, str] | None:
    """Split one script line exactly like ParseChainFXBlock does.

    ``;#!`` re-activates the rest of a commented line, everything after the
    first ``;`` is a comment, and ``=``, spaces and tabs all separate tokens.
    Only the first token after the key is the value.
    """

    marker = line.find(";#!")
    if marker >= 0:
        line = line[marker + 3:]
    line = re.split(r"[;\r\n]", line, maxsplit=1)[0]
    parts = [part for part in re.split(r"[= \t]+", line) if part]
    if not parts:
        return None
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _key(line: str) -> str:
    tokens = engine_tokens(line)
    return tokens[0].casefold() if tokens else ""


def _strtod_prefix(text: str) -> tuple[float, int]:
    """Return a C ``stof``-style prefix parse: (value, consumed characters)."""

    match = re.match(
        r"\s*[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?"
        r"|inf(?:inity)?|nan)", text, re.IGNORECASE)
    if match is None:
        return 0.0, 0
    try:
        return float(match.group(0)), match.end()
    except ValueError:
        return 0.0, 0


def _stof(text: str) -> float:
    return _strtod_prefix(text)[0]


def _stol(text: str) -> tuple[int, int]:
    """C ``stol`` with base 0 (accepts 0x.. hex); returns (value, consumed)."""

    match = re.match(r"\s*([+-]?)(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9]\d*)", text)
    if match is None:
        return 0, 0
    sign = -1 if match.group(1) == "-" else 1
    digits = match.group(2)
    if digits.lower().startswith("0x"):
        value = int(digits, 16)
    elif len(digits) > 1 and digits.startswith("0"):
        value = int(digits, 8)
    else:
        value = int(digits)
    return sign * value, match.end()


def script_int(text: str | None, default: int = 0) -> int:
    """Integer value of a script field the way the engine's stol reads it."""

    if not text:
        return default
    value, used = _stol(text)
    return value if used else default


def _full_float(text: str) -> float | None:
    value, used = _strtod_prefix(text)
    if used != len(text) or not text:
        return None
    return value if math.isfinite(value) else None


def _full_int(text: str) -> int | None:
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def split_tuple(text: str | None, count: int) -> list[str] | None:
    """``SplitTupleValue``: exactly *count* non-empty ``a_b_c`` parts, else None."""

    if text is None:
        return None
    parts = text.split("_")
    if len(parts) != count or not all(parts):
        return None
    return parts


def parse_float_range(text: str, separator: str = "_") -> tuple[float, float] | None:
    """``ParseFloatRangeValue``: fixed ``v`` or ``min_max`` (order normalized).

    Inside a compact tuple such as ``offset_xyz`` the range separator is ``~``.
    """

    parts = text.split(separator)
    if len(parts) == 1:
        value = _full_float(text)
        return None if value is None else (value, value)
    if len(parts) != 2:
        return None
    low, high = _full_float(parts[0]), _full_float(parts[1])
    if low is None or high is None:
        return None
    return (min(low, high), max(low, high))


def parse_int_range(text: str) -> tuple[int, int] | None:
    parts = text.split("_")
    if len(parts) == 1:
        value = _full_int(text)
        return None if value is None else (value, value)
    if len(parts) != 2:
        return None
    low, high = _full_int(parts[0]), _full_int(parts[1])
    if low is None or high is None:
        return None
    return (min(low, high), max(low, high))


def parse_int_list(text: str, *, allow_zero: bool,
                   max_items: int = MAX_COUNT) -> list[int] | None:
    """``ParseInt16ListValue``: underscore list of positive (or >=0) int16."""

    parts = text.split("_")
    if not text or len(parts) > max_items:
        return None
    values = []
    for part in parts:
        value = _full_int(part)
        if value is None or value > 32767 or value < (0 if allow_zero else 1):
            return None
        values.append(value)
    return values


def parse_tint(text: str) -> tuple[int, int, int, int] | None:
    """R_G_B[_A], each 0..255 (clamped); None when malformed."""

    parts = text.split("_")
    if len(parts) not in (3, 4):
        return None
    values = []
    for part in parts:
        value = _full_int(part)
        if value is None:
            return None
        values.append(max(0, min(255, value)))
    if len(values) == 3:
        values.append(255)
    return tuple(values)


def format_number(value: float) -> str:
    if not math.isfinite(value):
        return "0"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def format_range(low: float, high: float) -> str:
    if abs(low - high) < 1e-9:
        return format_number(low)
    return f"{format_number(low)}_{format_number(high)}"


# -- sounds --------------------------------------------------------------------

VEHICLE_SOUND_EVENTS = (
    "normal", "fire", "wait", "genesis", "explode", "crashland", "crashvhcl",
    "goingdown", "cockpit", "beamin", "beamout", "build", "airexplode")
# launch_player only has a shake package (shk_launch_player_*): the view shake
# of the player's own launches.
WEAPON_SOUND_EVENTS = ("normal", "launch", "hit", "launch_player")
PHYSICAL_SOUND_EVENTS = ("normal", "goingdown", "explode")
# Sounds that keep playing while their state lasts (TVhclProto::IsLoopingSnd).
LOOPING_VEHICLE_SOUNDS = frozenset({
    "normal", "fire", "wait", "genesis", "goingdown", "cockpit"})


@dataclass(frozen=True)
class ScreenFx:
    """``pal_<event>_*``: a palette flash of the whole view.

    The strength goes from ``mag0`` to ``mag1`` over ``time`` ms; a looping
    event holds ``mag0`` for as long as it loops.
    """

    slot: int
    mag0: float = 0.0
    mag1: float = 0.0
    time: int = 0


@dataclass(frozen=True)
class ShakeFx:
    """``shk_<event>_*``: the whole view shakes while the event plays.

    The strength goes from ``mag0`` to ``mag1`` over ``time`` ms (a looping event
    holds ``mag0``). Every frame the engine turns the view by random angles
    ``rnd * strength * pos`` around X, Y and Z (radians). ``radius`` and ``mute``
    only weaken the shake with the distance from the listener; the preview plays
    the event at the listener, so they are not applied.
    """

    slot: int
    mag0: float = 1.0
    mag1: float = 0.0
    time: int = 1000
    pos: tuple[float, float, float] = (0.2, 0.2, 0.2)


@dataclass(frozen=True)
class SoundSpec:
    """The ``snd_``, ``pal_`` and ``shk_`` packages of one event.

    ``sample`` may be empty when the event only flashes the palette. The
    engine plays the sample at (sample rate + pitch), a random whole pitch of
    the inclusive range for every play.
    """

    sample: str = ""
    volume: int = 0
    pitch: tuple[int, int] = (0, 0)
    palette: ScreenFx | None = None
    shake: ShakeFx | None = None


def _palette_fx(values: dict[str, str], event: str) -> ScreenFx | None:
    slot = script_int(values.get(f"pal_{event}_slot"))
    if slot == 0:
        return None
    return ScreenFx(
        slot=slot, mag0=_stof(values.get(f"pal_{event}_mag0", "0")),
        mag1=_stof(values.get(f"pal_{event}_mag1", "0")),
        time=script_int(values.get(f"pal_{event}_time")))


def _shake_fx(values: dict[str, str], event: str) -> ShakeFx | None:
    slot = script_int(values.get(f"shk_{event}_slot"))
    if event == "launch_player":
        # The player launch shake reads only these four keys; negatives become 0.
        if slot <= 0:
            return None
        time = max(0, script_int(values.get(f"shk_{event}_time"), 1000))
        mag0 = _stof(values.get(f"shk_{event}_mag0", "1"))
        mag1 = _stof(values.get(f"shk_{event}_mag1", "0"))
        return ShakeFx(
            slot=slot, time=time,
            mag0=mag0 if math.isfinite(mag0) and mag0 >= 0 else 0.0,
            mag1=mag1 if math.isfinite(mag1) and mag1 >= 0 else 0.0)
    if slot == 0:
        return None
    return ShakeFx(
        slot=slot, mag0=_stof(values.get(f"shk_{event}_mag0", "1")),
        mag1=_stof(values.get(f"shk_{event}_mag1", "0")),
        time=script_int(values.get(f"shk_{event}_time"), 1000),
        pos=tuple(_stof(values.get(f"shk_{event}_{axis}", "0.2"))
                  for axis in "xyz"))


def sound_specs(values: dict[str, str], events) -> dict[str, SoundSpec]:
    """Every event of ``events`` with a sample, a palette flash or a shake."""

    specs: dict[str, SoundSpec] = {}
    for event in events:
        sample = values.get(f"snd_{event}_sample", "")
        palette = _palette_fx(values, event)
        shake = _shake_fx(values, event)
        if not (sample or palette or shake):
            continue
        pitch = parse_int_range(values.get(f"snd_{event}_pitch", "")) or (0, 0)
        specs[event] = SoundSpec(
            sample=sample, volume=script_int(values.get(f"snd_{event}_volume")),
            pitch=pitch, palette=palette, shake=shake)
    return specs


# Palette slots 0..7 of the engine (GFXEngine::_clrEff); 4, 5 and 7 invert.
CLASSIC_PALETTE = (
    (1.0, 1.0, 1.0), (1.21, 0.0, 0.29), (0.13, 0.43, 2.17), (0.0, 1.60, 1.60),
    (1.0, 1.0, 1.0), (0.57, 0.59, 0.59), (1.4, 1.08, 1.12), (0.3, 0.60, 0.7))
INVERSE_PALETTE_SLOTS = frozenset({4, 5, 7})
MAX_PALETTE_EFFECTS = 8


@dataclass(frozen=True)
class EffectSettings:
    """Game-wide OpenNeoUA.ini values that shape sounds, palette flashes and shakes."""

    # game.global_shk_fade_inout / game.global_pal_fade_inout: (in, out) ms.
    shake_fade: tuple[int, int] = (0, 0)
    palette_fade: tuple[int, int] = (0, 0)
    # gfx.color_effects: 0 switches every palette flash off.
    color_effects: int = 1
    # audio.num_palfx: how many palette flashes play at once (0..8, 0 = none).
    num_palfx: int = 4
    # audio.volume: master volume of the samples (0..127).
    volume: int = 127


def parse_global_fade(value: str) -> tuple[int, int]:
    """``game.global_*_fade_inout = in_out`` in ms; anything malformed is 0_0."""

    parts = split_tuple(value, 2)
    if parts is None:
        return (0, 0)

    def milliseconds(text: str) -> int:
        number = _full_float(text) if "," not in text else None
        if number is None or number <= 0:
            return 0
        return int(min(round(number), 2147483647))

    return (milliseconds(parts[0]), milliseconds(parts[1]))


def parse_effect_settings(text: str) -> EffectSettings:
    """The effect-related keys of OpenNeoUA.ini, with the engine's defaults."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        code = line.split(";", 1)[0].strip()
        key, _sep, value = code.partition("=")
        if key.strip():
            values[key.strip().casefold()] = value.strip()

    def integer(key: str, default: int, low: int, high: int) -> int:
        parsed = _full_int(values.get(key, ""))
        return default if parsed is None else max(low, min(parsed, high))

    return EffectSettings(
        shake_fade=parse_global_fade(values.get("game.global_shk_fade_inout", "")),
        palette_fade=parse_global_fade(values.get("game.global_pal_fade_inout", "")),
        color_effects=integer("gfx.color_effects", 1, 0, 99),
        num_palfx=integer("audio.num_palfx", 4, 0, 8),
        volume=integer("audio.volume", 127, 0, 127))


def parse_color_settings(text: str) -> tuple[dict[int, tuple], dict[int, float]]:
    """Custom palette colors (slots 8..16) and slot powers from OpenNeoUA.ini."""

    custom: dict[int, tuple] = {}
    powers: dict[int, float] = {}
    for line in text.splitlines():
        code = line.split(";", 1)[0].strip()
        key, _sep, value = code.partition("=")
        key, value = key.strip().casefold(), value.strip()
        match = re.fullmatch(r"gfx\.color_eff\[(\d+)\]", key)
        if match and 8 <= int(match[1]) <= 16:
            parts = [part for part in re.split(r"[_,\s]+", value) if part]
            if len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts):
                custom[int(match[1])] = tuple(int(p) / 255.0 for p in parts)
            continue
        match = re.fullmatch(r"gfx\.color_eff_pwr\[(\d+)\]", key)
        if match and 1 <= int(match[1]) <= 7 and _full_float(value) is not None:
            powers[int(match[1])] = max(0.0, min(100.0, _full_float(value))) / 100.0
    return custom, powers


def palette_colors(effects, custom=None, powers=None):
    """(normclr, invclr) of the active palette effects, like the engine.

    ``effects`` is [(slot, strength)]. The screen becomes
    ``scene * normclr + (1 - scene) * invclr``. None when nothing is active.
    """

    if not effects:
        return None
    custom, powers = custom or {}, powers or {}
    remaining = 1.0
    chosen = []
    for slot, strength in sorted(effects, key=lambda entry: -entry[1])[:MAX_PALETTE_EFFECTS]:
        power = (powers.get(slot, 1.0) if 1 <= slot <= 7
                 else 1.0 if slot in custom else 0.0)
        chosen.append((slot, strength * power))
        remaining -= strength * power
    if remaining > 0:
        chosen.append((0, remaining))
    norm, inv, applied = [0.0] * 3, [0.0] * 3, False
    for slot, power in chosen:
        if 0 <= slot < len(CLASSIC_PALETTE):
            color, inverse = CLASSIC_PALETTE[slot], slot in INVERSE_PALETTE_SLOTS
        elif slot in custom:
            color, inverse = custom[slot], False
        else:
            continue
        applied = True
        target = inv if inverse else norm
        for axis in range(3):
            target[axis] += color[axis] * power
    return (tuple(norm), tuple(inv)) if applied else ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0))


@dataclass(frozen=True)
class SoundEvent:
    """A sound the preview plays: one-shot at ``start`` or a loop until ``end``."""

    start: float
    end: float | None          # None = plays once to its end
    spec: SoundSpec
    event: str                 # normal, launch, explode ...
    pitch: int = 0
    label: str = ""

    @property
    def loops(self) -> bool:
        return self.end is not None


# -- script blocks (read only) -----------------------------------------------

@dataclass
class FxBlock:
    """One ``begin_fx ... end`` block as its original script lines."""

    lines: list[str]

    def rows(self) -> list[tuple[int, str, str]]:
        """Active (line index, lowercase key, value) rows of the body."""

        rows = []
        for index in range(1, max(1, len(self.lines) - 1)):
            tokens = engine_tokens(self.lines[index])
            if tokens is not None:
                rows.append((index, tokens[0].casefold(), tokens[1]))
        return rows

    def value(self, key: str) -> str | None:
        found = None
        for _index, row_key, value in self.rows():
            if row_key == key:
                found = value
        return found

    @property
    def mode(self) -> str:
        return (self.value("mode") or MODE_VISUAL).casefold()

    @property
    def trigger(self) -> str:
        return (self.value("trigger") or "").casefold()


@dataclass(frozen=True)
class DestFx:
    """One vanilla ``dest_fx`` line: spawn FX vehicle ``vehicle_id``."""

    kind: str
    vehicle_id: int
    x: float
    y: float
    z: float
    accel: bool = False


def parse_dest_fx(value: str) -> DestFx | None:
    """Parse the ``Stok(p2, " _")`` dest_fx value; None = RESULT_BAD_DATA."""

    parts = [part for part in re.split(r"[ _]+", value) if part]
    if len(parts) < 5 or parts[0].casefold() not in LEGACY_TRIGGERS:
        return None
    vehicle_id = _stol(parts[1])[0]
    x, y, z = (_stof(part) for part in parts[2:5])
    accel = len(parts) > 5 and _stol(parts[5])[0] != 0
    return DestFx(parts[0].casefold(), vehicle_id, x, y, z, accel)


@dataclass
class DestFxLine:
    """Raw ``dest_fx`` script line plus its parsed value."""

    line: str

    @property
    def parsed(self) -> DestFx | None:
        # dest_fx values can contain spaces (" _" separators): keep the whole
        # right-hand side, not only the first engine token.
        code = re.split(r";", self.line, maxsplit=1)[0]
        return parse_dest_fx(code.partition("=")[2].strip())


@dataclass(frozen=True)
class ScaleFx:
    """Vanilla ``scale_fx = start_speed_accel_duration_vp1_vp2...``."""

    start: float
    speed: float
    accel: float
    duration: int
    vps: tuple[int, ...]


def parse_scale_fx(value: str) -> ScaleFx | None:
    parts = [part for part in value.split("_") if part != ""]
    if len(parts) < 4:
        return None
    vps = []
    for part in parts[4:]:
        vp = _stol(part)[0]
        # The runtime stops at the first zero entry.
        if vp == 0:
            break
        vps.append(vp)
    return ScaleFx(_stof(parts[0]), _stof(parts[1]), _stof(parts[2]),
                   _stol(parts[3])[0], tuple(vps[:MAX_SCALE_FX_VPS]))


# Top-level snd_/pal_/shk_ keys of a definition, grouped by effect: every key
# group of one kind and event is a row of the effects list.
KEY_GROUP_KINDS = {"snd": "sound", "pal": "palette", "shk": "shake"}
KEY_GROUP_FIELDS = {
    "sound": ("sample", "volume", "pitch", "radius", "ext"),
    "palette": ("slot", "mag0", "mag1", "time", "radius"),
    "shake": ("slot", "mag0", "mag1", "time", "radius", "mute", "x", "y", "z"),
}


def key_group_of(key: str) -> tuple[str, str] | None:
    """(kind, event) of a snd_/pal_/shk_ key of a known sound event, else None."""

    prefix, _sep, rest = key.casefold().partition("_")
    kind = KEY_GROUP_KINDS.get(prefix)
    if kind is None:
        return None
    # The longest event name wins: launch_player is not the launch event.
    known = sorted(set(VEHICLE_SOUND_EVENTS) | set(WEAPON_SOUND_EVENTS),
                   key=len, reverse=True)
    for event in known:
        if rest.startswith(event + "_") and len(rest) > len(event) + 1:
            return kind, event
    return None


@dataclass
class FxKeyGroup:
    """The consecutive ``snd_`` / ``pal_`` / ``shk_`` lines of one sound event.

    ``lines`` keeps the comments and blank lines between the keys.
    """

    lines: list[str]

    def rows(self) -> list[tuple[int, str, str]]:
        rows = []
        for index, line in enumerate(self.lines):
            tokens = engine_tokens(line)
            if tokens is not None:
                rows.append((index, tokens[0].casefold(), tokens[1]))
        return rows

    @property
    def values(self) -> dict[str, str]:
        """Last value of every key of the group."""

        return {key: value for _index, key, value in self.rows()}

    @property
    def group(self) -> tuple[str, str]:
        for _index, key, _value in self.rows():
            found = key_group_of(key)
            if found is not None:
                return found
        return ("sound", "")

    @property
    def kind(self) -> str:
        return self.group[0]

    @property
    def event(self) -> str:
        return self.group[1]


@dataclass
class FxBlank:
    """A new, still empty effect: what is written in it decides what it becomes."""

    lines: list[str] = field(default_factory=list)


FxItem = FxBlock | DestFxLine | FxKeyGroup | FxBlank


@dataclass
class FxDocument:
    """Every Destruction FX item of one script definition, in source order."""

    items: list[FxItem] = field(default_factory=list)


def _scope_token(line: str) -> str:
    code = re.split(r";|//|#", line.lstrip(), maxsplit=1)[0].strip()
    return code.split(None, 1)[0].casefold() if code else ""


def _fx_item_ranges(lines: list[str], start_line: int, end_line: int):
    """Yield (first, last, kind) for every FX item inside one definition.

    Nesting follows the script grammar: ``begin``/``begin_*`` opens a scope
    and ``end`` closes it. Only direct children of the definition count.
    Consecutive snd_/pal_/shk_ lines of the same event form one "keys" item.
    """

    depth = 1
    index = start_line + 1
    run = None   # [first, last, (kind, event)] of the key group being read
    while index < end_line:
        token = _scope_token(lines[index])
        keys = key_group_of(token_key) if depth == 1 and (
            token_key := _key(lines[index])) else None
        if run is not None and keys != run[2] and token:
            yield run[0], run[1], "keys"
            run = None
        if keys is not None:
            if run is None:
                run = [index, index, keys]
            else:
                run[1] = index
        elif depth == 1 and token == "begin_fx":
            first = index
            index += 1
            nested = 1
            while index < end_line:
                inner = _scope_token(lines[index])
                if inner == "begin" or inner.startswith("begin_"):
                    nested += 1
                elif inner == "end":
                    nested -= 1
                    if nested == 0:
                        break
                index += 1
            yield first, min(index, end_line - 1), "block"
        elif token == "begin" or token.startswith("begin_"):
            depth += 1
        elif token == "end":
            depth -= 1
        elif depth == 1 and token in DEST_FX_KEYS:
            yield index, index, "dest"
        index += 1
    if run is not None:
        yield run[0], run[1], "keys"


def extract_fx_document(text: str, start_line: int, end_line: int) -> FxDocument:
    """Read the Destruction FX items of one definition."""

    lines = text.splitlines()
    items: list[FxItem] = []
    for first, last, kind in _fx_item_ranges(lines, start_line, end_line):
        if kind == "block":
            items.append(FxBlock(lines[first:last + 1]))
        elif kind == "keys":
            items.append(FxKeyGroup(lines[first:last + 1]))
        else:
            items.append(DestFxLine(lines[first]))
    return FxDocument(items)


def top_level_value(text: str, start_line: int, end_line: int,
                    key: str) -> str | None:
    """Last top-level value of ``key`` inside one definition."""

    lines = text.splitlines()
    depth = 1
    found = None
    for index in range(start_line + 1, end_line):
        token = _scope_token(lines[index])
        if token == "begin" or token.startswith("begin_"):
            depth += 1
        elif token == "end":
            depth -= 1
        elif depth == 1 and token == key:
            code = re.split(r";", lines[index], maxsplit=1)[0]
            found = code.partition("=")[2].strip()
    return found



# -- engine-equivalent interpretation ----------------------------------------

@dataclass(frozen=True)
class VisualRef:
    vp: int = 0
    mesh3ds: str = ""
    base_path: str = ""
    tint: tuple[int, int, int, int] | None = None

    @property
    def label(self) -> str:
        if self.mesh3ds:
            return f"3DS {self.mesh3ds}"
        if self.base_path:
            return f"BASE {self.base_path}"
        return f"VP {self.vp}"


@dataclass(frozen=True)
class FxSpec:
    """Effective configuration the engine would build from one block."""

    mode: str
    trigger: str
    active: bool
    issues: tuple[FxIssue, ...]
    count: tuple[int, int] = (1, 1)
    offset_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    offset_max: tuple[float, float, float] = (0.0, 0.0, 0.0)
    random_rotation: bool = False
    spin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    start_size: float = 1.0
    mid_size: float | None = None
    end_size: float = 0.0
    duration: int = 0
    fade_in: int = 0
    fade_out: int = 0
    visuals: tuple[VisualRef, ...] = ()
    # Physical (inline)
    launch_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    launch_max: tuple[float, float, float] = (0.0, 0.0, 0.0)
    physical_vehicle: int = 0
    vp_models: tuple[int, ...] = ()
    vp_impacts: tuple[int, ...] = ()
    mass: float = 0.0
    airconst: float = 0.0
    force: float = 0.0
    maxrot: float = 0.0
    radius: float = 0.0
    lifetime: tuple[int, int] = (30000, 30000)
    visual_scale: float = 1.0
    physical_tint: tuple[int, int, int, int] | None = None
    physical_model: VisualRef | None = None
    # follow_carcass: the fragment mirrors the falling wreck of the unit.
    follow_carcass: bool = False
    # Ground decal
    texture: str = ""
    size: tuple[float, float] = (0.0, 0.0)
    stretch_x: tuple[float, float] = (1.0, 1.0)
    stretch_z: tuple[float, float] = (1.0, 1.0)
    decal_tint: tuple[int, int, int, int] = (255, 255, 255, 255)
    edge_fade: tuple[float, float] = (0.0, 0.0)
    permanent: bool = False
    # Smoke rising from the decal (None = the block has no smoke).
    smoke: decal_smoke.SmokeSpec | None = None
    fire: decal_smoke.SmokeSpec | None = None
    fire_propagation_chance: float = 0.0
    fire_propagation_radius: float = 0.0
    fire_propagation_count: int = 0
    fire_propagation_delay: int = 1500
    # Physical fragments: normal / goingdown / explode sounds.
    sounds: dict = field(default_factory=dict)

    @property
    def fatal(self) -> bool:
        return any(issue.severity == SEVERITY_FATAL for issue in self.issues)


def _nonnegative_ms(text: str) -> int:
    value = _stof(text)
    if not math.isfinite(value) or value <= 0:
        return 0
    return int(value)


def _interpret_smoke(rows: dict[str, str], issue) -> decal_smoke.SmokeSpec:
    """The smoke_* keys of a new_decal profile, with the engine's fallbacks.

    Mirrors ``ParseGroundDecalSmokeParam``: a bad value keeps the default of
    that key (with a warning), so a typo never removes the whole smoke.
    """

    defaults = decal_smoke.SmokeSpec()

    def bounded_int(key: str, low: int, high: int, default: int,
                    text: str | None = None) -> int:
        if text is None:
            if key not in rows:
                return default
            text = rows[key]
        if not re.fullmatch(r"[+-]?\d+", text):
            issue(SEVERITY_WARNING, f"Invalid {key} '{text}': using {default}.")
            return default
        return max(low, min(int(text), high))

    def int_range(key: str, low: int, high: int,
                  default: tuple[int, int]) -> tuple[int, int]:
        if key not in rows:
            return default
        parsed = parse_int_range(rows[key])
        if parsed is None:
            issue(SEVERITY_WARNING, f"Invalid {key} '{rows[key]}': "
                  f"using {default[0]}_{default[1]}.")
            return default
        return (max(low, min(parsed[0], high)), max(low, min(parsed[1], high)))

    def number(key: str, low: float, high: float, default: float) -> float:
        if key not in rows:
            return default
        parsed = _full_float(rows[key])
        if parsed is None:
            issue(SEVERITY_WARNING, f"Invalid {key} '{rows[key]}': "
                  f"using {format_number(default)}.")
            return default
        return max(low, min(parsed, high))

    size = defaults.size
    if "smoke_size" in rows:
        parsed = parse_float_range(rows["smoke_size"])
        if parsed and parsed[0] > 0 and parsed[1] > 0:
            size = (max(0.5, min(parsed[0], 200.0)), max(0.5, min(parsed[1], 200.0)))
        else:
            issue(SEVERITY_WARNING, f"Invalid smoke_size '{rows['smoke_size']}': "
                  f"using {format_range(*defaults.size)}.")

    tint = defaults.tint
    if "smoke_tint" in rows:
        parsed_tint = parse_tint(rows["smoke_tint"])
        if parsed_tint is None:
            issue(SEVERITY_WARNING, f"Invalid smoke_tint '{rows['smoke_tint']}': "
                  "using 255_255_255_255.")
            tint = (255, 255, 255, 255)
        else:
            tint = parsed_tint

    fade = (defaults.fade_in, defaults.fade_out)
    if "smoke_fade_inout" in rows:
        parts = split_tuple(rows["smoke_fade_inout"], 2)
        if parts is None:
            issue(SEVERITY_WARNING,
                  f"Invalid smoke_fade_inout '{rows['smoke_fade_inout']}': "
                  f"using {fade[0]}_{fade[1]}.")
        else:
            fade = tuple(
                bounded_int("smoke_fade_inout", 0, 3600000, default, part)
                for part, default in zip(parts, fade))

    return decal_smoke.SmokeSpec(
        enabled=True,
        chance=bounded_int("smoke_chance", 0, 100, defaults.chance),
        duration=int_range("smoke_duration", 0, 3600000, defaults.duration),
        fade_in=fade[0],
        fade_out=fade[1],
        count=int_range("smoke_count", 0, decal_smoke.MAX_PUFFS, defaults.count),
        size=size,
        growth=number("smoke_growth", 1.0, 10.0, defaults.growth),
        rise_speed=number("smoke_rise_speed", 0.0, 200.0, defaults.rise_speed),
        puff_time=int_range("smoke_puff_time", 200, 60000, defaults.puff_time),
        drift=number("smoke_drift", 0.0, 300.0, defaults.drift),
        spread=number("smoke_spread", 0.0, 100.0, defaults.spread),
        intensity=number("smoke_intensity", 0.0, 300.0, defaults.intensity),
        tint=tint)


def interpret_block(block: FxBlock, context: str) -> FxSpec:
    """Interpret one block with the exact rules of ParseChainFXBlock."""

    issues: list[FxIssue] = []

    def issue(severity: str, message: str) -> None:
        issues.append(FxIssue(severity, message))

    mode = MODE_VISUAL
    trigger = ""
    has_trigger = bad_trigger = bad_mode = False
    start_size, mid_size, end_size = 1.0, None, 0.0
    has_end_size = False
    count = (1, 1)
    offset_min = [0.0, 0.0, 0.0]
    offset_max = [0.0, 0.0, 0.0]
    launch_min = [0.0, 0.0, 0.0]
    launch_max = [0.0, 0.0, 0.0]
    spin = [0.0, 0.0, 0.0]
    duration = 0
    fade_in = fade_out = 0
    visuals: list[VisualRef] = []
    visual_tint = None
    physical_vehicle = 0
    has_physical_vehicle_key = has_inline = has_offset = False
    invalid_inline = False
    vp_models: list[int] = []
    vp_impacts: list[int] = []
    mesh3ds = base_model = ""
    physical_tint = None
    visual_scale = 1.0
    mass = airconst = force = maxrot = radius = 0.0
    lifetime = (30000, 30000)
    random_rotation = False
    follow_carcass = False
    sound_values: dict[str, str] = {}

    if not block.lines or _key(block.lines[-1]) != "end":
        issue(SEVERITY_FATAL,
              "Missing 'end': the rest of the script is read as part of this "
              "block (unexpected end of file).")

    for _index, key, value in block.rows():
        if key == "end":
            break
        if bad_mode:
            continue
        if key == "mode":
            if value.casefold() in MODES:
                mode = value.casefold()
            else:
                issue(SEVERITY_IGNORED,
                      f"Unknown mode '{value}': the whole block is ignored.")
                bad_mode = True
        elif key == "trigger":
            trigger = value.casefold()
            has_trigger = True
            if trigger not in TRIGGERS[context]:
                allowed = " / ".join(TRIGGERS[context])
                issue(SEVERITY_IGNORED,
                      f"Trigger '{value}' is not valid for a {context} "
                      f"(use {allowed}): the block is ignored.")
                bad_trigger = True
        elif key == "start_size":
            start_size = _stof(value)
        elif key == "mid_size":
            parsed = _full_float(value)
            if parsed is not None and parsed >= 0:
                mid_size = parsed
            else:
                issue(SEVERITY_WARNING,
                      f"Invalid mid_size '{value}': linear start -> end used.")
                mid_size = None
        elif key == "end_size":
            end_size = _stof(value)
            has_end_size = True
        elif key == "duration":
            duration = _stol(value)[0]
        elif key == "fade_inout":
            parts = split_tuple(value, 2)
            if parts is None:
                issue(SEVERITY_WARNING,
                      f"Invalid fade_inout '{value}': expected in_out.")
            else:
                fade_in, fade_out = _nonnegative_ms(parts[0]), _nonnegative_ms(parts[1])
        elif key == "count":
            parsed = parse_int_range(value)
            if parsed is None or parsed[0] <= 0 or parsed[1] <= 0:
                issue(SEVERITY_WARNING, f"Invalid count '{value}': using 1.")
                count = (1, 1)
            else:
                count = (min(parsed[0], MAX_COUNT), min(parsed[1], MAX_COUNT))
        elif key in ("offset_xyz", "launch_xyz"):
            if key == "offset_xyz":
                has_offset = True
                target_min, target_max = offset_min, offset_max
            else:
                has_inline = True
                target_min, target_max = launch_min, launch_max
            # x_y_z where every axis is a fixed value or a "min~max" range.
            parts = split_tuple(value, 3)
            axes = ([parse_float_range(part, "~") for part in parts]
                    if parts is not None else None)
            if axes is None or any(axis is None for axis in axes):
                issue(SEVERITY_WARNING, f"Invalid {key} '{value}': using 0_0_0.")
                axes = [(0.0, 0.0)] * 3
            for axis, (low, high) in enumerate(axes):
                target_min[axis], target_max[axis] = low, high
        elif key == "spin_xyz":
            parts = split_tuple(value, 3)
            if parts is None:
                issue(SEVERITY_WARNING,
                      f"Invalid spin_xyz '{value}': expected x_y_z.")
            else:
                for axis, part in enumerate(parts):
                    raw = _stof(part)
                    spin[axis] = (
                        0.0 if not math.isfinite(raw) or raw <= 0 else min(raw, SPIN_MAX))
        elif key == "vp_model":
            has_inline = True
            if mode == MODE_PHYSICAL:
                if value == "0":
                    vp_models = [0]
                else:
                    parsed = parse_int_list(value, allow_zero=False)
                    if parsed is None:
                        vp_models = []
                        invalid_inline = True
                        issue(SEVERITY_WARNING,
                              f"Invalid vp_model list '{value}'.")
                    else:
                        vp_models = parsed
                continue
            visuals.append(VisualRef(vp=_stol(value)[0]))
        elif key == "3ds_model":
            has_inline = True
            mesh3ds = value
            last = visuals[-1] if visuals else None
            if (last is not None and (last.vp > 0 or last.base_path)
                    and not last.mesh3ds):
                visuals[-1] = VisualRef(last.vp, value, last.base_path, last.tint)
            else:
                visuals.append(VisualRef(mesh3ds=value))
        elif key == "base_model":
            has_inline = True
            base_model = value
            last = visuals[-1] if visuals else None
            if (last is not None and last.vp > 0 and not last.mesh3ds
                    and not last.base_path):
                visuals[-1] = VisualRef(last.vp, last.mesh3ds, value, last.tint)
            else:
                visuals.append(VisualRef(base_path=value))
        elif key == "tint":
            parsed = parse_tint(value)
            if mode == MODE_PHYSICAL:
                has_inline = True
                physical_tint = parsed or (255, 255, 255, 255)
                continue
            visual_tint = parsed or (255, 255, 255, 255)
        elif key == "vp_impact":
            has_inline = True
            parsed = parse_int_list(value, allow_zero=True)
            if parsed is None:
                vp_impacts = []
                invalid_inline = True
                issue(SEVERITY_WARNING, f"Invalid vp_impact list '{value}'.")
            else:
                vp_impacts = parsed
        elif key == "scale":
            has_inline = True
            parsed = _stof(value)
            visual_scale = parsed if math.isfinite(parsed) and parsed > 0 else 1.0
        elif key in ("impact_energy", "impact_energy_radius"):
            has_inline = True
        elif key == "follow_carcass":
            has_inline = True
            follow_carcass = value == "1"
        elif key == "lifetime":
            has_inline = True
            parsed = parse_int_range(value)
            if parsed is not None and parsed[0] > 0 and parsed[1] > 0:
                lifetime = parsed
            else:
                lifetime = (30000, 30000)
                issue(SEVERITY_WARNING,
                      f"Invalid lifetime '{value}': using 30000 ms.")
        elif key in ("mass", "airconst", "force", "maxrot", "radius"):
            has_inline = True
            parsed = _full_float(value)
            zero_ok = key in ("airconst", "force", "maxrot")
            if parsed is None or not (parsed > 0 or (zero_ok and parsed == 0)):
                if key in ("mass", "radius"):
                    invalid_inline = True
                issue(SEVERITY_WARNING, f"Invalid {key} '{value}'.")
            elif key == "mass":
                mass = parsed
            elif key == "airconst":
                airconst = parsed
            elif key == "force":
                force = parsed
            elif key == "maxrot":
                maxrot = parsed
            else:
                radius = parsed
        elif key.startswith(_SOUND_PREFIXES):
            has_inline = True
            sound_values[key] = value
        elif key == "physical_vehicle":
            has_physical_vehicle_key = True
            physical_vehicle = _stol(value)[0]
        elif key == "random_rotation":
            random_rotation = value == "1"
        else:
            # Newer engines may add keys Studio does not know yet. The line
            # is kept verbatim and the preview continues without it.
            issue(SEVERITY_UNKNOWN,
                  f"Unknown key '{key}': Studio does not know it. If your "
                  "OpenNeoUA version does not support it either, the engine "
                  "stops loading the whole script file (PARSE ERROR unknown "
                  "keyword).")

    def build(active: bool, **values) -> FxSpec:
        if mode == MODE_PHYSICAL:
            values.setdefault("sounds", sound_specs(
                sound_values, PHYSICAL_SOUND_EVENTS))
        return FxSpec(mode=mode, trigger=trigger, active=active,
                      issues=tuple(issues), **values)

    common = dict(
        count=count, offset_min=tuple(offset_min),
        offset_max=tuple(offset_max), random_rotation=random_rotation)
    if any(item.severity == SEVERITY_FATAL for item in issues) \
            or bad_trigger or bad_mode:
        return build(False, **common)
    if not has_trigger:
        issue(SEVERITY_IGNORED, "No trigger: the block is ignored.")
        return build(False, **common)
    if not has_end_size:
        end_size = 0.0

    if mode == MODE_VISUAL:
        if duration <= 0 or not visuals:
            missing = []
            if duration <= 0:
                missing.append("a positive duration")
            if not visuals:
                missing.append("at least one vp_model/base_model/3ds_model")
            issue(SEVERITY_IGNORED,
                  "Visual block needs " + " and ".join(missing)
                  + ": it is ignored.")
            return build(False, **common)
        return build(
            True, spin=tuple(spin), start_size=start_size,
            mid_size=mid_size, end_size=end_size, duration=duration,
            fade_in=fade_in, fade_out=fade_out,
            visuals=tuple(VisualRef(v.vp, v.mesh3ds, v.base_path, visual_tint)
                          for v in visuals),
            **common)

    if mode == MODE_PHYSICAL:
        has_positive_vp = any(vp > 0 for vp in vp_models)
        pairs_valid = (not vp_impacts or len(vp_impacts) == len(vp_models)
                       or (not vp_models and len(vp_impacts) == 1))
        if physical_vehicle > 0 and not has_inline:
            return build(True, physical_vehicle=physical_vehicle, **common)
        if has_physical_vehicle_key or has_offset:
            issue(SEVERITY_IGNORED,
                  "Physical block mixes legacy keys (physical_vehicle / "
                  "offset_*) with inline fragment keys: it is ignored.")
            return build(False, **common)
        if not pairs_valid:
            issue(SEVERITY_IGNORED,
                  "vp_model and vp_impact lists have different sizes: the "
                  "block is ignored.")
            return build(False, **common)
        if (invalid_inline or mass <= 0 or radius <= 0
                or not (has_positive_vp or mesh3ds or base_model)):
            missing = []
            if mass <= 0:
                missing.append("mass > 0")
            if radius <= 0:
                missing.append("radius > 0")
            if not (has_positive_vp or mesh3ds or base_model):
                missing.append("a model")
            issue(SEVERITY_IGNORED,
                  "Incomplete physical fragment"
                  + (f" (needs {', '.join(missing)})" if missing else "")
                  + ": it is ignored.")
            return build(False, **common)
        model = VisualRef(mesh3ds=mesh3ds, base_path=base_model)
        return build(
            True, launch_min=tuple(launch_min), launch_max=tuple(launch_max),
            vp_models=tuple(vp_models), vp_impacts=tuple(vp_impacts),
            mass=mass, airconst=airconst, force=force, maxrot=maxrot,
            radius=radius, lifetime=lifetime, visual_scale=visual_scale,
            physical_tint=physical_tint, physical_model=model,
            follow_carcass=follow_carcass, **common)


# -- reusable ground decal profiles (new_decal) ---------------------------------

@dataclass(frozen=True)
class DecalProfile:
    """One reusable ``new_decal`` profile with an authored world-unit size."""
    profile_id: int
    spec: FxSpec
    name: str = ""

    def resolve(self, trigger: str, size=(0.0, 0.0)) -> FxSpec:
        return replace(self.spec, trigger=trigger, size=size)

def _interpret_fire(values: dict[str, str], issue) -> tuple[decal_smoke.SmokeSpec | None, float, float, int, int]:
    raw_gate = values.get("fire_change", "0").removesuffix("%")
    parsed_gate=max(0,min(100,int(raw_gate))) if re.fullmatch(r"[+-]?\d+",raw_gate) else 0
    if parsed_gate <= 0:
        return None, 0.0, 0.0, 0, 1500
    mapped = {"smoke_duration": values.get("fire_duration", "8000_16000"), "smoke_fade_inout": values.get("fire_fade_inout", "300_4000"), "smoke_count": values.get("fire_count", "2_6"), "smoke_size": values.get("fire_size", "15_35"), "smoke_growth": values.get("fire_growth", "1.4"), "smoke_rise_speed": values.get("fire_rise_speed", "60"), "smoke_puff_time": values.get("fire_puff_time", "350_900"), "smoke_drift": values.get("fire_drift", "15"), "smoke_spread": values.get("fire_spread", "65"), "smoke_intensity": values.get("fire_intensity", "180"), "smoke_tint": values.get("fire_tint", "255_140_25_220"), "smoke_chance": str(int(parsed_gate))}
    fire = replace(_interpret_smoke(mapped, issue), flame=True)
    chance = max(0.0, min(100.0, _full_float(values.get("fire_propagation_chance", "0").rstrip("%")) or 0.0))
    radius = max(0.0, min(12000.0, _full_float(values.get("fire_propagation_radius", "0")) or 0.0))
    count = max(0, min(8, int(_full_float(values.get("fire_propagation_count", "0")) or 0)))
    raw_delay=_full_float(values.get("fire_propagation_delay","1500"))
    delay=max(0,min(3600000,int(raw_delay if raw_delay is not None else 1500)))
    return fire, chance, radius, count, delay

def parse_decal_profile(profile_id: int, values: dict[str, str]) -> DecalProfile | None:
    """Mirror of ``DecalProfileParser``; None when the engine drops the profile."""

    issues: list[FxIssue] = []

    def issue(severity: str, message: str) -> None:
        issues.append(FxIssue(severity, message))

    stretch = [(1.0, 1.0), (1.0, 1.0)]
    tint = (255, 255, 255, 255)
    edge_fade = (0.0, 0.0)
    duration, permanent, duration_valid = 0, False, False
    fade_in = fade_out = 0

    if "stretch_xz" in values:
        parts = split_tuple(values["stretch_xz"], 2)
        if parts is None:
            issue(SEVERITY_WARNING,
                  f"Invalid stretch_xz '{values['stretch_xz']}': expected x_z.")
        else:
            for axis, part in enumerate(parts):
                parsed = parse_float_range(part, "~")
                if not parsed or parsed[0] <= 0 or parsed[1] <= 0:
                    issue(SEVERITY_WARNING,
                          f"Invalid stretch_xz '{values['stretch_xz']}': using 1.0.")
                    parsed = (1.0, 1.0)
                stretch[axis] = parsed
    if "tint" in values:
        tint = parse_tint(values["tint"]) or (255, 255, 255, 255)
    if "edge_fade" in values:
        text = values["edge_fade"]
        if "_" in text:
            parsed_range = parse_int_range(text)
            if parsed_range is None:
                issue(SEVERITY_WARNING, f"Invalid edge_fade range '{text}': using 0.")
            else:
                edge_fade = (float(max(0, min(parsed_range[0], 10))),
                             float(max(0, min(parsed_range[1], 10))))
        else:
            parsed_fade = _full_float(text)
            if parsed_fade is not None:
                fade = max(0.0, min(parsed_fade, 10.0))
                edge_fade = (fade, fade)
    if "duration" in values:
        text = values["duration"]
        if text.casefold() == "permanent":
            permanent, duration, duration_valid = True, 0, True
        else:
            duration, used = _stol(text)
            duration_valid = used == len(text) and duration > 0
    if "fade_inout" in values:
        parts = split_tuple(values["fade_inout"], 2)
        if parts is None:
            issue(SEVERITY_WARNING,
                  f"Invalid fade_inout '{values['fade_inout']}': expected in_out.")
        else:
            fade_in, fade_out = _nonnegative_ms(parts[0]), _nonnegative_ms(parts[1])

    # The engine ignores a profile without a valid duration or with a clear tint.
    if not duration_valid or (not permanent and duration <= 0) or tint[3] <= 0:
        return None
    if permanent:
        fade_out = 0
    else:
        fade_out = min(fade_out, duration)
        fade_in = min(fade_in, duration - fade_out)

    # Any smoke_* key switches the smoke on (smoke_count = 0 keeps it silent).
    smoke_rows = {key: value for key, value in values.items()
                  if key in decal_smoke.SMOKE_KEYS}
    smoke = _interpret_smoke(smoke_rows, issue) if smoke_rows else None
    fire_rows = {key: value for key, value in values.items() if key.startswith("fire_")}
    fire, fire_chance, fire_radius, fire_count, fire_delay = _interpret_fire(fire_rows, issue) if fire_rows else (None, 0.0, 0.0, 0, 1500)

    spec = FxSpec(
        mode=MODE_DECAL, trigger="", active=True, issues=tuple(issues),
        random_rotation=values.get("random_rotation") == "1",
        duration=duration, fade_in=fade_in, fade_out=fade_out,
        texture=values.get("texture", ""), stretch_x=stretch[0],
        stretch_z=stretch[1], decal_tint=tint, edge_fade=edge_fade,
        permanent=permanent, smoke=smoke, fire=fire, fire_propagation_chance=fire_chance, fire_propagation_radius=fire_radius, fire_propagation_count=fire_count, fire_propagation_delay=fire_delay)
    return DecalProfile(profile_id, spec, values.get("name", ""))


def decal_profiles_from_text(text: str) -> dict[int, DecalProfile]:
    """Every valid ``new_decal N ... end`` profile of a script text."""

    profiles: dict[int, DecalProfile] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"\s*new_decal\s+(\d+)", line, re.IGNORECASE)
        if match is None:
            continue
        end_line = next((cursor for cursor in range(index + 1, len(lines))
                         if _scope_token(lines[cursor]) == "end"), len(lines))
        profile = parse_decal_profile(
            int(match[1]), top_level_values(text, index, end_line))
        if profile is not None:
            profiles[profile.profile_id] = profile
        else:
            profiles.pop(int(match[1]), None)
    return profiles


def key_group_issues(group: FxKeyGroup, context: str) -> tuple[FxIssue, ...]:
    """Problems the engine would have with the keys of one sound event."""

    issues: list[FxIssue] = []
    kind, event = group.group
    events = VEHICLE_SOUND_EVENTS if context == CONTEXT_VEHICLE else WEAPON_SOUND_EVENTS
    if event not in events:
        issues.append(FxIssue(
            SEVERITY_FATAL,
            f"'{event}' is not a sound event of a {context}: the script file stops loading."))
    for _index, key, value in group.rows():
        found = key_group_of(key)
        if found is None or found[0] != kind or found[1] != event:
            issues.append(FxIssue(
                SEVERITY_IGNORED,
                f"'{key}' does not belong to this effect: keep one event and one "
                "snd_ / pal_ / shk_ package here."))
            continue
        field = key[len(("snd" if kind == "sound" else "pal" if kind == "palette"
                         else "shk") + "_" + event) + 1:]
        if event == "launch_player":
            valid = field in ("slot", "time", "mag0", "mag1")
        else:
            valid = field in KEY_GROUP_FIELDS[kind]
        if not valid:
            issues.append(FxIssue(
                SEVERITY_UNKNOWN,
                f"Unknown key '{key}': if the engine does not know it either, "
                "it stops loading the whole script file."))
        elif field not in ("sample", "ext", "pitch") and _full_float(value) is None:
            issues.append(FxIssue(
                SEVERITY_WARNING, f"{key} = '{value}' is not a number: 0 is used."))
    return tuple(issues)


def key_group_summary(group: FxKeyGroup) -> str:
    """One readable line, e.g. 'Shake · 0.8→0.5 · 250 ms'."""

    values = group.values
    kind, event = group.group
    if kind == "sound":
        sample = values.get(f"snd_{event}_sample", "")
        name = sample.replace("\\", "/").rsplit("/", 1)[-1] if sample else "no sample"
        extra = f"vol {values[f'snd_{event}_volume']}" if f"snd_{event}_volume" in values else ""
        return " · ".join(part for part in (name, extra) if part)
    prefix = "pal" if kind == "palette" else "shk"
    slot = values.get(f"{prefix}_{event}_slot", "0")
    mag0 = values.get(f"{prefix}_{event}_mag0", "1" if kind == "shake" else "0")
    mag1 = values.get(f"{prefix}_{event}_mag1", "0")
    time = values.get(f"{prefix}_{event}_time", "1000" if kind == "shake" else "0")
    return f"slot {slot} · {mag0}→{mag1} · {time} ms"


def dest_fx_issues(line: DestFxLine) -> tuple[FxIssue, ...]:
    if line.parsed is None:
        return (FxIssue(
            SEVERITY_FATAL,
            "Malformed dest_fx (expected type_vehicle_x_y_z with type "
            "death/megadeth/create/beam): the script file stops loading."),)
    return ()


# -- legacy FX vehicle catalog ----------------------------------------------

@dataclass(frozen=True)
class LegacyFxVehicle:
    """Fields of an FX vehicle used by the vanilla dest_fx simulation."""

    vehicle_id: int
    name: str = ""
    vp_dead: int = 0
    vp_megadeth: int = 0
    dead_visual: VisualRef = VisualRef()
    megadeth_visual: VisualRef = VisualRef()
    main_visuals: tuple[VisualRef, ...] = ()
    visual_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    mass: float = 1.0
    airconst: float = 0.0
    maxrot: float = 0.0
    radius: float = 0.0
    scale_fx: ScaleFx | None = None
    scale_fx_text: str = ""
    dest_fx: tuple[DestFx, ...] = ()
    chain_fx: tuple[FxSpec, ...] = ()
    source: str = ""
    sounds: dict = field(default_factory=dict)


def legacy_vehicle_from_values(vehicle_id: int, values: dict[str, str],
                               dest_values: list[str],
                               source: str = "") -> LegacyFxVehicle:
    def number(key: str, default: float) -> float:
        value = values.get(key)
        if value is None:
            return default
        parsed = _stof(value)
        return parsed if math.isfinite(parsed) else default

    scale_text = values.get("scale_fx", "")
    dests = tuple(
        dest for dest in (parse_dest_fx(value) for value in dest_values)
        if dest is not None)
    dead_vp = int(number("vp_dead", 4))
    megadeth_vp = int(number("vp_megadeth", 2))
    main_visuals = tuple(
        VisualRef(
            int(number(f"vp_{state}", default)),
            values.get(f"3ds_{state}", ""),
            values.get(f"base_{state}", ""))
        for state, default in (("normal", 0), ("fire", 1), ("wait", 3), ("genesis", 5)))
    dead_visual = VisualRef(
        dead_vp, values.get("3ds_dead", ""), values.get("base_dead", ""))
    megadeth_visual = VisualRef(
        megadeth_vp, values.get("3ds_megadeth", ""), values.get("base_megadeth", ""))
    return LegacyFxVehicle(
        vehicle_id=vehicle_id,
        name=values.get("name", ""),
        vp_dead=dead_vp,
        vp_megadeth=megadeth_vp,
        dead_visual=dead_visual,
        megadeth_visual=megadeth_visual,
        main_visuals=main_visuals,
        visual_scale=_positive_scale(values, ""),
        mass=number("mass", 1.0),
        airconst=number("airconst", 0.0),
        maxrot=number("maxrot", 0.0),
        radius=number("radius", 0.0),
        scale_fx=parse_scale_fx(scale_text) if scale_text else None,
        scale_fx_text=scale_text,
        dest_fx=dests,
        source=source,
        sounds=sound_specs(values, VEHICLE_SOUND_EVENTS),
    )


def legacy_vehicle_from_text(vehicle_id: int, text: str,
                             source: str = "") -> LegacyFxVehicle:
    """Read the top-level fields of one ``new_vehicle`` body text."""

    values: dict[str, str] = {}
    dests: list[str] = []
    chain_fx: list[FxSpec] = []
    lines = text.splitlines()
    depth = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        token = _scope_token(line)
        if depth == 0 and token == "begin_fx":
            block_lines = [line]
            block_depth = 1
            index += 1
            while index < len(lines) and block_depth > 0:
                nested_line = lines[index]
                nested_token = _scope_token(nested_line)
                block_lines.append(nested_line)
                if nested_token == "begin" or nested_token.startswith("begin_"):
                    block_depth += 1
                elif nested_token == "end":
                    block_depth -= 1
                index += 1
            spec = interpret_block(FxBlock(block_lines), CONTEXT_VEHICLE)
            if spec.active:
                chain_fx.append(spec)
            continue
        if token == "begin" or token.startswith("begin_"):
            depth += 1
            index += 1
            continue
        if token == "end":
            depth = max(0, depth - 1)
            index += 1
            continue
        if depth or not token:
            index += 1
            continue
        code = re.split(r";", line, maxsplit=1)[0]
        key, _sep, value = code.partition("=")
        key = key.strip().casefold()
        value = value.strip()
        if key in DEST_FX_KEYS:
            dests.append(value)
        elif key:
            values[key] = value
        index += 1
    return replace(legacy_vehicle_from_values(vehicle_id, values, dests, source),
                   chain_fx=tuple(chain_fx))


# -- preview simulation -------------------------------------------------------

Matrix3 = tuple[tuple[float, float, float], ...]
IDENTITY: Matrix3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _mat_mul(a: Matrix3, b: Matrix3) -> Matrix3:
    return tuple(
        tuple(sum(a[row][k] * b[k][col] for k in range(3)) for col in range(3))
        for row in range(3))


def _rot_x(angle: float) -> Matrix3:
    c, s = math.cos(angle), math.sin(angle)
    return ((1.0, 0.0, 0.0), (0.0, c, s), (0.0, -s, c))


def _rot_y(angle: float) -> Matrix3:
    c, s = math.cos(angle), math.sin(angle)
    return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))


def _rot_z(angle: float) -> Matrix3:
    c, s = math.cos(angle), math.sin(angle)
    return ((c, s, 0.0), (-s, c, 0.0), (0.0, 0.0, 1.0))


def spin_matrix(strength: tuple[float, float, float], age_ms: float) -> Matrix3:
    """World::Spin::BuildMatrix: one strength point = one turn per second."""

    radians_per_ms = 360.0 * 0.001 * math.pi / 180.0
    matrix = IDENTITY
    for value, builder in zip(strength, (_rot_x, _rot_y, _rot_z)):
        if value:
            matrix = _mat_mul(matrix, builder(value * age_ms * radians_per_ms))
    return matrix


def _random_rotation(rng: random.Random, enabled: bool) -> Matrix3:
    if not enabled:
        return IDENTITY
    full = 2.0 * math.pi
    return _mat_mul(_mat_mul(_rot_x(rng.uniform(0, full)),
                             _rot_y(rng.uniform(0, full))),
                    _rot_z(rng.uniform(0, full)))


def temporal_envelope(elapsed: float, total: float,
                      fade_in: float, fade_out: float) -> float:
    """World::ComputeTemporalEnvelope (fade_in/fade_out alpha factor)."""

    elapsed = max(0.0, elapsed)
    fade_in = max(0.0, fade_in)
    fade_out = max(0.0, fade_out)
    if total <= 0:
        if fade_in <= 0:
            return 1.0
        return max(0.0, min(1.0, elapsed / fade_in))
    fade_sum = fade_in + fade_out
    if fade_sum > total and fade_sum > 0:
        scale = total / fade_sum
        fade_in *= scale
        fade_out *= scale
    factor = 1.0
    if fade_in > 0 and elapsed < fade_in:
        factor = elapsed / fade_in
    if fade_out > 0 and elapsed > total - fade_out:
        factor = min(factor, (total - elapsed) / fade_out)
    return max(0.0, min(1.0, factor))


def visual_chain_state(spec: FxSpec, age_ms: float):
    """(visual index, size, alpha, spin matrix) of a visual chain at ``age``.

    Mirrors the chainFX branch of the transient VP update: one shared
    0..1 timeline, start -> mid at the half, mid -> end in the second half,
    and the VP list split into equal time slices.
    """

    t = 1.0 if spec.duration <= 0 else age_ms / spec.duration
    t = max(0.0, min(1.0, t))
    start = spec.start_size if math.isfinite(spec.start_size) and spec.start_size >= 0 else 0.0
    end = spec.end_size if math.isfinite(spec.end_size) and spec.end_size >= 0 else 0.0
    if spec.mid_size is not None:
        mid = spec.mid_size
        if t < 0.5:
            size = start + (mid - start) * (t * 2.0)
        else:
            size = mid + (end - mid) * ((t - 0.5) * 2.0)
    else:
        size = start + (end - start) * t
    count = len(spec.visuals)
    index = min(int(t * count), count - 1) if count else 0
    alpha = 1.0
    if spec.fade_in > 0 or spec.fade_out > 0:
        alpha = temporal_envelope(age_ms, spec.duration,
                                  spec.fade_in, spec.fade_out)
    return index, size, alpha, spin_matrix(spec.spin, age_ms)


@dataclass(frozen=True)
class PreviewItem:
    """One model to draw at the current preview time."""

    source: int                 # item index in the FxDocument, -1 = the unit
    kind: str                   # visual | physical | impact | legacy | ground_decal | decal_smoke | unit | decoration | tracer
    vp: int                     # VP id to draw (0 = marker only)
    position: tuple[float, float, float]
    rotation: Matrix3
    scale: tuple[float, float, float]
    alpha: float = 1.0
    tint: tuple[int, int, int, int] | None = None
    label: str = ""
    # External 3DS/BASE models, in engine priority; the VP is the fallback.
    external: tuple[str, ...] = ()
    # Drawn with additive blending (tracer glow).
    additive: bool = False
    # Ground decal only.
    texture: str = ""
    decal_size: tuple[float, float] = (0.0, 0.0)
    decal_edge_fade: float = 0.0


@dataclass(frozen=True)
class PreviewTrack:
    """Timeline bar of one source item, for the timeline widget."""

    source: int
    label: str
    start: float
    end: float
    fade_in: float = 0.0
    fade_out: float = 0.0
    marks: tuple[float, ...] = ()
    approximate: bool = False


# -- visual look of the definition itself --------------------------------------
# Main-model tint/spin, weapon mesh tracer and decoration_fx. They are plain
# top-level keys of the vehicle/weapon, not begin_fx blocks.

Tint = tuple[int, int, int, int]
WHITE: Tint = (255, 255, 255, 255)
DECORATION_PERIODIC = "periodic"
DECORATION_PERSISTENT = "persistent"
# Preview windows that let slow decorations show up before the unit dies.
DECORATION_PERSISTENT_PREVIEW_MS = 1500.0
DECORATION_PERIODIC_PREVIEW_MAX_MS = 12000.0
DECORATION_PERIODIC_PREVIEW_MIN_MS = 1200.0
# Death plasma (genesis VP) left by a destroyed vehicle: it lasts 70% of the
# vehicle energy, clamped to 10..25 s, and the preview plays its first part.
PLASMA_SCALE = 0.75
PLASMA_MIN_MS = 10000.0
PLASMA_MAX_MS = 25000.0
PLASMA_PREVIEW_MS = 6000.0
# Smallest radius the follow camera frames, so tiny or empty projectile
# models (DUMMY, tracer-only weapons) do not zoom the scene to a point.
FOLLOW_MIN_RADIUS = 80.0


def top_level_values(text: str, start_line: int, end_line: int) -> dict[str, str]:
    """Last value of every top-level key of one definition (engine tokens)."""

    lines = text.splitlines()
    depth = 1
    values: dict[str, str] = {}
    for index in range(start_line + 1, min(end_line, len(lines))):
        token = _scope_token(lines[index])
        if token == "begin" or token.startswith("begin_"):
            depth += 1
        elif token == "end":
            depth -= 1
        elif depth == 1:
            tokens = engine_tokens(lines[index])
            if tokens:
                values[tokens[0].casefold()] = tokens[1]
    return values


def _tint_value(values: dict[str, str], key: str, default: Tint) -> Tint:
    """Absent key keeps ``default``; a malformed value is neutral white."""

    text = values.get(key)
    if text is None:
        return default
    return parse_tint(text) or WHITE


def _positive_scale(values: dict[str, str], prefix: str) -> tuple[float, float, float]:
    """ParseVPScaleParam: finite and > 0, otherwise 1.0."""

    parts = split_tuple(values.get(f"{prefix}scale_xyz"), 3)
    if parts is None:
        return (1.0, 1.0, 1.0)
    result = []
    for text in parts:
        value = _stof(text)
        result.append(value if math.isfinite(value) and value > 0 else 1.0)
    return tuple(result)


def _spin_strength(values: dict[str, str], prefix: str) -> tuple[float, float, float]:
    """ParseVPSpinParam: Spin::ClampStrength, 0..10."""

    parts = split_tuple(values.get(f"{prefix}spin_xyz"), 3)
    if parts is None:
        return (0.0, 0.0, 0.0)
    result = []
    for text in parts:
        value = _stof(text)
        result.append(max(0.0, min(SPIN_MAX, value)) if math.isfinite(value) else 0.0)
    return tuple(result)


def _non_negative_int(text: str | None, default: int = 0) -> int:
    value = script_int(text, default)
    return value if value > 0 else 0


@dataclass(frozen=True)
class TracerSpec:
    """mesh_tracer_* of a weapon (TWeaponTracerConfig)."""

    mesh: str
    tint: Tint = (255, 210, 80, 180)
    tint_head: Tint | None = None
    tint_tail: Tint | None = None
    size_x: float = 3.0
    size_y: float = 3.0
    size_z: float = 300.0
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    head_size: float | None = None
    tail_size: float | None = None
    glow_rate: float = 0.0
    pulse_rate: float = 0.0
    pulse_speed: float = 0.0


def parse_tracer(values: dict[str, str], prefix: str = "mesh_tracer_") -> TracerSpec | None:
    """Tracer the engine would start (ConfigureWeaponTracer), else None."""

    if values.get(prefix + "enable") != "1" or not values.get(prefix + "path"):
        return None

    def bounded(text: str | None, maximum: float, default: float) -> float:
        if text is None:
            return default
        value = _full_float(text)
        if value is None or value <= 0:
            return 0.0
        return min(value, maximum)

    def non_negative(key: str, maximum: float, default: float | None) -> float | None:
        text = values.get(prefix + key)
        value = _full_float(text) if text is not None else None
        if value is None or value < 0:
            return default
        return min(value, maximum)

    def signed(text: str) -> float:
        value = _full_float(text)
        return 0.0 if value is None else max(-6000.0, min(6000.0, value))

    # size_xyz / pos_xyz are x_y_z; a malformed tuple keeps the engine defaults.
    size_parts = split_tuple(values.get(prefix + "size_xyz"), 3) or (None, None, None)
    pos_parts = split_tuple(values.get(prefix + "pos_xyz"), 3) or ("", "", "")
    size_x = bounded(size_parts[0], 100.0, 3.0)
    size_z = bounded(size_parts[2], 6000.0, 300.0)
    tint = _tint_value(values, prefix + "tint", (255, 210, 80, 180))
    size_y_value = _full_float(size_parts[1]) if size_parts[1] is not None else None
    size_y = (None if size_y_value is None or size_y_value < 0
              else min(size_y_value, 100.0))
    if size_z <= 0.01 or size_x <= 0.01 or tint[3] <= 0:
        return None
    return TracerSpec(
        mesh=values[prefix + "path"], tint=tint,
        tint_head=(_tint_value(values, prefix + "head_tint", WHITE)
                   if prefix + "head_tint" in values else None),
        tint_tail=(_tint_value(values, prefix + "tail_tint", WHITE)
                   if prefix + "tail_tint" in values else None),
        size_x=size_x, size_y=size_x if size_y is None else size_y,
        size_z=size_z,
        pos=tuple(signed(part) for part in pos_parts),
        head_size=non_negative("head_size", 100.0, None),
        tail_size=non_negative("tail_size", 100.0, None),
        glow_rate=non_negative("glow_rate", 10.0, 0.0),
        pulse_rate=non_negative("pulse_rate", 10.0, 0.0),
        pulse_speed=non_negative("pulse_speed", 10.0, 0.0))


@dataclass(frozen=True)
class DecorationSpec:
    """decoration_fx_* of a vehicle or weapon (TDecorationFXConfig)."""

    visual: VisualRef
    mode: str = DECORATION_PERIODIC
    interval: tuple[int, int] = (0, 0)
    count: tuple[int, int] = (0, 0)
    duration: int = 1000
    random_pos: float = 0.0
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    spin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    tint: Tint = WHITE
    fade_in: int = 0
    fade_out: int = 0
    trail_tint: Tint = WHITE
    trail_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    trail_fade_in: int = 0
    trail_fade_out: int = 0

    @property
    def persistent(self) -> bool:
        return self.mode == DECORATION_PERSISTENT

    @property
    def spawns(self) -> bool:
        """Periodic mode needs a positive interval and count (engine rule)."""

        return self.persistent or (min(self.interval) > 0 and min(self.count) > 0)


def parse_decoration(values: dict[str, str]) -> DecorationSpec | None:
    """Decoration the engine would spawn (UpdateDecorationFX), else None."""

    vp = _non_negative_int(values.get("decoration_fx_vp"))
    mesh = values.get("decoration_fx_3ds", "")
    base = values.get("decoration_fx_base", "")
    if vp <= 0 and not mesh and not base:
        return None

    def int_range(key: str, clamp: int | None) -> tuple[int, int]:
        parsed = parse_int_range(values.get(key, ""))
        if parsed is None:
            return (0, 0)
        if clamp is not None:
            parsed = tuple(max(0, min(clamp, value)) for value in parsed)
        return parsed

    def vector(key: str) -> tuple[float, float, float]:
        parts = split_tuple(values.get(key), 3)
        if parts is None:
            return (0.0, 0.0, 0.0)
        return tuple(_stof(part) for part in parts)

    def fades(key: str) -> tuple[int, int]:
        parts = split_tuple(values.get(key), 2)
        if parts is None:
            return (0, 0)
        return (_non_negative_int(parts[0]), _non_negative_int(parts[1]))

    fade = fades("decoration_fx_fade_inout")
    trail_fade = fades("decoration_fx_vp_trail_fade_inout")

    random_pos = _stof(values.get("decoration_fx_random_pos", "0"))
    return DecorationSpec(
        visual=VisualRef(vp=vp, mesh3ds=mesh, base_path=base),
        mode=(DECORATION_PERSISTENT
              if values.get("decoration_fx_mode", "").casefold() == "persistent"
              else DECORATION_PERIODIC),
        interval=int_range("decoration_fx_interval", None),
        count=int_range("decoration_fx_count", MAX_COUNT),
        duration=_non_negative_int(values.get("decoration_fx_duration"), 1000) or 1000,
        random_pos=random_pos if math.isfinite(random_pos) and random_pos > 0 else 0.0,
        offset=vector("decoration_fx_offset_xyz"),
        scale=_positive_scale(values, "decoration_fx_"),
        spin=_spin_strength(values, "decoration_fx_"),
        tint=_tint_value(values, "decoration_fx_tint", WHITE),
        fade_in=fade[0],
        fade_out=fade[1],
        trail_tint=_tint_value(values, "decoration_fx_vp_trail_tint", WHITE),
        trail_scale=_positive_scale(values, "decoration_fx_vp_trail_"),
        trail_fade_in=trail_fade[0],
        trail_fade_out=trail_fade[1])


def _manual_decal_size(value):
    size=parse_float_range(value)
    return size if size and all(math.isfinite(n) and 0<=n<=12000 for n in size) else (0.0,0.0)


def unit_look(values: dict[str, str], *, weapon: bool) -> dict:
    """UnitPhases keyword values read from the top-level keys of a definition."""

    return {
        "tint": parse_tint(values["tint"]) if "tint" in values else None,
        "spin": _spin_strength(values, ""),
        "trail_tint": (parse_tint(values["vp_trail_tint"])
                       if weapon and "vp_trail_tint" in values else None),
        "trail_scale": (_positive_scale(values, "vp_trail_")
                        if weapon else (1.0, 1.0, 1.0)),
        "tracer": parse_tracer(values) if weapon else None,
        "decoration": parse_decoration(values),
        # Projectiles never leave plasma; vehicles do (vp_genesis defaults to 5).
        "sounds": sound_specs(
            values, WEAPON_SOUND_EVENTS if weapon else VEHICLE_SOUND_EVENTS),
        "motion": parse_motion(values) if weapon else None,
        "cluster": parse_cluster(values) if weapon else None,
        "laser": parse_laser(values) if weapon else None,
        "delay_time": (_non_negative_int(values.get("delay_time"))
                       if weapon else 0),
        "debuff_id": script_int(values.get("debuff_id"), -1) if weapon else -1,
        "delay_debuff_id": (script_int(values.get("delay_time_debuff_id"), -1)
                            if weapon else -1),
        # artillery_shell vertical_barrage: rounds fall from arc_height.
        "fixed_speed": (_stof(values.get("artillery_shell_speed", "0"))
                        if weapon and values.get("model", "").casefold() == "artillery_shell"
                        and values.get("artillery_shell_mode", "").casefold() == "vertical_barrage"
                        else 0.0),
        "altitude": (min(3000.0, _stof(values.get("artillery_shell_arc_height", "0")))
                     if weapon and values.get("model", "").casefold() == "artillery_shell"
                     else 0.0),
        "start_speed": max(0.0, _stof(values.get("start_speed", "0"))),
        "genesis": VisualRef() if weapon else VisualRef(
            vp=script_int(values.get("vp_genesis"), 5),
            mesh3ds=values.get("3ds_genesis", ""),
            base_path=values.get("base_genesis", "")),
        "energy": max(0.0, _stof(values.get("energy", "0"))),
        "maxrot": _stof(values.get("maxrot", "0")),
        "ground_decal_id": (lambda decal_id: decal_id if decal_id >= 0 else -1)(
            script_int(values.get("ground_decal_id"), -1)),
        # The unit's own size, written in its script; a bad value is ignored.
        "decal_size": _manual_decal_size(values.get("ground_decal_size", "0_0")),
    }


@dataclass(frozen=True)
class MotionSpec:
    """Render-only spiral or chaos motion of a projectile (spiral_* / chaos_*).

    Chaos wins when both are valid, like ``GetProjectileVisualMotionDelta``.
    """

    kind: str          # "spiral" | "chaos"
    rate: float        # turns per second / random targets per second
    radius: float
    # Chaos picks its random targets from a per-projectile seed.
    seed: int = 0x9E3779B9 ^ 1

    def offset(self, age_ms: float) -> tuple[float, float, float]:
        """Local X/Y offset from the flight line (Z is the forward axis)."""

        age = max(age_ms, 0.0)
        if self.kind == "spiral":
            phase = math.fmod(self.rate * age * 0.001, 1.0) * 2.0 * math.pi
            return (math.cos(phase) * self.radius, -math.sin(phase) * self.radius, 0.0)
        motion = age * 0.001 * self.rate
        segment = math.floor(motion)
        fraction = motion - segment
        smooth = fraction * fraction * (3.0 - 2.0 * fraction)
        current = self._target(int(segment))
        following = self._target(int(segment) + 1)
        return tuple(a + (b - a) * smooth for a, b in zip(current, following))

    def _target(self, segment: int) -> tuple[float, float, float]:
        mask = 0xFFFFFFFF

        def unit(value: int) -> float:
            value &= mask
            value ^= value >> 16
            value = (value * 0x7FEB352D) & mask
            value ^= value >> 15
            value = (value * 0x846CA68B) & mask
            value ^= value >> 16
            return (value & 0x00FFFFFF) / 16777215.0

        seed = self.seed ^ ((segment * 0x85EBCA6B) & mask)
        angle = unit(seed ^ 0xA341316C) * 2.0 * math.pi
        radius = self.radius * math.sqrt(unit(seed ^ 0xC8013EA4))
        return (math.cos(angle) * radius, math.sin(angle) * radius, 0.0)

    def tilt(self, age_ms: float, speed: float) -> Matrix3:
        """Model rotation that follows the rendered curve (plus the spiral roll).

        ``speed`` is the projectile speed in units per second.
        """

        if self.kind == "spiral":
            phase = math.fmod(self.rate * max(age_ms, 0.0) * 0.001, 1.0) * 2.0 * math.pi
            omega = self.rate * 2.0 * math.pi
            velocity = (-math.sin(phase) * self.radius * omega,
                        -math.cos(phase) * self.radius * omega, 0.0)
        else:
            now, later = self.offset(age_ms), self.offset(age_ms + 1.0)
            velocity = tuple((b - a) * 1000.0 for a, b in zip(now, later))
        tangent = (velocity[0], velocity[1], speed + velocity[2])
        length = math.sqrt(sum(value * value for value in tangent))
        rotation = IDENTITY
        if length > 1e-6:
            tangent = tuple(value / length for value in tangent)
            axis = (-tangent[1], tangent[0], 0.0)            # z x tangent
            sine = math.sqrt(axis[0] ** 2 + axis[1] ** 2)
            if sine > 1e-6:
                rotation = _axis_rotation(
                    tuple(value / sine for value in axis), math.atan2(sine, tangent[2]))
            elif tangent[2] < 0:
                rotation = _rot_x(math.pi)
        if self.kind == "spiral":
            rotation = _mat_mul(rotation, _rot_z(phase))
        return rotation


def _axis_rotation(axis, angle: float) -> Matrix3:
    """Rodrigues rotation of ``angle`` radians around a unit ``axis``."""

    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    k = 1.0 - c
    return ((c + x * x * k, x * y * k - z * s, x * z * k + y * s),
            (y * x * k + z * s, c + y * y * k, y * z * k - x * s),
            (z * x * k - y * s, z * y * k + x * s, c + z * z * k))


def parse_motion(values: dict[str, str]) -> MotionSpec | None:
    """Spiral or chaos motion the engine would apply, else None."""

    def strength(key: str) -> float:
        value = _stof(values.get(key, "0"))
        return max(0.0, min(SPIN_MAX, value)) if math.isfinite(value) else 0.0

    def radius(key: str) -> float:
        value = _stof(values.get(key, "0"))
        return min(value, 1000.0) if math.isfinite(value) and value > 0 else 0.0

    if strength("chaos_factor") > 0 and radius("chaos_radius") > 0:
        return MotionSpec("chaos", strength("chaos_factor"), radius("chaos_radius"))
    if strength("spiral_speed") > 0 and radius("spiral_radius") > 0:
        return MotionSpec("spiral", strength("spiral_speed"), radius("spiral_radius"))
    return None


@dataclass(frozen=True)
class _Flight:
    """Where a projectile (or a unit phase) is and how it is oriented in time."""

    path: list
    heading: Matrix3 = IDENTITY
    motion: MotionSpec | None = None
    speed: float = 0.0               # units per second, for the motion tilt
    frozen_at: float | None = None   # the visual motion stops at impact

    def pose(self, time_ms: float):
        """(position, rotation) with the spiral/chaos motion applied."""

        base = _path_position(self.path, time_ms)
        if self.motion is None:
            return base, self.heading
        age = self.frozen_at if self.frozen_at is not None else time_ms
        offset = _rotate_vector(self.heading, self.motion.offset(age))
        rotation = _mat_mul(self.heading, self.motion.tilt(age, self.speed))
        return tuple(b + o for b, o in zip(base, offset)), rotation


@dataclass(frozen=True)
class ClusterSpec:
    """cluster_* of a weapon: it splits into ``weapon_id`` projectiles."""

    weapon_id: int
    count: tuple[int, int]
    # > 0: split after this many ms of flight; 0: split when it hits something.
    trigger_time: int = 0
    # How many split levels the children may add (0 = one split only).
    generations: int = 0
    horizontal: tuple[float, float] | None = None
    vertical: tuple[float, float] | None = None
    spread: tuple[float, float] = (0.0, 0.0)
    visual: VisualRef = VisualRef()
    sound: SoundSpec | None = None


CLUSTER_MAX_COUNT = 64
# The preview shows at most this many projectiles born from clusters.
CLUSTER_PREVIEW_LIMIT = 160
# A child that never reaches the ground is followed this long.
CLUSTER_FREE_FLIGHT_MS = 700.0
CLUSTER_MAX_FLIGHT_MS = 3000.0
# cluster_vp / 3ds / base is a transient visual of this length at the split.
CLUSTER_FLASH_MS = 1000.0


def parse_cluster(values: dict[str, str]) -> ClusterSpec | None:
    """The cluster the engine would split into (TryClusterSplit), else None."""

    if script_int(values.get("cluster_enable")) == 0:
        return None
    count = parse_int_range(values.get("cluster_count", ""))
    weapon_id = script_int(values.get("cluster_weapon_id"))
    if count is None or min(count) < 1 or weapon_id <= 0:
        return None
    trigger = parse_int_range(values.get("cluster_trigger_time", ""))
    return ClusterSpec(
        weapon_id=weapon_id,
        count=tuple(min(CLUSTER_MAX_COUNT, value) for value in count),
        trigger_time=trigger[0] if trigger and trigger[0] == trigger[1] > 0 else 0,
        generations=_non_negative_int(values.get("cluster_generations")),
        horizontal=parse_float_range(values.get("cluster_horizontal_angle", "")),
        vertical=parse_float_range(values.get("cluster_vertical_angle", "")),
        spread=tuple(max(0.0, _stof(part))
                     for part in (split_tuple(values.get("cluster_spread_xy"), 2)
                                  or ("0", "0"))),
        visual=VisualRef(vp=_non_negative_int(values.get("cluster_vp")),
                         mesh3ds=values.get("cluster_3ds", ""),
                         base_path=values.get("cluster_base", "")),
        sound=(lambda spec: None if spec is None else replace(
            spec, volume=spec.volume or 120))(
            sound_specs(values, ("cluster",)).get("cluster")))


@dataclass(frozen=True)
class DebuffSpec:
    """The visual side of a ``new_debuff`` profile hit units show."""

    debuff_id: int
    vps: tuple[int, ...] = ()
    mesh3ds: str = ""
    tint: Tint = WHITE
    trail_tint: Tint | None = None
    scale: float = 1.0
    max_offset: float = 0.0
    offset_percent: bool = False
    tick_time: int = 0
    duration: int = 0
    sound: SoundSpec | None = None


# Size the preview assumes for a hit unit when a debuff offset is a percentage.
DEBUFF_UNIT_SIZE = 60.0
DEBUFF_PREVIEW_MS = 5000.0
DELAY_PREVIEW_MAX_MS = 8000.0


def parse_debuff(debuff_id: int, values: dict[str, str]) -> DebuffSpec:
    """The visual and sound values of one debuff profile."""

    vps = parse_int_list(values.get("debuff_vp", ""), allow_zero=False) or []
    offset_text = values.get("debuff_random_max_offset", "")
    percent = offset_text.endswith("%")
    offset = _stof(offset_text.rstrip("%")) if offset_text else 0.0
    scale = _stof(values.get("debuff_scale", "1"))
    return DebuffSpec(
        debuff_id=debuff_id, vps=tuple(vps), mesh3ds=values.get("debuff_3ds", ""),
        tint=_tint_value(values, "debuff_tint", WHITE),
        trail_tint=(_tint_value(values, "debuff_vp_trail_tint", WHITE)
                    if "debuff_vp_trail_tint" in values else None),
        scale=scale if math.isfinite(scale) and scale > 0 else 1.0,
        max_offset=max(0.0, offset) if math.isfinite(offset) else 0.0,
        offset_percent=percent,
        tick_time=_non_negative_int(values.get("debuff_tick_time")),
        duration=_non_negative_int(values.get("debuff_duration")),
        sound=sound_specs(values, ("debuff",)).get("debuff"))


def debuff_profiles_from_text(text: str) -> dict[int, DebuffSpec]:
    """Every ``new_debuff N ... end`` profile of a script text."""

    profiles: dict[int, DebuffSpec] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"\s*new_debuff\s+(\d+)", lines[index], re.IGNORECASE)
        if match is None:
            index += 1
            continue
        depth, cursor = 1, index + 1
        while cursor < len(lines) and depth:
            token = _scope_token(lines[cursor])
            if token == "begin" or token.startswith("begin_"):
                depth += 1
            elif token == "end":
                depth -= 1
            cursor += 1
        end_line = cursor - 1 if depth == 0 else len(lines)
        profiles[int(match[1])] = parse_debuff(
            int(match[1]), top_level_values(text, index, end_line))
        index = cursor
    return profiles


@dataclass(frozen=True)
class LaserSpec:
    """model = laser: the beam is drawn instead of a flying projectile."""

    spacing: float = 40.0
    mesh: str = ""
    tint: Tint = WHITE
    size_x: float = 5.0
    size_y: float = 5.0
    glow_rate: float = 1.0
    impact_fade: float = 0.0


def parse_laser(values: dict[str, str]) -> LaserSpec | None:
    if values.get("model", "").casefold() != "laser":
        return None
    spacing = _stof(values.get("laser_spacing", "40"))
    size_parts = split_tuple(values.get("laser_mesh_size_xy"), 2) or ("5", "")
    size_x = _stof(size_parts[0])
    size_y = _full_float(size_parts[1])
    return LaserSpec(
        spacing=min(max(spacing, 5.0), 400.0) if math.isfinite(spacing) and spacing > 0 else 40.0,
        mesh=values.get("laser_mesh_path", "") if values.get("laser_mesh_enable") == "1" else "",
        tint=_tint_value(values, "laser_mesh_tint", WHITE),
        size_x=size_x if math.isfinite(size_x) and size_x > 0 else 5.0,
        size_y=size_y if size_y and size_y > 0 else (size_x if size_x > 0 else 5.0),
        glow_rate=max(0.0, min(10.0, _stof(values.get("laser_mesh_glow_rate", "1")))),
        impact_fade=max(0.0, _stof(values.get("laser_mesh_impact_fade_length", "0"))))


@dataclass(frozen=True)
class WeaponDef:
    """Another weapon of the game, as a cluster child needs it."""

    weapon_id: int
    unit: "UnitPhases"
    document: "FxDocument"


def _body_values(text: str) -> tuple[dict[str, str], int]:
    """Top-level values and the closing line of one ``new_weapon ... end`` text."""

    lines = text.splitlines()
    last = len(lines) - 1 if lines and _scope_token(lines[-1]) == "end" else len(lines)
    return top_level_values(text, 0, last), last


def weapon_def_from_text(weapon_id: int, text: str) -> WeaponDef:
    """Read one weapon definition (header, body and ``end``) for the preview."""

    values, last = _body_values(text)

    def state(name: str, default: int) -> VisualRef:
        return VisualRef(vp=script_int(values.get(f"vp_{name}"), default),
                         mesh3ds=values.get(f"3ds_{name}", ""),
                         base_path=values.get(f"base_{name}", ""))

    from entity_designer.shape import visual_transform_point

    scale = _positive_scale(values, "")
    euler = tuple(
        (lambda v: v if v is not None else 0.0)(_full_float(part))
        for part in (split_tuple(values.get("rotation_xyz"), 3) or ("0", "0", "0")))
    columns = [visual_transform_point(axis, scale, euler)
               for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
    transform = tuple(tuple(columns[col][row] for col in range(3)) for row in range(3))
    unit = UnitPhases(
        normal=state("normal", 0), dead=state("dead", 4),
        megadeth=state("megadeth", 2), model=values.get("model", "").casefold(),
        weapon=True, transform=transform, scale=scale, euler=euler,
        **unit_look(values, weapon=True))
    return WeaponDef(weapon_id, unit, extract_fx_document(text, 0, last))


def cluster_chain(unit: "UnitPhases", weapons: dict) -> list[WeaponDef]:
    """The weapons a cluster can spawn, children of children included."""

    chain: list[WeaponDef] = []
    seen: set[int] = set()
    pending = [unit]
    while pending:
        cluster = pending.pop().cluster
        child = weapons.get(cluster.weapon_id) if cluster is not None else None
        if child is not None and child.weapon_id not in seen:
            seen.add(child.weapon_id)
            chain.append(child)
            pending.append(child.unit)
    return chain


def weapon_vps(unit: "UnitPhases", document: "FxDocument", weapons: dict,
               seen: set | None = None, debuffs: dict | None = None) -> set[int]:
    """Every VP a weapon (and the weapons its cluster spawns) can show."""

    debuffs = debuffs or {}

    vps = {ref.vp for ref in (unit.normal, unit.dead, unit.megadeth)
           if ref.vp > 0}
    if unit.decoration is not None and unit.decoration.visual.vp > 0:
        vps.add(unit.decoration.visual.vp)
    for item in document.items:
        if isinstance(item, FxBlock):
            spec = interpret_block(item, CONTEXT_WEAPON)
            vps.update(visual.vp for visual in spec.visuals if visual.vp > 0)
            vps.update(vp for vp in spec.vp_models if vp > 0)
            vps.update(vp for vp in spec.vp_impacts if vp > 0)
    for debuff_id in (unit.debuff_id, unit.delay_debuff_id):
        if debuff_id in debuffs:
            vps.update(vp for vp in debuffs[debuff_id].vps if vp > 0)
    cluster = unit.cluster
    if cluster is not None:
        if cluster.visual.vp > 0:
            vps.add(cluster.visual.vp)
        seen = set() if seen is None else seen
        child = weapons.get(cluster.weapon_id)
        if child is not None and cluster.weapon_id not in seen:
            seen.add(cluster.weapon_id)
            vps |= weapon_vps(child.unit, child.document, weapons, seen, debuffs)
    return vps


# Classes that fly: their preview starts in the air.
FLYING_MODELS = frozenset({
    "plane", "heli", "flyer", "glider", "zeppelin", "cruiser", "ufo"})
DEFAULT_FLIGHT_ALTITUDE = 500.0


@dataclass(frozen=True)
class UnitPhases:
    """The previewed definition itself and its death models.

    Each state resolves like the engine: 3DS, then BASE, then the VP.
    Vehicles: tank/car go from normal straight to megadeth; every other
    class shows dead, falls as a wreck and switches to megadeth on ground
    contact. Weapons fly to the target and switch to dead (unit hit) or
    megadeth (ground hit).

    ``transform`` (scale/rotation), ``spin`` and the particle trail values
    belong to the main (normal) model only; ``tint`` colors every state.
    Weapons use ``altitude`` as the height they fall from.
    """

    normal: VisualRef = VisualRef()
    dead: VisualRef = VisualRef()
    megadeth: VisualRef = VisualRef()
    model: str = ""
    weapon: bool = False
    # Preview scale/rotation of the model (same as the static preview).
    transform: Matrix3 = IDENTITY
    overeof: float = 0.0
    altitude: float = 0.0
    # Main-model look read from the script.
    tint: Tint | None = None
    spin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    euler: tuple[float, float, float] = (0.0, 0.0, 0.0)
    trail_tint: Tint | None = None
    trail_scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    tracer: TracerSpec | None = None
    decoration: DecorationSpec | None = None
    motion: MotionSpec | None = None
    cluster: ClusterSpec | None = None
    laser: LaserSpec | None = None
    delay_time: int = 0
    debuff_id: int = -1
    delay_debuff_id: int = -1
    fixed_speed: float = 0.0
    start_speed: float = 0.0
    # snd_* packages of the definition, by event name.
    sounds: dict = field(default_factory=dict)
    # Genesis VP of the death plasma and the values that drive it.
    genesis: VisualRef = VisualRef()
    # States that share the runtime main-model scale rule.
    main_visuals: tuple[VisualRef, ...] = ()
    energy: float = 0.0
    maxrot: float = 0.0
    # Reusable decal, with a diameter authored on this source.
    ground_decal_id: int = -1
    # Manual world-space diameter range.
    decal_size: tuple[float, float] = (0.0, 0.0)

    @property
    def plasma_ms(self) -> float:
        """GetPlasmaDurationMs: how long the plasma would last in game."""

        return max(PLASMA_MIN_MS, min(PLASMA_MAX_MS, self.energy * 0.7))

    @property
    def skips_dead_phase(self) -> bool:
        return not self.weapon and self.model.casefold() in ("tank", "car")

    @property
    def flies(self) -> bool:
        return not self.weapon and self.model.casefold() in FLYING_MODELS

    def visual_matrix(self, age_ms: float) -> Matrix3:
        """Matrix of the main model: preview scale/rotation plus its spin.

        Engine order is rotation, then spin, then scale (``ypabact`` render).
        """

        if not any(self.spin):
            return self.transform
        from entity_designer.shape import visual_transform_point

        columns = [visual_transform_point(axis, (1.0, 1.0, 1.0), self.euler)
                   for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
        rotation = tuple(tuple(columns[col][row] for col in range(3))
                         for row in range(3))
        scale = tuple(tuple(self.scale[row] if row == col else 0.0
                            for col in range(3)) for row in range(3))
        return _mat_mul(_mat_mul(rotation, spin_matrix(self.spin, age_ms)), scale)

    def look_reach(self) -> float:
        """Radius needed to see the tracer and the decorations around the unit."""

        reach = 0.0
        if self.tracer is not None:
            reach = self.tracer.size_z * 0.6
        deco = self.decoration
        if deco is not None and deco.spawns:
            reach = max(reach, math.hypot(*deco.offset)
                        + deco.random_pos * 1.8 + 40.0 * max(deco.scale))
        return reach

    def particle_tint(self, main: bool) -> Tint | None:
        """Tint of the VP particles: weapons use vp_trail_tint on the main model."""

        return self.trail_tint if (self.weapon and main) else self.tint




# Preview events of the Visuals & FX tab.
SEQUENCE_TRIGGER = "sequence"          # vehicle: full death
WEAPON_HIT_UNIT = "hit_unit"           # weapon: detonate on a unit
WEAPON_HIT_GROUND = "hit_ground"       # weapon: impact_world on terrain
SEQUENCE_PREROLL_MS = 120.0
# How long the preview keeps the final model after the last effect ends.
MEGADETH_HOLD_MS = 1200.0
# Weapon flight shown before the impact: a vertical drop above the target.
WEAPON_FLIGHT_MS = 900.0
WEAPON_LAUNCH_HEIGHT = 550.0


def _uniform(rng: random.Random, low: float, high: float) -> float:
    return low if low == high else rng.uniform(low, high)


def _randint(rng: random.Random, low: int, high: int) -> int:
    return low if low == high else rng.randint(min(low, high), max(low, high))


def _external_paths(ref: VisualRef | None) -> tuple[str, ...]:
    if ref is None:
        return ()
    return tuple(path for path in (ref.mesh3ds, ref.base_path) if path)


def _look_rotation(direction) -> Matrix3:
    """Rotation turning model +Z (UA forward) toward ``direction``."""

    length = math.sqrt(sum(value * value for value in direction)) or 1.0
    forward = tuple(value / length for value in direction)
    up = (0.0, -1.0, 0.0)
    right = (up[1] * forward[2] - up[2] * forward[1],
             up[2] * forward[0] - up[0] * forward[2],
             up[0] * forward[1] - up[1] * forward[0])
    norm = math.sqrt(sum(value * value for value in right))
    if norm < 1e-6:
        # A vertical drop is parallel to the usual up axis.
        up = (0.0, 0.0, 1.0)
        right = (up[1] * forward[2] - up[2] * forward[1],
                 up[2] * forward[0] - up[0] * forward[2],
                 up[0] * forward[1] - up[1] * forward[0])
        norm = math.sqrt(sum(value * value for value in right))
        if norm < 1e-6:
            return IDENTITY
    right = tuple(value / norm for value in right)
    down = (forward[1] * right[2] - forward[2] * right[1],
            forward[2] * right[0] - forward[0] * right[2],
            forward[0] * right[1] - forward[1] * right[0])
    return tuple((right[row], down[row], forward[row]) for row in range(3))


def _cross(a, b) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _tracer_rotation(direction) -> Matrix3:
    """WeaponTracerRotationFromDir: columns are the mesh X, Y and Z axes."""

    hint = (0.0, 1.0, 0.0) if abs(direction[1]) < 0.9 else (1.0, 0.0, 0.0)
    x = _cross(hint, direction)
    norm = math.sqrt(sum(value * value for value in x))
    if norm < 1e-6:
        return IDENTITY
    x = tuple(value / norm for value in x)
    y = _cross(direction, x)
    norm = math.sqrt(sum(value * value for value in y)) or 1.0
    y = tuple(value / norm for value in y)
    return tuple((x[row], y[row], direction[row]) for row in range(3))


@dataclass
class _Fall:
    """Dead-state flight state (NC_STACK_ypabact::Move with flag 1)."""

    position: list[float]
    velocity: list[float]
    mass: float
    airconst: float

    def step(self, dt_s: float) -> None:
        mass = max(self.mass, 1e-6)
        force = [-self.velocity[0] * self.airconst,
                 mass * DEAD_GRAVITY - self.velocity[1] * self.airconst,
                 -self.velocity[2] * self.airconst]
        for axis in range(3):
            self.velocity[axis] += force[axis] * (dt_s / mass)
        speed = math.sqrt(sum(value * value for value in self.velocity))
        if speed > 0.1:
            for axis in range(3):
                self.position[axis] += (
                    self.velocity[axis] * dt_s * MOVE_POSITION_FACTOR)


@dataclass
class _Def:
    """A weapon being simulated: the previewed one or a cluster child."""

    unit: "UnitPhases"
    document: "FxDocument"
    specs: dict
    enabled: set | None = None
    # Source id of its events; None = the index of the item in the document.
    source: int | None = None
    salt: str = ""


class PreviewSimulation:
    """Deterministic preview of one destruction, as the engine plays it.

    ``trigger`` is ``"sequence"`` (vehicle death: normal -> dead +
    air_destroyed + dest_fx death -> wreck fall -> megadeth + destroyed +
    dest_fx megadeth), ``"hit_unit"``/``"hit_ground"`` (weapon flight and
    detonate / impact_world, with launch at the muzzle), or one single
    begin_fx/dest_fx trigger.
    Random choices are rolled once per seed so scrubbing always shows the
    same activation. All times are absolute ms.
    """

    def __init__(self, document: FxDocument, context: str, trigger: str, *,
                 seed: int = 1, origin=(0.0, 0.0, 0.0), ground_y: float = 0.0,
                 unit_radius: float = 0.0,
                 legacy_catalog: dict[int, LegacyFxVehicle] | None = None,
                 enabled: set[int] | None = None,
                 unit: UnitPhases | None = None,
                 weapons: dict[int, WeaponDef] | None = None,
                 debuffs: dict[int, DebuffSpec] | None = None,
                 decals: dict[int, DecalProfile] | None = None,
                 settings: EffectSettings | None = None) -> None:
        self.document = document
        self.context = context
        self.trigger = trigger
        self.seed = seed
        self.origin = tuple(origin)
        self.ground_y = ground_y
        self.unit_radius = unit_radius
        self.catalog = legacy_catalog or {}
        self.enabled = enabled
        self.unit = unit or UnitPhases(weapon=context == CONTEXT_WEAPON)
        self.unit = self._unit_with_authored_audio(self.unit, document, enabled)
        self.notes: list[str] = []
        self._events: list[tuple] = []
        # Wreck fall [(ms, position, landed)] while air_destroyed blocks are
        # built; physical fragments with follow_carcass copy it.
        self._wreck_path: list[tuple] | None = None
        self._sounds: list[SoundEvent] = []
        self._tracks: list[PreviewTrack] = []
        self._instances: list[VpInstance] | None = None
        self._specs = {
            index: interpret_block(item, context)
            for index, item in enumerate(document.items)
            if isinstance(item, FxBlock)}
        self.weapons = weapons or {}
        self.debuffs = debuffs or {}
        self.decals = decals or {}
        self.settings = settings or EffectSettings()
        self._root = _Def(self.unit, document, self._specs, enabled)
        self._cluster_projectiles = 0
        if trigger == SEQUENCE_TRIGGER and context == CONTEXT_VEHICLE:
            self._build_vehicle_sequence()
        elif trigger in (WEAPON_HIT_UNIT, WEAPON_HIT_GROUND) \
                and context == CONTEXT_WEAPON:
            self._build_weapon_sequence(trigger == WEAPON_HIT_GROUND)
        else:
            self._build_event(trigger, 0.0, self.origin)

    # -- event construction ---------------------------------------------------------

    def _unit_with_authored_audio(self, unit: "UnitPhases", document: FxDocument,
                                  enabled) -> "UnitPhases":
        """Sounds, palette flashes and shakes of the enabled key groups.

        The rows of the effects list own these keys: a hidden row is silent and
        an edited row changes the preview. A document without key groups keeps
        the sounds the unit was built with.
        """

        groups = [(index, item) for index, item in enumerate(document.items)
                  if isinstance(item, FxKeyGroup)]
        if not groups:
            return unit
        values: dict[str, str] = {}
        for index, group in groups:
            if enabled is None or index in enabled:
                values.update(group.values)
        events = WEAPON_SOUND_EVENTS if unit.weapon else VEHICLE_SOUND_EVENTS
        return replace(unit, sounds=sound_specs(values, events))

    def _rng(self, source: int, salt: str) -> random.Random:
        return random.Random(hash((self.seed, source, salt)) & 0xFFFFFFFF)

    def _build_event(self, trigger: str, start: float, origin,
                     prefix: str = "", decals_only: bool = False,
                     definition: "_Def | None" = None) -> None:
        """Spawn every enabled item listening to ``trigger`` at ``start``.

        ``decals_only`` skips the document and spawns just the ground decal.
        """

        definition = definition or self._root
        for index, item in enumerate(definition.document.items):
            if decals_only:
                break
            if definition.enabled is not None and index not in definition.enabled:
                continue
            source = index if definition.source is None else definition.source
            rng = self._rng(source, trigger + definition.salt)
            if isinstance(item, FxBlock):
                spec = definition.specs[index]
                if not spec.active or spec.trigger != trigger:
                    continue
                if spec.mode == MODE_VISUAL:
                    self._build_visual(source, spec, rng, start, origin, prefix)
                else:
                    self._build_physical(source, spec, rng, start, origin, prefix)
            elif isinstance(item, DestFxLine):
                dest = item.parsed
                if dest is None or dest.kind != trigger:
                    continue
                self._build_legacy(source, dest, origin, rng, start, 0,
                                   prefix=prefix)
        self._build_profile_decal(trigger, start, origin, prefix, definition)

    def _build_profile_decal(self, trigger: str, start: float, origin, prefix: str,
                             definition: "_Def") -> None:
        """The ground decal a unit gets from its ``ground_decal_id`` profile.

        Vehicles spawn it when destroyed, weapons on a terrain impact
        (``ResolveDecalProfileLinks``). A missing profile means no decal.
        """

        unit = definition.unit
        profile = self.decals.get(unit.ground_decal_id)
        if profile is None or trigger != ("impact_world" if unit.weapon else "destroyed"):
            return
        spec = profile.resolve(trigger, unit.decal_size)
        if spec.size[1] <= 0:
            return
        source = PROFILE_DECAL_SOURCE if definition.source is None else definition.source
        self._build_decal(source, spec, self._rng(source, trigger + definition.salt),
                          start, origin, prefix)

    def _unit_event(self, ref: VisualRef, start: float, end: float, path,
                    label: str, heading: Matrix3 = IDENTITY,
                    main: bool = False, *, motion: MotionSpec | None = None,
                    speed: float = 0.0, frozen_at: float | None = None,
                    unit: "UnitPhases | None" = None) -> None:
        """One phase of the unit. Only the main (normal) model carries the
        preview scale, rotation and spin; every state gets the tint."""

        if ref.vp <= 0 and not _external_paths(ref):
            return
        flight = _Flight(path, heading, motion, speed, frozen_at)
        self._events.append((-1, "unit", start, end,
                             (ref, flight, label, main, unit or self.unit)))

    # -- sounds ------------------------------------------------------------------------

    def _add_sound(self, sounds: dict, event: str, start: float,
                   end: float | None = None, label: str = "",
                   salt: str = "") -> None:
        """Queue ``event`` of ``sounds``: a loop when ``end`` is given."""

        spec = sounds.get(event)
        if spec is None:
            return
        rng = self._rng(hash((event, label, salt)) & 0xFFFF, "sound")
        self._sounds.append(SoundEvent(
            start, end, spec, event, _randint(rng, *spec.pitch), label))

    @property
    def sound_events(self) -> list[SoundEvent]:
        """Every sound of the preview, in start order."""

        return sorted(self._sounds, key=lambda sound: sound.start)

    def palette_effects_at(self, time_ms: float) -> list[tuple[int, float]]:
        """[(palette slot, strength)] of the flashes active at ``time_ms``."""

        settings = self.settings
        if settings.color_effects <= 0 or settings.num_palfx <= 0:
            return []
        palettes: list[tuple[int, float]] = []
        for sound in self._sounds:
            fx = sound.spec.palette
            if fx is None:
                continue
            strength = _screen_fx_strength(sound, fx, time_ms, settings.palette_fade)
            if strength is not None:
                palettes.append((fx.slot, strength))
        # The engine keeps only the audio.num_palfx strongest flashes.
        palettes.sort(key=lambda entry: -entry[1])
        return palettes[:settings.num_palfx]

    def shake_angles_at(self, time_ms: float) -> tuple[float, float, float]:
        """Turn of the view (radians around X, Y, Z) caused by the shakes now.

        Like the engine: the strongest four shakes add up
        ``rnd * strength * pos`` with a new random ``rnd`` every frame.
        """

        return self._shake_frame(time_ms, int(time_ms // SHAKE_FRAME_MS))

    def shake_angles_between(self, start_ms: float, end_ms: float
                             ) -> tuple[float, float, float]:
        """The strongest shake of the preview frames between two painted frames.

        The engine draws about 60 frames a second; the preview can paint far
        fewer, and a short shake would be missed or look sparse. Every 16 ms
        frame in between is evaluated and the one that turns the view most wins.
        """

        last = int(end_ms // SHAKE_FRAME_MS)
        first = max(int(start_ms // SHAKE_FRAME_MS) + 1, last - SHAKE_SCAN_FRAMES + 1)
        best = (0.0, 0.0, 0.0)
        for frame in range(first, last + 1):
            angles = self._shake_frame(
                min(end_ms, frame * SHAKE_FRAME_MS + SHAKE_FRAME_MS / 2.0), frame)
            if sum(value * value for value in angles) > sum(value * value for value in best):
                best = angles
        return best if any(best) else self._shake_frame(end_ms, last)

    def _shake_frame(self, time_ms: float, frame: int) -> tuple[float, float, float]:
        active = []
        for sound in self._sounds:
            fx = sound.spec.shake
            if fx is None:
                continue
            strength = _shake_strength(sound, fx, time_ms, self.settings.shake_fade)
            if strength is not None:
                active.append((strength, fx))
        if not active:
            return (0.0, 0.0, 0.0)
        rng = random.Random((self.seed * 1000003 + frame) & 0xFFFFFFFF)
        angles = [0.0, 0.0, 0.0]
        for strength, fx in sorted(active, key=lambda entry: -entry[0])[:MAX_SHAKES]:
            for axis in range(3):
                angles[axis] += rng.uniform(-1.0, 1.0) * strength * fx.pos[axis]
        return tuple(angles)

    # -- decoration_fx and weapon tracer ------------------------------------

    def _decoration_window(self, unit: "UnitPhases | None" = None) -> float:
        """Preview time the parent stays active so decorations can show up."""

        deco = (unit or self.unit).decoration
        if deco is None or not deco.spawns:
            return 0.0
        if deco.persistent:
            return DECORATION_PERSISTENT_PREVIEW_MS
        # Long enough for the first spawn (one interval in) to play out.
        return min(DECORATION_PERIODIC_PREVIEW_MAX_MS,
                   max(DECORATION_PERIODIC_PREVIEW_MIN_MS,
                       1.2 * max(deco.interval) + deco.duration))

    def _build_decoration(self, end: float, flight: _Flight,
                          unit: "UnitPhases | None" = None,
                          start: float = 0.0) -> None:
        """UpdateDecorationFX: spawns follow the parent until it stops (``end``)."""

        deco = (unit or self.unit).decoration
        if deco is None or not deco.spawns or end <= start:
            return
        rng = self._rng(-2, f"decoration{start:.0f}")
        if deco.persistent:
            self._events.append((-2, "decoration", start, end + deco.fade_out,
                                 (deco, deco.offset, flight, end, True)))
            self._tracks.append(PreviewTrack(
                -2, "Decoration · persistent", start, end + deco.fade_out,
                float(deco.fade_in), float(deco.fade_out)))
            return
        # The first update only arms the timer: the first spawn comes one
        # random interval after the parent appears.
        spawn = start + float(_randint(rng, *deco.interval))
        spawned = 0
        while spawn < end and spawned < 256:
            for _ in range(_randint(rng, *deco.count)):
                offset = tuple(
                    base + _uniform(rng, -deco.random_pos, deco.random_pos)
                    for base in deco.offset)
                self._events.append((
                    -2, "decoration", spawn, min(spawn + deco.duration, end),
                    (deco, offset, flight, end, False)))
                spawned += 1
            spawn += float(_randint(rng, *deco.interval))
        if spawned:
            self._tracks.append(PreviewTrack(
                -2, f"Decoration × {spawned}", start, end,
                float(deco.fade_in), float(deco.fade_out)))

    def _build_tracer(self, end: float, flight: _Flight,
                      unit: "UnitPhases | None" = None,
                      start: float = 0.0) -> None:
        """The mesh tracer exists only while the projectile flies."""

        tracer = (unit or self.unit).tracer
        if tracer is not None and end > start:
            self._events.append((-3, "tracer", start, end, (tracer, flight)))

    def _tracer_items(self, payload, time_ms: float) -> list[PreviewItem]:
        """RenderWeaponTracer: the last ``size_z`` of the head's path, in pieces."""

        spec, flight = payload

        def head_at(moment: float):
            position, rotation = flight.pose(moment)
            return tuple(a + b for a, b in zip(
                position, _rotate_vector(rotation, spec.pos)))

        # Head positions, newest first. A straight flight needs only its two
        # ends; spiral/chaos motion is sampled like the engine (every 16 ms).
        chain = [head_at(time_ms)]
        step = 16.0 if flight.motion is not None else max(time_ms, 1.0)
        moment, length = time_ms, 0.0
        while moment > 0 and length < spec.size_z and len(chain) < 321:
            moment = max(0.0, moment - step)
            chain.append(head_at(moment))
            length += math.dist(chain[-1], chain[-2])
        segments = []
        remaining = spec.size_z
        for newer, older in zip(chain, chain[1:]):
            span = math.dist(newer, older)
            if span <= 0.01:
                continue
            take = min(span, remaining)
            direction = tuple((n - o) / span for n, o in zip(newer, older))
            segments.append((tuple(n - d * take for n, d in zip(newer, direction)),
                             newer, take, direction))
            remaining -= take
            if remaining <= 0.01:
                break
        visible = sum(segment[2] for segment in segments)
        if visible <= 0.01:
            return []
        head_x = spec.size_x if spec.head_size is None else spec.head_size
        head_y = spec.size_y if spec.head_size is None else spec.head_size
        tail_x = spec.size_x if spec.tail_size is None else spec.tail_size
        tail_y = spec.size_y if spec.tail_size is None else spec.tail_size
        tint_head = spec.tint_head or spec.tint
        tint_tail = spec.tint_tail or spec.tint
        tapered = (abs(head_x - tail_x) > 1e-4 or abs(head_y - tail_y) > 1e-4
                   or tint_head != tint_tail)
        pulse = 1.0
        if spec.pulse_rate > 0 and spec.pulse_speed > 0:
            pulse = max(0.1, 1.0 + math.sin(
                time_ms * 0.001 * spec.pulse_speed * 2.0 * math.pi)
                * min(0.9, spec.pulse_rate * 0.1))
        glow = min(1.0, spec.glow_rate * 0.1)
        label = "tracer " + spec.mesh.replace("\\", "/").rsplit("/", 1)[-1]
        items = []
        done = 0.0
        for start, end, take, direction in segments:
            head_factor = 1.0 - done / visible
            tail_factor = 1.0 - (done + take) / visible
            done += take
            pieces = (max(1, min(8, math.ceil(12.0 * (head_factor - tail_factor))))
                      if tapered else 1)
            basis = _tracer_rotation(direction)
            for index in range(pieces):
                low, high = index / pieces, (index + 1) / pieces
                factor = tail_factor + (head_factor - tail_factor) * (low + high) * 0.5
                size_x = (tail_x + (head_x - tail_x) * factor) * pulse
                size_y = (tail_y + (head_y - tail_y) * factor) * pulse
                if size_x <= 0.01 and size_y <= 0.01:
                    continue
                center = tuple(a + (b - a) * (low + high) * 0.5
                               for a, b in zip(start, end))
                tint = tuple(round(a + (b - a) * factor)
                             for a, b in zip(tint_tail, tint_head))
                for additive, alpha in ((False, 1.0), (True, glow)):
                    if alpha <= 0:
                        continue
                    items.append(PreviewItem(
                        -3, "tracer", 0, center, basis,
                        (size_x, size_y, take / pieces), alpha, tint, label,
                        (spec.mesh,), additive))
        return items

    def _build_plasma(self, start: float, position) -> None:
        """DEATH2 of a vehicle: the genesis VP appears and shrinks away."""

        unit = self.unit
        if unit.weapon or (unit.genesis.vp <= 0 and not _external_paths(unit.genesis)):
            return
        end = start + min(unit.plasma_ms, PLASMA_PREVIEW_MS)
        self._events.append((-1, "plasma", start, end,
                             (unit.genesis, tuple(position), unit.plasma_ms,
                              unit.maxrot)))
        self._tracks.append(PreviewTrack(
            -1, "Unit · genesis plasma", start, end, approximate=True))

    def _laser_mesh_items(self, payload, time_ms: float) -> list[PreviewItem]:
        """Mesh laser: one beam from the muzzle to the contact, with a glow."""

        spec, launch, target = payload
        length = math.dist(launch, target)
        if length <= 0.01:
            return []
        direction = tuple((t - l) / length for l, t in zip(launch, target))
        basis = _tracer_rotation(direction)
        label = "laser " + spec.mesh.replace("\\", "/").rsplit("/", 1)[-1]
        pieces = 8 if spec.impact_fade > 0 else 1
        fade = min(spec.impact_fade, length)
        items = []
        for index in range(pieces):
            low, high = index / pieces, (index + 1) / pieces
            middle = (low + high) * 0.5
            alpha = 1.0
            if pieces > 1 and fade > 0 and middle * length > length - fade:
                ratio = (middle * length - (length - fade)) / fade
                alpha = 1.0 + (0.02 - 1.0) * min(1.0, ratio)
            center = tuple(l + (t - l) * middle for l, t in zip(launch, target))
            for additive, power in ((False, 1.0), (True, min(1.0, spec.glow_rate * 0.1))):
                if power <= 0:
                    continue
                tint = (*spec.tint[:3], round(spec.tint[3] * alpha))
                items.append(PreviewItem(
                    -3, "tracer", 0, center, basis,
                    (spec.size_x, spec.size_y, length / pieces), power, tint,
                    label, (spec.mesh,), additive))
        return items

    def _build_vehicle_sequence(self) -> None:
        unit = self.unit
        death = max(SEQUENCE_PREROLL_MS, self._decoration_window())
        altitude = max(0.0, unit.altitude) if not unit.skips_dead_phase else 0.0
        kill = (self.origin[0], self.origin[1] - altitude, self.origin[2])
        self._unit_event(unit.normal, 0.0, death, [(0.0, kill, False)],
                         "normal", main=True)
        self._build_decoration(death, _Flight([(0.0, kill, False)]))
        sounds = unit.sounds
        self._add_sound(sounds, "normal", 0.0, death, "unit")
        if unit.skips_dead_phase:
            # Tank/car: SetState(DEAD) becomes DEATH2 immediately.
            land_ms, landed = death, kill
            self.notes.append(
                f"model = {unit.model}: straight to megadeth, no dead model, "
                "no air_destroyed and no dest_fx death (engine rule).")
        else:
            # Dead wrecks fall without air resistance: CrashOrLand sets
            # _airconst = 0 for non-inline dead vehicles.
            fall = _Fall(list(kill), [0.0, 0.0, 0.0], 1.0, 0.0)
            path = self._record_path(fall, LEGACY_DEAD_LIFETIME_MS,
                                     max(unit.overeof, 0.0))
            shifted = [(death + ms, position, flag)
                       for ms, position, flag in path]
            land_ms, landed = shifted[-1][0], shifted[-1][1]
            self._wreck_path = shifted
            self._build_event("air_destroyed", death, kill, "Air · ")
            self._wreck_path = None
            self._build_event("death", death, kill, "Death · ")
            if unit.flies and altitude > 0:
                self._add_sound(sounds, "airexplode", death, label="unit")
            self._add_sound(sounds, "goingdown", death, land_ms, "unit")
            self._add_sound(sounds, "crashland", land_ms, label="unit")
            self._unit_event(unit.dead, death, land_ms, shifted, "dead")
            if land_ms > death + 1.0:
                self._tracks.append(PreviewTrack(
                    -1, "Unit · wreck falls", death, land_ms, approximate=True))
        ground = (landed[0], self.origin[1], landed[2])
        self._build_event("destroyed", land_ms, ground, "Ground · ")
        self._build_event("megadeth", land_ms, ground, "Mega · ")
        self._add_sound(sounds, "explode", land_ms, label="unit")
        self._build_plasma(land_ms, landed)
        hold_end = max(self.duration, land_ms) + MEGADETH_HOLD_MS
        self._unit_event(unit.megadeth, land_ms, hold_end,
                         [(land_ms, landed, True)], "megadeth")
        self._tracks.insert(0, PreviewTrack(
            -1, "Unit · killed", death, land_ms,
            marks=(land_ms,) if land_ms > death else ()))

    def _build_weapon_sequence(self, hit_ground: bool) -> None:
        unit = self.unit
        height = unit.altitude if unit.altitude > 0 else WEAPON_LAUNCH_HEIGHT
        target = (self.origin[0],
                  self.ground_y if hit_ground else self.origin[1],
                  self.origin[2])
        launch = (target[0], target[1] - height, target[2])
        # Constant speed: a higher launch point takes proportionally longer.
        impact = max(WEAPON_FLIGHT_MS * height / WEAPON_LAUNCH_HEIGHT,
                     self._decoration_window())
        if unit.fixed_speed > 0:
            impact = max(height / (unit.fixed_speed * 0.001), 300.0,
                         self._decoration_window())
        cluster = unit.cluster
        if cluster is not None and cluster.trigger_time > 0:
            # A cluster that splits in flight needs a flight long enough.
            impact = max(impact, cluster.trigger_time * 1.3)
        stop = self._build_flight(self._root, 0.0, launch, target, impact,
                                  hit_ground=hit_ground, generation=0, root=True)
        self._tracks.insert(0, PreviewTrack(
            -1, "Projectile flight", 0.0, stop, marks=(stop,)))
        if unit.tracer is not None:
            self._tracks.insert(1, PreviewTrack(-3, "Mesh tracer", 0.0, stop))

    def _build_flight(self, definition: "_Def", start: float, launch, target,
                      end: float, *, hit_ground: bool, generation: int,
                      root: bool) -> float:
        """One projectile from ``launch`` (at ``start``) to ``target`` (at ``end``).

        Returns the time it stops: its split time when a cluster splits it in
        flight, else its impact time.
        """

        unit = definition.unit
        heading = tuple(t - l for t, l in zip(target, launch))
        look = _look_rotation(heading)
        speed = math.dist(launch, target) / max((end - start) * 0.001, 1e-3)
        path = [(start, launch, False), (end, target, True)]
        flight = _Flight(path, look, unit.motion, speed)
        cluster = self._cluster_of(definition, generation)
        split = (start + cluster.trigger_time
                 if cluster is not None and 0 < cluster.trigger_time
                 and start + cluster.trigger_time < end else None)
        stop = end if split is None else split
        who = "weapon" if root else f"cluster{definition.salt}"
        if unit.laser is not None:
            # A laser is a beam from the muzzle to the contact, not a projectile.
            self._build_laser(unit, launch, target, look, start, end)
        else:
            self._unit_event(unit.normal, start, stop, path, "normal", look,
                             main=True, motion=unit.motion, speed=speed,
                             unit=unit)
        self._build_event("launch", start, launch, "Launch · ", definition=definition)
        self._add_sound(unit.sounds, "launch", start, label=who, salt=definition.salt)
        if root:
            self._add_sound(unit.sounds, "launch_player", start, label=who,
                            salt=definition.salt)
        self._add_sound(unit.sounds, "normal", start, stop, who, definition.salt)
        self._build_decoration(stop, flight, unit, start)
        self._build_tracer(stop, flight, unit, start)
        if split is not None:
            # The projectile is replaced by its children: no impact effects.
            # Like the engine (_position), children and flash start from the real
            # path point, not from the render-only spiral/chaos offset of the parent.
            position = _path_position(path, split)
            self._spawn_cluster(cluster, definition, position, look, split,
                                speed, generation)
            return stop
        blast = end
        if not hit_ground:
            self._hit_unit_effects(unit, target, end)
            if unit.delay_time > 0 and unit.laser is None:
                # delay_time: the projectile sticks to the unit and explodes later.
                blast = end + min(float(unit.delay_time), DELAY_PREVIEW_MAX_MS)
                self._unit_event(unit.normal, end, blast, [(end, target, True)],
                                 "attached", look, main=True, unit=unit)
                if root:
                    self.notes.append(
                        f"delay_time: the weapon sticks to the unit and "
                        f"detonates {unit.delay_time} ms after the hit.")
        self._add_sound(unit.sounds, "hit", blast, label=who, salt=definition.salt)
        end = blast
        if hit_ground:
            # DEATH2: vp_megadeth, impact_world chain, dest_fx megadeth.
            self._build_event("impact_world", end, target, "Ground hit · ",
                              definition=definition)
            self._build_event("megadeth", end, target, "Mega · ",
                              definition=definition)
            final, label = unit.megadeth, "megadeth"
        else:
            # DEAD: vp_dead, detonate chain, dest_fx death.
            self._build_event("detonate", end, target, "Unit hit · ",
                              definition=definition)
            self._build_event("death", end, target, "Death · ",
                              definition=definition)
            if root:
                # The engine spawns ground decals only on terrain hits. They
                # are previewed here too so they can be checked without
                # switching mode.
                before = len(self._events)
                self._build_event("impact_world", end,
                                  (target[0], self.ground_y, target[2]),
                                  "Preview · ", decals_only=True)
                if len(self._events) > before:
                    self.notes.append(
                        "Hits a unit: the ground decal is shown for preview "
                        "only. The engine spawns it when the weapon hits the "
                        "terrain.")
            final, label = unit.dead, "dead"
        # The projectile's visual motion stays frozen on its last pose.
        self._unit_event(final, end, end + IMPACT_PREVIEW_MS,
                         [(end, target, True)], label, look,
                         motion=unit.motion, speed=speed, frozen_at=end,
                         unit=unit)
        if cluster is not None and cluster.trigger_time <= 0:
            # trigger_time 0: the split happens on impact.
            self._spawn_cluster(cluster, definition, target, look, end, speed,
                                generation)
        return stop

    # -- laser beam and debuffs --------------------------------------------------------------

    def _build_laser(self, unit: "UnitPhases", launch, target, look: Matrix3,
                     start: float, end: float) -> None:
        """Beam from the muzzle to the contact, for the whole flight time."""

        spec = unit.laser
        length = math.dist(launch, target)
        if length <= 0.01:
            return
        if spec.mesh:
            self._events.append((-3, "laser_mesh", start, end,
                                 (spec, tuple(launch), tuple(target))))
            return
        # Legacy beam: normal-state models every ``spacing`` units.
        count = min(120, int(length // spec.spacing) + 1)
        positions = [tuple(l + (t - l) * ((index + 0.5) * spec.spacing / length)
                           for l, t in zip(launch, target))
                     for index in range(count)
                     if (index + 0.5) * spec.spacing <= length]
        if positions and (unit.normal.vp > 0 or _external_paths(unit.normal)):
            self._events.append((-1, "laser", start, end,
                                 (unit.normal, positions, look, unit)))

    def _hit_unit_effects(self, unit: "UnitPhases", position, at: float) -> None:
        """Debuffs a hit unit shows (debuff_id, delay_time_debuff_id)."""

        for debuff_id in dict.fromkeys((unit.debuff_id, unit.delay_debuff_id)):
            if debuff_id < 0:
                continue
            spec = self.debuffs.get(debuff_id)
            if spec is None:
                note = f"debuff {debuff_id} is not in the loaded game scripts."
                if note not in self.notes:
                    self.notes.append(note)
                continue
            self._spawn_debuff(spec, position, at)

    def _spawn_debuff(self, spec: DebuffSpec, position, at: float) -> None:
        """Debuff visuals: once on the hit and again at every tick."""

        window = min(float(spec.duration) if spec.duration > 0 else DEBUFF_PREVIEW_MS,
                     DEBUFF_PREVIEW_MS)
        lifetime = float(max(1000, min(5000, spec.tick_time + 100)))
        rng = self._rng(-5, f"debuff{spec.debuff_id}{at:.0f}")
        radius = spec.max_offset * (DEBUFF_UNIT_SIZE / 100.0
                                    if spec.offset_percent else 1.0)
        times = [0.0]
        while spec.tick_time > 0 and times[-1] + spec.tick_time < window and len(times) < 40:
            times.append(times[-1] + spec.tick_time)
        visuals = [VisualRef(vp=vp) for vp in spec.vps]
        if spec.mesh3ds:
            visuals.append(VisualRef(mesh3ds=spec.mesh3ds))
        for tick in times:
            for ref in visuals:
                offset = tuple(_uniform(rng, -radius, radius) for _ in range(3))
                self._events.append((-5, "debuff", at + tick, at + tick + lifetime,
                                     (ref, tuple(p + o for p, o in zip(position, offset)),
                                      spec)))
        if visuals:
            self._tracks.append(PreviewTrack(
                -5, f"Debuff {spec.debuff_id} · {spec.tick_time or 'once'} ms",
                at, at + window))
        if spec.sound is not None:
            loop_end = at + window if spec.tick_time <= 0 else None
            for tick in ([0.0] if loop_end else times):
                self._add_sound({"debuff": spec.sound}, "debuff", at + tick, loop_end,
                                "debuff", f"{spec.debuff_id}")

    # -- weapon clusters ------------------------------------------------------------------

    def _cluster_of(self, definition: "_Def", generation: int):
        """The cluster ``definition`` may split with at this generation."""

        spec = definition.unit.cluster
        if spec is None:
            return None
        if generation > 0 and (spec.generations <= 0
                               or generation + 1 > spec.generations):
            return None
        if spec.weapon_id not in self.weapons:
            note = (f"cluster_weapon_id {spec.weapon_id} is not in the loaded "
                    "game catalog.")
            if note not in self.notes:
                self.notes.append(note)
            return None
        return spec

    def _cluster_direction(self, rng, look: Matrix3, spec: ClusterSpec,
                           index: int, total: int):
        """Direction of one child: parent heading, cluster angles, then spread."""

        forward = tuple(look[row][2] for row in range(3))
        direction = forward
        if spec.horizontal is not None or spec.vertical is not None:
            h_low, h_high = sorted(spec.horizontal or (0.0, 0.0))
            v_low, v_high = sorted(spec.vertical or (0.0, 0.0))
            if spec.horizontal is not None and abs(h_high - h_low - 360.0) <= 0.001:
                yaw = h_low + index / total * 360.0
            elif spec.horizontal is not None:
                yaw = rng.uniform(h_low, h_high)
            else:
                yaw = 0.0
            pitch = rng.uniform(v_low, v_high) if spec.vertical is not None else 0.0
            yaw, pitch = math.radians(yaw), math.radians(pitch)
            local = (math.sin(yaw) * math.cos(pitch), math.sin(pitch),
                     math.cos(yaw) * math.cos(pitch))
            direction = _rotate_vector(look, local)
        spread_x, spread_y = spec.spread
        if spread_x > 0 or spread_y > 0:
            length = math.sqrt(sum(v * v for v in direction)) or 1.0
            aim = tuple(v / length for v in direction)
            right = tuple(look[row][0] for row in range(3))
            dot = sum(a * r for a, r in zip(aim, right))
            right = tuple(r - a * dot for r, a in zip(right, aim))
            norm = math.sqrt(sum(v * v for v in right))
            if norm > 1e-3:
                right = tuple(v / norm for v in right)
                up = _cross(aim, right)
                shift_x = rng.uniform(-1.0, 1.0) * math.tan(math.radians(spread_x))
                shift_y = rng.uniform(-1.0, 1.0) * math.tan(math.radians(spread_y))
                aim = tuple(a + r * shift_x + u * shift_y
                            for a, r, u in zip(aim, right, up))
                length = math.sqrt(sum(v * v for v in aim)) or 1.0
                direction = tuple(v / length for v in aim)
        length = math.sqrt(sum(v * v for v in direction)) or 1.0
        return tuple(v / length for v in direction)

    def _spawn_cluster(self, spec: ClusterSpec, parent: "_Def", position,
                       look: Matrix3, at: float, speed: float,
                       generation: int) -> None:
        """TryClusterSplit: the flash, the sound and the child projectiles."""

        child = self.weapons.get(spec.weapon_id)
        if child is None:
            return
        rng = self._rng(-4 - generation, f"cluster{at:.0f}{parent.salt}")
        total = _randint(rng, *spec.count)
        position = tuple(position)
        if spec.visual.vp > 0 or _external_paths(spec.visual):
            self._events.append((-3, "cluster", at, at + CLUSTER_FLASH_MS,
                                 (spec.visual, position, look)))
        if spec.sound is not None:
            self._add_sound({"cluster": spec.sound}, "cluster", at,
                            label="cluster", salt=parent.salt)
        specs = {index: interpret_block(item, CONTEXT_WEAPON)
                 for index, item in enumerate(child.document.items)
                 if isinstance(item, FxBlock)}
        # Children use the speed of the previewed projectile, scaled by how
        # much faster or slower their weapon is.
        ratio = (child.unit.start_speed / parent.unit.start_speed
                 if child.unit.start_speed > 0 and parent.unit.start_speed > 0
                 else 1.0)
        child_speed = max(speed * ratio, 1.0)
        saved_tracks, self._tracks = self._tracks, []
        last = at
        spawned = 0
        for index in range(total):
            if self._cluster_projectiles >= CLUSTER_PREVIEW_LIMIT:
                break
            self._cluster_projectiles += 1
            spawned += 1
            direction = self._cluster_direction(rng, look, spec, index, total)
            # Descending children fall to the ground; others are followed
            # for a short while and burst in the air.
            hit_ground = direction[1] > 0.05 and position[1] < self.ground_y - 1.0
            distance = ((self.ground_y - position[1]) / direction[1] if hit_ground
                        else child_speed * CLUSTER_FREE_FLIGHT_MS * 0.001)
            flight_ms = min(distance / child_speed * 1000.0, CLUSTER_MAX_FLIGHT_MS)
            if flight_ms < distance / child_speed * 1000.0:
                distance, hit_ground = child_speed * flight_ms * 0.001, False
            target = tuple(p + d * distance for p, d in zip(position, direction))
            if hit_ground:
                target = (target[0], self.ground_y, target[2])
            definition = _Def(child.unit, child.document, specs, None, -4,
                              f"-{generation}-{index}-{at:.0f}")
            last = max(last, self._build_flight(
                definition, at, position, target, at + max(flight_ms, 50.0),
                hit_ground=hit_ground, generation=generation + 1, root=False))
        # Rows of clusters that split again stay; the children's own effect
        # rows would only clutter the timeline.
        nested = [track for track in self._tracks if track.source == -4]
        self._tracks = saved_tracks + nested
        if spawned:
            self._tracks.append(PreviewTrack(
                -4, f"Cluster × {spawned} · weapon {spec.weapon_id}", at, last))

    def _build_visual(self, source, spec, rng, start, origin, prefix) -> None:
        count = _randint(rng, *spec.count)
        for _ in range(count):
            offset = tuple(_uniform(rng, low, high)
                           for low, high in zip(spec.offset_min, spec.offset_max))
            position = tuple(o + d for o, d in zip(origin, offset))
            rotation = _random_rotation(rng, spec.random_rotation)
            self._events.append((source, MODE_VISUAL, start,
                                 start + spec.duration,
                                 (spec, position, rotation)))
        slices = len(spec.visuals)
        marks = tuple(start + spec.duration * index / slices
                      for index in range(1, slices))
        self._tracks.append(PreviewTrack(
            source, f"{prefix}Visual × {count}", start,
            start + spec.duration, float(spec.fade_in), float(spec.fade_out),
            marks))

    def _build_physical(self, source, spec, rng, start, origin, prefix) -> None:
        count = _randint(rng, *spec.count)
        if spec.physical_vehicle > 0:
            # Legacy physical_vehicle path: identical to a dest_fx spawn.
            for _ in range(count):
                offset = tuple(
                    _uniform(rng, low, high)
                    for low, high in zip(spec.offset_min, spec.offset_max))
                dest = DestFx("death", spec.physical_vehicle, *offset)
                self._build_legacy(source, dest, origin, rng, start, 0,
                                   random_rotation=spec.random_rotation,
                                   prefix=prefix)
            return
        end_max = start
        for _ in range(count):
            launch = [_uniform(rng, low, high)
                      for low, high in zip(spec.launch_min, spec.launch_max)]
            speed = math.sqrt(sum(value * value for value in launch))
            if speed > 0.001 and spec.mass > 0:
                impulse = spec.force / spec.mass
                launch = [value + value / speed * impulse for value in launch]
            vp, impact = 0, 0
            if spec.vp_models:
                pick = _randint(rng, 0, len(spec.vp_models) - 1)
                vp = spec.vp_models[pick]
                if len(spec.vp_impacts) == len(spec.vp_models):
                    impact = spec.vp_impacts[pick]
            elif len(spec.vp_impacts) == 1:
                impact = spec.vp_impacts[0]
            lifetime = float(_randint(rng, *spec.lifetime))
            rotation = _random_rotation(rng, spec.random_rotation)
            wreck = self._wreck_path
            event_spec = spec
            if spec.follow_carcass and wreck and len(wreck) > 1:
                # follow_carcass: the fragment copies the falling wreck until
                # the wreck lands, so it has no spin of its own.
                path = [(ms - start, position, landed)
                        for ms, position, landed in wreck if ms >= start]
                event_spec = replace(spec, maxrot=0.0)
            else:
                # Inline fragments keep their authored air resistance.
                fall = _Fall(list(origin), launch, spec.mass, spec.airconst)
                path = self._record_path(fall, lifetime, max(spec.radius, 0.0))
            land_ms, final = start + path[-1][0], path[-1][1]
            self._events.append((source, MODE_PHYSICAL, start, land_ms,
                                 (event_spec, vp, path, rotation)))
            # Fragments are born dead: normal once, goingdown while they fly,
            # explode when they end.
            fragment = f"fragment {source}"
            salt = f"{start:.0f}-{len(self._events)}"
            self._add_sound(spec.sounds, "normal", start, label=fragment, salt=salt)
            self._add_sound(spec.sounds, "goingdown", start, land_ms,
                            fragment, salt)
            self._add_sound(spec.sounds, "explode", land_ms, label=fragment,
                            salt=salt)
            end = land_ms
            if impact > 0:
                self._events.append((
                    source, "impact", land_ms, land_ms + IMPACT_PREVIEW_MS,
                    (impact, final, spec.visual_scale, spec.physical_tint)))
                end = land_ms + IMPACT_PREVIEW_MS
            end_max = max(end_max, end)
        self._tracks.append(PreviewTrack(
            source, f"{prefix}Physical × {count}", start, end_max,
            approximate=True))

    def _record_path(self, fall: _Fall, lifetime: float, clearance: float):
        """Sample one dead flight every step: [(ms, position, landed)].

        DeadTimeUpdate splits each frame into three Move sub-steps; the
        flight ends on ground contact or when its lifetime expires. A
        fragment that never lands is followed for 8 s in the preview.
        """

        path = [(0.0, tuple(fall.position), False)]
        elapsed = 0.0
        limit = min(lifetime, 8000.0)
        while elapsed < limit:
            step = min(SIMULATION_STEP_MS, limit - elapsed)
            for _ in range(3):
                fall.step(step / 3000.0)
            elapsed += step
            # Ground contact counts only while descending (UA +Y is down),
            # so fragments launched upward from near the ground take off.
            landed = (fall.velocity[1] > 0.0
                      and fall.position[1] + clearance >= self.ground_y)
            if landed:
                fall.position[1] = self.ground_y - clearance
            path.append((elapsed, tuple(fall.position), landed))
            if landed:
                break
        return path

    def _build_decal(self, source, spec, rng, start, origin, prefix) -> None:
        size = _uniform(rng, *spec.size)
        stretch_x = _uniform(rng, *spec.stretch_x)
        stretch_z = _uniform(rng, *spec.stretch_z)
        angle = rng.uniform(0.0, 2.0 * math.pi) if spec.random_rotation else 0.0
        edge = (float(_randint(rng, int(spec.edge_fade[0]), int(spec.edge_fade[1])))
                if spec.edge_fade[0] != spec.edge_fade[1] else spec.edge_fade[0])
        end = math.inf if spec.permanent else start + float(spec.duration)
        position = (origin[0], self.ground_y, origin[2])
        self._events.append((source, MODE_DECAL, start, end,
                             (spec, position, angle, size * stretch_x,
                              size * stretch_z, edge)))
        shown_end = end if math.isfinite(end) else start + max(4000.0, spec.fade_in)
        self._tracks.append(PreviewTrack(
            source, f"{prefix}Ground decal", start, shown_end,
            float(spec.fade_in), float(spec.fade_out)))
        if spec.smoke is not None:
            self._build_decal_smoke(source, spec.smoke, rng, start, position,
                                    0.5 * math.hypot(size * stretch_x, size * stretch_z),
                                    prefix)

        if spec.fire is not None:
            ignited=self._build_decal_smoke(source,spec.fire,rng,start,position,
                0.5*math.hypot(size*stretch_x,size*stretch_z),prefix)
            if ignited and spec.fire_propagation_radius>0:
                child=replace(spec,fire_propagation_count=0,
                              fire=replace(spec.fire,chance=100),
                              smoke=spec.smoke or decal_smoke.SmokeSpec(enabled=True))
                for _ in range(min(8,spec.fire_propagation_count)):
                    if rng.random()*100>=spec.fire_propagation_chance:
                        continue
                    angle=rng.uniform(0,2*math.pi)
                    distance=spec.fire_propagation_radius*math.sqrt(rng.random())
                    origin=(position[0]+math.cos(angle)*distance,self.ground_y,
                            position[2]+math.sin(angle)*distance)
                    self._build_decal(source,child,rng,start+spec.fire_propagation_delay,origin,prefix)

    def _build_decal_smoke(self, source, smoke, rng, start, position, radius,
                           prefix) -> None:
        seed = rng.getrandbits(24)
        if not decal_smoke.rolls_smoke(smoke, seed):
            return
        count = decal_smoke.scaled_count(decal_smoke.emitter_count(smoke, seed), radius)
        real = decal_smoke.emitter_duration(smoke, seed)
        if count <= 0 or real <= 0:
            return
        shown = min(float(real), SMOKE_PREVIEW_MS)
        self._events.append((source, "decal_fire" if smoke.flame else "decal_smoke", start, start + shown,
                             (smoke, position, radius, seed, count, real)))
        self._tracks.append(PreviewTrack(
            source, f"{prefix}Decal fire" if smoke.flame else f"{prefix}Decal smoke", start, start + shown,
            float(smoke.fade_in), float(smoke.fade_out) if real <= shown else 0.0,
            approximate=real > shown))
        if real > shown:
            self.notes.append(
                f"Decal smoke lasts {real / 1000:.0f} s in the engine; the "
                f"preview plays its first {shown / 1000:.0f} s.")

        return True

    def _build_legacy(self, source, dest, origin, rng, start, depth, *,
                      random_rotation: bool = False,
                      radius: float | None = None, prefix: str = "") -> None:
        vehicle = self.catalog.get(dest.vehicle_id)
        if vehicle is None:
            self.notes.append(
                f"dest_fx vehicle {dest.vehicle_id} is not in the loaded "
                "game catalog.")
            return
        radius = self.unit_radius if radius is None else radius
        offset = (dest.x, dest.y, dest.z)
        length = math.sqrt(sum(value * value for value in offset))
        position = list(origin)
        # StartDestFX: the offset is a direction scaled to the unit radius,
        # applied only to units larger than 31.
        if radius > 31.0 and length > 0.1:
            position = [o + value / length * radius
                        for o, value in zip(origin, offset)]
        rotation = _random_rotation(rng, random_rotation)
        scale_fx = vehicle.scale_fx
        label = (f"{prefix}{dest.kind} → {vehicle.name or 'vehicle'} "
                 f"{vehicle.vehicle_id}")
        if scale_fx is not None and scale_fx.vps and scale_fx.duration > 0:
            # Scale-effect vehicles stay at their spawn point
            # (CrashOrLand__sub0 replaces the movement step).
            self._events.append((source, "scale_fx", start,
                                 start + scale_fx.duration,
                                 (scale_fx, tuple(position), rotation,
                                  vehicle.maxrot)))
            fx_end = start + scale_fx.duration
            salt = f"{start:.0f}-{len(self._events)}"
            self._add_sound(vehicle.sounds, "goingdown", start, fx_end, label, salt)
            self._add_sound(vehicle.sounds, "explode", fx_end, label=label, salt=salt)
            marks = tuple(start + scale_fx.duration * index / len(scale_fx.vps)
                          for index in range(1, len(scale_fx.vps)))
            self._tracks.append(PreviewTrack(
                source, f"{label} (scale_fx)", start,
                start + scale_fx.duration, marks=marks))
            return
        # No scale_fx: the FX vehicle falls with vp_dead (no air resistance
        # for dead non-inline vehicles), then shows its megadeth VP and
        # triggers its own megadeth dest_fx chain.
        if dest.accel:
            self.notes.append(
                "dest_fx accel adds the unit's flight speed; the preview "
                "unit is static.")
        fall = _Fall(position, list(offset), vehicle.mass, 0.0)
        path = self._record_path(fall, LEGACY_DEAD_LIFETIME_MS,
                                 max(vehicle.radius, 0.0))
        land_ms = start + path[-1][0]
        final = path[-1][1]
        shifted = [(start + ms, pos, landed) for ms, pos, landed in path]
        salt = f"{start:.0f}-{len(self._events)}"
        self._add_sound(vehicle.sounds, "goingdown", start, land_ms, label, salt)
        self._add_sound(vehicle.sounds, "explode", land_ms, label=label, salt=salt)
        self._events.append((source, "legacy", start, land_ms,
                             (vehicle.vp_dead, shifted, rotation)))
        end = land_ms
        if vehicle.vp_megadeth > 0:
            self._events.append((source, "impact", land_ms,
                                 land_ms + IMPACT_PREVIEW_MS,
                                 (vehicle.vp_megadeth, final, 1.0, None)))
            end = land_ms + IMPACT_PREVIEW_MS
        self._tracks.append(PreviewTrack(
            source, label, start, end, approximate=True))
        if depth + 1 < LEGACY_NESTING_LIMIT:
            for child in vehicle.dest_fx:
                if child.kind == "megadeth":
                    self._build_legacy(source, child, final, rng, land_ms,
                                       depth + 1, radius=vehicle.radius,
                                       prefix=prefix)

    # -- sampling -----------------------------------------------------------------------

    @property
    def tracks(self) -> list[PreviewTrack]:
        return list(self._tracks)

    @property
    def duration(self) -> float:
        ends = [end for _source, kind, _start, end, _payload in self._events
                if math.isfinite(end) and kind != "unit"]
        ends += [track.end for track in self._tracks if math.isfinite(track.end)]
        return max(ends, default=0.0)

    @property
    def shows_unit(self) -> bool:
        """True when the unit is drawn through its own phase items."""

        return any(kind == "unit" for _s, kind, _a, _b, _p in self._events)

    def items_at(self, time_ms: float) -> list[PreviewItem]:
        items: list[PreviewItem] = []
        for source, kind, start, end, payload in self._events:
            if time_ms < start or time_ms >= end:
                continue
            age = time_ms - start
            if kind == MODE_VISUAL:
                spec, position, rotation = payload
                index, size, alpha, spin = visual_chain_state(spec, age)
                visual = spec.visuals[index]
                items.append(PreviewItem(
                    source, kind, visual.vp, position,
                    _mat_mul(rotation, spin), (size, size, size), alpha,
                    visual.tint, visual.label, _external_paths(visual)))
            elif kind == MODE_PHYSICAL:
                spec, vp, path, rotation = payload
                position = _path_position(path, age)
                turn = _rot_y(spec.maxrot * age * 0.001)
                scale = spec.visual_scale
                items.append(PreviewItem(
                    source, kind, vp, position, _mat_mul(turn, rotation),
                    (scale, scale, scale), 1.0, spec.physical_tint,
                    f"VP {vp}", _external_paths(spec.physical_model)))
            elif kind == "impact":
                vp, position, scale, tint = payload
                items.append(PreviewItem(
                    source, "impact", vp, position, IDENTITY,
                    (scale, scale, scale), 1.0, tint, f"impact VP {vp}"))
            elif kind == "scale_fx":
                scale_fx, position, rotation, maxrot = payload
                size = _scale_fx_size(scale_fx, age)
                count = len(scale_fx.vps)
                index = min(int(age * count / scale_fx.duration), count - 1)
                turn = _rot_y(maxrot * age * 0.001)
                items.append(PreviewItem(
                    source, "legacy", scale_fx.vps[index], position,
                    _mat_mul(turn, rotation), (size, size, size), 1.0, None,
                    f"scale_fx VP {scale_fx.vps[index]}"))
            elif kind == "legacy":
                vp, path, rotation = payload
                position = _path_position(path, time_ms)
                items.append(PreviewItem(
                    source, "legacy", vp, position, rotation,
                    (1.0, 1.0, 1.0), 1.0, None, f"vp_dead {vp}"))
            elif kind == "unit":
                ref, flight, label, main, owner = payload
                position, heading = flight.pose(time_ms)
                rotation = (_mat_mul(heading, owner.visual_matrix(time_ms))
                            if main else heading)
                tint = owner.tint
                items.append(PreviewItem(
                    -1, "unit", ref.vp, position,
                    rotation, (1.0, 1.0, 1.0), 1.0,
                    None if tint in (None, WHITE) else tint,
                    f"{label} · {ref.label}", _external_paths(ref)))
            elif kind == "plasma":
                ref, position, total, maxrot = payload
                size = _plasma_scale(total, age)
                tint = self.unit.tint
                items.append(PreviewItem(
                    -1, "plasma", ref.vp, position,
                    _rot_y(maxrot * 2.0 * age * 0.001), (size, size, size), 1.0,
                    None if tint in (None, WHITE) else tint,
                    f"genesis plasma · {ref.label}", _external_paths(ref)))
            elif kind == "decoration":
                deco = payload[0]
                alpha = _decoration_alpha(payload, start, time_ms, trail=False)
                position, heading = _decoration_pose(payload, time_ms)
                items.append(PreviewItem(
                    -2, "decoration", deco.visual.vp, position,
                    _mat_mul(heading, spin_matrix(deco.spin, age)),
                    deco.scale, alpha,
                    None if deco.tint == WHITE else deco.tint,
                    f"decoration · {deco.visual.label}",
                    _external_paths(deco.visual)))
            elif kind == "laser":
                ref, positions, heading, owner = payload
                tint = owner.tint
                rotation = _mat_mul(heading, owner.visual_matrix(time_ms))
                items.extend(PreviewItem(
                    -1, "laser", ref.vp, position, rotation, (1.0, 1.0, 1.0), 1.0,
                    None if tint in (None, WHITE) else tint, "laser beam",
                    _external_paths(ref)) for position in positions)
            elif kind == "laser_mesh":
                items.extend(self._laser_mesh_items(payload, time_ms))
            elif kind == "debuff":
                ref, position, spec = payload
                age = time_ms - start
                items.append(PreviewItem(
                    -5, "debuff", ref.vp, position, IDENTITY,
                    (spec.scale,) * 3, 1.0,
                    None if spec.tint == WHITE else spec.tint,
                    f"debuff {spec.debuff_id} · {ref.label}", _external_paths(ref)))
            elif kind == "cluster":
                ref, position, heading = payload
                items.append(PreviewItem(
                    -4, "cluster", ref.vp, position, heading, (1.0, 1.0, 1.0),
                    1.0, None, f"cluster · {ref.label}", _external_paths(ref)))
            elif kind == "tracer":
                items.extend(self._tracer_items(payload, time_ms))
            elif kind == MODE_DECAL:
                spec, position, angle, width, depth, edge = payload
                alpha = temporal_envelope(
                    age, 0.0 if spec.permanent else spec.duration,
                    spec.fade_in, spec.fade_out)
                items.append(PreviewItem(
                    source, MODE_DECAL, 0, position, _rot_y(angle),
                    (1.0, 1.0, 1.0), alpha, spec.decal_tint, "decal",
                    texture=spec.texture, decal_size=(width, depth),
                    decal_edge_fade=edge))
            elif kind in ("decal_smoke","decal_fire"):
                smoke, position, radius, seed, count, real = payload
                gain = smoke.intensity * 0.01 * smoke.tint[3] / 255.0
                for slot in range(count):
                    puff = decal_smoke.evaluate(
                        smoke, radius, seed, count, real, slot, age)
                    if puff is None:
                        continue
                    shade = min(1.0, (0.85 + 0.5 * puff.phase) * puff.shade)
                    rgb = tuple(int(round(min(255.0, value * shade)))
                                for value in smoke.tint[:3])
                    items.append(PreviewItem(
                        source, kind, 0,
                        tuple(base + offset for base, offset
                              in zip(position, puff.offset)),
                        IDENTITY, (puff.size*.45,puff.size*1.6,puff.size) if smoke.flame else (puff.size,)*3,
                        min(1.0, puff.alpha * gain), (*rgb, 255), "fire" if smoke.flame else "smoke",additive=smoke.flame))
        return items

    def offset_boxes(self) -> list[tuple[int, tuple, tuple]]:
        """Offset ranges of enabled visual blocks, for the viewport overlay."""

        boxes = []
        for source, spec in self._specs.items():
            if self.enabled is not None and source not in self.enabled:
                continue
            if (spec.active and spec.mode == MODE_VISUAL
                    and spec.offset_min != spec.offset_max):
                low = tuple(o + v for o, v in zip(self.origin, spec.offset_min))
                high = tuple(o + v for o, v in zip(self.origin, spec.offset_max))
                boxes.append((source, low, high))
        return boxes

    def extent(self, samples: int = 16) -> tuple[tuple, float] | None:
        """Center and radius covering every item over the whole timeline."""

        length = self.duration
        points = []
        for step in range(samples + 1):
            time_ms = length * step / samples if length > 0 else 0.0
            for item in self.items_at(time_ms):
                if item.kind == MODE_DECAL:
                    reach = 0.5 * max(item.decal_size)
                elif item.kind == "tracer":
                    reach = 0.5 * item.scale[2]
                elif item.kind in ("decal_smoke","decal_fire"):
                    reach = 0.5 * item.scale[0]
                else:
                    reach = 60.0 * max(item.scale)
                points.append((item.position, reach))
        if not points:
            return None
        lows = [min(p[axis] - r for p, r in points) for axis in range(3)]
        highs = [max(p[axis] + r for p, r in points) for axis in range(3)]
        center = tuple((low + high) * 0.5 for low, high in zip(lows, highs))
        radius = max(high - low for low, high in zip(lows, highs)) * 0.5
        return center, max(radius, 50.0)

    # -- particle emitters ------------------------------------------------------------

    def vp_instances(self) -> list["VpInstance"]:
        """Every VP render instance with its lifetime and motion.

        Each one is what the engine keeps as one ``GenRenderInstance``: a
        visual chain slot, a fragment, an impact VP, a scale_fx slot or a
        unit phase. Its particle emitters run while it exists.
        """

        if self._instances is None:
            self._instances = []
            for source, kind, start, end, payload in self._events:
                self._instances.extend(_instances_for_event(
                    source, kind, start, end, payload, self.unit))
        return self._instances

    def particles_at(self, time_ms: float,
                     emitters: dict[int, list["EmitterSpec"]],
                     limit: int = 6000) -> list["ParticleSprite"]:
        """Every live particle of every emitter VP at ``time_ms``."""

        sprites: list[ParticleSprite] = []
        for index, instance in enumerate(self.vp_instances()):
            if instance.start > time_ms:
                continue
            for emitter_index, emitter in enumerate(emitters.get(instance.vp, ())):
                if instance.trail[2] != 1.0:
                    emitter = replace(emitter, lifetime=max(
                        1, int(emitter.lifetime * instance.trail[2])))
                _emit_particles(sprites, instance, index, emitter_index,
                                emitter, time_ms, self.seed, limit)
                if len(sprites) >= limit:
                    return sprites
        return sprites



class VpInstance:
    """Lifetime, motion and look of one rendered VP instance."""

    def __init__(self, source: int, vp: int, start: float, end: float, *,
                 position, scale, rotation: Matrix3 = IDENTITY,
                 tint=None, alpha=None, trail=(1.0, 1.0, 1.0)) -> None:
        self.source = source
        # vp_trail_scale: X/Y size of the particles, Z their lifetime.
        self.trail = trail
        self.vp = vp
        self.start = start
        self.end = end
        self._position = position
        self._scale = scale
        self.rotation = rotation
        self.tint = tint
        self._alpha = alpha

    def position(self, time_ms: float):
        return self._position(time_ms) if callable(self._position) else self._position

    def scale(self, time_ms: float):
        return self._scale(time_ms) if callable(self._scale) else self._scale

    def alpha(self, time_ms: float) -> float:
        return self._alpha(time_ms) if callable(self._alpha) else 1.0


def _plasma_scale(total_ms: float, age_ms: float) -> float:
    """sb_0x4874c4: scale follows the square root of the remaining plasma time."""

    return math.sqrt(max(0.0, total_ms - age_ms) / max(total_ms, 1.0)) * PLASMA_SCALE


def _global_fade_factor(sound: SoundEvent, duration: float, time_ms: float,
                        fade: tuple[int, int]) -> float:
    """game.global_*_fade_inout: SFX_EffectTemporalFactor of the engine.

    A one-shot fades over the length of its package; a loop has no end, so only
    its fade-in applies.
    """

    if fade[0] <= 0 and fade[1] <= 0:
        return 1.0
    return temporal_envelope(
        time_ms - sound.start, 0.0 if sound.loops else float(duration),
        float(fade[0]), float(fade[1]))


def _screen_fx_strength(sound: SoundEvent, fx: ScreenFx, time_ms: float,
                        fade: tuple[int, int] = (0, 0)):
    """Strength of a palette package at ``time_ms``, None when over."""

    age = time_ms - sound.start
    if age < 0:
        return None
    if sound.loops:
        strength = fx.mag0 if time_ms < sound.end else None
    elif fx.time <= 0 or age > fx.time:
        return None
    else:
        strength = fx.mag0 + (fx.mag1 - fx.mag0) * age / fx.time
    if strength is None:
        return None
    return strength * _global_fade_factor(sound, fx.time, time_ms, fade)


def _shake_strength(sound: SoundEvent, fx: ShakeFx, time_ms: float,
                    fade: tuple[int, int] = (0, 0)):
    """Strength of a shake package at ``time_ms``, None when over."""

    age = time_ms - sound.start
    if age < 0:
        return None
    if sound.loops:
        strength = fx.mag0 if time_ms < sound.end else None
    elif fx.time <= 0 or age > fx.time:
        return None
    else:
        strength = fx.mag0 + (fx.mag1 - fx.mag0) * age / fx.time
    if strength is None:
        return None
    return strength * _global_fade_factor(sound, fx.time, time_ms, fade)


def _decoration_pose(payload, time_ms: float):
    """(position, rotation): the parent pose plus the rotated local offset.

    Once the parent stopped the decoration stays where it was left.
    """

    _deco, local, flight, window_end, _persistent = payload
    position, rotation = flight.pose(min(time_ms, window_end))
    offset = _rotate_vector(rotation, local)
    return tuple(p + o for p, o in zip(position, offset)), rotation


def _decoration_alpha(payload, start: float, time_ms: float, *, trail: bool) -> float:
    """Fade of the model (or of its VP particles when ``trail``)."""

    deco, _local, _flight, window_end, persistent = payload
    fade_in = deco.trail_fade_in if trail else deco.fade_in
    fade_out = deco.trail_fade_out if trail else deco.fade_out
    if not persistent:
        return temporal_envelope(time_ms - start, deco.duration, fade_in, fade_out)
    # Persistent: fades in once, then out when the parent stops.
    alpha = temporal_envelope(time_ms - start, 0.0, fade_in, 0.0)
    if time_ms > window_end and fade_out > 0:
        alpha *= max(0.0, 1.0 - (time_ms - window_end) / fade_out)
    return alpha


def _instances_for_event(source, kind, start, end, payload, unit=None):
    if kind == "decoration":
        deco = payload[0]
        if deco.visual.vp <= 0:
            return []
        return [VpInstance(
            source, deco.visual.vp, start, end,
            position=lambda time_ms, payload=payload:
                _decoration_pose(payload, time_ms)[0],
            scale=deco.scale, rotation=payload[2].heading,
            tint=None if deco.trail_tint == WHITE else deco.trail_tint,
            alpha=lambda time_ms, payload=payload, start=start:
                _decoration_alpha(payload, start, time_ms, trail=True),
            trail=deco.trail_scale)]
    if kind == "cluster":
        ref, position, heading = payload
        return [VpInstance(source, ref.vp, start, end, position=position,
                           scale=(1.0, 1.0, 1.0), rotation=heading)] if ref.vp > 0 else []
    if kind == "plasma":
        ref, position, total, _maxrot = payload
        if ref.vp <= 0:
            return []
        return [VpInstance(
            source, ref.vp, start, end, position=position,
            scale=lambda time_ms, total=total, start=start:
                (_plasma_scale(total, time_ms - start),) * 3,
            tint=None if unit is None or unit.tint in (None, WHITE) else unit.tint)]
    if kind == MODE_VISUAL:
        spec, position, rotation = payload
        count = len(spec.visuals)
        slice_ms = spec.duration / count

        def scale_at(time_ms, spec=spec, start=start):
            size = visual_chain_state(spec, time_ms - start)[1]
            return (size, size, size)

        def alpha_at(time_ms, spec=spec, start=start):
            return visual_chain_state(spec, time_ms - start)[2]

        return [VpInstance(source, visual.vp, start + index * slice_ms,
                           start + (index + 1) * slice_ms, position=position,
                           scale=scale_at, rotation=rotation, tint=visual.tint,
                           alpha=alpha_at)
                for index, visual in enumerate(spec.visuals) if visual.vp > 0]
    if kind == MODE_PHYSICAL:
        spec, vp, path, rotation = payload
        scale = (spec.visual_scale,) * 3
        return [VpInstance(
            source, vp, start, end,
            position=lambda time_ms, path=path, start=start:
                _path_position(path, time_ms - start),
            scale=scale, rotation=rotation, tint=spec.physical_tint)] if vp > 0 else []
    if kind == "impact":
        vp, position, scale, tint = payload
        return [VpInstance(source, vp, start, end, position=position,
                           scale=(scale,) * 3, tint=tint)]
    if kind == "scale_fx":
        scale_fx, position, rotation, _maxrot = payload
        count = len(scale_fx.vps)
        slice_ms = scale_fx.duration / count

        def scale_at(time_ms, scale_fx=scale_fx, start=start):
            size = _scale_fx_size(scale_fx, time_ms - start)
            return (size, size, size)

        return [VpInstance(source, vp, start + index * slice_ms,
                           start + (index + 1) * slice_ms, position=position,
                           scale=scale_at, rotation=rotation)
                for index, vp in enumerate(scale_fx.vps)]
    if kind in ("legacy", "unit"):
        tint, trail = None, (1.0, 1.0, 1.0)
        if kind == "unit":
            # The main model's matrix already carries its preview scale.
            ref, flight, _label, main, owner = payload
            unit = owner
            vp = ref.vp
            path, heading = flight.path, flight.heading
            rotation = (_mat_mul(heading, unit.transform)
                        if main and unit is not None else heading)
            if unit is not None:
                tint = unit.particle_tint(main)
                tint = None if tint in (None, WHITE) else tint
                if unit.weapon and main:
                    trail = unit.trail_scale
        else:
            vp, path, rotation = payload
        position = ((lambda time_ms, flight=flight: flight.pose(time_ms)[0])
                    if kind == "unit" else
                    (lambda time_ms, path=path: _path_position(path, time_ms)))
        return [VpInstance(
            source, vp, start, end, position=position,
            scale=(1.0, 1.0, 1.0), rotation=rotation, tint=tint,
            trail=trail)] if vp > 0 else []
    return []


def _path_position(path, time_ms: float) -> tuple[float, float, float]:
    """Interpolate a [(ms, position, landed)] path at ``time_ms``."""

    previous = path[0]
    for sample in path:
        if sample[0] >= time_ms:
            span = sample[0] - previous[0]
            if span <= 0:
                return sample[1]
            ratio = (time_ms - previous[0]) / span
            return tuple(a + (b - a) * ratio
                         for a, b in zip(previous[1], sample[1]))
        previous = sample
    return path[-1][1]


def _scale_fx_size(scale_fx: ScaleFx, age_ms: float) -> float:
    """Integrate CrashOrLand__sub0: speed += accel*dt; scale += speed*dt."""

    size = scale_fx.start
    speed = scale_fx.speed
    elapsed = 0.0
    while elapsed < age_ms:
        step = min(SIMULATION_STEP_MS, age_ms - elapsed)
        dt = step / 1000.0
        speed += scale_fx.accel * dt
        size += speed * dt
        elapsed += step
    return size


# -- summaries ---------------------------------------------------------------

def block_summary(block: FxBlock, context: str) -> str:
    """One readable line, e.g. 'Visual · 3 VP · 0→2.5 · 800 ms'."""

    spec = interpret_block(block, context)
    mode = (block.value("mode") or MODE_VISUAL).casefold()
    parts = [mode.replace("_", " ").title()]
    if spec.mode == MODE_VISUAL and spec.visuals:
        parts.append(f"{len(spec.visuals)} VP")
        sizes = [format_number(spec.start_size)]
        if spec.mid_size is not None:
            sizes.append(format_number(spec.mid_size))
        sizes.append(format_number(spec.end_size))
        parts.append("→".join(sizes))
        parts.append(f"{spec.duration} ms")
    elif spec.mode == MODE_PHYSICAL and spec.vp_models:
        parts.append(f"VP {'/'.join(str(vp) for vp in spec.vp_models)}")
    if spec.count != (1, 1):
        parts.append(f"×{format_range(*spec.count)}")
    return " · ".join(parts)


def dest_summary(line: DestFxLine,
                 catalog: dict[int, LegacyFxVehicle] | None = None) -> str:
    dest = line.parsed
    if dest is None:
        return "dest_fx (invalid)"
    vehicle = (catalog or {}).get(dest.vehicle_id)
    name = f" {vehicle.name}" if vehicle and vehicle.name else ""
    kind = "scale_fx" if vehicle and vehicle.scale_fx else "falling"
    detail = f" · {kind}" if vehicle else " · not in catalog"
    return (f"Vanilla {dest.kind} · vehicle {dest.vehicle_id}{name}{detail}")
