"""Native particle preview shared by model, snapshot and destruction tools."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from types import SimpleNamespace

Matrix3 = tuple[tuple[float, ...], ...]

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
    size *= math.sqrt(instance.trail[0] * instance.trail[1])
    stage =min(int(age / (emitter.lifetime / emitter.stages)), emitter.stages - 1)
    sprites.append(ParticleSprite(
        instance.source, instance.vp, emitter_index, position, size, stage,
        instance.tint, instance.alpha(min(time_ms, instance.end - 1e-6))))


def _rotate_vector(matrix: Matrix3, vector) -> tuple[float, float, float]:
    return tuple(sum(matrix[row][k] * vector[k] for k in range(3))
                 for row in range(3))


@dataclass
class ParticleEmitter:
    attributes: object
    origin: tuple[float, float, float]
    matrix: list[list[float]]
    stages: list[tuple[int, int, object]]
    owner: str
    seed: int

    @property
    def duration_ticks(self) -> float:
        a = self.attributes
        return max(0, a.context_stop_generation) + max(0, a.lifetime)

    def samples(self, ticks: float):
        """Preview one native burst with the shared destruction-FX simulator."""
        spec = EmitterSpec.from_attributes(
            (0.0, 0.0, 0.0), self.attributes, len(self.stages))
        instance = SimpleNamespace(
            start=0.0, end=float(max(spec.gen_lifetime, spec.gen_end)),
            position=lambda _t: self.origin, scale=lambda _t: (1.0, 1.0, 1.0),
            rotation=self.matrix, source=0, vp=0, trail=(1.0, 1.0, 1.0),
            tint=None, alpha=lambda _t: 1.0)
        sprites = []
        _emit_particles(sprites, instance, 0, 0, spec, ticks, self.seed, 6000)
        for sprite in sprites:
            if sprite.size > 0:
                yield sprite.position, sprite.size, self.stages[sprite.stage]
