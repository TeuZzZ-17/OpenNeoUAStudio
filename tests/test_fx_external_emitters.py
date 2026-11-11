"""A BASE that replaces a VP brings its own emitters, and asking for them never blocks its loading."""

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('NME_RENDERER', 'software')

import pytest
from PySide6.QtWidgets import QApplication

from entity_designer import destruction_fx as fx
from entity_designer.viewport import CollisionViewport


@pytest.fixture
def viewport():
    QApplication.instance() or QApplication([])
    widget = CollisionViewport()
    yield widget
    widget.close()


def test_asking_for_emitters_before_the_loader_exists_does_not_cache_a_failure(viewport):
    # First frame: the editor asks for the emitters before it hands the loader over.
    assert viewport.fx_vp_emitter_specs(("Data/Models/x.BASE", 120)) == []
    assert "Data/Models/x.BASE" not in viewport._fx_external_cache
    loaded = []
    viewport.set_fx_preview(active=True, external_loader=lambda path: loaded.append(path))
    item = fx.PreviewItem(-1, "unit", 120, (0.0, 0.0, 0.0), fx.IDENTITY, (1.0, 1.0, 1.0),
                          external=("Data/Models/x.BASE",))
    viewport._fx_external_for(item)
    assert loaded == ["Data/Models/x.BASE"], "the model is still loaded afterwards"


def test_unit_phase_with_a_base_keys_its_emitters_by_that_file():
    unit = fx.UnitPhases(normal=fx.VisualRef(116), dead=fx.VisualRef(120, base_path="Data/a.BASE"))
    simulation = fx.PreviewSimulation(fx.FxDocument(), fx.CONTEXT_VEHICLE,
                                      fx.SEQUENCE_TRIGGER, unit=unit)
    keys = simulation.emitter_keys()
    assert ("Data/a.BASE", 120) in keys and 116 in keys
