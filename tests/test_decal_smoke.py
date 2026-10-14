"""Ground decal smoke: smoke_* keys, engine-exact puff math and the preview timeline."""

import math

import pytest

from entity_designer import decal_smoke as ds
from entity_designer import destruction_fx as fx


def _doc(text: str, context: str = fx.CONTEXT_WEAPON):
    return fx.extract_fx_document(text, 0, text.count("\n") - 1)


def _spec(block_body: str, context: str = fx.CONTEXT_WEAPON) -> fx.FxSpec:
    text = "new_weapon 9\nbegin_fx\n" + block_body + "end\nend\n"
    document = fx.extract_fx_document(text, 0, text.count("\n") - 1)
    return fx.interpret_block(document.items[0], context)


DECAL = ("    trigger = impact_world\n    mode = ground_decal\n"
         "    size = 100\n    duration = permanent\n")

WEAPON = (
    "new_weapon 9\n"
    "begin_fx\n" + DECAL.replace("size = 100", "size = 100_100") +
    "    smoke = 1\n    smoke_duration = 60000_90000\n    smoke_count = 30\n"
    "    smoke_size = 16_30\n    smoke_intensity = 120\n"
    "end\n"
    "end\n")


# -- engine reference ---------------------------------------------------------------------
# Values printed by the real C++ header (world/grounddecal_smoke.h) for an emitter
# of radius 100, seed 0x1234567, 60 s, default settings (count 10_30).

def test_hash_matches_the_engine_bit_for_bit():
    assert ds.hash32(0x1234567, 3, 0x534D4F4B) == 1002249942
    assert ds.hash32(0, 0, 0) == 1927377462
    assert ds.hash32(0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF) == 4176881856


ENGINE_PUFFS = {
    # (slot, age): (x, y, z, size, alpha, phase, shade) or None when the slot is off
    (0, 1000): (-19.70844, -17.93864, -21.93082, 17.48323, 0.66790, 0.08787, 0.91781),
    (5, 1000): (-25.33253, -142.01463, 37.73175, 29.98936, 0.14789, 0.85211, 0.94771),
    (17, 1000): None,
    (0, 7000): (0.29065, -43.45150, 44.04598, 33.88243, 0.70719, 0.29281, 0.86036),
    (5, 7000): (-28.90949, -133.56656, 9.22390, 58.08822, 0.20121, 0.79879, 1.08121),
    (17, 7000): (3.21460, -43.12204, 42.79415, 30.43523, 0.70527, 0.29473, 0.99306),
    (0, 31000): (28.72028, -21.01558, -7.45842, 21.05286, 0.83259, 0.11259, 1.02907),
    (5, 31000): (-23.60769, -99.77426, 47.54117, 33.44664, 0.41448, 0.58552, 1.11525),
    (17, 31000): (10.50184, -30.32071, 3.52445, 26.86207, 0.80972, 0.19028, 0.93785),
    (0, 58000): (-8.64806, -73.57973, 51.58511, 23.21131, 0.46517, 0.53483, 0.91814),
    (5, 58000): None,
    (17, 58000): None,
}


def test_puffs_match_the_engine_reference_values():
    spec = ds.SmokeSpec(enabled=True)
    count = ds.scaled_count(spec.count[1], 100.0)
    assert count == 40
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

def test_block_without_smoke_keys_stays_vanilla():
    spec = _spec(DECAL)
    assert spec.active and spec.smoke is None
    assert not [i for i in spec.issues if i.severity == fx.SEVERITY_UNKNOWN]


def test_smoke_keys_are_read_and_known_to_studio():
    spec = _spec(DECAL + "    smoke = 1\n    smoke_chance = 80\n    smoke_duration = 5000_3000\n"
                 "    smoke_fade_in = 100\n    smoke_fade_out = 200\n    smoke_count = 30\n"
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
    assert _spec(DECAL + "    smoke = 1\n    smoke_count = 30_10\n").smoke.count == (10, 30)
    assert _spec(DECAL + "    smoke = 1\n    smoke_count = 5_9\n").smoke.count == (5, 9)
    assert _spec(DECAL + "    smoke = 1\n    smoke_count = 500_900\n").smoke.count == (ds.MAX_PUFFS, ds.MAX_PUFFS)
    spec = _spec(DECAL + "    smoke = 1\n    smoke_count = 3_x\n")
    assert spec.smoke.count == ds.SmokeSpec().count             # bad range keeps the default
    assert any("smoke_count" in i.message for i in spec.issues)


def test_smoke_off_unless_exactly_one():
    assert _spec(DECAL + "    smoke = 0\n    smoke_count = 40\n").smoke is None
    assert _spec(DECAL + "    smoke = yes\n").smoke is None
    assert _spec(DECAL + "    smoke_count = 40\n").smoke is None     # smoke = 1 is required


def test_bad_values_keep_the_defaults_and_warn():
    spec = _spec(DECAL + "    smoke = 1\n    smoke_count = banana\n    smoke_size = 0\n"
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


def test_smoke_keys_outside_a_ground_decal_are_ignored_with_a_warning():
    spec = _spec("    trigger = impact_world\n    mode = visual\n    duration = 300\n"
                 "    vp_model = 5\n    smoke = 1\n")
    assert spec.active and spec.smoke is None
    assert any("only work with mode = ground_decal" in i.message for i in spec.issues)


# -- preview timeline ---------------------------------------------------------------------

def _simulation(text: str = WEAPON, hit=fx.WEAPON_HIT_GROUND):
    document = _doc(text)
    unit = fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61), weapon=True)
    return fx.PreviewSimulation(document, fx.CONTEXT_WEAPON, hit, unit=unit)


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
    short = WEAPON.replace("60000_90000", "6000_6000").replace("smoke_count = 30\n",
                                                               "smoke_count = 30\n    smoke_fade_out = 2000\n")
    simulation = _simulation(short)
    track = next(t for t in simulation.tracks if t.label.endswith("Decal smoke"))
    assert track.end - track.start == pytest.approx(6000)
    assert not track.approximate
    assert not any("Decal smoke lasts" in note for note in simulation.notes)


def test_no_smoke_items_without_smoke_keys():
    plain = WEAPON.replace("    smoke = 1\n", "")
    simulation = _simulation(plain)
    assert not [i for i in simulation.items_at(fx.WEAPON_FLIGHT_MS + 5000) if i.kind == "decal_smoke"]
    assert not any(t.label.endswith("Decal smoke") for t in simulation.tracks)


def test_smoke_chance_zero_never_smokes():
    off = WEAPON.replace("smoke_intensity = 120\n", "smoke_intensity = 120\n    smoke_chance = 0\n")
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
