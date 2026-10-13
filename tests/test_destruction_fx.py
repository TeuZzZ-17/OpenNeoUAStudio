"""Destruction FX preview: engine rules, death sequences, particles, reload."""

import math

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer.editor import find_script_blocks

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
                 "mid_size = 4\nend_size = 2\nduration = 900\nfade_out = 300\n"
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


def test_weapon_flies_to_the_target_and_explodes():
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
    flying = next(item for item in on_unit.items_at(impact / 2) if item.kind == "unit")
    assert flying.vp == 60 and flying.position != (0.0, 0.0, 0.0)
    after = on_unit.items_at(impact + 10)
    assert _units(on_unit, impact + 10) == [61]
    assert [item.vp for item in after if item.kind == "visual"] == [50]
    on_ground = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON,
                                     fx.WEAPON_HIT_GROUND, unit=unit)
    assert _units(on_ground, impact + 10) == [62]
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


# -- workspace -------------------------------------------------------------------------

@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_preview_tab_shows_effects_and_reloads_from_file(app, tmp_path):
    from entity_designer.editor import CollisionEditorWindow, VehicleModelReference

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
        assert panel.effects_list.count() == 3
        assert panel.unit.flies and panel.unit.altitude == fx.DEFAULT_FLIGHT_ALTITUDE
        assert panel.unit.dead.base_path == "Data/Models/Base/WRECK.BASE"
        # Hide one effect: the preview keeps only the others.
        panel.effects_list.item(1).setCheckState(Qt.CheckState.Unchecked)
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
