"""Ground decal smoke: smoke_* keys, engine-exact puff math and the preview timeline."""

import math

import pytest

from entity_designer import decal_smoke as ds
from entity_designer import destruction_fx as fx


def _spec(profile_body: str) -> fx.FxSpec:
    """Decal template of a ``new_decal 1`` profile with this body."""

    return fx.decal_profiles_from_text(f"new_decal 1\n{profile_body}end\n")[1].spec


DECAL = "    duration = permanent\n"

PROFILE = (
    "new_decal 1\n" + DECAL +
    "    smoke_duration = 60000_90000\n    smoke_count = 30\n"
    "    smoke_size = 16_30\n    smoke_intensity = 120\n"
    "end\n")


# -- engine reference ---------------------------------------------------------------------
# Values printed by the real C++ header (world/grounddecal_smoke.h) for an emitter
# of radius 100, seed 0x1234567, 60 s, default settings (count 10_30).

def test_hash_matches_the_engine_bit_for_bit():
    assert ds.hash32(0x1234567, 3, 0x534D4F4B) == 1002249942
    assert ds.hash32(0, 0, 0) == 1927377462
    assert ds.hash32(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF) == 4176881856


ENGINE_PUFFS = {
    (0, 1000): (-19.7084446, -17.9386444, -21.9308243, 30.2423496, 0.6679048, 0.0878696, 0.9178073),
    (5, 1000): (-25.3325291, -142.0146332, 37.7317543, 64.6698074, 0.1478935, 0.8521065, 0.9477074),
    (17, 1000): None,
    (0, 7000): (0.2906466, -43.4514999, 44.0459824, 49.2587814, 0.7071867, 0.2928133, 0.8603628),
    (5, 7000): (-28.9094887, -133.5665588, 9.2239017, 83.99646, 0.2012111, 0.7987889, 1.081212),
    (17, 7000): (3.2145996, -43.122036, 42.7941475, 46.7361603, 0.7052712, 0.2947288, 0.9930639),
    (0, 31000): (28.7202759, -21.0155754, -7.4584165, 33.7296677, 0.8325899, 0.1125865, 1.0290742),
    (5, 31000): (-23.6076889, -99.7742615, 47.541172, 58.525341, 0.4144816, 0.5855184, 1.1152503),
    (17, 31000): (10.5018387, -30.3207054, 3.5244522, 40.6329651, 0.8097205, 0.1902795, 0.9378468),
    (0, 58000): (-8.6480608, -73.5797272, 51.5851097, 49.1875954, 0.465168, 0.534832, 0.9181426),
    (5, 58000): None,
    (17, 58000): None,
}


def test_puffs_match_the_engine_reference_values():
    spec = ds.SmokeSpec(enabled=True)
    count = ds.scaled_count(spec.count[1], 100.0)
    assert count == 50
    for (slot, age), expected in ENGINE_PUFFS.items():
        puff = ds.evaluate(spec, 100.0, 0x1234567, count, 60000, slot, age)
        if expected is None:
            assert puff is None, (slot, age)
            continue
        assert puff is not None, (slot, age)
        x, y, z, size, alpha, phase, shade = expected
        # The engine computes in float32, Studio in double precision.
        assert puff.offset == pytest.approx((x, y, z), abs=0.2), (slot, age)
        assert puff.size == pytest.approx(size, abs=0.2)
        assert puff.alpha == pytest.approx(alpha, abs=0.02)
        assert puff.phase == pytest.approx(phase, abs=0.003)
        assert puff.shade == pytest.approx(shade, abs=0.005)


def test_puff_count_roll_matches_the_engine():
    # smoke_count = 10_30 rolled per decal with the same hash as the engine.
    spec = ds.SmokeSpec(enabled=True)
    for seed, expected in ((0x1234567, 13), (1, 19), (99, 22), (0xABCDEF, 29)):
        assert ds.emitter_count(spec, seed) == expected
    fixed = ds.SmokeSpec(enabled=True, count=(7, 7))
    assert all(ds.emitter_count(fixed, seed) == 7 for seed in range(50))
    rolled = {ds.emitter_count(spec, seed) for seed in range(400)}
    assert min(rolled) == 10 and max(rolled) == 30


def test_count_scales_with_radius_and_stays_bounded():
    assert ds.scaled_count(6, 50.0) == 6
    assert ds.scaled_count(6, 5.0) == 3
    assert ds.scaled_count(6, 500.0) == 18
    assert ds.scaled_count(30, 500.0) == ds.MAX_PUFFS
    assert ds.scaled_count(0, 100.0) == 0
    assert ds.scaled_count(6, 0.0) == 0
    assert ds.scaled_count(6, math.nan) == 0


def test_intensity_fades_in_and_out_exactly_like_the_engine():
    assert ds.intensity(-1, 1000, 100, 100) == 0.0
    assert ds.intensity(1000, 1000, 100, 100) == 0.0
    assert ds.intensity(300, 10000, 600, 5000) == pytest.approx(0.5)
    assert ds.intensity(5000, 10000, 600, 5000) == 1.0
    assert ds.intensity(9000, 10000, 600, 5000) == pytest.approx(0.2)
    assert ds.intensity(0, 10000, 0, 0) == 1.0 and ds.intensity(9999, 10000, 0, 0) == 1.0


# -- script keys --------------------------------------------------------------------------

def test_profile_without_smoke_keys_stays_vanilla():
    spec = _spec(DECAL)
    assert spec.active and spec.smoke is None
    assert not [i for i in spec.issues if i.severity == fx.SEVERITY_UNKNOWN]


def test_smoke_keys_are_read_and_known_to_studio():
    spec = _spec(DECAL + "    smoke_chance = 80\n    smoke_duration = 5000_3000\n"
                 "    smoke_fade_inout = 100_200\n    smoke_count = 30\n"
                 "    smoke_size = 16_30\n    smoke_growth = 4\n    smoke_rise_speed = 10\n"
                 "    smoke_puff_time = 4000_7500\n    smoke_drift = 5\n    smoke_spread = 60\n"
                 "    smoke_intensity = 120\n    smoke_tint = 10_20_30_40\n")
    smoke = spec.smoke
    assert smoke is not None and smoke.enabled
    assert (smoke.chance, smoke.fade_in, smoke.fade_out) == (80, 100, 200)
    assert smoke.count == (30, 30)                             # a single value is a fixed count
    assert smoke.duration == (3000, 5000)                     # reversed range is normalized
    assert smoke.size == (16.0, 30.0) and smoke.puff_time == (4000, 7500)
    assert (smoke.growth, smoke.rise_speed, smoke.drift, smoke.spread, smoke.intensity) == (4, 10, 5, 60, 120)
    assert smoke.tint == (10, 20, 30, 40)
    assert not spec.issues


def test_smoke_count_accepts_a_min_max_range():
    assert _spec(DECAL + "    smoke_count = 30_10\n").smoke.count == (10, 30)
    assert _spec(DECAL + "    smoke_count = 5_9\n").smoke.count == (5, 9)
    assert _spec(DECAL + "    smoke_count = 500_900\n").smoke.count == (ds.MAX_PUFFS, ds.MAX_PUFFS)
    spec = _spec(DECAL + "    smoke_count = 3_x\n")
    assert spec.smoke.count == ds.SmokeSpec().count             # bad range keeps the default
    assert any("smoke_count" in i.message for i in spec.issues)


def test_any_smoke_key_switches_the_smoke_on():
    # No separate "smoke = 1" switch: one smoke_* key is enough.
    assert _spec(DECAL + "    smoke_count = 40\n").smoke.enabled
    assert _spec(DECAL + "    smoke_tint = 1_2_3\n").smoke.enabled
    # smoke_count = 0 keeps the smoke block but it never produces a puff.
    assert _spec(DECAL + "    smoke_count = 0\n").smoke.count == (0, 0)


def test_bad_values_keep_the_defaults_and_warn():
    spec = _spec(DECAL + "    smoke_count = banana\n    smoke_size = 0\n"
                 "    smoke_duration = x_y\n    smoke_tint = 1_2\n    smoke_growth = 99\n"
                 "    smoke_chance = 500\n    smoke_puff_time = 1\n    smoke_spread = -5\n")
    smoke = spec.smoke
    defaults = ds.SmokeSpec()
    assert smoke.count == defaults.count and smoke.size == defaults.size
    assert smoke.duration == defaults.duration
    assert smoke.tint == (255, 255, 255, 255)                 # like every invalid tint
    assert smoke.growth == 10.0 and smoke.chance == 100        # clamped
    assert smoke.puff_time == (200, 200) and smoke.spread == 0.0
    warnings = [i for i in spec.issues if i.severity == fx.SEVERITY_WARNING]
    assert len(warnings) == 4                                   # count, size, duration, tint


def test_smoke_keys_inside_a_begin_fx_are_unknown_keys():
    # The old begin_fx ground_decal block is gone: smoke_* only belongs to new_decal.
    text = ("new_weapon 9\nbegin_fx\n    trigger = impact_world\n    mode = visual\n"
            "    duration = 300\n    vp_model = 5\n    smoke_count = 3\nend\nend\n")
    document = fx.extract_fx_document(text, 0, text.count("\n") - 1)
    spec = fx.interpret_block(document.items[0], fx.CONTEXT_WEAPON)
    assert any(i.severity == fx.SEVERITY_UNKNOWN and "smoke_count" in i.message
               for i in spec.issues)


# -- preview timeline ---------------------------------------------------------------------

def _simulation(profile: str = PROFILE, hit=fx.WEAPON_HIT_GROUND):
    document = fx.extract_fx_document("new_weapon 9\nend\n", 0, 1)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61), weapon=True,
                         ground_decal_id=1, decal_size=(100.0,100.0))
    return fx.PreviewSimulation(document, fx.CONTEXT_WEAPON, hit, unit=unit,
                                decals=fx.decal_profiles_from_text(profile))


def test_preview_shows_rising_smoke_above_the_ground():
    simulation = _simulation()
    when = fx.WEAPON_FLIGHT_MS + 8000
    puffs = [i for i in simulation.items_at(when) if i.kind == "decal_smoke"]
    assert len(puffs) > 5
    assert all(p.position[1] < simulation.ground_y for p in puffs)      # UA height grows towards -Y
    assert all(0.0 < p.alpha <= 1.0 and p.scale[0] >= ds.MIN_PARTICLE_SIZE for p in puffs)
    assert all(p.vp == 0 and p.tint is not None for p in puffs)
    # The decal itself is still there and not touched by the smoke.
    assert [i for i in simulation.items_at(when) if i.kind == fx.MODE_DECAL]


def test_preview_plays_only_the_first_part_of_a_long_smoke():
    simulation = _simulation()
    track = next(t for t in simulation.tracks if t.label.endswith("Decal smoke"))
    assert track.end - track.start == pytest.approx(fx.SMOKE_PREVIEW_MS)
    assert track.approximate
    assert any("Decal smoke lasts" in note for note in simulation.notes)
    after = fx.WEAPON_FLIGHT_MS + fx.SMOKE_PREVIEW_MS + 100
    assert not [i for i in simulation.items_at(after) if i.kind == "decal_smoke"]


def test_short_smoke_is_previewed_to_its_end():
    short = PROFILE.replace("60000_90000", "6000_6000").replace("smoke_count = 30\n",
                                                                "smoke_count = 30\n    smoke_fade_inout = 3000_2000\n")
    simulation = _simulation(short)
    track = next(t for t in simulation.tracks if t.label.endswith("Decal smoke"))
    assert track.end - track.start == pytest.approx(6000)
    assert not track.approximate
    assert not any("Decal smoke lasts" in note for note in simulation.notes)


def test_no_smoke_items_without_smoke_keys():
    plain = "new_decal 1\n" + DECAL + "end\n"
    simulation = _simulation(plain)
    assert not [i for i in simulation.items_at(fx.WEAPON_FLIGHT_MS + 5000) if i.kind == "decal_smoke"]
    assert not any(t.label.endswith("Decal smoke") for t in simulation.tracks)


def test_smoke_chance_zero_never_smokes():
    off = PROFILE.replace("smoke_intensity = 120\n", "smoke_intensity = 120\n    smoke_chance = 0\n")
    simulation = _simulation(off)
    assert not [i for i in simulation.items_at(fx.WEAPON_FLIGHT_MS + 5000) if i.kind == "decal_smoke"]


def test_extent_includes_the_plume():
    simulation = _simulation()
    center, radius = simulation.extent()
    assert radius >= 50.0 and all(math.isfinite(v) for v in center)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_viewport_paints_soft_smoke_discs(app):
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    simulation = _simulation()
    center, radius = simulation.extent()
    viewport = CollisionViewport()
    viewport.resize(640, 480)
    viewport._center = center
    viewport._scale = 1.0 / radius
    viewport._zoom = 1.0
    viewport._pan = QPointF(0.0, 0.0)
    puffs = [i for i in simulation.items_at(fx.WEAPON_FLIGHT_MS + 9000) if i.kind == "decal_smoke"]
    assert puffs

    background = QColor(40, 44, 52)
    image = QImage(640, 480, QImage.Format.Format_ARGB32)
    image.fill(background)
    painter = QPainter(image)
    try:
        viewport._draw_fx_smoke(painter, QRectF(0, 0, 640, 480), viewport._camera_state(), puffs)
    finally:
        painter.end()
    painted = sum(1 for y in range(0, 480, 2) for x in range(0, 640, 2)
                  if image.pixel(x, y) != background.rgb())
    assert painted > 50
