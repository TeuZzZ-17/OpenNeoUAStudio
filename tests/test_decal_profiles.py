"""Reusable ground decal profiles (new_decal) and their preview in Destruction FX."""

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from entity_designer import destruction_fx as fx

PROFILE = (
    "new_decal 1\n"
    "    name = destroyed\n"
    "    texture = Interface/GroundDecals/impact_01.png\n"
    "    size_by_model_% = 150_200\n"
    "    stretch_xz = 0.8~1.2_0.9\n"
    "    tint = 255_255_255_180\n"
    "    random_rotation = 1\n"
    "    edge_fade = 5_10\n"
    "    duration = permanent\n"
    "    fade_inout = 100_1000\n"
    "    smoke_count = 30_10\n"
    "end\n"
)


def _profiles(text: str = PROFILE):
    return fx.decal_profiles_from_text(text)


def _profile(body: str) -> fx.DecalProfile | None:
    return _profiles(f"new_decal 1\n{body}end\n").get(1)


# -- size rule ----------------------------------------------------------------------------

def test_size_is_a_percentage_of_the_model_diameter():
    profile = _profile("    duration = permanent\n    size_by_model_% = 150_200\n")
    assert profile.size_for(100) == pytest.approx((150, 200))     # up to twice the unit
    assert profile.size_for(40) == pytest.approx((60, 80))


def test_missing_size_key_keeps_the_model_size():
    profile = _profile("    duration = permanent\n")
    assert profile.model_percent == (100.0, 100.0)
    assert profile.size_for(80) == (80, 80)


@pytest.mark.parametrize("diameter", [0, -5, float("nan"), float("inf")])
def test_no_model_means_no_decal_size(diameter):
    assert _profile("    duration = permanent\n").size_for(diameter) == (0.0, 0.0)


@pytest.mark.parametrize("written", ["30", "30%", "30_40", "30%_40%", "30_40%", "30%_40"])
def test_size_by_model_accepts_optional_percent_signs(written):
    profile = _profile(f"    duration = permanent\n    size_by_model_% = {written}\n")
    low, _, high = written.replace("%", "").partition("_")
    assert profile.model_percent == (float(low), float(high or low))
    assert not profile.spec.issues


def test_invalid_size_by_model_keeps_the_default_and_warns():
    profile = _profile("    duration = permanent\n    size_by_model_% = abc\n")
    assert profile.model_percent == (100.0, 100.0)
    assert any("size_by_model_%" in issue.message for issue in profile.spec.issues)


def test_the_old_size_keys_do_nothing_any_more():
    body = ("    duration = permanent\n    size = 200_210\n    size_explosion_% = 10_20\n"
            "    size_silhouette = 300\n    size_min = 99\n")
    profile = _profile(body)
    assert profile.model_percent == (100.0, 100.0)
    assert profile.size_for(50) == (50, 50)


# -- parser -------------------------------------------------------------------------------

def test_profile_parser_reads_every_key():
    profile = _profiles()[1]
    spec = profile.spec
    assert profile.name == "destroyed" and profile.model_percent == (150, 200)
    assert spec.permanent and spec.fade_in == 100 and spec.fade_out == 0
    assert spec.stretch_x == (0.8, 1.2) and spec.stretch_z == (0.9, 0.9)
    assert spec.edge_fade == (5, 10) and spec.random_rotation
    assert spec.texture == "Interface/GroundDecals/impact_01.png"
    assert spec.decal_tint == (255, 255, 255, 180)
    assert spec.smoke is not None and spec.smoke.count == (10, 30)


def test_invalid_profiles_are_dropped():
    assert _profile("    texture = x.png\n") is None                      # no duration
    assert _profile("    duration = permanent\n    tint = 1_2_3_0\n") is None  # clear tint
    assert _profile("    duration = 0\n") is None
    assert _profile("    duration = 12ms\n") is None
    assert _profiles("new_decal\n    duration = permanent\nend\n") == {}


def test_timed_decal_fades_never_exceed_the_duration():
    spec = _profile("    duration = 5000\n    fade_inout = 9000_9000\n").spec
    assert not spec.permanent and spec.duration == 5000
    assert (spec.fade_in, spec.fade_out) == (0, 5000)


def test_a_later_definition_replaces_the_previous_one():
    profiles = _profiles("new_decal 2\n    size_by_model_% = 5\n    duration = 100\nend\n"
                         "new_decal 2\n    size_by_model_% = 10\n    duration = 100\nend\n")
    assert profiles[2].model_percent == (10, 10)


def test_shipped_ground_decals_script_loads():
    path = Path(__file__).resolve().parents[3] / "UA Rising" / "Data" / "Scripts" / "Ground_Decals.cfg"
    if not path.is_file():
        pytest.skip("game scripts are not next to Studio")
    profile = _profiles(path.read_text(encoding="utf-8-sig"))[1]
    assert profile.model_percent == (150, 200)
    assert profile.spec.smoke is not None


def test_begin_fx_ground_decal_mode_is_gone():
    text = ("new_vehicle 1\nbegin_fx\n    trigger = destroyed\n    mode = ground_decal\n"
            "    size = 50\n    duration = 1000\nend\nend\n")
    document = fx.extract_fx_document(text, 0, text.count("\n") - 1)
    spec = fx.interpret_block(document.items[0], fx.CONTEXT_VEHICLE)
    assert not spec.active
    assert any("Unknown mode" in issue.message for issue in spec.issues)


# -- link from the unit -------------------------------------------------------------------

def test_unit_look_reads_the_decal_link():
    for weapon, text in ((False, "new_vehicle 1\n ground_decal_id = 4\nend\n"),
                         (True, "new_weapon 1\n ground_decal_id = 4\nend\n")):
        assert fx.unit_look(fx.top_level_values(text, 0, 2), weapon=weapon)["ground_decal_id"] == 4
    assert fx.unit_look({}, weapon=False)["ground_decal_id"] == -1
    assert fx.unit_look({"ground_decal_id": "-3"}, weapon=True)["ground_decal_id"] == -1


# -- preview ------------------------------------------------------------------------------

def _decal_items(simulation, when):
    return [item for item in simulation.items_at(when) if item.kind == fx.MODE_DECAL]


def test_vehicle_destruction_spawns_the_profile_decal_on_the_ground():
    document = fx.extract_fx_document("new_vehicle 5\nend\n", 0, 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(10), megadeth=fx.VisualRef(12), model="tank",
                         ground_decal_id=1, model_diameter=80)
    profiles = _profiles(PROFILE.replace("stretch_xz = 0.8~1.2_0.9\n", ""))
    simulation = fx.PreviewSimulation(document, fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER,
                                      unit=unit, decals=profiles)
    track = next(t for t in simulation.tracks if t.label.endswith("Ground decal"))
    shown = _decal_items(simulation, track.start + 2000)
    assert len(shown) == 1
    assert shown[0].position[1] == simulation.ground_y
    assert 120 <= shown[0].decal_size[0] <= 160                      # 150-200 % of 80
    assert shown[0].texture == "Interface/GroundDecals/impact_01.png"
    assert not _decal_items(simulation, track.start - 10)


def test_unit_without_a_model_or_a_known_decal_id_shows_nothing():
    document = fx.extract_fx_document("new_vehicle 5\nend\n", 0, 1)
    for decal_id, diameter in ((-1, 80), (7, 80), (1, 0)):
        unit = fx.UnitPhases(normal=fx.VisualRef(10), model="tank", ground_decal_id=decal_id,
                             model_diameter=diameter)
        simulation = fx.PreviewSimulation(document, fx.CONTEXT_VEHICLE, fx.SEQUENCE_TRIGGER,
                                          unit=unit, decals=_profiles())
        assert not any(t.label.endswith("Ground decal") for t in simulation.tracks)


def test_weapon_ground_hit_uses_the_projectile_model_size():
    document = fx.extract_fx_document("new_weapon 9\nend\n", 0, 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, ground_decal_id=1,
                         model_diameter=40)
    profiles = _profiles("new_decal 1\n    size_by_model_% = 500\n    duration = permanent\nend\n")
    simulation = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON, fx.WEAPON_HIT_GROUND,
                                      unit=unit, decals=profiles)
    shown = _decal_items(simulation, fx.WEAPON_FLIGHT_MS + 500)
    assert len(shown) == 1 and shown[0].decal_size == pytest.approx((200, 200))


# -- model sphere measured on the loaded model -----------------------------------------------

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_viewport_measures_the_model_sphere_of_one_owner(app):
    from entity_designer.viewport import CollisionViewport

    viewport = CollisionViewport()
    viewport._faces = [SimpleNamespace(owner="body"), SimpleNamespace(owner="effect")]
    viewport._model_preview_base_faces = [
        ((-10, -5, -2), (10, 5, 2), (0, 0, 0)),
        ((1000, 0, 0), (1000, 10, 0), (1010, 0, 0)),
    ]
    assert viewport.owner_model_sphere_diameter("body", (1, 1, 1)) == pytest.approx(
        2 * math.sqrt(129))
    # Scale is applied per axis before measuring.
    assert viewport.owner_model_sphere_diameter("body", (2, 1, 1)) == pytest.approx(
        2 * math.sqrt(429))
    assert viewport.owner_model_sphere_diameter("missing", (1, 1, 1)) == 0.0


# -- ground_decal_size_by_model_% of the unit's own script ---------------------------------

def test_unit_script_size_replaces_the_profile_size():
    profile = _profile("    duration = permanent\n    size_by_model_% = 150_200\n")
    assert profile.size_for(10) == pytest.approx((15, 20))
    assert profile.size_for(10, (1000, 1300)) == pytest.approx((100, 130))
    assert profile.size_for(0, (1000, 1300)) == (0.0, 0.0)           # still no model, no decal


def test_unit_look_reads_the_size_override():
    values = fx.top_level_values(
        "new_weapon 1\n ground_decal_id = 1\n ground_decal_size_by_model_% = 1000%_1300\nend\n", 0, 3)
    assert fx.unit_look(values, weapon=True)["decal_percent"] == (1000.0, 1300.0)
    assert fx.unit_look({}, weapon=True)["decal_percent"] is None
    assert fx.unit_look({"ground_decal_size_by_model_%": "abc"}, weapon=False)["decal_percent"] is None


def test_preview_uses_the_unit_size():
    document = fx.extract_fx_document("new_weapon 9\nend\n", 0, 1)
    profiles = _profiles("new_decal 1\n    size_by_model_% = 150\n    duration = permanent\nend\n")
    sizes = []
    for percent in (None, (1000.0, 1000.0)):
        unit = fx.UnitPhases(normal=fx.VisualRef(60), weapon=True, ground_decal_id=1,
                             model_diameter=14.4, decal_percent=percent)
        simulation = fx.PreviewSimulation(document, fx.CONTEXT_WEAPON, fx.WEAPON_HIT_GROUND,
                                          unit=unit, decals=profiles)
        sizes.append(_decal_items(simulation, fx.WEAPON_FLIGHT_MS + 500)[0].decal_size[0])
    assert sizes == pytest.approx([21.6, 144.0])
