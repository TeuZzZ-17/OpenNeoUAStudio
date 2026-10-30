"""OpenNeoUA Cloud profiles (`new_cloud <id>` blocks) and the particle motion used by the map preview.

Parsing follows the engine (parsers_world.cpp, AtmosphericFXProfileParser) and the motion follows
yw_fx.cpp (UpdateAtmosphericFX): meshes drift with their own velocity and are replaced when they
expire or leave the volume. Studio spreads that volume over the whole map instead of the viewer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from studio.exchange.portable import find_existing_path

from .script_catalog import script_texts
from .three_ds import read_3ds

MAX_COUNT = 512
# Upper bound for the particles Studio spawns to cover a large map at the game density.
MAX_MAP_COUNT = 4000
FALLBACK_LIFETIME_MS = 1000


@dataclass
class CloudProfile:
    id: int
    name: str = ''
    mesh_3ds: str = ''
    count: int = 0
    spawn_radius: tuple = (0.0, 0.0, 0.0)
    spawn_offset: tuple = (0.0, 0.0, 0.0)
    lifetime: tuple = (0, 0)
    velocity: tuple = (0.0, 0.0, 0.0)
    velocity_random: tuple = (0.0, 0.0, 0.0)
    scale: tuple = (1.0, 1.0, 1.0)
    spin: tuple = (0.0, 0.0, 0.0)
    fade: tuple = (0, 0)
    tint: tuple = (1.0, 1.0, 1.0, 1.0)

    @property
    def label(self) -> str:
        return f'{self.id} - {self.name}' if self.name else str(self.id)


def _tuple(value: str, count: int, convert=float):
    parts = value.split('_')
    if len(parts) != count:
        return None
    try:
        result = tuple(convert(part) for part in parts)
    except ValueError:
        return None
    return result if all(math.isfinite(v) for v in result) else None


def _tint(value: str):
    parts = value.split('_')
    if len(parts) not in (3, 4):
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    numbers += [255] * (4 - len(numbers))
    return tuple(max(0, min(255, n)) / 255 for n in numbers)


def _finish(profile: CloudProfile):
    """Same acceptance rules as the engine: a mesh and a positive count are required."""
    if not profile.mesh_3ds or profile.count <= 0:
        return None
    profile.count = min(profile.count, MAX_COUNT)
    low, high = profile.lifetime
    if low <= 0 and high <= 0:
        low = high = FALLBACK_LIFETIME_MS
    elif low <= 0:
        low = high
    elif high <= 0:
        high = low
    profile.lifetime = (min(low, high), max(low, high))
    profile.scale = tuple(v if v > 0 else 1.0 for v in profile.scale)
    return profile


def parse_cloud_profiles(text: str) -> dict[int, CloudProfile]:
    """Collect every usable `new_cloud` block of a script; a repeated ID replaces the earlier one."""
    profiles: dict[int, CloudProfile] = {}
    current = None
    for raw in text.splitlines():
        line = raw.split(';', 1)[0].strip()
        if not line:
            continue
        words = line.split(None, 1)
        word = words[0].lower()
        if word == 'new_cloud':
            current = None
            try:
                number = int(words[1].strip(), 0) if len(words) > 1 else -1
            except ValueError:
                number = -1
            if 0 <= number <= 2147483647:
                current = CloudProfile(number)
            continue
        if current is None:
            continue
        if word == 'end':
            ready = _finish(current)
            if ready is not None:
                profiles[ready.id] = ready
            current = None
            continue
        key, value = (line.split('=', 1) + [''])[:2] if '=' in line else (word, words[1] if len(words) > 1 else '')
        key, value = key.strip().lower(), value.strip()
        if key == 'name':
            current.name = value
        elif key == 'mesh_3ds':
            current.mesh_3ds = value
        elif key == 'count':
            try:
                current.count = int(value, 0)
            except ValueError:
                pass
        elif key in ('spawn_radius_xyz', 'velocity_random_xyz'):
            numbers = _tuple(value, 3)
            if numbers is not None:
                setattr(current, 'spawn_radius' if key == 'spawn_radius_xyz' else 'velocity_random',
                        tuple(max(0.0, v) for v in numbers))
        elif key in ('spawn_offset_xyz', 'velocity_xyz', 'spin_xyz', 'scale_xyz'):
            numbers = _tuple(value, 3)
            if numbers is not None:
                setattr(current, {'spawn_offset_xyz': 'spawn_offset', 'velocity_xyz': 'velocity',
                                  'spin_xyz': 'spin', 'scale_xyz': 'scale'}[key], numbers)
        elif key in ('lifetime_minmax', 'fade_inout'):
            numbers = _tuple(value, 2)
            if numbers is not None:
                setattr(current, 'lifetime' if key == 'lifetime_minmax' else 'fade',
                        tuple(max(0, int(v)) for v in numbers))
        elif key == 'tint':
            tint = _tint(value)
            current.tint = tint if tint is not None else (1.0, 1.0, 1.0, 1.0)
    return profiles


def load_cloud_profiles(scripts: Path, extra: str = '') -> dict[int, CloudProfile]:
    """Every Cloud profile reachable from Startup.cfg (and the level script), ordered by ID."""
    profiles: dict[int, CloudProfile] = {}
    try:
        for text in script_texts(Path(scripts), extra):
            profiles.update(parse_cloud_profiles(text))
    except OSError:
        pass
    return dict(sorted(profiles.items()))


@dataclass
class CloudMesh:
    """Merged 3DS geometry of a profile: vertices (V,3) and triangles (F,3)."""
    vertices: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    faces: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=int))


def load_cloud_mesh(data: Path, reference: str) -> CloudMesh | None:
    value = reference.replace('\\', '/').strip()
    if value.casefold().startswith('data/'):
        value = value[5:]
    path = find_existing_path(Path(data), value)
    if path is None or not path.is_file():
        return None
    try:
        meshes, _materials = read_3ds(path)
    except (OSError, ValueError):
        return None
    vertices, faces, offset = [], [], 0
    for mesh in meshes:
        vertices.extend(mesh.vertices)
        faces.extend((a + offset, b + offset, c + offset) for a, b, c in mesh.faces)
        offset += len(mesh.vertices)
    if not faces:
        return None
    return CloudMesh(np.array(vertices, dtype=float), np.array(faces, dtype=int))


def spin_matrices(spin, age_ms: np.ndarray) -> np.ndarray:
    """Per-particle rotation (N,3,3): each spin point is one revolution per second, like World::Spin."""
    angles = np.outer(age_ms * 0.001 * 2 * math.pi, np.clip(np.nan_to_num(spin), 0, 10))
    c, s = np.cos(angles), np.sin(angles)
    one, zero = np.ones(len(age_ms)), np.zeros(len(age_ms))
    rx = np.stack([one, zero, zero, zero, c[:, 0], -s[:, 0], zero, s[:, 0], c[:, 0]], 1).reshape(-1, 3, 3)
    ry = np.stack([c[:, 1], zero, s[:, 1], zero, one, zero, -s[:, 1], zero, c[:, 1]], 1).reshape(-1, 3, 3)
    rz = np.stack([c[:, 2], -s[:, 2], zero, s[:, 2], c[:, 2], zero, zero, zero, one], 1).reshape(-1, 3, 3)
    return rx @ ry @ rz


def fade_envelope(age: np.ndarray, life: np.ndarray, fade_in: float, fade_out: float) -> np.ndarray:
    """World::ComputeTemporalEnvelope: linear fade-in and fade-out, scaled down when they overlap."""
    total = fade_in + fade_out
    scale = np.where(total > life, life / max(total, 1e-9), 1.0)
    fin, fout = fade_in * scale, fade_out * scale
    factor = np.ones_like(age)
    if fade_in > 0:
        factor = np.where(age < fin, age / np.maximum(fin, 1e-9), factor)
    if fade_out > 0:
        start = life - fout
        factor = np.where(age > start, np.minimum(factor, (life - age) / np.maximum(fout, 1e-9)), factor)
    return np.clip(factor, 0.0, 1.0)


def map_particle_count(profile: CloudProfile, area: float) -> int:
    """Particles needed to keep the game's density when the volume grows to the whole map."""
    footprint = 4.0 * profile.spawn_radius[0] * profile.spawn_radius[2]
    if footprint <= 0 or area <= footprint:
        return profile.count
    return min(MAX_MAP_COUNT, math.ceil(profile.count * area / footprint))


class CloudSimulation:
    """Particles of one profile spread over a fixed map box, stepped with real elapsed time.

    The game keeps the cloud only around the viewer to save performance. Studio fills the whole
    map instead (box = (x_min, x_max, z_min, z_max)); `ground_y` replaces the viewer height.
    """

    def __init__(self, profile: CloudProfile, box: tuple, ground_y: float = 0.0,
                 count: int | None = None, seed: int | None = None):
        self.profile = profile
        self.box = box
        self.ground_y = ground_y
        self.rng = np.random.default_rng(seed)
        n = profile.count if count is None else count
        self.pos = np.zeros((n, 3))
        self.vel = np.zeros((n, 3))
        self.life = np.ones(n)
        self.age = np.zeros(n)
        self._started = False

    def _respawn(self, index: np.ndarray, fresh_age: bool):
        p, n = self.profile, len(index)
        if not n:
            return
        x0, x1, z0, z1 = self.box
        signed = lambda extent: (self.rng.random((n, 3)) * 2 - 1) * np.asarray(extent)
        self.pos[index, 0] = self.rng.uniform(x0, x1, n)
        self.pos[index, 2] = self.rng.uniform(z0, z1, n)
        self.pos[index, 1] = self.ground_y + p.spawn_offset[1] + signed(p.spawn_radius)[:, 1]
        self.vel[index] = np.asarray(p.velocity) + signed(p.velocity_random)
        self.life[index] = self.rng.uniform(p.lifetime[0], p.lifetime[1], n)
        # A new preview starts with a running cloud instead of an empty map.
        self.age[index] = 0.0 if fresh_age else self.rng.random(n) * self.life[index]

    def step(self, dt_ms: float):
        if not self._started:
            self._respawn(np.arange(len(self.age)), fresh_age=False)
            self._started = True
        dt_ms = max(0.0, min(dt_ms, 100.0))
        self.age += dt_ms
        self.pos += self.vel * (dt_ms / 1000.0)
        x0, x1, z0, z1 = self.box
        reach = np.maximum(200.0, np.asarray(self.profile.spawn_radius[1]) * 2 + 200.0)
        outside = ((self.pos[:, 0] < x0) | (self.pos[:, 0] > x1) | (self.pos[:, 2] < z0)
                   | (self.pos[:, 2] > z1) | (np.abs(self.pos[:, 1] - self.ground_y
                                                       - self.profile.spawn_offset[1]) > reach)
                   | (self.age >= self.life))
        self._respawn(np.nonzero(outside)[0], fresh_age=True)

    def alpha(self) -> np.ndarray:
        return fade_envelope(self.age, self.life, *self.profile.fade) * self.profile.tint[3]
