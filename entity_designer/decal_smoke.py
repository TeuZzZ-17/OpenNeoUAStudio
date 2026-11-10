"""Ground decal smoke: settings of a ``new_decal`` profile and the puff math.

``evaluate`` is a port of ``World::GroundDecalSmoke::Evaluate`` (engine file
``world/grounddecal_smoke.h``). The engine keeps no particles: every puff is
recomputed from the age of the smoking decal, so the preview can do the same
for any timeline position. The integer hash is bit-exact; the float math runs in
double precision here, so positions can differ from the engine by rounding.
Pure Python, no Qt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_PUFFS = 50
GROUND_LIFT = 2.0            # keeps puffs clear of the decal surface bias
MIN_PARTICLE_SIZE = 1.0

_MASK = 0xFFFFFFFF

# effect_mode is mandatory: without one of these values nothing is emitted.
MODE_GROUND_PULSE = "ground_pulse"   # short intermittent flashes around the decal
MODE_RISE = "rise"                   # camera-facing 3DS puffs that climb away
MODES = (MODE_GROUND_PULSE, MODE_RISE)

# Keys that only one mode reads; the engine warns when a profile uses the other mode.
GROUND_PULSE_KEYS = ("effect_base", "effect_vp", "effect_pulse_interval", "effect_pulse_time",
                     "effect_scale")
RISE_KEYS = ("effect_fade_inout", "effect_count", "effect_size", "effect_growth",
             "effect_rise_speed", "effect_puff_time", "effect_drift", "effect_intensity")

# Every effect key of a profile; any of them marks the profile as having an effect.
SMOKE_KEYS = (
    "effect_mode", "effect_3ds", "effect_active_time", "effect_retrigger_interval",
    "effect_chance", "effect_spread", "effect_tint",
) + GROUND_PULSE_KEYS + RISE_KEYS


@dataclass(frozen=True)
class SmokeSpec:
    """Smoke settings of one ground decal block (defaults = engine defaults).

    Sizes, speeds and spreads are percentages of the decal radius.
    """

    enabled: bool = False
    mesh3ds: str = ""
    base_model: str = ""
    mode: str = ""                        # MODE_GROUND_PULSE or MODE_RISE; "" = disabled
    pulse_vp: int = 0
    pulse_interval: tuple[int, int] = (900, 900)   # ms between pulses; min~max rolls it per pulse
    pulse_time: int = 120
    mesh_scale: float = 0.1
    chance: int = 100
    active_time: tuple[int, int] = (20000, 25000)
    retrigger: tuple[int, int] = (0, 0)   # ms between activations; (0, 0) = one activation
    fade_in: int = 3000
    fade_out: int = 20000
    count: tuple[int, int] = (10, 30)     # puffs at once on a decal of radius 50, rolled per decal
    size: tuple[float, float] = (35.0, 50.0)
    growth: float = 3.3                   # final size / initial size
    rise_speed: float = 25.0              # percent of the radius per second
    puff_time: tuple[int, int] = (4000, 7500)
    drift: float = 35.0
    spread: float = 35.0
    intensity: float = 120.0
    tint: tuple[int, int, int, int] = (70, 70, 70, 150)


@dataclass(frozen=True)
class SmokePuff:
    offset: tuple[float, float, float]    # from the decal centre; UA height grows towards -Y
    size: float
    alpha: float                          # 0..1 life shape, before tint alpha and intensity
    phase: float                          # 0 = just born, 1 = about to disappear
    shade: float                          # brightness multiplier, differs per puff


def hash32(seed: int, a: int, b: int) -> int:
    h = (seed ^ 0x9E3779B9) & _MASK
    h ^= (a + 0x85EBCA6B + ((h << 6) & _MASK) + (h >> 2)) & _MASK
    h ^= (b + 0xC2B2AE35 + ((h << 6) & _MASK) + (h >> 2)) & _MASK
    h ^= h >> 16
    h = (h * 0x7FEB352D) & _MASK
    h ^= h >> 15
    h = (h * 0x846CA68B) & _MASK
    h ^= h >> 16
    return h


def hash_unit(value: int) -> float:
    return (value & 0x00FFFFFF) / 16777216.0


def _range_from(value: int, low: float, high: float) -> float:
    return low + (high - low) * hash_unit(value)


def emits(spec: SmokeSpec) -> bool:
    """True when the engine would emit: a valid mode and a model that mode can draw."""

    if spec.mode == MODE_RISE:
        return bool(spec.mesh3ds)
    if spec.mode == MODE_GROUND_PULSE:
        return bool(spec.mesh3ds or spec.base_model or spec.pulse_vp > 0)
    return False


def pulse_interval(spec: SmokeSpec, seed: int, cycle: int) -> int:
    """Length in ms of the interval that starts pulse ``cycle`` (engine ``PulseInterval``)."""

    low, high = spec.pulse_interval
    if high <= low:
        return max(200, low)
    return max(200, int(_range_from(hash32(seed, cycle, 0x494E5456), float(low), float(high))))


def pulse_cycle(spec: SmokeSpec, seed: int, age: float) -> tuple[int, int, int]:
    """Pulse number, start and interval length containing ``age`` (each pulse starts when the previous interval ends)."""

    cycle, start = 0, 0
    length = pulse_interval(spec, seed, 0)
    while age >= start + length:
        start += length
        cycle += 1
        length = pulse_interval(spec, seed, cycle)
    return cycle, start, length


def intensity(age: float, duration: float, fade_in: float, fade_out: float) -> float:
    """Linear fade-in, full strength, linear fade-out ending at ``duration``."""

    if duration <= 0 or age < 0 or age >= duration:
        return 0.0
    up = min(1.0, age / fade_in) if fade_in > 0 else 1.0
    down = min(1.0, (duration - age) / fade_out) if fade_out > 0 else 1.0
    return up * down


def scaled_count(base: int, radius: float) -> int:
    """Puffs at once for a decal of ``radius`` (``base`` is given for radius 50)."""

    if base <= 0 or not math.isfinite(radius) or radius <= 0:
        return 0
    scale = max(0.5, min(radius / 50.0, 3.0))
    return max(1, min(int(math.floor(base * scale + 0.5)), MAX_PUFFS))


def rolls_smoke(spec: SmokeSpec, seed: int) -> bool:
    """The engine's stable per-decal roll against ``effect_chance``."""

    return hash_unit(hash32(seed, 0x43484E43, 0)) * 100.0 < spec.chance


def emitter_count(spec: SmokeSpec, seed: int) -> int:
    """The puff count this decal rolled inside ``effect_count = min~max``."""

    low, high = spec.count
    rolled = low + int(hash_unit(hash32(seed, 0x434E5452, 0)) * (high - low + 1))
    return min(rolled, high)


def emitter_duration(spec: SmokeSpec, seed: int) -> int:
    return int(_range_from(hash32(seed, 0x53445552, 0),
                           float(spec.active_time[0]), float(spec.active_time[1])))


def retrigger_interval(spec: SmokeSpec, seed: int) -> int:
    """Delay between two activations of the whole effect; 0 means one activation."""

    low, high = spec.retrigger
    if high <= 0:
        return 0
    low = max(0, low)
    high = max(low, high)
    if high <= low:
        return max(200, low)
    return max(200, int(_range_from(hash32(seed, 0x52545247, 0),
                                    float(low), float(high))))


def activation(spec: SmokeSpec, seed: int, age: float) -> tuple[float, int] | None:
    """Age inside the current activation and its length, or None while waiting.

    Without a retrigger interval the effect runs once for ``effect_active_time``.
    With one, every interval starts a new activation of at most that length.
    """

    active = emitter_duration(spec, seed)
    if active <= 0 or age < 0:
        return None
    retrigger = retrigger_interval(spec, seed)
    if retrigger <= 0:
        return (age, active) if age < active else None
    cycle_age = age % retrigger
    length = min(active, retrigger)
    return (cycle_age, length) if cycle_age < length else None


def evaluate(spec: SmokeSpec, radius: float, seed: int, count: int,
             duration: float, slot: int, age: float) -> SmokePuff | None:
    """One puff slot at ``age`` ms after the decal appeared, None when it is off.

    A slot is a puff position that keeps restarting: its period is fixed and
    the place of every new cycle is re-rolled from the cycle number. Slots
    above the current intensity are off, so puffs disappear one by one.
    """

    level = intensity(age, duration, spec.fade_in, spec.fade_out)
    gate = level * count - slot
    if gate <= 0.0:
        return None

    slot_hash = hash32(seed, slot, 0x534D4F4B)
    period = max(200.0, _range_from(hash32(slot_hash, 1, 0),
                                    float(spec.puff_time[0]), float(spec.puff_time[1])))
    cycles = (age + period * hash_unit(hash32(slot_hash, 2, 0))) / period
    cycle = int(cycles)
    phase = cycles - cycle
    cycle_hash = hash32(slot_hash, cycle, 3)

    angle = 2.0 * math.pi * hash_unit(hash32(cycle_hash, 4, 0))
    distance = math.sqrt(hash_unit(hash32(cycle_hash, 5, 0))) * spec.spread * 0.01 * radius
    base_size = _range_from(hash32(cycle_hash, 6, 0), spec.size[0], spec.size[1]) * 0.01 * radius

    wind = 2.0 * math.pi * hash_unit(hash32(seed, 0x57494E44, 0))
    rise = spec.rise_speed * 0.01 * radius * (phase * period * 0.001)
    drift = rise * spec.drift * 0.01

    size = max(MIN_PARTICLE_SIZE, base_size * 0.6 * (1.0 + (spec.growth - 1.0) * phase))
    return SmokePuff(
        offset=(math.cos(angle) * distance + math.cos(wind) * drift,
                -(GROUND_LIFT + 0.05 * radius + rise),
                math.sin(angle) * distance + math.sin(wind) * drift),
        size=size,
        alpha=min(1.0, phase / 0.12) * (1.0 - phase) * min(1.0, gate),
        phase=phase,
        shade=0.8 + 0.4 * hash_unit(hash32(cycle_hash, 7, 0)))
