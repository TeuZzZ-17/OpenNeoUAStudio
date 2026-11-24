"""disable_genesis and air_dead: the Death behavior row of the Visuals && FX tab."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer import fx_compat, fx_visuals as visuals
from entity_designer.fx_panel import DestructionFxPanel
from entity_designer.script_model import find_script_blocks

VEHICLE_SCRIPT = (
    "new_vehicle 5\n"
    "    model = plane\n"
    "    vp_normal = 10\n"
    "    vp_genesis = 5\n"
    "end\n")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _block(text, kind="new_vehicle"):
    return next(b for b in find_script_blocks(text) if b.kind == kind)


# -- fx_visuals: parsing, validation and saving --------------------------------------------

def test_values_are_checked():
    assert visuals.normalize_value("air_dead", "1") == "1"
    assert visuals.normalize_value("air_dead", "0") == ""
    assert visuals.normalize_value("disable_genesis", "1") == "1"
    assert visuals.normalize_value("disable_genesis", "At_Death") == "at_death"
    assert visuals.normalize_value("disable_genesis", "0") == ""
    # Every boolean form of the engine is accepted and written back as the script form 0/1.
    assert visuals.normalize_value("air_dead", "yes") == "1"
    assert visuals.normalize_value("air_dead", "off") == ""
    assert visuals.normalize_value("disable_genesis", "true") == "1"
    for key, bad in (("air_dead", "maybe"), ("disable_genesis", "later")):
        with pytest.raises(ValueError):
            visuals.normalize_value(key, bad)


def test_parse_death_text_rejects_foreign_keys():
    assert visuals.parse_death_text("air_dead = 1\ndisable_genesis = at_death") == {
        "air_dead": "1", "disable_genesis": "at_death"}
    with pytest.raises(ValueError, match="only disable_genesis, air_dead"):
        visuals.parse_death_text("vp_normal = 1")


def test_death_summary():
    assert visuals.death_summary({}) == "Not set"
    assert visuals.death_summary({"air_dead": "1"}) == "Air dead"
    assert visuals.death_summary({"disable_genesis": "at_death"}) == "No genesis (at_death)"


def test_save_writes_both_keys_and_vehicle_only_restriction(tmp_path):
    path = tmp_path / "Vehicles.cfg"
    path.write_bytes(VEHICLE_SCRIPT.encode("utf-8"))
    block = _block(VEHICLE_SCRIPT)
    origins = [(path, block.start_line, block.end_line)]
    snapshot = {path: path.read_bytes()}
    visuals.save_values(origins, {"air_dead": "1", "disable_genesis": "at_death"}, snapshot)
    text = path.read_text(encoding="utf-8")
    assert "air_dead = 1" in text and "disable_genesis = at_death" in text
    values = visuals.read_effective_values(origins)
    assert values["air_dead"] == "1" and values["disable_genesis"] == "at_death"

    weapon_script = "new_weapon 6\n    vp_normal = 20\nend\n"
    wpath = tmp_path / "Weapons.cfg"
    wpath.write_bytes(weapon_script.encode("utf-8"))
    wblock = _block(weapon_script, "new_weapon")
    worigins = [(wpath, wblock.start_line, wblock.end_line)]
    with pytest.raises(ValueError, match="only supported for vehicles"):
        visuals.save_values(worigins, {"air_dead": "1"}, {wpath: wpath.read_bytes()})


def test_death_level_is_openneoua_only():
    assert fx_compat.death_level({"air_dead": "1"}) == fx_compat.OPENNEOUA


# -- destruction_fx: reading the keys and the preview simulation ---------------------------

def test_unit_look_reads_air_dead_and_disable_genesis_for_vehicles_only():
    values = {"air_dead": "1", "disable_genesis": "At_Death"}
    assert fx.unit_look(values, weapon=False)["air_dead"] is True
    assert fx.unit_look(values, weapon=False)["disable_genesis"] == "at_death"
    assert fx.unit_look(values, weapon=True)["air_dead"] is False
    assert fx.unit_look(values, weapon=True)["disable_genesis"] == ""
    assert fx.unit_look({}, weapon=False)["air_dead"] is False
    assert fx.unit_look({}, weapon=False)["disable_genesis"] == ""


def test_air_dead_flyer_dies_in_place_with_no_falling_wreck():
    unit = fx.UnitPhases(normal=fx.VisualRef(10), megadeth=fx.VisualRef(12),
                         model="plane", altitude=300.0, air_dead=True)
    assert unit.skips_dead_phase
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    death = fx.SEQUENCE_PREROLL_MS
    mega = next(item for item in simulation.items_at(death + 1) if item.kind == "unit")
    assert mega.vp == 12
    assert mega.position[1] == pytest.approx(-300.0)
    assert any("air_dead" in note for note in simulation.notes)


def test_air_dead_still_plays_the_configured_air_destroyed_effect():
    """Regression: air_dead only skips the falling wreck, not the death FX.

    The real engine fires air_destroyed/dest_fx death from SetStateInternal
    the instant energy reaches 0 (ypabact.cpp), unconditionally on air_dead;
    only the later falling-wreck time is cut short.
    """
    from entity_designer.script_model import find_script_blocks

    script = (
        "new_vehicle 5\n"
        "    model = heli\n"
        "    vp_normal = 10\n"
        "    vp_megadeth = 12\n"
        "    air_dead = 1\n"
        "begin_fx\n"
        "    trigger    = air_destroyed\n"
        "    mode       = visual\n"
        "    duration   = 500\n"
        "    vp_model   = 290\n"
        "end\n"
        "end\n")
    block = next(b for b in find_script_blocks(script) if b.object_id == 5)
    document = fx.extract_fx_document(script, block.start_line, block.end_line)
    unit = fx.UnitPhases(normal=fx.VisualRef(10), megadeth=fx.VisualRef(12),
                         model="heli", altitude=300.0, air_dead=True)
    simulation = fx.PreviewSimulation(
        document, fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    moment = fx.SEQUENCE_PREROLL_MS + 10
    assert any(item.vp == 290 for item in simulation.items_at(moment))


def test_air_dead_is_ignored_on_ground_vehicles_and_without_the_flag():
    grounded = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                             model="tank", air_dead=True)
    assert grounded.skips_dead_phase  # already true from the tank/car engine rule
    flying = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                           model="plane", altitude=300.0, air_dead=False)
    assert not flying.skips_dead_phase


@pytest.mark.parametrize("disable_genesis", ["1", "at_death"])
def test_disable_genesis_removes_the_death_plasma(disable_genesis):
    unit = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                         megadeth=fx.VisualRef(12), model="tank",
                         genesis=fx.VisualRef(5), energy=20000,
                         disable_genesis=disable_genesis)
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    land = fx.SEQUENCE_PREROLL_MS
    assert not [i for i in simulation.items_at(land + 1) if i.kind == "plasma"]
    assert any("disable_genesis" in note for note in simulation.notes)


def test_disable_genesis_off_keeps_the_death_plasma():
    unit = fx.UnitPhases(normal=fx.VisualRef(10), dead=fx.VisualRef(11),
                         megadeth=fx.VisualRef(12), model="tank",
                         genesis=fx.VisualRef(5), energy=20000, disable_genesis="")
    simulation = fx.PreviewSimulation(
        fx.FxDocument(), fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER, unit=unit)
    land = fx.SEQUENCE_PREROLL_MS
    assert [i for i in simulation.items_at(land + 1) if i.kind == "plasma"]


# -- the Visuals && FX panel: the Death behavior row shows up and saves --------------------

def _rows(panel):
    return {panel.effects_list.item(i).data(Qt.ItemDataRole.UserRole): panel.effects_list.item(i)
            for i in range(panel.effects_list.count())}


def test_death_behavior_row_in_the_panel(app, tmp_path):
    path = tmp_path / "Vehicles.cfg"
    path.write_text(VEHICLE_SCRIPT, encoding="utf-8")
    block = _block(VEHICLE_SCRIPT)
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_VEHICLE,
                          fx.extract_fx_document(VEHICLE_SCRIPT, block.start_line, block.end_line))
        panel.set_script_sources([(path, block.start_line, block.end_line)])
        assert visuals.DEATH_INDEX not in _rows(panel)
        panel.add_effect()
        drawer = panel.code_drawer
        drawer.code.setPlainText("air_dead = 1\ndisable_genesis = at_death")
        assert drawer._apply()
        row = _rows(panel)[visuals.DEATH_INDEX]
        assert "Air dead" in row.text() and "No genesis (at_death)" in row.text()
        assert panel.save_rows(None)
        text = path.read_text(encoding="utf-8")
        assert "air_dead = 1" in text and "disable_genesis = at_death" in text
    finally:
        panel.close()
