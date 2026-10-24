"""Reusable ground decal profiles (new_decal) and their preview in Destruction FX."""

import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from entity_designer import destruction_fx as fx

PROFILE = (
    "new_decal 1\n"
    "    name = destroyed\n"
    "    texture = Interface/GroundDecals/impact_01.png\n"
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


# -- parser -------------------------------------------------------------------------------


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


# -- model sphere measured on the loaded model -----------------------------------------------

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
