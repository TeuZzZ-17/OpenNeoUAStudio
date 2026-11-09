"""Terrain craters of a weapon (crater_radius / crater_depth / crater_rim), as the engine digs them.

A line by line copy of the ground part of the engine's ``world/terraincrater_core.h``
(shape, raised rim, overlapping hits capped at 1.75 times the deepest, depth
limited to half the radius, baked slope shading) and of the key rules of
``world/parsers_weapon.cpp``. The preview only digs flat ground: a preview shot
always lands on the ground plane, never on a building.

World Y points down, like the engine: a positive drop is lower ground.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

RADIUS_KEY = "crater_radius"
DEPTH_KEY = "crater_depth"
RIM_KEY = "crater_rim"
KEYS = (RADIUS_KEY, DEPTH_KEY, RIM_KEY)

# Limits of the engine parser (crater_rim is a percentage of the depth).
SIZE_MAX = 12000.0
RIM_MAX = 100.0
RIM_DEFAULT = 20.0

# terraincrater_core.h
REACH = 1.6
MAX_DEPTH_RATIO = 0.5


@dataclass(frozen=True)
class CraterConfig:
    """The three keys of a weapon as ranges (min, max)."""

    radius: tuple[float, float]
    depth: tuple[float, float]
    rim: tuple[float, float] = (RIM_DEFAULT, RIM_DEFAULT)


@dataclass(frozen=True)
class Shape:
    """One crater: impact point, radius, depth (already limited) and rim (0..1)."""

    x: float
    y: float
    z: float
    radius: float
    depth: float
    rim: float


def parse_range(key: str, value: str) -> tuple[float, float]:
    """A crater key as the engine reads it: a number or min~max, else ValueError."""

    from entity_designer.destruction_fx import parse_float_range

    limit = RIM_MAX if key == RIM_KEY else SIZE_MAX
    numbers = parse_float_range(value.strip().strip('"'))
    if numbers is None or not all(math.isfinite(n) and 0.0 <= n <= limit for n in numbers):
        raise ValueError(f"{key} must be a number or min~max between 0 and {limit:g}.")
    low, high = sorted(numbers)
    return low, high


def config_from_values(values: dict[str, str]) -> CraterConfig | None:
    """The crater of a weapon, or None when the engine digs none.

    Both crater_radius and crater_depth are needed; any invalid key switches
    the crater off (parsers_weapon.cpp).
    """

    try:
        radius = parse_range(RADIUS_KEY, values[RADIUS_KEY]) if values.get(RADIUS_KEY) else (0.0, 0.0)
        depth = parse_range(DEPTH_KEY, values[DEPTH_KEY]) if values.get(DEPTH_KEY) else (0.0, 0.0)
        rim = (parse_range(RIM_KEY, values[RIM_KEY]) if values.get(RIM_KEY)
               else (RIM_DEFAULT, RIM_DEFAULT))
    except ValueError:
        return None
    if radius[1] <= 0.0 or depth[1] <= 0.0:
        return None
    return CraterConfig(radius, depth, rim)


def make(x: float, y: float, z: float, radius: float, depth: float, rim: float) -> Shape | None:
    """World::Crater::Make: None for a crater the engine would refuse."""

    if not all(math.isfinite(v) for v in (x, y, z, radius, depth, rim)) or radius <= 0 or depth <= 0:
        return None
    return Shape(x, y, z, radius, min(depth, radius * MAX_DEPTH_RATIO), max(0.0, min(rim, 1.0)))


def roll(config: CraterConfig, rng: random.Random, x: float, y: float, z: float) -> Shape | None:
    """One crater of the weapon at an impact point: sizes picked inside the ranges."""

    def between(low_high):
        low, high = low_high
        return low if not high > low else low + (high - low) * rng.random()

    return make(x, y, z, between(config.radius), between(config.depth), between(config.rim) / 100.0)


def reach(shape: Shape) -> float:
    return shape.radius * (REACH if shape.rim > 0.0 else 1.0)


def _smooth(edge0, edge1, value):
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _smooth_slope(edge0, edge1, value):
    t = (value - edge0) / (edge1 - edge0)
    return np.where((t > 0.0) & (t < 1.0), 6.0 * t * (1.0 - t) / (edge1 - edge0), 0.0)


def sample(shapes, x, z):
    """Drop, slope along x and z, and deepest crater at ground points (arrays).

    ShapeSample + SampleAt of the engine for points on the ground the craters
    were dug in (the vertical band of the engine keeps all of them).
    """

    x = np.asarray(x, float)
    z = np.asarray(z, float)
    drop = np.zeros_like(x)
    slope_x = np.zeros_like(x)
    slope_z = np.zeros_like(x)
    deepest = np.zeros_like(x)
    highest_rim = np.zeros_like(x)
    for shape in shapes:
        dx, dz = x - shape.x, z - shape.z
        distance = np.hypot(dx, dz)
        inside_reach = distance < reach(shape)
        d = distance / shape.radius
        inside = 1.0 - d * d
        bowl = d < 1.0
        one = np.where(bowl, shape.depth * inside * inside, 0.0)
        change = np.where(bowl, -4.0 * shape.depth * d * inside, 0.0)
        if shape.rim > 0.0:
            ridge = (d - 1.0) / 0.22
            near_rim = ridge > -3.5
            bump = np.where(near_rim, shape.depth * shape.rim * np.exp(-ridge * ridge), 0.0)
            fade = 1.0 - _smooth(1.3, REACH, d)
            one = one - bump * fade
            change = change - (bump * (-2.0 * ridge / 0.22) * fade - bump * _smooth_slope(1.3, REACH, d))
        one = np.where(inside_reach, one, 0.0)
        change = np.where(inside_reach, change, 0.0)
        along = np.where(distance > 1e-9, change / np.maximum(distance, 1e-9) / shape.radius, 0.0)
        reached = inside_reach & ((one != 0.0) | (along != 0.0))
        drop += one
        slope_x += along * dx
        slope_z += along * dz
        deepest = np.where(reached, np.maximum(deepest, shape.depth), deepest)
        highest_rim = np.where(reached, np.maximum(highest_rim, shape.depth * shape.rim), highest_rim)
    lowest = deepest * 1.75
    capped = (drop > lowest) | (drop < -highest_rim)
    drop = np.where(capped, np.clip(drop, -highest_rim, lowest), drop)
    slope_x = np.where(capped, 0.0, slope_x)
    slope_z = np.where(capped, 0.0, slope_z)
    return drop, slope_x, slope_z, deepest


def shade(drop, slope_x, slope_z, deepest):
    """ShadeOf: baked light of the dug ground, 1 = unchanged."""

    nx, ny, nz = slope_x, -np.ones_like(drop), slope_z
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    lighting = (nx * -0.45 + ny * -0.80 + nz * 0.40) / length
    depth_shade = np.where(deepest > 0.0, 1.0 - 0.30 * np.maximum(0.0, drop) / np.maximum(deepest, 1e-9), 1.0)
    value = np.clip(lighting / 0.943 * depth_shade, 0.35, 1.25)
    untouched = (drop == 0.0) & (slope_x == 0.0) & (slope_z == 0.0)
    return np.where(untouched, 1.0, value)
