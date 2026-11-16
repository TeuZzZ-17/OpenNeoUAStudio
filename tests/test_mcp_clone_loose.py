"""Regression coverage for copying a loose historical BASE with its resources."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from studio_mcp import core


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


SOURCE = _reference_source()
SOURCE_DIR = SOURCE.parent
requires_reference = pytest.mark.skipif(not SOURCE.is_file(), reason='historical local reference unavailable')


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setattr(core, 'writable_roots', lambda: [tmp_path.resolve()])
    return core.resolve_source(str(SOURCE), 'set3')


def test_clone_rejects_archive_sources_and_path_traversal(tmp_path):
    archive = core.Source(tmp_path / 'dummy.BASE', None, 'embedded', set_object=object())
    with pytest.raises(core.McpToolError, match='studio_export_model'):
        core.clone_loose_model(archive, str(tmp_path / 'out'), 'Name', 'NEWSTEM1')
    loose = core.Source(tmp_path / 'dummy.BASE', None, 'loose')
    with pytest.raises(core.McpToolError, match='plain file name'):
        core.clone_loose_model(loose, str(tmp_path / 'out'), '../escape', 'NEWSTEM1')
    assert not (tmp_path / 'out').exists()


@requires_reference
def test_clone_keeps_geometry_materials_and_loose_dependencies(source, tmp_path):
    output = tmp_path / 'clone'
    result = core.clone_loose_model(source, str(output), 'AlphaPower', 'APWR0001')
    base = output / 'AlphaPower.BASE'
    assert Path(result['base_file']) == base
    assert (output / 'APWR0001.sklt').read_bytes() == (SOURCE_DIR / 'ST_POWR1.sklt').read_bytes()
    for name in ['HYPE.ILBM', 'BODEN2.ILBM', 'FX2.ILBM', 'POWER1.ANM', 'POWER2.ANM', 'POWER3.ANM']:
        assert (output / name).read_bytes() == (SOURCE_DIR / name).read_bytes()
    original = core.load_family(source)
    cloned = core.load_family(core.Source(base, source.setbas_path, 'clone'))
    assert original.root_object.skeleton.points == cloned.root_object.skeleton.points
    assert original.root_object.skeleton.polygons == cloned.root_object.skeleton.polygons
    assert len(original.root_object.base_object.ades) == len(cloned.root_object.base_object.ades)
    assert cloned.root_object.base_object.skeleton_name.endswith('APWR0001.sklt')
    assert core.validate(core.Source(base, source.setbas_path, 'clone'))['ok']


@requires_reference
def test_clone_rejects_existing_target_and_invalid_names(source, tmp_path):
    target = tmp_path / 'already'
    target.mkdir()
    (target / 'keep.txt').write_text('untouched')
    with pytest.raises(core.McpToolError):
        core.clone_loose_model(source, str(target), 'AlphaPower', 'APWR0001')
    assert (target / 'keep.txt').read_text() == 'untouched'
    for name, stem in [('../outside', 'APWR0001'), ('GoodName', 'short'), ('GoodName', 'ST_POWR1')]:
        with pytest.raises(core.McpToolError):
            core.clone_loose_model(source, str(tmp_path / 'new'), name, stem)
    assert not (tmp_path / 'new').exists()


@requires_reference
def test_clone_refuses_a_missing_local_dependency(monkeypatch, tmp_path):
    source_dir = tmp_path / 'incomplete'
    shutil.copytree(SOURCE_DIR, source_dir)
    (source_dir / 'POWER1.ANM').unlink()
    # Set3 may itself contain POWER1 after the alpha model was installed.
    # Request a name that cannot resolve through that fallback either.
    base = source_dir / 'ST_POWR1.base'
    payload = base.read_bytes()
    assert b'POWER1' in payload
    base.write_bytes(payload.replace(b'POWER1', b'MISS01'))
    monkeypatch.setattr(core, 'writable_roots', lambda: [tmp_path.resolve()])
    source = core.resolve_source(str(source_dir / 'ST_POWR1.base'), 'set3')
    with pytest.raises(core.McpToolError):
        core.clone_loose_model(source, str(tmp_path / 'out'), 'AlphaPower', 'APWR0001')
    assert not (tmp_path / 'out').exists()


@requires_reference
def test_mcp_stdio_exposes_clone_and_runs_it(tmp_path):
    from mcp.client import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run():
        env = os.environ.copy()
        env['ONUA_MCP_WRITE_ROOTS'] = str(tmp_path)
        params = StdioServerParameters(command=sys.executable, args=['-m', 'studio_mcp.server'], env=env)
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert 'studio_clone_loose_model' in names
                result = await session.call_tool('studio_clone_loose_model', {
                    'source': str(SOURCE), 'out_dir': str(tmp_path / 'protocol'),
                    'base_name': 'AlphaPower', 'skeleton_stem': 'APWR0001', 'set_hint': 'set3',
                })
                return json.loads(result.content[0].text)

    result = asyncio.run(run())
    assert result['validation']['ok']
    assert (tmp_path / 'protocol' / 'AlphaPower.BASE').is_file()
