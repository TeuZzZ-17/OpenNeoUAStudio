"""Regression coverage for the original-map compatibility reports in issue #3."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from map_editor.core.building_defs import BuildingDef, GunMount
from map_editor.core.game_installation import GameInstallation
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf, new_gate
from map_editor.core.map_diagnostics import map_issues
from map_editor.core.sector_resolver import SectorResolver
from map_editor.core.set_sdf_parser import SetSdf, SdfSector
from map_editor.core.special_objects import update_special
from map_editor.core.vehicle_defs import VehicleVisual, load_vehicle_files
from map_editor.render.squad_scene import ground_height
from map_editor.render.terrain_mesh import TerrainMesh
from map_editor.ui.special_panel import DurationSpinBox, format_duration
from tests.map_integration.test_special_objects import app, win  # noqa: F401 - shared pytest fixtures


def test_gate_keeps_all_destinations_in_order_including_duplicates():
    doc = LdfDocument(mw=5, mh=5)
    doc.visible_gate_slots = 1
    doc.gates[1] = new_gate() | dict(x=2, y=2, targets=[40, 50, 40])
    text = dumps_ldf(doc)
    assert text.count('target_level = ') == 3
    assert loads_ldf(text).gates[1]['targets'] == [40, 50, 40]
    for targets in ([], [0], [2147483647]):
        update_special(doc, 'gate', 1, {'targets': targets})
        assert loads_ldf(dumps_ldf(doc)).gates[1]['targets'] == targets
    for targets in ([-1], [2147483648], ['40']):
        before = doc.snapshot()
        with pytest.raises(ValueError):
            update_special(doc, 'gate', 1, {'targets': targets})
        assert doc.snapshot() == before


def test_gate_ui_edits_multiple_destinations_and_undo(win):
    win.doc.visible_gate_slots = 1
    win.doc.gates[1].update(x=2, y=2, targets=[40, 50])
    win._refresh_specials()
    panel = win.special_panels['gate']
    panel.refresh(win.doc, 1)
    assert panel.target_levels.text() == '40, 50'
    panel.target_levels.setText('60, 70, 80')
    panel.target_levels.editingFinished.emit()
    assert win.doc.gates[1]['targets'] == [60, 70, 80]
    win.undo()
    assert win.doc.gates[1]['targets'] == [40, 50]
    panel.target_levels.setText('bad, 70')
    panel.target_levels.editingFinished.emit()
    assert win.doc.gates[1]['targets'] == [40, 50]
    assert panel.target_levels.text() == '40, 50'


def test_countdown_uses_game_ticks_and_preserves_fractional_values(app):
    spin = DurationSpinBox()
    for ticks, label in ((0, '00:00'), (1, '00:01'), (1024, '00:01'),
                         (1025, '00:02'), (61440, '01:00'), (307200, '05:00'),
                         (2147483647, '34952:32')):
        spin.setValue(ticks)
        assert spin.text() == label == format_duration(ticks)
        spin.interpretText()
        assert spin.value() == ticks
    spin.setValue(1025)
    spin.stepBy(1)
    assert spin.value() == 2049
    assert spin._parse_ticks('05:00') == 307200
    assert spin._parse_ticks('34952:32') is None  # one tick above the integer limit


def test_legacy_map_scripts_exclude_gem_actions_and_enable_lists(tmp_path):
    scripts = tmp_path / 'Scripts'
    scripts.mkdir()
    (scripts / 'startup.scr').write_text('include script:ROBOS.scr\n')
    (scripts / 'ROBOS.scr').write_text('new_vehicle 176\nmodel = robo\nvp_normal = 3\nend\n')
    (scripts / 'Ghor.scr').write_text('modify_vehicle 176\nvp_normal = 9\nend\n')
    doc = LdfDocument(mw=5, mh=5)
    text = dumps_ldf(doc).replace('; --- USER SCRIPT ---', '')
    legacy = ('include script:Ghor.scr\n'
              'begin_enable 1\nvehicle = 176\nend\n'
              'begin_gem\nsec_x = 2\nsec_y = 2\nbuilding = 50\ntype = 3\n'
              'begin_action\nmodify_vehicle 176\nadd_energy = 42\nend\nend\nend\n')
    text = text.replace('begin_maps', legacy + 'begin_maps')
    loaded = loads_ldf(text)
    assert 'include script:Ghor.scr' in loaded.script_content
    assert 'begin_enable' not in loaded.script_content
    assert 'add_energy' not in loaded.script_content
    assert loaded.tech[1]['veh'] == [176]
    assert loaded.gems[1]['actions'][0]['id'] == 176
    vehicles = load_vehicle_files(scripts, loaded.script_content)
    assert vehicles[176].model == 'robo' and vehicles[176].vp_normal == 9
    assert loads_ldf(dumps_ldf(loaded)).script_content.strip() == loaded.script_content.strip()


def test_script_alias_nested_include_order_and_cycle(tmp_path):
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'startup.scr').write_text('include script:nested/first.scr\n')
    (tmp_path / 'nested/first.scr').write_text(
        'new_vehicle 176\nmodel = robo\nvp_normal = 2\nend\n'
        'include script:startup.scr\ninclude script:second.scr\n')
    (tmp_path / 'second.scr').write_text('modify_vehicle 176\nvp_normal = 8\nend\n')
    assert load_vehicle_files(tmp_path)[176].vp_normal == 8


def test_expansion_installation_prefers_its_sibling_levels(tmp_path):
    for path in ('dataxp/Scripts', 'dataxp/Set1/Objects', 'dataxp/Set1/Scripts',
                 'levelsxp/Single', 'Levels/Single', 'Music'):
        (tmp_path / path).mkdir(parents=True)
    (tmp_path / 'dataxp/Set1/Objects/SET.BAS').write_bytes(b'')
    (tmp_path / 'dataxp/Set1/Scripts/set.sdf').write_text('')
    for chosen in (tmp_path, tmp_path / 'dataxp'):
        installation = GameInstallation.suggest(chosen)
        assert installation.data == tmp_path / 'dataxp'
        assert installation.folder('levels') == tmp_path / 'levelsxp'
        assert installation.folder('music') == tmp_path / 'Music'
        assert installation.available_sets() == [1]


def _diagnostic_library():
    sdf = SetSdf(sectors={typ: SdfSector(typ, 0, 0, 0, ()) for typ in (0, 1, 2, *range(248, 256))})
    resolver = SectorResolver(sdf)
    return SimpleNamespace(
        resolver=resolver,
        vehicles={176: VehicleVisual(model='robo')},
        buildings={1: BuildingDef(1, 'Power', 'kraftwerk', 1, power=40),
                   2: BuildingDef(2, 'Flak', 'building', 2, guns=[GunMount(3)]),
                   3: BuildingDef(3, 'Broken building', 'building', 241)},
        mesh=lambda typ: SimpleNamespace(resolved=resolver.resolve_typ(typ), missing=[]),
        actor_mesh=lambda _vehicle: SimpleNamespace(missing=[]))


def test_diagnostics_distinguish_neutral_power_and_inactive_flak():
    doc = LdfDocument(mw=5, mh=5)
    doc.grids['blg'][1][1] = '01'
    doc.grids['blg'][1][2] = '02'
    lib = _diagnostic_library()
    issues = map_issues(doc, lib)
    assert len(issues) == 2 and all('owner 0' in issue.message for issue in issues)
    doc.grids['own'][1][1] = doc.grids['own'][1][2] = 7
    issues = map_issues(doc, lib)
    assert any(issue.severity == 'info' and 'neutral' in issue.message for issue in issues)
    assert any(issue.severity == 'warning' and 'guns do not spawn' in issue.message for issue in issues)
    doc.host_stations.append(dict(owner=7, veh=176, x=2, y=2))
    assert not map_issues(doc, lib)
    doc.host_stations[0]['_preview'] = True
    assert len(map_issues(doc, lib)) == 2


def test_diagnostics_report_missing_authored_and_building_sector_references():
    doc = LdfDocument(mw=5, mh=5)
    doc.grids['type'][1][1] = 'f0'
    doc.grids['blg'][1][2] = 'ff'
    doc.grids['blg'][2][2] = '03'
    issues = map_issues(doc, _diagnostic_library())
    assert len(issues) == 3 and all(issue.severity == 'error' for issue in issues)
    assert {issue.cell for issue in issues} == {(1, 1), (2, 1), (2, 2)}
    assert any('0xf1' in issue.message for issue in issues)


def test_missing_sector_under_actor_has_safe_editor_height():
    doc = LdfDocument(mw=5, mh=5)
    doc.grids['type'][2][2] = 'f0'
    lib = _diagnostic_library()
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    assert ground_height(doc, terrain, lib, 2700, -2700) == terrain.cell_y(2, 2)


def test_special_buildings_do_not_get_ordinary_owner_zero_warnings():
    doc = LdfDocument(mw=5, mh=5)
    doc.visible_gem_slots = 1
    doc.gems[1].update(x=1, y=1, blg=2)
    doc.grids['blg'][1][1] = '02'
    issues = map_issues(doc, _diagnostic_library())
    assert len(issues) == 1 and 'guns do not spawn' in issues[0].message


def test_diagnostics_report_missing_model_and_actor_resources():
    doc = LdfDocument(mw=5, mh=5)
    doc.grids['type'][1][1] = '01'
    lib = _diagnostic_library()
    original = lib.mesh
    lib.mesh = lambda typ: SimpleNamespace(resolved=original(typ).resolved,
                                           missing=['missing.base'] if typ == 1 else [])
    doc.squads.append(dict(owner=1, veh=1, x=2, y=2))
    lib.actor_mesh = lambda _vehicle: SimpleNamespace(missing=['VP 999'])
    before = doc.snapshot()
    issues = map_issues(doc, lib)
    assert len(issues) == 2 and all(issue.severity == 'error' for issue in issues)
    assert any('missing.base' in issue.message for issue in issues)
    assert any('VP 999' in issue.message for issue in issues)
    assert doc.snapshot() == before


def test_left_panel_issues_refresh_after_paint_host_edit_and_undo(win):
    power = next(bid for bid, definition in win.buildings.items() if definition.has_power)
    win.doc.grids['blg'][2][2] = f'{power:02x}'
    win._after_history()
    rows = [win.map_issues_list.item(i) for i in range(win.map_issues_list.count())]
    item = next(item for item in rows if 'owner 0' in item.text())
    win._find_map_issue(item)
    assert win.view.selection == {(2, 2)}
    assert item.data(Qt.ItemDataRole.UserRole) == (2, 2)
    win.history.push(win.doc)
    win.doc.grids['own'][2][2] = 7
    win._after_history()
    assert any('neutral' in win.map_issues_list.item(i).text() for i in range(win.map_issues_list.count()))
    win.undo()
    assert any('owner 0' in win.map_issues_list.item(i).text() for i in range(win.map_issues_list.count()))
    assert all('1024' in spin.toolTip() for key, spin in win.host_panel.ai_spins.items() if key.endswith('_delay'))
