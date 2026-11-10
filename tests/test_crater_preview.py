"""Weapon terrain craters in the Entity Designer: keys, engine math, preview."""

import math
import random

import numpy as np
import pytest

from entity_designer import crater_preview as cp
from entity_designer import destruction_fx as fx
from entity_designer import fx_compat, fx_visuals


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# -- keys ------------------------------------------------------------------------------

def test_both_radius_and_depth_are_needed_like_the_engine():
    assert cp.config_from_values({}) is None
    assert cp.config_from_values({"crater_radius": "200"}) is None
    assert cp.config_from_values({"crater_depth": "30"}) is None
    config = cp.config_from_values({"crater_radius": "120~150", "crater_depth": "40~25"})
    assert config.radius == (120.0, 150.0) and config.depth == (25.0, 40.0)
    assert config.rim == (20.0, 20.0)      # absent rim = 20 %


def test_an_invalid_key_switches_the_crater_off():
    good = {"crater_radius": "100", "crater_depth": "30"}
    assert cp.config_from_values({**good, "crater_rim": "abc"}) is None
    assert cp.config_from_values({**good, "crater_rim": "150"}) is None
    assert cp.config_from_values({**good, "crater_radius": "-5"}) is None
    assert cp.config_from_values({**good, "crater_depth": "13000"}) is None
    assert cp.config_from_values({**good, "crater_rim": "0"}).rim == (0.0, 0.0)


# -- the engine's shape ------------------------------------------------------------------

def test_depth_is_limited_to_half_the_radius():
    assert cp.make(0, 0, 0, 100, 500, 0).depth == 50.0
    assert cp.make(0, 0, 0, 0, 30, 0) is None
    assert cp.make(0, 0, 0, 100, 30, 7).rim == 1.0


def test_shape_matches_terraincrater_core():
    crater = cp.make(150, 0, 150, 100, 30, 0.2)
    drop = cp.sample([crater], np.array([150.0, 250.0, 310.0]), np.array([150.0, 150.0, 150.0]))[0]
    assert drop[0] == pytest.approx(30.0)                  # centre down by the depth
    assert drop[1] < 0.0 and drop[1] == pytest.approx(-30 * 0.2, abs=0.5)   # raised rim
    assert drop[2] == 0.0                                    # nothing past the reach
    pile = [cp.make(0, 0, 0, 100, 40, 0)] * 5
    assert cp.sample(pile, np.array([0.0]), np.array([0.0]))[0][0] == pytest.approx(70.0)


def test_exact_slope_matches_the_measured_slope():
    craters = [cp.make(150, 0, 150, 100, 30, 0.2), cp.make(230, 0, 120, 90, 25, 0.3)]
    xs, zs = np.meshgrid(np.arange(40, 330, 7.3), np.arange(40, 300, 9.1))
    _drop, sx, sz, _deep = cp.sample(craters, xs, zs)
    h = 0.01
    fx_ = (cp.sample(craters, xs + h, zs)[0] - cp.sample(craters, xs - h, zs)[0]) / (2 * h)
    fz_ = (cp.sample(craters, xs, zs + h)[0] - cp.sample(craters, xs, zs - h)[0]) / (2 * h)
    assert np.max(np.abs(sx - fx_)) < 1e-3 and np.max(np.abs(sz - fz_)) < 1e-3


def test_shading_like_the_engine():
    crater = cp.make(150, 0, 150, 100, 30, 0.2)
    far = cp.sample([crater], np.array([2000.0]), np.array([2000.0]))
    centre = cp.sample([crater], np.array([150.0]), np.array([150.0]))
    assert cp.shade(*far)[0] == pytest.approx(1.0)
    assert cp.shade(*centre)[0] < 0.9


# -- the Crater row --------------------------------------------------------------------

def test_crater_row_reads_checks_and_summarises_the_keys():
    text = fx_visuals.crater_text({"crater_radius": "120~150", "crater_depth": "60", "x": "1"})
    assert text == "crater_radius = 120~150\ncrater_depth = 60"
    updates = fx_visuals.parse_crater_text(text + "\ncrater_rim = 35 ; border")
    assert updates == {"crater_radius": "120~150", "crater_depth": "60", "crater_rim": "35"}
    with pytest.raises(ValueError):
        fx_visuals.parse_crater_text("ground_decal_id = 1")
    with pytest.raises(ValueError):
        fx_visuals.parse_crater_text("crater_rim = 150")
    assert fx_visuals.crater_summary({}) == "Not set"
    assert "needs radius and depth" in fx_visuals.crater_summary({"crater_radius": "100"})
    assert fx_compat.crater_level({"crater_depth": "30"}) == fx_compat.OPENNEOUA
    assert fx_compat.crater_level({}) == fx_compat.OPENNEOUA


def test_saving_the_crater_row_keeps_the_rest_of_the_script(tmp_path):
    script = tmp_path / "Weapons.cfg"
    script.write_text("new_weapon 24\n    ground_decal_id = 1\n    ground_decal_size = 120~150\n"
                      "    name = Pike_Rocket\n    energy = 100\nend\n", encoding="utf-8")
    assert fx_visuals.CRATER_INDEX not in fx_visuals.StateSession([(script, 0, 5)]).originals
    session = fx_visuals.StateSession([(script, 0, 5)], weapon=True)
    session.set_draft(fx_visuals.CRATER_INDEX, "crater_radius = 120~150\ncrater_depth = 140\ncrater_rim = 35")
    session.save([fx_visuals.CRATER_INDEX])
    text = script.read_text(encoding="utf-8")
    assert "    ground_decal_size = 120~150\n    crater_radius = 120~150\n" in text
    assert "crater_depth = 140" in text and "crater_rim = 35" in text
    assert "name = Pike_Rocket" in text and text.endswith("end\n")
    session.set_draft(fx_visuals.CRATER_INDEX, "")
    session.save([fx_visuals.CRATER_INDEX])
    assert "crater_" not in script.read_text(encoding="utf-8")


# -- the preview -----------------------------------------------------------------------

PIKE = {"crater_radius": "120~150", "crater_depth": "140~150", "crater_rim": "35"}


def _weapon(values=PIKE, **extra):
    return fx.UnitPhases(normal=fx.VisualRef(60), dead=fx.VisualRef(61), weapon=True,
                         crater=cp.config_from_values(values), **extra)


def _craters(simulation, time_ms):
    return [item for item in simulation.items_at(time_ms) if item.kind == "crater"]


def test_a_ground_hit_digs_one_crater_inside_the_ranges():
    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_GROUND, unit=_weapon())
    impact = simulation.tracks[0].end
    assert not _craters(simulation, impact - 50)
    craters = _craters(simulation, impact + 50)
    assert len(craters) == 1
    radius, depth, rim = craters[0].scale
    assert 120 <= radius <= 150 and depth == pytest.approx(radius * 0.5) and rim == pytest.approx(0.35)
    assert _craters(simulation, impact + 60000), "craters are permanent"
    assert any(track.label.endswith("Crater") for track in simulation.tracks)


def test_unit_hit_previews_crater_and_respects_visibility_and_missing_keys():
    on_unit = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                   fx.WEAPON_HIT_UNIT, unit=_weapon())
    impact = on_unit.tracks[0].end
    assert not _craters(on_unit, impact - 50)
    assert len(_craters(on_unit, impact + 50)) == 1
    hidden_unit = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                       fx.WEAPON_HIT_UNIT, unit=_weapon(), craters=False)
    assert not _craters(hidden_unit, impact + 50)
    hidden = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                  fx.WEAPON_HIT_GROUND, unit=_weapon(), craters=False)
    assert not _craters(hidden, impact + 50)
    plain = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                 fx.WEAPON_HIT_GROUND, unit=_weapon({}))
    assert not _craters(plain, impact + 50)


def test_unit_look_reads_the_crater_of_weapons_only():
    assert fx.unit_look(PIKE, weapon=True)["crater"].radius == (120.0, 150.0)
    assert fx.unit_look(PIKE, weapon=False)["crater"] is None


def _cluster_weapons():
    parent = fx.weapon_def_from_text(42, "new_weapon 42\nmodel = bomb\nvp_normal = 1\n"
        "cluster_weapon_id = 43\ncluster_count = 3\ncluster_trigger_time = 100\n"
        "cluster_spread_xy = 20|20\nend\n")
    child = fx.weapon_def_from_text(43, "new_weapon 43\nmodel = bomb\nvp_normal = 2\n"
        "crater_radius = 20~30\ncrater_depth = 10~15\ncrater_rim = 5~10\nend\n")
    return parent, child


@pytest.mark.parametrize("trigger", [fx.WEAPON_HIT_GROUND, fx.WEAPON_HIT_UNIT])
def test_cluster_child_craters_have_visible_timeline_and_persist(trigger):
    parent, child = _cluster_weapons()
    sim = fx.PreviewSimulation(parent.document, fx.CONTEXT_WEAPON, trigger,
                               unit=parent.unit, weapons={43: child})
    events = [event for event in sim._events if event[1] == "crater"]
    assert len(events) == 3
    last_impact = max(event[2] for event in events)
    assert len(_craters(sim, last_impact + 1)) == 3
    assert any("Cluster craters" in track.label for track in sim.tracks)
    assert sim.duration >= last_impact + 4000

    root_hidden = fx.PreviewSimulation(parent.document, fx.CONTEXT_WEAPON, trigger,
                                       unit=parent.unit, weapons={43: child}, root_craters=False)
    assert len(_craters(root_hidden, root_hidden.duration - 1)) == 3
    children_hidden = fx.PreviewSimulation(parent.document, fx.CONTEXT_WEAPON, trigger,
                                           unit=parent.unit, weapons={43: child}, child_craters=False)
    assert not _craters(children_hidden, children_hidden.duration)


def test_child_crater_row_only_exists_when_child_has_craters(app):
    from PySide6.QtCore import Qt
    from entity_designer.fx_panel import DestructionFxPanel

    parent, child = _cluster_weapons()
    panel = DestructionFxPanel()
    try:
        panel.set_context(fx.CONTEXT_WEAPON, parent.document, unit=parent.unit,
                          weapons={43: child})
        rows = [panel.effects_list.item(i) for i in range(panel.effects_list.count())]
        child_row = next(row for row in rows if "Cluster craters" in row.text())
        assert "43" in child_row.text() and "Not vanilla compatible" in child_row.toolTip()
        assert child_row.checkState() == Qt.CheckState.Checked
        assert _craters(panel.simulation, panel.simulation.duration - 1)
        child_row.setCheckState(Qt.CheckState.Unchecked)
        assert not _craters(panel.simulation, panel.simulation.duration - 1)
        parent_without = fx.PreviewSimulation(parent.document, fx.CONTEXT_WEAPON,
                                               fx.WEAPON_HIT_GROUND, unit=parent.unit)
        assert not _craters(parent_without, parent_without.duration)
    finally:
        panel.close()


def test_cluster_child_craters_are_drawn_in_the_viewport(app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    parent, child = _cluster_weapons()
    simulation = fx.PreviewSimulation(parent.document, fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_GROUND, unit=parent.unit,
                                      weapons={43: child})
    impact = max(event[2] for event in simulation._events if event[1] == "crater") + 1
    craters = _craters(simulation, impact)
    viewport = CollisionViewport()
    viewport.resize(600, 400)

    def frame(items):
        viewport.set_fx_preview(active=True, items=items, show_unit=False, time_ms=impact,
                                weapon_context=True)
        viewport._yaw, viewport._pitch, viewport._scale = 35.0, 32.0, 1.0 / 260.0
        image = QImage(600, 400, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.black)
        painter = QPainter(image)
        viewport._paint_fx_viewport(painter)
        painter.end()
        return image

    flat, dug = frame([]), frame(craters)
    assert any(flat.pixelColor(x, y) != dug.pixelColor(x, y)
               for x in range(0, 600, 3) for y in range(0, 400, 3))
    viewport.close()


def test_viewport_digs_the_ground_and_puts_the_decal_on_it(app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_GROUND, unit=_weapon())
    crater = _craters(simulation, fx.WEAPON_FLIGHT_MS + 50)
    viewport = CollisionViewport()
    viewport.resize(600, 400)

    def frame(items):
        viewport.set_fx_preview(active=True, items=items, show_unit=False, time_ms=1500.0,
                                weapon_context=True)
        viewport._yaw, viewport._pitch, viewport._scale = 35.0, 32.0, 1.0 / 260.0
        image = QImage(600, 400, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.black)
        painter = QPainter(image)
        viewport._paint_fx_viewport(painter)
        painter.end()
        return image

    flat, dug = frame([]), frame(crater)
    changed = sum(flat.pixelColor(x, y) != dug.pixelColor(x, y)
                  for x in range(0, 600, 6) for y in range(0, 400, 6))
    assert changed > 100, "the crater is drawn"
    # The bottom of the bowl is darker than its sunny rim, as the engine shades it.
    centre = dug.pixelColor(300, 205).lightness()
    assert any(dug.pixelColor(x, y).lightness() > centre + 10
               for x in range(200, 400, 5) for y in range(120, 300, 5))
    decal = fx.PreviewItem(fx.PROFILE_DECAL_SOURCE, fx.MODE_DECAL, 0, (0.0, 0.0, 0.0),
                           fx.IDENTITY, (1.0, 1.0, 1.0), 1.0, None, "decal",
                           texture="", decal_size=(120.0, 120.0))
    canvas = QImage(600, 400, QImage.Format.Format_RGBA8888)
    painter = QPainter(canvas)
    covered = viewport._draw_fx_craters(painter, viewport.rect().toRectF(), viewport._camera_state(),
                                        crater, [decal])
    painter.end()
    assert covered == {id(decal)}, "a decal on the crater is painted on the dug ground"


def test_same_view_reuses_the_drawn_crater_and_a_new_view_redraws_it(app):
    from PySide6.QtGui import QColor, QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_GROUND, unit=_weapon())
    craters = _craters(simulation, fx.WEAPON_FLIGHT_MS + 50)
    viewport = CollisionViewport()
    viewport.resize(600, 400)
    try:
        viewport.set_fx_preview(active=True, items=craters, show_unit=False, time_ms=1500.0,
                                weapon_context=True)
        viewport._yaw, viewport._pitch, viewport._scale = 35.0, 32.0, 1.0 / 260.0

        def frame():
            image = QImage(600, 400, QImage.Format.Format_RGBA8888)
            image.fill(QColor(255, 0, 255))
            painter = QPainter(image)
            viewport._draw_fx_craters(painter, viewport.rect().toRectF(),
                                      viewport._camera_state(), craters, [])
            painter.end()
            return image

        first = frame()
        layer = viewport._fx_crater_layer[1]
        assert frame() == first and viewport._fx_crater_layer[1] is layer, "same view: same layer"
        viewport._yaw = 80.0
        assert frame() != first and viewport._fx_crater_layer[1] is not layer, "new view: drawn again"
    finally:
        viewport.close()


def test_near_lip_of_a_deep_crater_leaves_no_gap(app):
    """Low camera over a deep crater: no see-through slivers at the near lip."""
    from PySide6.QtGui import QColor, QImage, QPainter
    from entity_designer.viewport import CollisionViewport

    unit = _weapon({"crater_radius": "300", "crater_depth": "150", "crater_rim": "20"})
    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON,
                                      fx.WEAPON_HIT_GROUND, unit=unit)
    craters = _craters(simulation, fx.WEAPON_FLIGHT_MS + 50)
    viewport = CollisionViewport()
    viewport.resize(900, 520)
    try:
        for pitch, yaw in ((20, 35), (25, 35), (25, 200), (10, 0)):
            viewport.set_fx_preview(active=True, items=craters, show_unit=False,
                                    time_ms=1500.0, weapon_context=True)
            viewport._yaw, viewport._pitch, viewport._scale = float(yaw), float(pitch), 1.0 / 160
            image = QImage(900, 520, QImage.Format.Format_RGBA8888)
            image.fill(QColor(255, 0, 255))
            painter = QPainter(image)
            viewport._draw_fx_craters(painter, viewport.rect().toRectF(),
                                      viewport._camera_state(), craters, [])
            painter.end()
            raw = np.frombuffer(image.constBits(), np.uint8).reshape(
                520, image.bytesPerLine() // 4, 4)[:, :900, :3].astype(int)
            empty = (abs(raw[..., 0] - 255) < 12) & (raw[..., 1] < 12) & (abs(raw[..., 2] - 255) < 12)
            solid = ~empty
            # An empty pixel with crater above and below it in its column is a gap.
            above = np.maximum.accumulate(solid, axis=0)
            below = np.maximum.accumulate(solid[::-1], axis=0)[::-1]
            assert int((empty & above & below).sum()) < 40, (pitch, yaw)
    finally:
        viewport.close()
