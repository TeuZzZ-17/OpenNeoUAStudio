"""Destruction FX preview: engine rules, death sequences, particles, reload."""

import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer.script_model import find_script_blocks

UNIT = (
    "new_vehicle 5\n"
    "    name = Weasel\n"
    "    model = heli\n"
    "    vp_normal = 10\n"
    "    vp_dead = 11\n"
    "    vp_megadeth = 12\n"
    "    base_dead = Data/Models/Base/WRECK.BASE\n"
    "    dest_fx = death_40_0_-20_0\n"
    "begin_fx\n"
    "    trigger    = destroyed\n"
    "    mode       = visual\n"
    "    duration   = 800\n"
    "    vp_model   = 281\n"
    "    vp_model   = 282\n"
    "    vp_model   = 283\n"
    "end\n"
    "begin_fx\n"
    "    trigger    = air_destroyed\n"
    "    mode       = visual\n"
    "    duration   = 500\n"
    "    vp_model   = 290\n"
    "end\n"
    "end\n"
    "new_vehicle 40\n"
    "    name = FX_RING\n"
    "    maxrot = 1\n"
    "    scale_fx = 0.5_15_-9_3000_229_230_231_0_99\n"
    "end\n"
)



def _offset(panel):
    """Model-state rows lead the effects list; effect N is the row after them."""
    return len(panel.state_session.originals)


def _item(panel, number):
    return panel.effects_list.item(number + _offset(panel))


def _effect_rows(panel):
    return panel.effects_list.count() - _offset(panel)

def _document(text=UNIT):
    block = next(block for block in find_script_blocks(text)
                 if block.object_id == 5)
    return fx.extract_fx_document(text, block.start_line, block.end_line)


def _catalog():
    return {40: fx.legacy_vehicle_from_text(40, UNIT.split("new_vehicle 40")[1])}


def _spec(body, context=fx.CONTEXT_VEHICLE):
    return fx.interpret_block(
        fx.FxBlock(["begin_fx", *body.strip().splitlines(), "end"]), context)


# -- reading and engine rules ---------------------------------------------------

def test_extraction_reads_blocks_and_dest_lines():
    document = _document()
    assert [type(item).__name__ for item in document.items] == [
        "DestFxLine", "FxBlock", "FxBlock"]
    spec = fx.interpret_block(document.items[1], fx.CONTEXT_VEHICLE)
    assert spec.active and [visual.vp for visual in spec.visuals] == [281, 282, 283]
    assert fx.top_level_value(UNIT, 1, 22, "base_dead") == "Data/Models/Base/WRECK.BASE"


def test_visual_without_duration_or_model_is_ignored():
    spec = _spec("trigger = destroyed\nmode = visual\nvp_model = 3")
    assert not spec.active
    assert spec.issues[-1].severity == fx.SEVERITY_IGNORED


def test_trigger_validity_depends_on_context():
    body = "trigger = detonate\nmode = visual\nduration = 100\nvp_model = 3"
    assert _spec(body, fx.CONTEXT_WEAPON).active
    assert not _spec(body, fx.CONTEXT_VEHICLE).active


def test_unknown_key_is_reported_but_previewed():
    spec = _spec("trigger = destroyed\nmode = visual\nduration = 100\n"
                 "vp_model = 3\nfuture_key = 1")
    assert spec.active
    assert spec.issues[0].severity == fx.SEVERITY_UNKNOWN


def test_dest_fx_and_scale_fx_parsing():
    dest = fx.parse_dest_fx("megadeth_118_+20_-50_0_1")
    assert (dest.kind, dest.vehicle_id, dest.x, dest.y, dest.accel) == (
        "megadeth", 118, 20.0, -50.0, True)
    assert fx.dest_fx_issues(fx.DestFxLine("dest_fx = boom_1_0_0_0"))[0].severity \
        == fx.SEVERITY_FATAL
    assert fx.parse_scale_fx("0.5_15_-9_3000_229_230_0_99").vps == (229, 230)


def test_visual_chain_matches_engine_timeline():
    spec = _spec("trigger = destroyed\nmode = visual\nstart_size = 0\n"
                 "mid_size = 4\nend_size = 2\nduration = 900\nfade_inout = 0_300\n"
                 "vp_model = 1\nvp_model = 2\nvp_model = 3")
    index, size, _alpha, _spin = fx.visual_chain_state(spec, 450)
    assert index == 1 and math.isclose(size, 4.0)
    index, _size, alpha, _spin = fx.visual_chain_state(spec, 750)
    assert index == 2 and math.isclose(alpha, 0.5)


# -- death sequences --------------------------------------------------------------------

def _units(simulation, time_ms):
    return [item.vp for item in simulation.items_at(time_ms) if item.kind == "unit"]


def test_flying_unit_dies_in_the_air_falls_and_lands():
    unit = fx.UnitPhases(
        normal=fx.VisualRef(10), dead=fx.VisualRef(11),
        megadeth=fx.VisualRef(12), model="heli", altitude=600)
    assert unit.flies
    simulation = fx.PreviewSimulation(
        _document(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit,
        legacy_catalog=_catalog())
    death = fx.SEQUENCE_PREROLL_MS
    assert _units(simulation, 0) == [10]
    assert simulation.items_at(0)[0].position[1] == pytest.approx(-600)
    air = simulation.items_at(death + 10)
    assert [item.vp for item in air if item.kind == "visual"] == [290]
    assert any(item.kind == "legacy" and item.vp == 229 for item in air)
    assert _units(simulation, death + 10) == [11]
    land = next(track.end for track in simulation.tracks
                if track.label == "Unit · killed")
    assert land > death + 500
    assert _units(simulation, land + 5) == [12]
    ground = simulation.items_at(land + 5)
    assert [item.vp for item in ground if item.kind == "visual"] == [281]


_CARCASS_UNIT = (
    "new_vehicle 5\n"
    "    name = Weasel\n"
    "    model = heli\n"
    "begin_fx\n"
    "    trigger = air_destroyed\n"
    "    mode = physical\n"
    "    vp_model = 202\n"
    "    mass = 40\n"
    "    radius = 10\n"
    "    maxrot = 3\n"
    "    airconst = 500\n"
    "    launch_xyz = 0_0_0\n"
    "    follow_carcass = {value}\n"
    "end\n"
    "end\n"
)


def _fragment_and_wreck(value, time_ms):
    text = _CARCASS_UNIT.format(value=value)
    unit = fx.UnitPhases(
        normal=fx.VisualRef(10), dead=fx.VisualRef(11),
        megadeth=fx.VisualRef(12), model="heli", altitude=600)
    simulation = fx.PreviewSimulation(
        _document(text), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    items = simulation.items_at(time_ms)
    fragment = next(item for item in items if item.kind == fx.MODE_PHYSICAL)
    wreck = next(item for item in items if item.kind == "unit")
    return fragment, wreck


def test_follow_carcass_is_parsed_only_for_exact_one():
    for value, expected in (("1", True), ("0", False), ("2", False), ("x", False)):
        spec = _spec("trigger = air_destroyed\nmode = physical\nvp_model = 3\n"
                     f"mass = 40\nradius = 10\nfollow_carcass = {value}")
        assert spec.active and spec.follow_carcass is expected
        assert not [i for i in spec.issues if i.severity == fx.SEVERITY_UNKNOWN]


def test_follow_vehicle_is_no_longer_a_known_key():
    spec = _spec("trigger = air_destroyed\nmode = physical\nvp_model = 3\n"
                 "mass = 40\nradius = 10\nfollow_vehicle = 1")
    assert spec.issues[0].severity == fx.SEVERITY_UNKNOWN


def test_follow_carcass_fragment_stays_on_the_falling_wreck():
    moment = fx.SEQUENCE_PREROLL_MS + 200
    fragment, wreck = _fragment_and_wreck(1, moment)
    assert fragment.position == pytest.approx(wreck.position)
    # Without the flag the fragment drops with its own air resistance.
    free, wreck = _fragment_and_wreck(0, moment)
    assert free.position != pytest.approx(wreck.position)


def test_ground_unit_goes_straight_to_megadeth():
    unit = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                         megadeth=fx.VisualRef(12), model="tank")
    simulation = fx.PreviewSimulation(
        _document(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit,
        legacy_catalog=_catalog())
    moment = fx.SEQUENCE_PREROLL_MS + 10
    assert _units(simulation, moment) == [12]
    assert not any(item.kind == "legacy" for item in simulation.items_at(moment))


def test_unit_states_carry_external_models_in_engine_order():
    unit = fx.UnitPhases(
        normal=fx.VisualRef(10),
        dead=fx.VisualRef(11, mesh3ds="Data/a.3ds", base_path="Data/b.base"),
        megadeth=fx.VisualRef(12), model="plane", altitude=300)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    dead = next(item for item in simulation.items_at(fx.SEQUENCE_PREROLL_MS + 5)
                if item.kind == "unit")
    assert dead.external == ("Data/a.3ds", "Data/b.base") and dead.vp == 11


def test_weapon_drops_vertically_to_the_target_and_explodes():
    weapon = (
        "new_weapon 7\n"
        "begin_fx\n    trigger = detonate\n    mode = visual\n"
        "    duration = 400\n    vp_model = 50\nend\n"
        "begin_fx\n    trigger = impact_world\n    mode = visual\n"
        "    duration = 400\n    vp_model = 51\nend\n"
        "end\n")
    document = fx.extract_fx_document(weapon, 0, weapon.count("\n") - 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         megadeth=fx.VisualRef(62), weapon=True)
    impact = fx.WEAPON_FLIGHT_MS
    on_unit = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                   fx.WEAPON_HIT_UNIT, unit=unit)
    flying = next(item for item in on_unit.items_at(impact / 2)
                  if item.kind == "unit")
    assert flying.vp == 60
    assert flying.position == pytest.approx(
        (0.0, -fx.WEAPON_LAUNCH_HEIGHT / 2, 0.0))
    assert tuple(flying.rotation[row][2] for row in range(3)) == pytest.approx(
        (0.0, 1.0, 0.0))
    after = on_unit.items_at(impact + 10)
    assert _units(on_unit, impact + 10) == [61]
    assert [item.vp for item in after if item.kind == "visual"] == [50]
    on_ground = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                     fx.WEAPON_HIT_GROUND, unit=unit)
    assert _units(on_ground, impact + 10) == [62]
    falling_ground = next(item for item in on_ground.items_at(impact / 2)
                          if item.kind == "unit")
    assert falling_ground.position == pytest.approx(
        (0.0, -fx.WEAPON_LAUNCH_HEIGHT / 2, 0.0))
    landed = next(item for item in on_ground.items_at(impact + 10)
                  if item.kind == "unit")
    assert tuple(landed.rotation[row][2] for row in range(3)) == pytest.approx(
        (0.0, 1.0, 0.0))
    assert [item.vp for item in on_ground.items_at(impact + 10)
            if item.kind == "visual"] == [51]


def test_scale_fx_vehicle_grows_in_place():
    document = fx.FxDocument([fx.DestFxLine("dest_fx = death_40_0_-20_0")])
    simulation = fx.PreviewSimulation(document, fx.CONTEXT_VEHICLE, "death",
                                      legacy_catalog=_catalog())
    early, late = simulation.items_at(100)[0], simulation.items_at(2900)[0]
    assert early.position == late.position
    assert late.vp == 231 and late.scale[0] > early.scale[0]


def test_particles_follow_emitter_window_and_outlive_their_vp():
    emitter = fx.EmitterSpec(
        point=(0.0, 0.0, 0.0), accel_start=(0.0, 0.0, 0.0),
        accel_end=(0.0, 0.0, 0.0), magnify_start=(0.0, -1.0, 0.0),
        magnify_end=(0.0, -1.0, 0.0), start_speed=100.0,
        gen_lifetime=1000000, gen_start=0, gen_end=200, birth_rate=50,
        lifetime=1000, start_size=40.0, end_size=10.0, noise=0.0, stages=4)
    document = fx.FxDocument([fx.FxBlock(
        ["begin_fx", "trigger = destroyed", "mode = visual",
         "duration = 300", "vp_model = 7", "end"])])
    simulation = fx.PreviewSimulation(document, fx.CONTEXT_VEHICLE, "destroyed")
    # 1024 / 50 = one birth every 20 ms inside the 0..200 ms window.
    assert len(simulation.particles_at(100, {7: [emitter]})) == 6
    late = simulation.particles_at(600, {7: [emitter]})
    assert len(late) == 10
    assert all(math.dist(sprite.position, (0.0, 0.0, 0.0)) > 20.0 for sprite in late)
    assert simulation.particles_at(1300, {7: [emitter]}) == []


# -- look of the definition: tint, spin, tracer, decoration -------------------------------

LOOK = (
    "new_weapon 9\n"
    "    tint = 70_70_120_200\n"
    "    spin_xyz = 0_0_2\n"
    "    vp_trail_tint = 50_150_255_130\n"
    "    vp_trail_scale_xyz = 2_1_0.4\n"
    "    mesh_tracer_enable = 1\n"
    "    mesh_tracer_path = Data/Models/3ds/Tracer.3ds\n"
    "    mesh_tracer_tint = 255_180_60_120\n"
    "    mesh_tracer_size_xyz = 1_1_80\n"
    "    decoration_fx_mode = periodic\n"
    "    decoration_fx_vp = 224\n"
    "    decoration_fx_interval = 300_300\n"
    "    decoration_fx_count = 2\n"
    "    decoration_fx_duration = 500\n"
    "    decoration_fx_offset_xyz = 0_0_-40\n"
    "    decoration_fx_tint = 255_95_70_200\n"
    "    decoration_fx_spin_xyz = 0_1.5_0\n"
    "begin_fx\n    trigger = detonate\n    tint = 1_2_3\nend\n"
    "end\n")


def _look(weapon=True):
    block = find_script_blocks(LOOK)[0]
    values = fx.top_level_values(LOOK, block.start_line, block.end_line)
    return fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         megadeth=fx.VisualRef(62), weapon=weapon,
                         **fx.unit_look(values, weapon=weapon))


def _weapon_simulation(unit, trigger=fx.WEAPON_HIT_UNIT):
    return fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON, trigger,
                                unit=unit)


def test_look_keys_are_read_from_the_top_level_only():
    unit = _look()
    assert unit.tint == (70, 70, 120, 200) and unit.spin == (0.0, 0.0, 2.0)
    assert unit.trail_tint == (50, 150, 255, 130)
    assert unit.trail_scale == (2.0, 1.0, 0.4)
    assert unit.tracer.mesh == "Data/Models/3ds/Tracer.3ds"
    assert (unit.tracer.size_x, unit.tracer.size_y, unit.tracer.size_z) == (1.0, 1.0, 80.0)
    assert unit.decoration.spawns and unit.decoration.offset == (0.0, 0.0, -40.0)
    # Vehicles have no mesh tracer and no vp_trail look.
    vehicle = _look(weapon=False)
    assert vehicle.tracer is None and vehicle.trail_tint is None


def test_tracer_needs_enable_path_and_valid_sizes():
    base = {"mesh_tracer_enable": "1", "mesh_tracer_path": "Data/a.3ds"}
    assert fx.parse_tracer(base) is not None
    assert fx.parse_tracer({**base, "mesh_tracer_enable": "0"}) is None
    assert fx.parse_tracer({"mesh_tracer_enable": "1"}) is None
    assert fx.parse_tracer({**base, "mesh_tracer_size_xyz": "3_3_0"}) is None
    assert fx.parse_tracer({**base, "mesh_tracer_tint": "1_2_3_0"}) is None
    spec = fx.parse_tracer({**base, "mesh_tracer_head_size": "9"})
    assert spec.head_size == 9.0 and spec.tail_size is None
    assert spec.tint == (255, 210, 80, 180) and spec.size_z == 300.0


def test_decoration_periodic_needs_interval_and_count():
    assert fx.parse_decoration({}) is None
    only_vp = fx.parse_decoration({"decoration_fx_vp": "5"})
    assert only_vp is not None and not only_vp.spawns
    persistent = fx.parse_decoration({"decoration_fx_3ds": "Data/a.3ds",
                                      "decoration_fx_mode": "persistent"})
    assert persistent.spawns and persistent.visual.mesh3ds == "Data/a.3ds"


def test_tint_covers_every_state_but_spin_only_the_main_model():
    simulation = _weapon_simulation(_look())
    early = next(i for i in simulation.items_at(100) if i.kind == "unit")
    later = next(i for i in simulation.items_at(500) if i.kind == "unit")
    assert early.tint == later.tint == (70, 70, 120, 200)
    assert early.rotation != later.rotation  # spin_z turns the main model
    flight = simulation.tracks[0].end
    after = next(i for i in simulation.items_at(flight + 10)
                 if i.kind == "unit")
    assert after.vp == 61 and after.tint == (70, 70, 120, 200)
    again = next(i for i in simulation.items_at(flight + 300)
                 if i.kind == "unit")
    assert after.rotation == again.rotation  # the dead model does not spin


def test_weapon_altitude_sets_launch_height_and_flight_time():
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, altitude=1100)
    simulation = _weapon_simulation(unit, fx.WEAPON_HIT_GROUND)
    start = next(i for i in simulation.items_at(0) if i.kind == "unit")
    assert start.position[1] == pytest.approx(-1100)
    assert simulation.tracks[0].end == pytest.approx(
        fx.WEAPON_FLIGHT_MS * 1100 / fx.WEAPON_LAUNCH_HEIGHT)


def test_tracer_trails_behind_the_projectile_and_ends_at_impact():
    simulation = _weapon_simulation(_look())
    flight = simulation.tracks[0].end
    # Just after launch the tail is clamped at the launch point.
    young = [i for i in simulation.items_at(20) if i.kind == "tracer"]
    assert len(young) == 1 and young[0].scale[2] < 80.0
    # In flight the whole 80 units trail above the weapon (UA -Y is up).
    mid = [i for i in simulation.items_at(flight / 2) if i.kind == "tracer"]
    weapon = next(i for i in simulation.items_at(flight / 2) if i.kind == "unit")
    assert mid[0].scale == pytest.approx((1.0, 1.0, 80.0))
    assert mid[0].position[1] == pytest.approx(weapon.position[1] - 40.0)
    assert mid[0].external == ("Data/Models/3ds/Tracer.3ds",)
    assert mid[0].tint == (255, 180, 60, 120)
    assert not [i for i in simulation.items_at(flight + 5) if i.kind == "tracer"]


def test_tapered_tracer_is_split_and_glow_adds_additive_layers():
    spec = fx.parse_tracer({
        "mesh_tracer_enable": "1", "mesh_tracer_path": "Data/a.3ds",
        "mesh_tracer_size_xyz": "4_4_300", "mesh_tracer_tail_size": "0",
        "mesh_tracer_glow_rate": "5"})
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, tracer=spec)
    pieces = [i for i in _weapon_simulation(unit).items_at(500)
              if i.kind == "tracer"]
    body = [i for i in pieces if not i.additive]
    glow = [i for i in pieces if i.additive]
    assert len(body) == len(glow) == 8
    widths = [i.scale[0] for i in body]
    assert widths == sorted(widths) and widths[0] < widths[-1] <= 4.0
    assert glow[0].alpha == pytest.approx(0.5)


def test_persistent_decoration_follows_the_weapon_and_fades_when_it_stops():
    deco = fx.parse_decoration({
        "decoration_fx_vp": "224", "decoration_fx_mode": "persistent",
        "decoration_fx_offset_xyz": "0_0_-40", "decoration_fx_fade_inout": "200_400",
        "decoration_fx_tint": "255_0_0_255"})
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, decoration=deco)
    simulation = _weapon_simulation(unit)
    flight = simulation.tracks[0].end
    assert flight >= fx.DECORATION_PERSISTENT_PREVIEW_MS
    first = next(i for i in simulation.items_at(100) if i.kind == "decoration")
    assert first.alpha == pytest.approx(0.5) and first.tint == (255, 0, 0, 255)
    weapon = next(i for i in simulation.items_at(600) if i.kind == "unit")
    attached = next(i for i in simulation.items_at(600) if i.kind == "decoration")
    # Offset z = -40 is behind the falling weapon, so above it (UA -Y is up).
    assert attached.position[1] == pytest.approx(weapon.position[1] - 40.0)
    fading = next(i for i in simulation.items_at(flight + 200)
                  if i.kind == "decoration")
    assert fading.alpha == pytest.approx(0.5)
    # Once the weapon stopped the decoration stays where it was left.
    assert fading.position[1] == pytest.approx(-40.0)
    assert not [i for i in simulation.items_at(flight + 500)
                if i.kind == "decoration"]


def test_periodic_decoration_spawns_after_one_interval_and_expires():
    unit = _look()
    simulation = _weapon_simulation(unit)
    assert not [i for i in simulation.items_at(250) if i.kind == "decoration"]
    now = [i for i in simulation.items_at(350) if i.kind == "decoration"]
    assert len(now) == 2 and all(i.vp == 224 for i in now)
    # 300 ms spawn + 500 ms duration: the first pair is gone at 850 ms.
    assert not [i for i in simulation.items_at(805) if i.kind == "decoration"
                and i.position in [n.position for n in now]]
    # The same seed gives the same offsets when scrubbing again.
    again = _weapon_simulation(unit)
    assert [i.position for i in again.items_at(350) if i.kind == "decoration"] \
        == [i.position for i in now]


def test_vehicle_waits_long_enough_for_a_slow_decoration():
    deco = fx.parse_decoration({
        "decoration_fx_vp": "5", "decoration_fx_interval": "4000",
        "decoration_fx_count": "1", "decoration_fx_duration": "1000"})
    unit = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                         megadeth=fx.VisualRef(12), model="tank", decoration=deco)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    assert any(i.kind == "decoration" for i in simulation.items_at(4500))
    assert _units(simulation, 4500) == [10]


def test_decoration_particles_use_the_trail_look():
    deco = fx.parse_decoration({
        "decoration_fx_vp": "7", "decoration_fx_mode": "persistent",
        "decoration_fx_vp_trail_tint": "70_220_255_255",
        "decoration_fx_vp_trail_scale_xyz": "1_1_0.1"})
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, decoration=deco)
    simulation = _weapon_simulation(unit)
    emitter = fx.EmitterSpec(
        point=(0.0, 0.0, 0.0), accel_start=(0.0, 0.0, 0.0),
        accel_end=(0.0, 0.0, 0.0), magnify_start=(0.0, -1.0, 0.0),
        magnify_end=(0.0, -1.0, 0.0), start_speed=100.0,
        gen_lifetime=1000000, gen_start=0, gen_end=2000, birth_rate=50,
        lifetime=1000, start_size=10.0, end_size=10.0, noise=0.0, stages=1)
    sprites = simulation.particles_at(600, {7: [emitter]})
    assert sprites and all(s.tint == (70, 220, 255, 255) and s.source == -2
                           for s in sprites)
    # trail_scale_z = 0.1 cuts the particle life to 100 ms (5 births of 20 ms).
    assert len(sprites) <= 6


# -- death plasma (genesis VP) and ground decals -------------------------------------------

def test_genesis_defaults_to_vp_5_and_projectiles_have_none():
    vehicle = fx.unit_look({}, weapon=False)
    assert vehicle["genesis"].vp == 5
    custom = fx.unit_look({"vp_genesis": "35", "3ds_genesis": "Data/g.3ds",
                           "energy": "36000", "maxrot": "1.5"}, weapon=False)
    assert custom["genesis"] == fx.VisualRef(35, mesh3ds="Data/g.3ds")
    assert custom["energy"] == 36000.0 and custom["maxrot"] == 1.5
    assert fx.unit_look({"vp_genesis": "35"}, weapon=True)["genesis"].vp == 0


def test_plasma_lasts_seventy_percent_of_the_energy_within_limits():
    assert fx.UnitPhases(energy=1000).plasma_ms == 10000
    assert fx.UnitPhases(energy=20000).plasma_ms == pytest.approx(14000)
    assert fx.UnitPhases(energy=90000).plasma_ms == 25000


def test_destroyed_vehicle_leaves_a_shrinking_spinning_plasma():
    unit = fx.UnitPhases(
        normal=fx.VisualRef(10), dead=fx.VisualRef(11), megadeth=fx.VisualRef(12),
        model="tank", genesis=fx.VisualRef(5), energy=20000, maxrot=1.5)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    land = fx.SEQUENCE_PREROLL_MS
    assert not [i for i in simulation.items_at(land - 5) if i.kind == "plasma"]
    first = next(i for i in simulation.items_at(land + 1) if i.kind == "plasma")
    later = next(i for i in simulation.items_at(land + 3500) if i.kind == "plasma")
    assert first.vp == 5 and first.scale[0] == pytest.approx(0.75, abs=0.01)
    # sqrt(remaining / total) * 0.75
    assert later.scale[0] == pytest.approx(0.75 * math.sqrt(1 - 3500 / 14000), abs=0.01)
    assert first.rotation != later.rotation
    # The megadeth model stays under the plasma, and the preview waits for it.
    assert _units(simulation, land + 3500) == [12]
    assert simulation.duration >= land + fx.PLASMA_PREVIEW_MS


def test_weapons_and_models_without_genesis_leave_no_plasma():
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         genesis=fx.VisualRef(5), weapon=True)
    simulation = _weapon_simulation(unit)
    assert not [i for i in simulation.items_at(1000) if i.kind == "plasma"]
    unit = fx.UnitPhases(normal=fx.VisualRef(10), model="tank",
                         genesis=fx.VisualRef(0))
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    assert not [i for i in simulation.items_at(1000) if i.kind == "plasma"]


def test_decal_edge_fade_clears_the_corners_and_keeps_the_center():
    from PySide6.QtGui import QColor, QImage
    from entity_designer import fx_viewport

    solid = QImage(64, 64, QImage.Format.Format_RGBA8888)
    solid.fill(QColor(10, 20, 30, 255))
    untouched = fx_viewport._bake_edge_fade(solid, 0.0)
    assert untouched.pixelColor(0, 0).alpha() == 255
    baked = fx_viewport._bake_edge_fade(solid, 10.0)
    assert baked.pixelColor(32, 32).alpha() > 240
    assert baked.pixelColor(0, 0).alpha() == 0
    assert baked.pixelColor(32, 8).alpha() < baked.pixelColor(32, 24).alpha()
    disc = fx_viewport._procedural_decal(64)
    assert disc.pixelColor(32, 32).alpha() == 255 and disc.pixelColor(0, 0).alpha() == 0
    assert disc.pixelColor(32, 32).red() < disc.pixelColor(32, 4).red()


# -- weapon decals in unit mode, launch flash, spiral and chaos ------------------------------

DECAL_PROFILES = fx.decal_profiles_from_text(
    "new_decal 1\n    texture = Interface/GroundDecals/impact_01.png\n"
    "    duration = permanent\nend\n")

DECAL_WEAPON = (
    "new_weapon 9\n"
    "    ground_decal_id = 1\n"
    "begin_fx\n    trigger = impact_world\n    mode = visual\n"
    "    duration = 400\n    vp_model = 51\nend\n"
    "end\n")


def test_hit_unit_preview_also_shows_the_weapon_ground_decal():
    document = fx.extract_fx_document(
        DECAL_WEAPON, 0, DECAL_WEAPON.count("\n") - 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         weapon=True, ground_decal_id=1, decal_size=(100.0,100.0))
    on_unit = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                   fx.WEAPON_HIT_UNIT, unit=unit,
                                   decals=DECAL_PROFILES)
    impact = fx.WEAPON_FLIGHT_MS
    shown = [i for i in on_unit.items_at(impact + 50)
             if i.kind == fx.MODE_DECAL]
    assert len(shown) == 1 and shown[0].position[1] == 0.0
    # Only the decal is borrowed: the impact_world visual stays terrain-only.
    assert not [i for i in on_unit.items_at(impact + 50) if i.kind == "visual"]
    assert any("preview only" in note for note in on_unit.notes)
    on_ground = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                     fx.WEAPON_HIT_GROUND, unit=unit,
                                     decals=DECAL_PROFILES)
    assert {i.kind for i in on_ground.items_at(impact + 50)} >= {"visual", fx.MODE_DECAL}
    assert not any("preview only" in note for note in on_ground.notes)


LAUNCH_WEAPON = (
    "new_weapon 1\n"
    "begin_fx\n    trigger = launch\n    mode = visual\n"
    "    offset_xyz = 0_0_0\n    start_size = 0.2\n    end_size = 0.2\n"
    "    duration = 1000\n    vp_model = 130\nend\n"
    "end\n")


def test_launch_trigger_is_a_weapon_only_begin_fx_at_the_muzzle():
    body = "trigger = launch\nmode = visual\nduration = 100\nvp_model = 1"
    assert _spec(body, fx.CONTEXT_WEAPON).active
    assert not _spec(body, fx.CONTEXT_VEHICLE).active

    document = fx.extract_fx_document(
        LAUNCH_WEAPON, 0, LAUNCH_WEAPON.count("\n") - 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True)
    simulation = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_UNIT, unit=unit)
    flash = next(i for i in simulation.items_at(500) if i.kind == "visual")
    assert flash.vp == 130
    assert flash.position[1] == pytest.approx(-fx.WEAPON_LAUNCH_HEIGHT)
    assert not [i for i in simulation.items_at(1005) if i.kind == "visual"]


def test_spiral_orbits_the_flight_line_and_chaos_wins_over_it():
    assert fx.parse_motion({}) is None
    assert fx.parse_motion({"spiral_speed": "2", "spiral_radius": "0"}) is None
    spiral = fx.parse_motion({"spiral_speed": "2", "spiral_radius": "30"})
    assert spiral.kind == "spiral"
    assert fx.parse_motion({"spiral_speed": "2", "spiral_radius": "30",
                            "chaos_factor": "1", "chaos_radius": "10"}).kind == "chaos"
    # Two turns per second: back on the start point after 500 ms, 30 units out.
    start, quarter = spiral.offset(0), spiral.offset(125)
    assert start == pytest.approx((30.0, 0.0, 0.0))
    assert math.hypot(*quarter[:2]) == pytest.approx(30.0)
    assert spiral.offset(500)[0] == pytest.approx(30.0, abs=1e-6)


def test_chaos_stays_inside_its_radius_and_is_repeatable():
    chaos = fx.parse_motion({"chaos_factor": "2.5", "chaos_radius": "30"})
    samples = [chaos.offset(ms) for ms in range(0, 3000, 37)]
    assert all(math.hypot(x, y) <= 30.0 + 1e-6 and z == 0.0 for x, y, z in samples)
    assert len({tuple(round(v, 3) for v in sample) for sample in samples}) > 20
    assert samples == [chaos.offset(ms) for ms in range(0, 3000, 37)]


def test_weapon_motion_moves_the_model_off_the_line_and_freezes_at_impact():
    motion = fx.parse_motion({"spiral_speed": "1", "spiral_radius": "30"})
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         weapon=True, motion=motion)
    simulation = _weapon_simulation(unit)
    flight = simulation.tracks[0].end
    # The weapon falls straight down: the orbit is sideways (X/Z), never Y.
    for moment in (100, 250, 600):
        item = next(i for i in simulation.items_at(moment) if i.kind == "unit")
        straight = -fx.WEAPON_LAUNCH_HEIGHT + fx.WEAPON_LAUNCH_HEIGHT * moment / flight
        assert item.position[1] == pytest.approx(straight, abs=1e-6)
        assert math.hypot(item.position[0], item.position[2]) == pytest.approx(30.0)
    # After impact the last pose stays frozen on the dead model.
    first = next(i for i in simulation.items_at(flight + 10) if i.kind == "unit")
    later = next(i for i in simulation.items_at(flight + 300) if i.kind == "unit")
    assert first.position == later.position and first.rotation == later.rotation


def test_tracer_follows_a_spiral_instead_of_a_straight_line():
    motion = fx.parse_motion({"spiral_speed": "2", "spiral_radius": "30"})
    spec = fx.parse_tracer({"mesh_tracer_enable": "1",
                            "mesh_tracer_path": "Data/a.3ds",
                            "mesh_tracer_size_xyz": "3_3_200"})
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, tracer=spec,
                         motion=motion)
    simulation = _weapon_simulation(unit)
    pieces = [i for i in simulation.items_at(600) if i.kind == "tracer"]
    assert len(pieces) > 4
    assert sum(i.scale[2] for i in pieces) == pytest.approx(200.0, rel=0.02)
    radii = [math.hypot(i.position[0], i.position[2]) for i in pieces]
    assert all(radius > 1.0 for radius in radii)


# -- sounds ----------------------------------------------------------------------------------

SOUND_UNIT = (
    "new_weapon 9\n"
    "    snd_launch_sample = sounds/launch.wav\n"
    "    snd_launch_volume = 100\n"
    "    snd_launch_pitch = 1000_2000\n"
    "    snd_normal_sample = Data/Sounds/loop.wav\n"
    "    snd_normal_volume = 90\n"
    "    snd_hit_sample = sounds/hit.wav\n"
    "    snd_hit_pitch = 15000_10000\n"
    "begin_fx\n    trigger = detonate\n    snd_launch_sample = no.wav\nend\n"
    "end\n")


def _sound_unit(weapon=True, **override):
    block = find_script_blocks(SOUND_UNIT)[0]
    values = fx.top_level_values(SOUND_UNIT, block.start_line, block.end_line)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61),
                         weapon=weapon, **fx.unit_look(values, weapon=weapon))
    return __import__("dataclasses").replace(unit, **override) if override else unit


def _snd(sample, volume=100, pitch=(0, 0)):
    return fx.SoundSpec(sample, volume, pitch)


def test_sound_packages_are_read_from_the_top_level_only():
    sounds = _sound_unit().sounds
    assert set(sounds) == {"launch", "normal", "hit"}
    assert sounds["launch"] == fx.SoundSpec("sounds/launch.wav", 100, (1000, 2000))
    assert sounds["normal"].volume == 90 and sounds["normal"].pitch == (0, 0)
    # A reversed range is normalized like the engine does.
    assert sounds["hit"].pitch == (10000, 15000)
    assert fx.sound_specs({"snd_explode_sample": ""}, fx.VEHICLE_SOUND_EVENTS) == {}


def test_weapon_plays_launch_then_loops_while_flying_then_hits():
    simulation = _weapon_simulation(_sound_unit())
    flight = simulation.tracks[0].end
    events = {event.event: event for event in simulation.sound_events}
    assert events["launch"].start == 0 and not events["launch"].loops
    assert 1000 <= events["launch"].pitch <= 2000
    assert (events["normal"].start, events["normal"].end) == (0, flight)
    assert events["hit"].start == flight and not events["hit"].loops
    assert 10000 <= events["hit"].pitch <= 15000
    # The same seed plays the same pitch again.
    again = _weapon_simulation(_sound_unit())
    assert [e.pitch for e in again.sound_events] == [
        e.pitch for e in simulation.sound_events]


def test_flying_vehicle_sounds_follow_its_death():
    sounds = {name: _snd(f"{name}.wav") for name in (
        "normal", "airexplode", "goingdown", "crashland", "explode", "fire")}
    unit = fx.UnitPhases(
        normal=fx.VisualRef(10), dead=fx.VisualRef(11), megadeth=fx.VisualRef(12),
        model="heli", altitude=600, sounds=sounds)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    death = fx.SEQUENCE_PREROLL_MS
    land = next(t.end for t in simulation.tracks if t.label == "Unit · killed")
    events = {e.event: e for e in simulation.sound_events}
    assert (events["normal"].start, events["normal"].end) == (0, death)
    assert events["airexplode"].start == death and not events["airexplode"].loops
    assert (events["goingdown"].start, events["goingdown"].end) == (death, land)
    assert events["crashland"].start == land and events["explode"].start == land
    assert "fire" not in events  # firing is not part of a death


def test_ground_vehicle_explodes_at_once_without_falling_sounds():
    sounds = {name: _snd(f"{name}.wav") for name in (
        "normal", "airexplode", "goingdown", "crashland", "explode")}
    unit = fx.UnitPhases(normal=fx.VisualRef(10), megadeth=fx.VisualRef(12),
                         model="tank", sounds=sounds)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    assert {e.event for e in simulation.sound_events} == {"normal", "explode"}
    explode = next(e for e in simulation.sound_events if e.event == "explode")
    assert explode.start == fx.SEQUENCE_PREROLL_MS


def test_physical_fragments_and_fx_vehicles_bring_their_own_sounds():
    block = fx.FxBlock([
        "begin_fx", "trigger = destroyed", "mode = physical", "mass = 10",
        "radius = 5", "vp_model = 31", "force = 100", "lifetime = 3000",
        "snd_normal_sample = sounds/born.wav",
        "snd_goingdown_sample = sounds/fall.wav",
        "snd_explode_sample = sounds/impact.wav", "end"])
    spec = fx.interpret_block(block, fx.CONTEXT_VEHICLE)
    assert set(spec.sounds) == {"normal", "goingdown", "explode"}
    unit = fx.UnitPhases(normal=fx.VisualRef(10), megadeth=fx.VisualRef(12),
                         model="tank")
    simulation = fx.PreviewSimulation(
        fx.FxDocument([block]), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    names = [e.spec.sample for e in simulation.sound_events]
    assert names == ["sounds/born.wav", "sounds/fall.wav", "sounds/impact.wav"]
    fall = next(e for e in simulation.sound_events if e.event == "goingdown")
    impact = next(e for e in simulation.sound_events if e.event == "explode")
    assert fall.loops and fall.end == impact.start > fall.start
    # An FX vehicle of a dest_fx line falls with its own sounds.
    catalog = {40: fx.legacy_vehicle_from_text(
        40, "    snd_goingdown_sample = sounds/g.wav\n"
            "    snd_explode_sample = sounds/e.wav\n")}
    legacy = fx.PreviewSimulation(
        fx.FxDocument([fx.DestFxLine("dest_fx = death_40_0_0_0")]),
        fx.CONTEXT_VEHICLE, "death", legacy_catalog=catalog)
    assert [e.event for e in legacy.sound_events] == ["goingdown", "explode"]


def test_hidden_effects_do_not_make_sounds():
    block = fx.FxBlock([
        "begin_fx", "trigger = destroyed", "mode = physical", "mass = 10",
        "radius = 5", "vp_model = 31", "force = 100", "lifetime = 3000",
        "snd_explode_sample = sounds/impact.wav", "end"])
    simulation = fx.PreviewSimulation(
        fx.FxDocument([block]), fx.CONTEXT_VEHICLE, "destroyed", enabled=set())
    assert simulation.sound_events == []


# -- sound player ---------------------------------------------------------------------------

class _FakeVoice:
    def __init__(self):
        self.played = None
        self.stopped = False

    def play(self, path, gain, loop):
        self.played = (path, gain, loop)

    def stop(self):
        self.stopped = True

    def finished(self):
        return self.stopped


def _wav_file(folder, name="tone.wav", rate=22050, frames=200):
    from entity_designer import fx_audio

    path = folder / name
    path.write_bytes(fx_audio.pcm_wav(rate, 1, b"\x00\x10" * frames))
    return path


def test_wav_files_of_the_game_are_converted_to_16_bit():
    from pathlib import Path

    from entity_designer import fx_audio

    sounds = Path(__file__).resolve().parents[3] / "UA Rising" / "Data" / "Sounds"
    if not sounds.is_dir():
        pytest.skip("game sounds are not installed")
    for name, rate, channels in (("AAGun.wav", 22050, 1), ("New/Expl04.wav", 48000, 2)):
        if not (sounds / name).is_file():
            continue
        decoded = fx_audio.read_wav(sounds / name)
        assert decoded is not None and decoded[0] == rate and decoded[1] == channels
        assert len(decoded[2]) % (2 * channels) == 0 and len(decoded[2]) > 0
    assert fx_audio.read_wav(sounds / "missing.wav") is None


def test_player_follows_the_clock_and_pitch_changes_the_sample_rate(tmp_path):
    import wave

    from entity_designer import fx_audio

    _wav_file(tmp_path)
    voices = []

    def factory():
        voices.append(_FakeVoice())
        return voices[-1]

    player = fx_audio.FxSoundPlayer(
        resolver=lambda name: tmp_path / "tone.wav" if name == "tone.wav" else None,
        voice_factory=factory)
    shot = fx.SoundEvent(100, None, _snd("tone.wav", 127, (2000, 2000)), "launch", 2000)
    loop = fx.SoundEvent(0, 500, _snd("tone.wav", 64), "normal", 0)
    events = [loop, shot]
    player.step(events, -1, 50)
    assert len(voices) == 1 and voices[0].played[2] is True
    assert voices[0].played[1] == pytest.approx(64 / 127)
    player.step(events, 50, 150)
    assert len(voices) == 2 and voices[1].played[2] is False
    assert voices[1].played[1] == pytest.approx(1.0)
    with wave.open(str(voices[1].played[0])) as copy:
        assert copy.getframerate() == 22050 + 2000
    player.step(events, 150, 600)
    assert voices[0].stopped  # the loop ended with its state
    # Pause, then resume in the middle of a loop.
    player.stop_all()
    player.resume([fx.SoundEvent(0, 900, _snd("tone.wav"), "normal", 0)], 300)
    assert voices[-1].played[2] is True
    player.set_muted(True)
    assert voices[-1].stopped
    count = len(voices)
    player.step(events, -1, 50)
    assert len(voices) == count  # muted: nothing starts


def test_player_reports_missing_samples(tmp_path):
    from entity_designer import fx_audio

    player = fx_audio.FxSoundPlayer(resolver=lambda name: None,
                                    voice_factory=_FakeVoice)
    player.step([fx.SoundEvent(0, None, _snd("nope.wav"), "hit")], -1, 10)
    assert player.missing == {"nope.wav"}


def test_panel_has_a_mute_button_and_plays_sounds_while_running(app, tmp_path):
    from entity_designer.fx_panel import DestructionFxPanel

    _wav_file(tmp_path)
    voices = []
    panel = DestructionFxPanel()
    panel.sound.resolver = lambda name: tmp_path / "tone.wav"
    panel.sound._voice_factory = lambda: voices.append(_FakeVoice()) or voices[-1]
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True,
                         sounds={"launch": _snd("tone.wav")})
    panel.set_context(fx.CONTEXT_WEAPON, fx.FxDocument(), unit=unit)
    assert panel.mute_button.isCheckable() and not panel.mute_button.isChecked()
    # The first clock step after a start from 0 fires the launch sound.
    panel._advance_sound(10.0)
    assert len(voices) == 1
    panel.seek(0.0)
    assert voices[0].stopped  # a jump in time silences everything
    panel.mute_button.setChecked(True)
    assert panel.sound.muted
    panel._advance_sound(10.0)
    assert len(voices) == 1


# -- weapon clusters --------------------------------------------------------------------------

CLUSTER_PARENT = (
    "new_weapon 28\n"
    "    vp_normal = 113\n    vp_dead = 22\n    start_speed = 100\n"
    "    cluster_enable = 1\n    cluster_count = 2_5\n    cluster_weapon_id = 29\n"
    "    cluster_trigger_time = 600\n    cluster_spread_xy = 10_10\n"
    "    cluster_vp = 112\n    cluster_generations = 1\n"
    "    snd_cluster_sample = sounds/Smex02.wav\n    snd_cluster_pitch = 0\n"
    "    snd_cluster_volume = 120\n"
    "end\n")
CLUSTER_CHILD = (
    "new_weapon 29\n"
    "    vp_normal = 130\n    vp_dead = 23\n    start_speed = 100\n"
    "    snd_launch_sample = sounds/child.wav\n"
    "begin_fx\n    trigger = impact_world\n    mode = visual\n"
    "    duration = 300\n    vp_model = 77\nend\n"
    "end\n")


def _cluster_weapons(parent_text=CLUSTER_PARENT):
    return {28: fx.weapon_def_from_text(28, parent_text),
            29: fx.weapon_def_from_text(29, CLUSTER_CHILD)}


def _cluster_simulation(weapons=None, trigger=fx.WEAPON_HIT_UNIT, parent=28):
    weapons = _cluster_weapons() if weapons is None else weapons
    root = weapons[parent]
    return fx.PreviewSimulation(root.document, fx.CONTEXT_WEAPON, trigger,
                                unit=root.unit, weapons=weapons)


def test_cluster_keys_are_read_like_the_engine():
    cluster = fx.weapon_def_from_text(28, CLUSTER_PARENT).unit.cluster
    assert cluster.weapon_id == 29 and cluster.count == (2, 5)
    assert cluster.trigger_time == 600 and cluster.generations == 1
    assert cluster.spread == (10.0, 10.0) and cluster.visual.vp == 112
    assert cluster.sound.sample == "sounds/Smex02.wav" and cluster.sound.volume == 120
    assert fx.parse_cluster({"cluster_enable": "0", "cluster_count": "2",
                             "cluster_weapon_id": "3"}) is None
    assert fx.parse_cluster({"cluster_enable": "1", "cluster_count": "0",
                             "cluster_weapon_id": "3"}) is None
    assert fx.parse_cluster({"cluster_enable": "1", "cluster_count": "2"}) is None
    # A trigger range is not a time: the split then happens on impact.
    ranged = fx.parse_cluster({"cluster_enable": "1", "cluster_count": "3",
                               "cluster_weapon_id": "3", "cluster_trigger_time": "100_200"})
    assert ranged.trigger_time == 0
    assert fx.parse_cluster({"cluster_enable": "1", "cluster_count": "99",
                             "cluster_weapon_id": "3"}).count == (64, 64)


def test_cluster_replaces_the_parent_with_its_children_at_the_trigger_time():
    simulation = _cluster_simulation()
    items = simulation.items_at(500)
    assert [i.vp for i in items if i.kind == "unit"] == [113]
    after = simulation.items_at(700)
    units = [i for i in after if i.kind == "unit"]
    assert 2 <= len(units) <= 5 and {i.vp for i in units} == {130}
    flash = [i for i in after if i.kind == "cluster"]
    assert len(flash) == 1 and flash[0].vp == 112
    assert simulation.tracks[0].end == pytest.approx(600)  # the parent stopped
    assert any(t.label.startswith("Cluster") for t in simulation.tracks)
    # No impact effects for the parent: only the children detonate later.
    assert not [i for i in simulation.items_at(650) if i.kind == "visual"]
    ends = [e.start for e in simulation.sound_events if e.event == "cluster"]
    assert ends == [600]


def test_cluster_starts_from_the_real_path_not_from_the_chaos_offset():
    # chaos/spiral only move the drawing; like the engine's _position, the flash
    # and the children start from the path point the weapon really reached.
    chaotic = CLUSTER_PARENT.replace(
        "start_speed = 100", "start_speed = 100\n    chaos_factor = 2.5\n    chaos_radius = 30")
    plain = _cluster_simulation()
    moved = _cluster_simulation(_cluster_weapons(chaotic))
    flash_plain = [i for i in plain.items_at(700) if i.kind == "cluster"][0]
    flash_moved = [i for i in moved.items_at(700) if i.kind == "cluster"][0]
    assert flash_moved.position == pytest.approx(flash_plain.position)


def test_cluster_children_fall_to_the_ground_with_their_own_effects():
    simulation = _cluster_simulation(trigger=fx.WEAPON_HIT_GROUND)
    last = max(t.end for t in simulation.tracks if t.label.startswith("Cluster"))
    landed = simulation.items_at(last + 5)
    assert [i.vp for i in landed if i.kind == "visual"] and \
        {i.vp for i in landed if i.kind == "visual"} == {77}
    assert all(abs(i.position[1]) < 1e-6 for i in landed if i.kind == "visual")
    launches = [e for e in simulation.sound_events if e.event == "launch"]
    # The parent has no launch sound: every child (2 to 5) launches at the split.
    assert 2 <= len(launches) <= 5
    assert all(e.start == pytest.approx(600) for e in launches)


def test_cluster_generations_limit_how_often_children_split_again():
    # The children are the same weapon and split 150 ms after they are born.
    self_splitting = CLUSTER_PARENT.replace(
        "cluster_weapon_id = 29", "cluster_weapon_id = 28").replace(
        "cluster_trigger_time = 600", "cluster_trigger_time = 150")
    weapons = _cluster_weapons(self_splitting)
    # generations = 1: the children of the parent do not split again.
    simulation = _cluster_simulation(weapons, parent=28)
    assert len([t for t in simulation.tracks if t.label.startswith("Cluster")]) == 1
    deeper = _cluster_weapons(self_splitting.replace("generations = 1", "generations = 2"))
    simulation = _cluster_simulation(deeper, parent=28)
    assert len([t for t in simulation.tracks if t.label.startswith("Cluster")]) > 1
    assert sum(1 for e in simulation._events if e[1] == "unit") <= 400


def test_missing_cluster_weapon_is_reported_and_the_parent_impacts_normally():
    simulation = _cluster_simulation({28: fx.weapon_def_from_text(28, CLUSTER_PARENT)})
    assert any("cluster_weapon_id 29" in note for note in simulation.notes)
    assert [i.kind for i in simulation.items_at(100) if i.kind == "unit"] == ["unit"]


def test_cluster_models_are_loaded_with_the_weapon():
    weapons = _cluster_weapons()
    root = weapons[28]
    vps = fx.weapon_vps(root.unit, root.document, weapons)
    assert {113, 22, 112, 130, 23, 77} <= vps
    assert [w.weapon_id for w in fx.cluster_chain(root.unit, weapons)] == [29]


def test_spread_and_angles_shape_the_child_directions():
    weapons = _cluster_weapons(CLUSTER_PARENT.replace(
        "cluster_spread_xy = 10_10",
        "cluster_horizontal_angle = 0_360\n    cluster_vertical_angle = 30_30\n"
        "    cluster_spread_xy = 0_0")
        .replace("cluster_count = 2_5", "cluster_count = 4"))
    simulation = _cluster_simulation(weapons)
    units = [i for i in simulation.items_at(700) if i.kind == "unit"]
    assert len(units) == 4
    # Four evenly spaced yaw angles fan the children out on both sides.
    spots = {tuple(round(v, 1) + 0.0 for v in i.position) for i in units}
    xs = sorted(spot[0] for spot in spots)
    assert len(spots) == 4 and xs[0] < 0 < xs[-1]


# -- delay_time, debuffs, laser, artillery, palette, shake ------------------------------------

DEBUFF_TEXT = (
    "new_debuff 2\n"
    "    debuff_vp = 191_191\n    debuff_tint = 0_255_0_255\n    debuff_scale = 4.0\n"
    "    debuff_random_max_offset = 90%\n    debuff_tick_time = 2000\n"
    "    debuff_duration = 900000\n    snd_debuff_sample = sounds/zap.wav\n"
    "end\n"
    "new_debuff 3\n    debuff_duration = 4000\nend\n")


def test_debuff_profiles_are_read_from_script_text():
    profiles = fx.debuff_profiles_from_text(DEBUFF_TEXT)
    assert sorted(profiles) == [2, 3]
    spec = profiles[2]
    assert spec.vps == (191, 191) and spec.scale == 4.0 and spec.tint == (0, 255, 0, 255)
    assert spec.max_offset == 90.0 and spec.offset_percent and spec.tick_time == 2000
    assert spec.sound.sample == "sounds/zap.wav"
    assert profiles[3].vps == ()


def test_delay_time_sticks_the_weapon_and_detonates_later_with_a_debuff():
    weapon = (
        "new_weapon 4\n    vp_normal = 60\n    vp_dead = 61\n    delay_time = 5000\n"
        "    delay_time_debuff_id = 2\n"
        "begin_fx\n    trigger = detonate\n    mode = visual\n    duration = 300\n"
        "    vp_model = 77\nend\nend\n")
    definition = fx.weapon_def_from_text(4, weapon)
    assert definition.unit.delay_time == 5000 and definition.unit.delay_debuff_id == 2
    simulation = fx.PreviewSimulation(
        definition.document, fx.CONTEXT_WEAPON, fx.WEAPON_HIT_UNIT,
        unit=definition.unit, debuffs=fx.debuff_profiles_from_text(DEBUFF_TEXT))
    flight = fx.WEAPON_FLIGHT_MS
    stuck = [i for i in simulation.items_at(flight + 2000) if i.kind == "unit"]
    assert stuck and stuck[0].vp == 60 and stuck[0].position[1] == 0.0
    assert not [i for i in simulation.items_at(flight + 2000) if i.kind == "visual"]
    blast = [i for i in simulation.items_at(flight + 5100) if i.kind == "visual"]
    assert [i.vp for i in blast] == [77]
    # The debuff shows on the first hit, then again at every tick (2 s).
    first = [i for i in simulation.items_at(flight + 100) if i.kind == "debuff"]
    later = [i for i in simulation.items_at(flight + 2100) if i.kind == "debuff"]
    assert len(first) == 2 and first[0].scale == (4.0, 4.0, 4.0)
    assert first[0].tint == (0, 255, 0, 255) and later
    assert any("delay_time" in note for note in simulation.notes)
    assert 191 in fx.weapon_vps(definition.unit, definition.document, {},
                                None, fx.debuff_profiles_from_text(DEBUFF_TEXT))


def test_laser_draws_a_beam_of_models_from_the_muzzle_to_the_contact():
    laser = fx.weapon_def_from_text(125, (
        "new_weapon 125\n    model = laser\n    vp_normal = 60\n"
        "    laser_spacing = 100\nend\n"))
    assert laser.unit.laser.spacing == 100
    unit = __import__("dataclasses").replace(laser.unit, altitude=1000)
    simulation = _weapon_simulation(unit)
    beam = [i for i in simulation.items_at(100) if i.kind == "laser"]
    assert len(beam) == 10 and all(i.vp == 60 for i in beam)
    ys = sorted(i.position[1] for i in beam)
    assert ys[0] == pytest.approx(-950) and ys[-1] == pytest.approx(-50)
    assert not [i for i in simulation.items_at(100) if i.kind == "unit"]


def test_mesh_laser_is_one_stretched_beam_with_a_glow():
    laser = fx.weapon_def_from_text(125, (
        "new_weapon 125\n    model = laser\n    vp_normal = 60\n"
        "    laser_mesh_enable = 1\n    laser_mesh_path = Data/Models/3ds/laser.3ds\n"
        "    laser_mesh_size_xy = 8_0\n    laser_mesh_glow_rate = 5\n"
        "    laser_mesh_tint = 10_20_30_200\nend\n"))
    simulation = _weapon_simulation(__import__("dataclasses").replace(laser.unit, altitude=800))
    pieces = [i for i in simulation.items_at(100) if i.kind == "tracer"]
    body = [i for i in pieces if not i.additive]
    assert len(body) == 1 and body[0].scale == pytest.approx((8.0, 8.0, 800.0))
    assert body[0].tint == (10, 20, 30, 200) and body[0].external == ("Data/Models/3ds/laser.3ds",)
    assert [i for i in pieces if i.additive][0].alpha == pytest.approx(0.5)


def test_artillery_barrage_falls_from_its_arc_height_at_the_shell_speed():
    shell = fx.weapon_def_from_text(118, (
        "new_weapon 118\n    model = artillery_shell\n"
        "    artillery_shell_mode = vertical_barrage\n"
        "    artillery_shell_arc_height = 3300\n    artillery_shell_speed = 1800\n"
        "    vp_normal = 60\nend\n"))
    assert shell.unit.altitude == 3000 and shell.unit.fixed_speed == 1800
    simulation = _weapon_simulation(shell.unit)
    assert simulation.tracks[0].end == pytest.approx(3000 / 1.8)


def test_palette_and_shake_packages_run_for_their_time():
    values = {
        "pal_launch_slot": "8", "pal_launch_mag0": "1", "pal_launch_mag1": "0",
        "pal_launch_time": "200", "shk_hit_slot": "2", "shk_hit_mag0": "2",
        "shk_hit_mag1": "2", "shk_hit_time": "100", "shk_hit_x": "0.1"}
    specs = fx.sound_specs(values, fx.WEAPON_SOUND_EVENTS)
    assert specs["launch"].sample == "" and specs["launch"].palette.slot == 8
    # The view shake is part of the preview: a package without sample or palette.
    assert specs["hit"].sample == "" and specs["hit"].palette is None
    assert specs["hit"].shake == fx.ShakeFx(
        slot=2, mag0=2.0, mag1=2.0, time=100, pos=(0.1, 0.2, 0.2))
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, sounds=specs)
    simulation = _weapon_simulation(unit)
    assert simulation.palette_effects_at(100) == [(8, pytest.approx(0.5))]
    assert simulation.palette_effects_at(250) == []
    # Pitching a palette-only package never tries to play a file.
    assert not [e for e in simulation.sound_events if e.spec.sample]


def test_palette_colors_follow_the_engine_formula():
    # No effect: nothing to apply.
    assert fx.palette_colors([]) is None
    norm, inv = fx.palette_colors([(1, 0.5)])
    # Half red slot plus the remaining half of the neutral slot 0.
    assert norm == pytest.approx((0.5 * 1.21 + 0.5, 0.5, 0.5 * 0.29 + 0.5))
    assert inv == (0.0, 0.0, 0.0)
    norm, inv = fx.palette_colors([(4, 1.0)])
    assert norm == (0.0, 0.0, 0.0) and inv == (1.0, 1.0, 1.0)  # inverse slot
    # A custom slot only counts when OpenNeoUA.ini defines it.
    assert fx.palette_colors([(9, 1.0)], {9: (1.0, 0.4, 0.03)})[0] == pytest.approx((1.0, 0.4, 0.03))
    assert fx.palette_colors([(9, 0.5)])[0] == pytest.approx((1.0, 1.0, 1.0))
    custom, powers = fx.parse_color_settings(
        "gfx.color_eff[9]  = 255_102_7 ; Orange\ngfx.color_eff_pwr[2] = 50\n"
        "gfx.color_eff[3] = 1_2_3\n")
    assert custom == {9: pytest.approx((1.0, 0.4, 7 / 255))} and powers == {2: 0.5}


def test_palette_is_applied_like_the_engine_shader(app):
    from PySide6.QtGui import QColor, QImage
    from entity_designer import fx_viewport

    image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    image.fill(QColor(100, 200, 50))
    flashed = fx_viewport.apply_palette(image, (1.0, 0.0, 2.0), (0.0, 0.5, 0.0))
    pixel = flashed.pixelColor(1, 1)
    assert pixel.red() == 100 and pixel.blue() == 100
    assert abs(pixel.green() - (1 - 200 / 255) * 0.5 * 255) <= 1


def test_palette_mute_button_follows_the_sound_button_and_switches_flashes_off(app):
    from entity_designer.fx_panel import DestructionFxPanel

    panel = DestructionFxPanel()
    specs = fx.sound_specs({"pal_launch_slot": "1", "pal_launch_mag0": "1",
                            "pal_launch_time": "500"}, fx.WEAPON_SOUND_EVENTS)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, sounds=specs)
    panel.set_context(fx.CONTEXT_WEAPON, fx.FxDocument(), unit=unit)
    panel.seek(100)
    assert panel.palette_effects()
    layout = panel.mute_button.parentWidget().layout().itemAt(0).layout()
    # The view-shake button sits between the sound and the palette buttons.
    assert layout.indexOf(panel.shake_button) == layout.indexOf(panel.mute_button) + 1
    assert layout.indexOf(panel.pal_button) == layout.indexOf(panel.shake_button) + 1
    panel.pal_button.setChecked(True)
    assert panel.palette_effects() == []
    # The slash of the switched-off icon is white, like the other icons.
    image = panel.pal_button.icon().pixmap(20, 20).toImage()
    assert image.pixelColor(10, 10).red() > 200 and image.pixelColor(10, 10).blue() > 200


def test_speed_changes_the_pitch_of_the_sounds(tmp_path):
    import wave

    from entity_designer import fx_audio

    _wav_file(tmp_path)
    voices = []

    def factory():
        voices.append(_FakeVoice())
        return voices[-1]

    player = fx_audio.FxSoundPlayer(resolver=lambda name: tmp_path / "tone.wav",
                                    voice_factory=factory)
    shot = fx.SoundEvent(0, None, _snd("tone.wav"), "hit", 0)
    player.set_speed(0.5)
    player.step([shot], -1, 10)
    with wave.open(str(voices[-1].played[0])) as copy:
        assert copy.getframerate() == 11025  # half the speed, half the rate
    player.set_speed(2.0)
    assert voices[-1].stopped
    player.step([shot], -1, 10)
    with wave.open(str(voices[-1].played[0])) as copy:
        assert copy.getframerate() == 44100


def test_very_slow_sounds_are_stretched_to_a_rate_the_devices_play(tmp_path):
    import wave

    from entity_designer import fx_audio

    _wav_file(tmp_path, frames=200)
    voices = []

    def factory():
        voices.append(_FakeVoice())
        return voices[-1]

    player = fx_audio.FxSoundPlayer(resolver=lambda name: tmp_path / "tone.wav",
                                    voice_factory=factory)
    player.set_speed(0.1)  # 22050 Hz becomes 2205 Hz: too low to be heard
    player.step([fx.SoundEvent(0, None, _snd("tone.wav"), "hit", 0)], -1, 10)
    with wave.open(str(voices[-1].played[0])) as copy:
        assert copy.getframerate() == fx_audio.STANDARD_RATE
        # 200 frames at 2205 Hz last as long as 4000 frames at 44100 Hz.
        assert copy.getnframes() == 4000


# -- grid clipping ------------------------------------------------------------------------

def test_near_plane_clipping_keeps_only_what_is_in_front_of_the_camera():
    from entity_designer import viewport

    near = viewport._NEAR_CAMERA_Z
    kept = viewport._near_clip_segment((0, 0, near - 1), (0, 0, near + 1))
    assert kept[0] == (0, 0, near - 1) and kept[1][2] == pytest.approx(near)
    assert viewport._near_clip_segment((0, 0, near + 1), (1, 0, near + 2)) is None
    square = [(-1, 0, near - 1), (1, 0, near - 1), (1, 0, near + 1),
              (-1, 0, near + 1)]
    clipped = viewport._near_clip_polygon(square)
    assert len(clipped) == 4 and all(point[2] <= near + 1e-9 for point in clipped)
    assert viewport._near_clip_polygon([(0, 0, near + 1)] * 3) == []


# -- workspace -------------------------------------------------------------------------

@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_preview_tab_shows_effects_and_reloads_from_file(app, tmp_path):
    from entity_designer.editor import CollisionEditorWindow
    from entity_designer.script_model import VehicleModelReference

    script = tmp_path / "Vehicles.cfg"
    script.write_text(UNIT, encoding="utf-8")
    window = CollisionEditorWindow()
    try:
        block = find_script_blocks(UNIT)[0]
        reference = VehicleModelReference(block=block, vp_normal=10)
        window._active_script_path = script
        window._active_script_kind = "new_vehicle"
        window._active_script_id = 5
        window._active_model_reference = reference
        window._load_fx_context(UNIT, block, reference)
        panel = window.fx_panel
        assert _effect_rows(panel) == 3
        assert panel.unit.flies and panel.unit.altitude == fx.DEFAULT_FLIGHT_ALTITUDE
        assert panel.unit.dead.base_path == "Data/Models/Base/WRECK.BASE"
        # Hide one effect: the preview keeps only the others.
        _item(panel, 1).setCheckState(Qt.CheckState.Unchecked)
        assert 1 in panel.hidden_items
        script.write_text(UNIT.replace("duration   = 800", "duration   = 1600"),
                          encoding="utf-8")
        window.reload_fx_from_file()
        block_spec = fx.interpret_block(panel.document.items[1], fx.CONTEXT_VEHICLE)
        assert block_spec.duration == 1600
        assert 1 in panel.hidden_items
        window.properties_tabs.setCurrentIndex(window.fx_tab_index)
        assert window.viewport.fx_preview_active
        assert not window.fx_panel.transport_bar.isHidden()
        window.properties_tabs.setCurrentIndex(window.collision_tab_index)
        assert not window.viewport.fx_preview_active
        assert window.fx_panel.transport_bar.isHidden()
    finally:
        window._modified = False
        window.close()
