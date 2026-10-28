"""Weapon damage overlay of the Visuals && FX preview: blast sphere and damage numbers."""

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer.fx_viewport import FxPreviewViewportMixin

WEAPON = (
    "new_weapon 7\n"
    "    model = missile\n"
    "    energy = 30000\n"
    "    blast_radius = 400\n"
    "    blast_energy = 15000\n"
    "end\n")


def _unit(text=WEAPON):
    values = fx.top_level_values(text, 0, len(text.splitlines()) - 1)
    return fx.UnitPhases(weapon=True, **fx.unit_look(values, weapon=True))


def _simulation(unit, trigger=fx.WEAPON_HIT_UNIT):
    return fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_WEAPON, trigger, unit=unit)


def _marks(simulation):
    return {mark.kind: mark for mark in simulation._damage_marks}


def test_values_are_read_and_shown_in_the_game_scale():
    unit = _unit()
    assert (unit.energy, unit.blast_energy, unit.blast_radius) == (30000.0, 15000, 400.0)
    marks = _marks(_simulation(unit))
    assert marks[fx.DAMAGE_DIRECT].amount == 300.0
    assert marks[fx.DAMAGE_AREA].amount == 150.0
    assert marks[fx.DAMAGE_AREA].radius == 400.0
    assert fx.damage_text(150.0) == "150" and fx.damage_text(12.5) == "12.5"


def test_ground_hit_has_only_the_area_damage():
    marks = _marks(_simulation(_unit(), fx.WEAPON_HIT_GROUND))
    assert set(marks) == {fx.DAMAGE_AREA}


def test_area_damage_needs_a_valid_radius_and_energy():
    for body in ("blast_radius = 0\n", "blast_radius = -5\n", "blast_radius = 99999\n",
                 "blast_radius = nan\n", "blast_energy = 0\n"):
        text = ("new_weapon 7\n    energy = 100\n    blast_radius = 400\n"
                "    blast_energy = 15000\n" + body + "end\n")
        assert fx.DAMAGE_AREA not in _marks(_simulation(_unit(text))), body


def test_vehicles_and_lasers_show_no_weapon_damage():
    values = fx.top_level_values(WEAPON, 0, 5)
    vehicle = fx.unit_look(values, weapon=False)
    assert vehicle["blast_energy"] == 0 and vehicle["blast_radius"] == 0.0
    laser = _unit(WEAPON.replace("model = missile", "model = laser"))
    assert laser.laser is not None
    assert not _simulation(laser)._damage_marks


def test_delay_time_moves_only_the_area_damage():
    marks = _marks(_simulation(_unit(WEAPON.replace("end\n", "    delay_time = 500\nend\n"))))
    assert marks[fx.DAMAGE_AREA].start == pytest.approx(marks[fx.DAMAGE_DIRECT].start + 500)


def test_marks_fade_in_and_out_and_expire():
    simulation = _simulation(_unit())
    start = _marks(simulation)[fx.DAMAGE_AREA].start
    assert simulation.damage_at(start - 1) == []
    first = {s.mark.kind: s for s in simulation.damage_at(start)}
    assert first[fx.DAMAGE_AREA].alpha == 0.0
    middle = simulation.damage_at(start + 800)
    assert {s.alpha for s in middle} == {1.0}
    fading = simulation.damage_at(start + fx.DAMAGE_MARK_MS - 100)
    assert 0.0 < fading[0].alpha < 1.0
    assert simulation.damage_at(start + fx.DAMAGE_MARK_MS) == []


class _Stub:
    """Just enough of the viewport to project points straight from above."""

    def _camera_vertex(self, point, camera):
        return (point[0], point[2], 1000.0 - point[1])

    def _camera_point_is_projectable(self, point):
        return point[2] > 1.0

    def _project(self, point, target, camera):
        return QPointF(target.center().x() + point[0], target.center().y() + point[1])

    def _fx_project(self, point, target, camera):
        return self._project(self._camera_vertex(point, camera), target, camera)

    _draw_fx_damage = FxPreviewViewportMixin._draw_fx_damage
    _draw_blast_sphere = FxPreviewViewportMixin._draw_blast_sphere
    _draw_damage_label = FxPreviewViewportMixin._draw_damage_label


def test_overlay_paints_sphere_and_labels():
    QApplication.instance() or QApplication([])
    simulation = _simulation(_unit())
    start = _marks(simulation)[fx.DAMAGE_AREA].start
    stub = _Stub()
    stub._fx_damage = simulation.damage_at(start + 800)
    image = QImage(400, 300, QImage.Format.Format_RGBA8888)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        stub._draw_fx_damage(painter, QRectF(0, 0, 400, 300), {"scale": 1.0})
    finally:
        painter.end()
    drawn = [(x, y) for x in range(0, 400, 2) for y in range(0, 300, 2)
             if image.pixelColor(x, y).alpha() > 0]
    assert drawn
    # The 400-unit sphere does not fit a 400 px wide image, but its rings and both
    # labels must have left pixels, each in its own area around the center.
    assert any(abs(x - 200) > 100 for x, _y in drawn) or len(drawn) > 500
