"""Destruction FX preview: script reading, engine rules and simulation.

The Destruction FX tab only previews effects; scripts are edited as text
outside Studio and reloaded. Two effect systems are read:

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

import math
import random
import re
from dataclasses import dataclass, field

CONTEXT_VEHICLE = "vehicle"
CONTEXT_WEAPON = "weapon"

MODE_VISUAL = "visual"
MODE_PHYSICAL = "physical"
MODE_DECAL = "ground_decal"
MODES = (MODE_VISUAL, MODE_PHYSICAL, MODE_DECAL)

TRIGGERS = {
    CONTEXT_VEHICLE: ("destroyed", "air_destroyed"),
    CONTEXT_WEAPON: ("detonate", "impact_world"),
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
SIMULATION_STEP_MS = 10.0
LEGACY_NESTING_LIMIT = 3

_SOUND_PREFIXES = ("snd_", "pal_", "shk_")
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


def parse_float_range(text: str) -> tuple[float, float] | None:
    """``ParseFloatRangeValue``: fixed ``v`` or ``min_max`` (order normalized)."""

    parts = text.split("_")
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


FxItem = FxBlock | DestFxLine


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
    """

    depth = 1
    index = start_line + 1
    while index < end_line:
        token = _scope_token(lines[index])
        if depth == 1 and token == "begin_fx":
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


def extract_fx_document(text: str, start_line: int, end_line: int) -> FxDocument:
    """Read the Destruction FX items of one definition."""

    lines = text.splitlines()
    items: list[FxItem] = []
    for first, last, kind in _fx_item_ranges(lines, start_line, end_line):
        if kind == "block":
            items.append(FxBlock(lines[first:last + 1]))
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
    # Ground decal
    texture: str = ""
    size: tuple[float, float] = (0.0, 0.0)
    stretch_x: tuple[float, float] = (1.0, 1.0)
    stretch_z: tuple[float, float] = (1.0, 1.0)
    decal_tint: tuple[int, int, int, int] = (255, 255, 255, 255)
    edge_fade: tuple[float, float] = (0.0, 0.0)
    permanent: bool = False

    @property
    def fatal(self) -> bool:
        return any(issue.severity == SEVERITY_FATAL for issue in self.issues)


def _nonnegative_ms(text: str) -> int:
    value = _stof(text)
    if not math.isfinite(value) or value <= 0:
        return 0
    return int(value)


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
    decal_duration_valid = permanent = False
    fade_in = fade_out = 0
    visuals: list[VisualRef] = []
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
    texture = ""
    size = (0.0, 0.0)
    stretch_x = (1.0, 1.0)
    stretch_z = (1.0, 1.0)
    decal_tint = (255, 255, 255, 255)
    random_rotation = False
    edge_fade = (0.0, 0.0)

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
            if value.casefold() == "permanent":
                duration, permanent, decal_duration_valid = 0, True, True
            else:
                parsed, used = _stol(value)
                duration = parsed
                permanent = False
                decal_duration_valid = used == len(value) and parsed > 0
        elif key == "fade_in":
            fade_in = _nonnegative_ms(value)
        elif key == "fade_out":
            fade_out = _nonnegative_ms(value)
        elif key == "count":
            parsed = parse_int_range(value)
            if parsed is None or parsed[0] <= 0 or parsed[1] <= 0:
                issue(SEVERITY_WARNING, f"Invalid count '{value}': using 1.")
                count = (1, 1)
            else:
                count = (min(parsed[0], MAX_COUNT), min(parsed[1], MAX_COUNT))
        elif key in ("offset_x", "offset_y", "offset_z",
                     "launch_x", "launch_y", "launch_z"):
            axis = "xyz".index(key[-1])
            if key.startswith("offset"):
                has_offset = True
                target_min, target_max = offset_min, offset_max
            else:
                has_inline = True
                target_min, target_max = launch_min, launch_max
            parsed = parse_float_range(value)
            if parsed is None:
                issue(SEVERITY_WARNING, f"Invalid {key} '{value}': using 0.")
                parsed = (0.0, 0.0)
            target_min[axis], target_max[axis] = parsed
        elif key in ("spin_x", "spin_y", "spin_z"):
            raw = _stof(value)
            spin["xyz".index(key[-1])] = (
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
        elif key == "tint" and mode != MODE_DECAL:
            parsed = parse_tint(value)
            if mode == MODE_PHYSICAL:
                has_inline = True
                physical_tint = parsed or (255, 255, 255, 255)
                continue
            if not visuals:
                issue(SEVERITY_WARNING,
                      "tint without a preceding vp_model/base_model/3ds_model "
                      "is ignored.")
                continue
            last = visuals[-1]
            visuals[-1] = VisualRef(last.vp, last.mesh3ds, last.base_path,
                                    parsed or (255, 255, 255, 255))
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
        elif key in ("impact_energy", "impact_energy_radius",
                     "follow_vehicle"):
            has_inline = True
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
        elif key == "physical_vehicle":
            has_physical_vehicle_key = True
            physical_vehicle = _stol(value)[0]
        elif key == "texture":
            texture = value
        elif key == "size":
            parsed = parse_float_range(value)
            size = parsed if parsed and parsed[0] > 0 and parsed[1] > 0 else (0.0, 0.0)
        elif key in ("stretch_x", "stretch_z"):
            parsed = parse_float_range(value)
            if not parsed or parsed[0] <= 0 or parsed[1] <= 0:
                issue(SEVERITY_WARNING, f"Invalid {key} '{value}': using 1.0.")
                parsed = (1.0, 1.0)
            if key == "stretch_x":
                stretch_x = parsed
            else:
                stretch_z = parsed
        elif key == "tint":
            decal_tint = parse_tint(value) or (255, 255, 255, 255)
        elif key == "random_rotation":
            random_rotation = value == "1"
        elif key == "edge_fade":
            if "_" in value:
                parsed = parse_int_range(value)
                if parsed is None:
                    issue(SEVERITY_WARNING,
                          f"Invalid edge_fade range '{value}': using 0.")
                    edge_fade = (0.0, 0.0)
                else:
                    edge_fade = (max(0, min(parsed[0], 10)),
                                 max(0, min(parsed[1], 10)))
            else:
                parsed = _full_float(value)
                fade = max(0.0, min(parsed, 10.0)) if parsed is not None else 0.0
                edge_fade = (fade, fade)
        else:
            # Newer engines may add keys Studio does not know yet. The line
            # is kept verbatim and the preview continues without it.
            issue(SEVERITY_UNKNOWN,
                  f"Unknown key '{key}': Studio does not know it. If your "
                  "OpenNeoUA version does not support it either, the engine "
                  "stops loading the whole script file (PARSE ERROR unknown "
                  "keyword).")

    def build(active: bool, **values) -> FxSpec:
        return FxSpec(mode=mode, trigger=trigger, active=active,
                      issues=tuple(issues), **values)

    common = dict(
        count=count, offset_min=tuple(offset_min),
        offset_max=tuple(offset_max), random_rotation=random_rotation)
    if any(item.severity == SEVERITY_FATAL for item in issues) \
            or bad_trigger or bad_mode:
        return build(False, **common)
    if mode == MODE_DECAL and not has_trigger:
        trigger = "destroyed" if context == CONTEXT_VEHICLE else "impact_world"
        has_trigger = True
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
            fade_in=fade_in, fade_out=fade_out, visuals=tuple(visuals),
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
            physical_tint=physical_tint, physical_model=model, **common)

    valid_trigger = (
        (context == CONTEXT_WEAPON and trigger == "impact_world")
        or (context == CONTEXT_VEHICLE and trigger == "destroyed"))
    if not valid_trigger:
        needed = "impact_world" if context == CONTEXT_WEAPON else "destroyed"
        issue(SEVERITY_IGNORED,
              f"A {context} ground_decal supports only trigger = {needed}: "
              "the block is ignored.")
        return build(False, **common)
    if (size[0] <= 0 or not decal_duration_valid
            or (not permanent and duration <= 0) or decal_tint[3] <= 0):
        issue(SEVERITY_IGNORED,
              "Incomplete or disabled ground_decal (needs size > 0, a valid "
              "duration and tint alpha > 0): it is ignored.")
        return build(False, **common)
    if permanent:
        decal_fade_out, decal_fade_in = 0, fade_in
    else:
        decal_fade_out = min(fade_out, duration)
        decal_fade_in = min(fade_in, duration - decal_fade_out)
    return build(
        True, duration=duration, fade_in=decal_fade_in,
        fade_out=decal_fade_out, texture=texture, size=size,
        stretch_x=stretch_x, stretch_z=stretch_z, decal_tint=decal_tint,
        edge_fade=edge_fade, permanent=permanent, **common)


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
    mass: float = 1.0
    airconst: float = 0.0
    maxrot: float = 0.0
    radius: float = 0.0
    scale_fx: ScaleFx | None = None
    scale_fx_text: str = ""
    dest_fx: tuple[DestFx, ...] = ()
    source: str = ""


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
    return LegacyFxVehicle(
        vehicle_id=vehicle_id,
        name=values.get("name", ""),
        vp_dead=int(number("vp_dead", 0)),
        vp_megadeth=int(number("vp_megadeth", 0)),
        mass=number("mass", 1.0),
        airconst=number("airconst", 0.0),
        maxrot=number("maxrot", 0.0),
        radius=number("radius", 0.0),
        scale_fx=parse_scale_fx(scale_text) if scale_text else None,
        scale_fx_text=scale_text,
        dest_fx=dests,
        source=source,
    )


def legacy_vehicle_from_text(vehicle_id: int, text: str,
                             source: str = "") -> LegacyFxVehicle:
    """Read the top-level fields of one ``new_vehicle`` body text."""

    values: dict[str, str] = {}
    dests: list[str] = []
    depth = 0
    for line in text.splitlines():
        token = _scope_token(line)
        if token == "begin" or token.startswith("begin_"):
            depth += 1
            continue
        if token == "end":
            depth = max(0, depth - 1)
            continue
        if depth or not token:
            continue
        code = re.split(r";", line, maxsplit=1)[0]
        key, _sep, value = code.partition("=")
        key = key.strip().casefold()
        value = value.strip()
        if key in DEST_FX_KEYS:
            dests.append(value)
        elif key:
            values[key] = value
    return legacy_vehicle_from_values(vehicle_id, values, dests, source)


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
    kind: str                   # visual | physical | impact | legacy | ground_decal | unit
    vp: int                     # VP id to draw (0 = marker only)
    position: tuple[float, float, float]
    rotation: Matrix3
    scale: tuple[float, float, float]
    alpha: float = 1.0
    tint: tuple[int, int, int, int] | None = None
    label: str = ""
    # External 3DS/BASE models, in engine priority; the VP is the fallback.
    external: tuple[str, ...] = ()
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

    @property
    def skips_dead_phase(self) -> bool:
        return not self.weapon and self.model.casefold() in ("tank", "car")

    @property
    def flies(self) -> bool:
        return not self.weapon and self.model.casefold() in FLYING_MODELS


# Preview events of the Destruction FX tab.
SEQUENCE_TRIGGER = "sequence"          # vehicle: full death
WEAPON_HIT_UNIT = "hit_unit"           # weapon: detonate on a unit
WEAPON_HIT_GROUND = "hit_ground"       # weapon: impact_world on terrain
SEQUENCE_PREROLL_MS = 600.0
# How long the preview keeps the final model after the last effect ends.
MEGADETH_HOLD_MS = 1200.0
# Weapon flight shown before the impact: from the upper left to the target.
WEAPON_FLIGHT_MS = 900.0
WEAPON_LAUNCH_OFFSET = (-1100.0, -550.0, -700.0)


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
        return IDENTITY
    right = tuple(value / norm for value in right)
    down = (forward[1] * right[2] - forward[2] * right[1],
            forward[2] * right[0] - forward[0] * right[2],
            forward[0] * right[1] - forward[1] * right[0])
    return tuple((right[row], down[row], forward[row]) for row in range(3))


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


class PreviewSimulation:
    """Deterministic preview of one destruction, as the engine plays it.

    ``trigger`` is ``"sequence"`` (vehicle death: normal -> dead +
    air_destroyed + dest_fx death -> wreck fall -> megadeth + destroyed +
    dest_fx megadeth), ``"hit_unit"``/``"hit_ground"`` (weapon flight and
    detonate / impact_world), or one single begin_fx/dest_fx trigger.
    Random choices are rolled once per seed so scrubbing always shows the
    same activation. All times are absolute ms.
    """

    def __init__(self, document: FxDocument, context: str, trigger: str, *,
                 seed: int = 1, origin=(0.0, 0.0, 0.0), ground_y: float = 0.0,
                 unit_radius: float = 0.0,
                 legacy_catalog: dict[int, LegacyFxVehicle] | None = None,
                 enabled: set[int] | None = None,
                 unit: UnitPhases | None = None) -> None:
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
        self.notes: list[str] = []
        self._events: list[tuple] = []
        self._tracks: list[PreviewTrack] = []
        self._instances: list[VpInstance] | None = None
        self._specs = {
            index: interpret_block(item, context)
            for index, item in enumerate(document.items)
            if isinstance(item, FxBlock)}
        if trigger == SEQUENCE_TRIGGER and context == CONTEXT_VEHICLE:
            self._build_vehicle_sequence()
        elif trigger in (WEAPON_HIT_UNIT, WEAPON_HIT_GROUND) \
                and context == CONTEXT_WEAPON:
            self._build_weapon_sequence(trigger == WEAPON_HIT_GROUND)
        else:
            self._build_event(trigger, 0.0, self.origin)

    # -- event construction ---------------------------------------------------------

    def _rng(self, source: int, salt: str) -> random.Random:
        return random.Random(hash((self.seed, source, salt)) & 0xFFFFFFFF)

    def _build_event(self, trigger: str, start: float, origin,
                     prefix: str = "") -> None:
        """Spawn every enabled item listening to ``trigger`` at ``start``."""

        for source, item in enumerate(self.document.items):
            if self.enabled is not None and source not in self.enabled:
                continue
            rng = self._rng(source, trigger)
            if isinstance(item, FxBlock):
                spec = self._specs[source]
                if not spec.active or spec.trigger != trigger:
                    continue
                if spec.mode == MODE_VISUAL:
                    self._build_visual(source, spec, rng, start, origin, prefix)
                elif spec.mode == MODE_PHYSICAL:
                    self._build_physical(source, spec, rng, start, origin, prefix)
                else:
                    self._build_decal(source, spec, rng, start, origin, prefix)
            else:
                dest = item.parsed
                if dest is None or dest.kind != trigger:
                    continue
                self._build_legacy(source, dest, origin, rng, start, 0,
                                   prefix=prefix)

    def _unit_event(self, ref: VisualRef, start: float, end: float, path,
                    label: str, rotation: Matrix3 | None = None) -> None:
        if ref.vp <= 0 and not _external_paths(ref):
            return
        rotation = self.unit.transform if rotation is None else rotation
        self._events.append((-1, "unit", start, end, (ref, path, label, rotation)))

    def _build_vehicle_sequence(self) -> None:
        unit = self.unit
        death = SEQUENCE_PREROLL_MS
        altitude = max(0.0, unit.altitude) if not unit.skips_dead_phase else 0.0
        kill = (self.origin[0], self.origin[1] - altitude, self.origin[2])
        self._unit_event(unit.normal, 0.0, death, [(0.0, kill, False)],
                         "normal")
        if unit.skips_dead_phase:
            # Tank/car: SetState(DEAD) becomes DEATH2 immediately.
            land_ms, landed = death, kill
            self.notes.append(
                f"model = {unit.model}: straight to megadeth, no dead model, "
                "no air_destroyed and no dest_fx death (engine rule).")
        else:
            self._build_event("air_destroyed", death, kill, "Air · ")
            self._build_event("death", death, kill, "Death · ")
            # Dead wrecks fall without air resistance: CrashOrLand sets
            # _airconst = 0 for non-inline dead vehicles.
            fall = _Fall(list(kill), [0.0, 0.0, 0.0], 1.0, 0.0)
            path = self._record_path(fall, LEGACY_DEAD_LIFETIME_MS,
                                     max(unit.overeof, 0.0))
            shifted = [(death + ms, position, flag)
                       for ms, position, flag in path]
            land_ms, landed = shifted[-1][0], shifted[-1][1]
            self._unit_event(unit.dead, death, land_ms, shifted, "dead")
            if land_ms > death + 1.0:
                self._tracks.append(PreviewTrack(
                    -1, "Unit · wreck falls", death, land_ms, approximate=True))
        ground = (landed[0], self.origin[1], landed[2])
        self._build_event("destroyed", land_ms, ground, "Ground · ")
        self._build_event("megadeth", land_ms, ground, "Mega · ")
        hold_end = max(self.duration, land_ms) + MEGADETH_HOLD_MS
        self._unit_event(unit.megadeth, land_ms, hold_end,
                         [(land_ms, landed, True)], "megadeth")
        self._tracks.insert(0, PreviewTrack(
            -1, "Unit · killed", death, land_ms,
            marks=(land_ms,) if land_ms > death else ()))

    def _build_weapon_sequence(self, hit_ground: bool) -> None:
        unit = self.unit
        target = (self.origin[0],
                  self.ground_y if hit_ground else self.origin[1],
                  self.origin[2])
        launch = tuple(t + o for t, o in zip(target, WEAPON_LAUNCH_OFFSET))
        heading = tuple(t - l for t, l in zip(target, launch))
        rotation = _mat_mul(_look_rotation(heading), unit.transform)
        impact = WEAPON_FLIGHT_MS
        self._unit_event(unit.normal, 0.0, impact,
                         [(0.0, launch, False), (impact, target, True)],
                         "normal", rotation)
        if hit_ground:
            # DEATH2: vp_megadeth, impact_world chain, dest_fx megadeth.
            self._build_event("impact_world", impact, target, "Ground hit · ")
            self._build_event("megadeth", impact, target, "Mega · ")
            final, label = unit.megadeth, "megadeth"
        else:
            # DEAD: vp_dead, detonate chain, dest_fx death.
            self._build_event("detonate", impact, target, "Unit hit · ")
            self._build_event("death", impact, target, "Death · ")
            final, label = unit.dead, "dead"
        self._unit_event(final, impact, impact + IMPACT_PREVIEW_MS,
                         [(impact, target, True)], label, rotation)
        self._tracks.insert(0, PreviewTrack(
            -1, "Projectile flight", 0.0, impact, marks=(impact,)))

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
            # Inline fragments keep their authored air resistance.
            fall = _Fall(list(origin), launch, spec.mass, spec.airconst)
            path = self._record_path(fall, lifetime, max(spec.radius, 0.0))
            land_ms, final = start + path[-1][0], path[-1][1]
            self._events.append((source, MODE_PHYSICAL, start, land_ms,
                                 (spec, vp, path, rotation)))
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
                ref, path, label, rotation = payload
                items.append(PreviewItem(
                    -1, "unit", ref.vp, _path_position(path, time_ms),
                    rotation, (1.0, 1.0, 1.0), 1.0, None,
                    f"{label} · {ref.label}", _external_paths(ref)))
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
                reach = 60.0 * max(item.scale) if item.kind != MODE_DECAL \
                    else 0.5 * max(item.decal_size)
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
                self._instances.extend(
                    _instances_for_event(source, kind, start, end, payload))
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
                _emit_particles(sprites, instance, index, emitter_index,
                                emitter, time_ms, self.seed, limit)
                if len(sprites) >= limit:
                    return sprites
        return sprites



# Vanilla particle steps were tuned for ~60 fps frames of the 1024 Hz clock.
PARTICLE_FRAME_MS = 1000.0 / 60.0


@dataclass(frozen=True)
class EmitterSpec:
    """One ``particle.class`` emitter of a VP (particle.cpp attributes)."""

    point: tuple[float, float, float]
    accel_start: tuple[float, float, float]
    accel_end: tuple[float, float, float]
    magnify_start: tuple[float, float, float]
    magnify_end: tuple[float, float, float]
    start_speed: float
    gen_lifetime: int
    gen_start: int
    gen_end: int
    birth_rate: int
    lifetime: int
    start_size: float
    end_size: float
    noise: float
    stages: int

    @classmethod
    def from_attributes(cls, point, attributes, stages: int) -> "EmitterSpec":
        return cls(
            point=tuple(point),
            accel_start=tuple(attributes.accel_start),
            accel_end=tuple(attributes.accel_end),
            magnify_start=tuple(attributes.magnify_start),
            magnify_end=tuple(attributes.magnify_end),
            start_speed=float(attributes.start_speed),
            gen_lifetime=max(1, int(attributes.context_lifetime)),
            gen_start=int(attributes.context_start_generation),
            gen_end=int(attributes.context_stop_generation),
            birth_rate=int(attributes.birth_rate),
            lifetime=max(1, int(attributes.lifetime)),
            start_size=float(attributes.start_size),
            end_size=float(attributes.end_size),
            # SetNoisePower stores a tenth of the authored value.
            noise=float(attributes.noise) * 0.1,
            stages=max(1, int(stages)))


@dataclass(frozen=True)
class ParticleSprite:
    """One camera-facing particle quad to draw."""

    source: int
    vp: int
    emitter: int
    position: tuple[float, float, float]
    size: float
    stage: int
    tint: tuple[int, int, int, int] | None
    alpha: float


class VpInstance:
    """Lifetime, motion and look of one rendered VP instance."""

    def __init__(self, source: int, vp: int, start: float, end: float, *,
                 position, scale, rotation: Matrix3 = IDENTITY,
                 tint=None, alpha=None) -> None:
        self.source = source
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


def _instances_for_event(source, kind, start, end, payload):
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
        if kind == "unit":
            # The unit transform already carries its preview scale.
            ref, path, _label, rotation = payload
            vp = ref.vp
        else:
            vp, path, rotation = payload
        return [VpInstance(
            source, vp, start, end,
            position=lambda time_ms, path=path: _path_position(path, time_ms),
            scale=(1.0, 1.0, 1.0), rotation=rotation)] if vp > 0 else []
    return []


def _particle_rng(*key) -> random.Random:
    return random.Random(hash(key) & 0xFFFFFFFF)


def _emit_particles(sprites, instance, index, emitter_index, emitter,
                    time_ms, seed, limit) -> None:
    """Replay NC_STACK_particle::Emit and ParticleSystem::UpdateRender.

    Births follow the generator window [gen_start, gen_end) of the instance
    age, repeated every max(context lifetime, gen_end), one particle every
    1024 / birth_rate ms. Particles outlive their emitter instance. Motion
    uses the closed form of the per-frame steps: the velocity gains
    accel * frameScale each frame and the noise adds a random walk.
    """

    if emitter.birth_rate <= 0 or emitter.gen_end <= emitter.gen_start:
        return
    pause = max(1, 1024 // emitter.birth_rate)
    cycle = max(emitter.gen_lifetime, emitter.gen_end)
    alive_until = min(instance.end, time_ms)
    first_birth = max(instance.start, time_ms - emitter.lifetime)
    if alive_until < instance.start:
        return
    frame_scale = PARTICLE_FRAME_MS * 60.0 / 1024.0
    frame_seconds = PARTICLE_FRAME_MS / 1000.0
    accel_delta = tuple((end - start) / emitter.gen_lifetime for start, end
                        in zip(emitter.accel_start, emitter.accel_end))
    magnify_delta = tuple((end - start) / emitter.gen_lifetime for start, end
                          in zip(emitter.magnify_start, emitter.magnify_end))
    cycle_index = int(max(0.0, first_birth - instance.start) // cycle)
    while True:
        cycle_start = instance.start + cycle_index * cycle
        if cycle_start > alive_until:
            return
        birth_index = 0
        birth = cycle_start + emitter.gen_start
        if birth < first_birth:
            birth_index = int((first_birth - birth) // pause)
            birth += birth_index * pause
        while birth < cycle_start + emitter.gen_end and birth <= alive_until:
            if birth >= first_birth and birth < instance.end:
                _add_particle(sprites, instance, index, emitter_index, emitter,
                              time_ms, seed, birth, cycle_index, birth_index,
                              frame_scale, frame_seconds, accel_delta,
                              magnify_delta)
                if len(sprites) >= limit:
                    return
            birth += pause
            birth_index += 1
        cycle_index += 1


def _add_particle(sprites, instance, index, emitter_index, emitter, time_ms,
                  seed, birth, cycle_index, birth_index, frame_scale,
                  frame_seconds, accel_delta, magnify_delta) -> None:
    age = time_ms - birth
    if age < 0 or age >= emitter.lifetime:
        return
    rng = _particle_rng(seed, index, emitter_index, cycle_index, birth_index)
    emitter_age = (birth - instance.start) % max(emitter.gen_lifetime, emitter.gen_end)
    scale = instance.scale(birth)
    magnify = tuple(start + delta * emitter_age
                    for start, delta in zip(emitter.magnify_start, magnify_delta))
    magnify = _rotate_vector(instance.rotation,
                             tuple(m * s for m, s in zip(magnify, scale)))
    direction = tuple(m + rng.uniform(-1.0, 1.0) for m in magnify)
    length = math.sqrt(sum(value * value for value in direction))
    if length <= 1e-9:
        return
    velocity = tuple(value * emitter.start_speed / length for value in direction)
    origin = instance.position(birth)
    point = _rotate_vector(instance.rotation,
                           tuple(p * s for p, s in zip(emitter.point, scale)))
    frames = age / PARTICLE_FRAME_MS
    # Mean acceleration over the particle life so far (linear in age).
    accel = tuple(start + delta * age * 0.5
                  for start, delta in zip(emitter.accel_start, accel_delta))
    noise = emitter.noise * frame_scale * math.sqrt(frames)
    position = tuple(
        o + p + frame_seconds * (frames * v + a * frame_scale
                                 * frames * (frames + 1) * 0.5)
        + noise * rng.uniform(-1.0, 1.0)
        for o, p, v, a in zip(origin, point, velocity, accel))
    size = emitter.start_size + (emitter.end_size - emitter.start_size) \
        * age / emitter.lifetime
    size *= max(scale) if max(scale) > 0 else 1.0
    stage = min(int(age / (emitter.lifetime / emitter.stages)), emitter.stages - 1)
    sprites.append(ParticleSprite(
        instance.source, instance.vp, emitter_index, position, size, stage,
        instance.tint, instance.alpha(min(time_ms, instance.end - 1e-6))))


def _rotate_vector(matrix: Matrix3, vector) -> tuple[float, float, float]:
    return tuple(sum(matrix[row][k] * vector[k] for k in range(3))
                 for row in range(3))


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
    elif spec.mode == MODE_DECAL and spec.texture:
        parts.append(spec.texture.replace("\\", "/").rsplit("/", 1)[-1])
        parts.append("permanent" if spec.permanent else f"{spec.duration} ms")
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
