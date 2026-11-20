"""A texture-only MCP edit must survive save and reload."""
from pathlib import Path
import os
import shutil

import pytest

from studio_mcp import core, edit


def test_model_texture_follows_the_shared_order_set_folder_model_folder_subfolders_archive(tmp_path):
    from studio.assets.family import AssetFamily, _load_texture
    from studio.exchange.portable import AssetResolver
    from studio.formats.setbas import read_setbas
    from studio_mcp.textures import ilbm_bytes

    try:
        set3 = Path(core._set_info("set3")["set_bas"])
    except core.McpToolError:
        pytest.skip("Set 3 is not installed")
    archive = read_setbas(set3)
    texture = next(r.resource_name for r in archive.resources
                   if r.class_id == "ilbm.class" and r.decodable
                   and r.resource_name.casefold().endswith(".ilbm")
                   and len(archive.find(r.resource_name, "ilbm.class")) == 1)
    name = texture.replace("\\", "/").rsplit("/", 1)[-1]
    textures = tmp_path / "Textures"
    (textures / "Set1").mkdir(parents=True)
    variant_dir = textures / "Set3"
    variant_dir.mkdir()
    palette = [(i, i, i) for i in range(256)]
    (textures / name).write_bytes(ilbm_bytes(bytes([10] * 16), 16, 1, palette))
    (textures / "Set1" / name).write_bytes(ilbm_bytes(bytes([20] * 16), 16, 1, palette))
    (variant_dir / name).write_bytes(ilbm_bytes(bytes([30] * 16), 16, 1, palette))

    def load(name, *, with_archive=True):
        family = AssetFamily(base_path=tmp_path / "Tank_Normal.BASE", setbas_path=set3,
                             setbas_archive=archive if with_archive else None)
        _load_texture(family, AssetResolver([tmp_path], prefer_earliest_root=True), name)
        return family

    result = load(texture)
    assert result.texture_refs[texture].path == variant_dir / name
    assert result.textures[texture].pixels == bytes([30] * 16)

    # A broken variant is skipped: the default file of the model folder is next.
    (variant_dir / name).write_bytes(b"broken variant")
    result = load(texture)
    assert result.texture_refs[texture].path == textures / name
    assert result.textures[texture].pixels == bytes([10] * 16)

    (variant_dir / name).unlink()
    assert load(texture).texture_refs[texture].path == textures / name
    assert load(texture, with_archive=False).textures[texture].pixels == bytes([10] * 16)

    # Without the model-folder default, any subfolder (even another set) is used before the archive.
    (textures / name).unlink()
    result = load(texture)
    assert result.texture_refs[texture].status == "fallback"
    assert result.textures[texture].pixels == bytes([20] * 16)

    # Only when no model file exists is the active SET.BAS used.
    (textures / "Set1" / name).unlink()
    assert load(texture).texture_refs[texture].status == "setbas"
    assert load(texture).textures[texture].has_body
    assert load("NOT_IN_SET.ILBM").texture_refs["NOT_IN_SET.ILBM"].status == "missing"


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
