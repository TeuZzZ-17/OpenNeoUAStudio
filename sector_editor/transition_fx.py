"""Explosions of a sector cell when it changes damage state, as the engine plays them.

Engine (yw.cpp, sub_44FD6C): before each damage step, every effect group of the model being
left spawns the FX vehicle ``ObjectID`` already dead at the model position plus the group
offset, with an impulse towards (x, -150, z). A dead FX vehicle falls with its vp_dead, then
shows vp_megadeth and runs its megadeth dest_fx chain: the same flight the Entity Designer's
dest_fx preview simulates, so its PreviewSimulation and viewport FX pass are reused here.
"""

from __future__ import annotations

from functools import lru_cache
import math
import random

from map_editor import bootstrap


class _ScriptsOnly:
    """Stand-in installation for load_game_objects when no game is registered (MCP, tests)."""

    def folder(self, name):
        data = bootstrap.game_data_dir()
        return bootstrap.find_ci(data, name) or data / name


@lru_cache(maxsize=1)
def _catalog_cache(scripts_key: str):
    from entity_designer import destruction_fx as fx
    from studio.assets.catalog import effect_objects_by_id, load_game_objects
    install = bootstrap.installation() or _ScriptsOnly()
    return {obj.object_id: fx.legacy_vehicle_from_text(obj.object_id, obj.effective_text, str(obj.source))
            for obj in effect_objects_by_id(load_game_objects(install)).values()}


def fx_vehicle_catalog():
    """Bruch definitions and legacy vehicle fallbacks used by sector effects."""
    install = bootstrap.installation() or _ScriptsOnly()
    return _catalog_cache(str(install.folder("scripts")))


def effect_position(effect):
    """SET.SDF effect group (id, x, z, height) -> model space (x, y down, z), as yw_parse_lego."""
    _vehicle, x, z, height = effect
    return (float(x), -float(height), -float(z))


def transition_simulation(steps, catalog=None):
    """PreviewSimulation of the explosions of ``steps``: one effect list per damage step.

    Steps fire one after the other, as the engine loops over intermediate states.
    Returns None when no step has explosions.
    """
    from entity_designer import destruction_fx as fx
    catalog = fx_vehicle_catalog() if catalog is None else catalog
    if not any(steps):
        return None
    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_VEHICLE, "death",
                                      legacy_catalog=catalog)
    rng = random.Random(1)
    for effects in steps:
        for effect in effects:
            origin = effect_position(effect)
            # Engine impulse (sub_44FD6C + ApplyImpulse at the spawn point): direction
            # (x, -150, z) of the offset, speed 2.5 * mass 50 * force 30 / (FX mass + 50).
            direction = (origin[0], -150.0, origin[2])
            length = math.sqrt(sum(v * v for v in direction))
            vehicle = catalog.get(int(effect[0]))
            speed = (30.0 if vehicle and vehicle.bruch_effects else
                     3750.0 / ((vehicle.mass if vehicle else 1.0) + 50.0))
            velocity = [v / length * speed for v in direction]
            dest = fx.DestFx("death", int(effect[0]), *velocity)
            simulation._build_legacy(-1, dest, origin, rng, 0.0, 0, radius=0.0)
    simulation._instances = None        # rebuild the emitter instances with the new events
    return simulation


def required_vps(simulation, step_ms: float = 50.0) -> set[int]:
    """Every VP the simulation shows over its whole duration."""
    vps, time = set(), 0.0
    while time <= simulation.duration:
        vps.update(int(item.vp) for item in simulation.items_at(time) if item.vp)
        time += step_ms
    return vps
