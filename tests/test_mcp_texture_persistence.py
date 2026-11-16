"""A texture-only MCP edit must survive save and reload."""
from pathlib import Path
import os
import shutil

import pytest

from studio_mcp import core, edit


def _reference_source():
    explicit = os.environ.get('ONUA_TEST_ALPHA_MODEL')
    if explicit:
        return Path(explicit)
    try:
        project = core.data_root().parent.parent
    except core.McpToolError:
        return Path('__historical_model_not_installed__')
    return (project / 'Guide-Backup-Materiali/Materiale Beta - Alpha - Unreleased/01_Modelli'
            / 'Set di gioco (build ago98)/YPA_SET_ICE - ST_POWR1/ST_POWR1.base')


@pytest.mark.skipif(not _reference_source().is_file(), reason='historical local reference unavailable')
def test_texture_only_change_is_written_and_preserves_geometry(monkeypatch, tmp_path):
    monkeypatch.setattr(core, 'writable_roots', lambda: [tmp_path.resolve()])
    source = core.resolve_source(str(_reference_source()), 'set3')
    output = core.clone_loose_model(source, str(tmp_path / 'model'), 'AlphaPower', 'APWR0001')
    model = Path(output['base_file'])
    shutil.copyfile(model.parent / 'HYPE.ILBM', model.parent / 'AHP1.ILBM')
    before = core.load_family(core.resolve_source(str(model), 'set3')).root_object
    old_points = list(before.skeleton.points)
    old_polygons = list(before.skeleton.polygons)
    chosen = [entry.poly_id for block in before.base_object.ades
              if block.texture and block.texture.name == 'HYPE.ILBM' for entry in block.atts]
    assert chosen

    result = edit.run(str(model), [{'op': 'set_texture', 'select': chosen, 'texture': 'AHP1.ILBM'}],
                      set_hint='set3')
    assert result['written']
    after = core.load_family(core.resolve_source(str(model), 'set3')).root_object
    assert after.skeleton.points == old_points
    assert after.skeleton.polygons == old_polygons
    assert any(b.texture and b.texture.name == 'AHP1.ILBM' for b in after.base_object.ades)
    assert not any(b.texture and b.texture.name == 'HYPE.ILBM' for b in after.base_object.ades)

    again = edit.run(str(model), [{'op': 'set_texture', 'select': chosen, 'texture': 'AHP1.ILBM'}],
                     set_hint='set3')
    assert not again['written']
