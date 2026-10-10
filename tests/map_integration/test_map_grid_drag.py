"""Regression checks for moving authored grid objects."""

from map_editor.core.history import History
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.special_objects import add_special, update_special
from map_editor.tools.map_clipboard import copy_grid_cells, move_grid_cell


def test_drag_replaces_destination_without_swapping_terrain_or_owner():
    doc = LdfDocument(mw=8, mh=8)
    doc.grids['type'][2][2] = 'a1'
    doc.grids['blg'][2][2] = '02'
    doc.grids['type'][4][4] = 'b2'
    doc.grids['hgt'][2][2] = 30
    doc.grids['hgt'][4][4] = 31
    doc.grids['own'][2][2] = 1
    doc.grids['own'][4][4] = 3
    snapshot = doc.snapshot()
    clip = copy_grid_cells(doc, {(2, 2)}, 'building', ('type', 'blg'))
    history = History()
    history.push(doc)
    assert move_grid_cell(doc, (2, 2), (4, 4), clip)
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('a1', '02')
    assert (doc.grids['hgt'][2][2], doc.grids['hgt'][4][4]) == (30, 31)
    assert (doc.grids['own'][2][2], doc.grids['own'][4][4]) == (1, 3)
    restored = loads_ldf(dumps_ldf(doc))
    assert (restored.grids['type'][2][2], restored.grids['blg'][2][2]) == ('00', '00')
    assert (restored.grids['type'][4][4], restored.grids['blg'][4][4]) == ('a1', '02')
    assert history.undo(doc) and doc.snapshot() == snapshot


def test_drag_can_move_an_ordinary_sector_and_refuses_map_border():
    doc = LdfDocument(mw=8, mh=8)
    doc.grids['type'][2][2] = 'a1'
    clip = copy_grid_cells(doc, {(2, 2)}, 'sector', ('type', 'blg'))
    assert not move_grid_cell(doc, (2, 2), (0, 0), clip)
    assert doc.grids['type'][2][2] == 'a1'
    assert move_grid_cell(doc, (2, 2), (3, 2), clip)
    assert doc.grids['type'][2][2] == '00'
    assert doc.grids['type'][2][3] == 'a1'


def test_relocating_specials_always_clears_the_origin_sector():
    from map_editor.core.building_defs import BuildingDef
    doc = LdfDocument(mw=9, mh=9)
    for row, kind in enumerate(('gate', 'item', 'gem'), start=2):
        slot = add_special(doc, kind)
        changes = {'x': 2, 'y': row}
        if kind == 'gem':
            changes.update(blg=65, actions=[{'target_type': 'modify_vehicle',
                                             'id': 1, 'param': 'enable', 'val': '0'}])
        update_special(doc, kind, slot, changes, {65: BuildingDef(65, sec_type=42)})
        origin = (2, row)
        update_special(doc, kind, slot, {'x': 4, 'y': row},
                       {65: BuildingDef(65, sec_type=42)})
        assert doc.grids['type'][origin[1]][origin[0]] == '00'
        assert doc.grids['blg'][origin[1]][origin[0]] == '00'
        saved = loads_ldf(dumps_ldf(doc))
        assert saved.grids['type'][origin[1]][origin[0]] == '00'
        assert saved.grids['blg'][origin[1]][origin[0]] == '00'

    slot = add_special(doc, 'gate')
    update_special(doc, 'gate', slot, {'x': 6, 'y': 6})
    doc.grids['type'][6][6] = 'ab'
    doc.grids['blg'][6][6] = '80'
    update_special(doc, 'gate', slot, {'x': 7, 'y': 6})
    assert (doc.grids['type'][6][6], doc.grids['blg'][6][6]) == ('00', '00')


def test_special_move_clears_every_sector_it_leaves():
    """All three kinds leave flat empty ground after every move."""
    from map_editor.core.building_defs import BuildingDef
    from map_editor.core.special_objects import special_store

    definitions = {50: BuildingDef(50, sec_type=42)}
    for kind, first_visual in (
        ('gate', ('03', '19')),
        ('item', ('f5', '23')),
        ('gem', ('2a', '32')),
    ):
        doc = LdfDocument(mw=9, mh=9)
        slot = add_special(doc, kind)
        old = special_store(doc, kind)[slot]
        old.update(x=2, y=2)
        if kind == 'gem':
            old['actions'] = [{'target_type': 'modify_vehicle', 'id': 1,
                               'param': 'enable', 'val': '0'}]
        # Simulate a loaded LDF where the ordinary grid holds the special model.
        doc.grids['type'][2][2], doc.grids['blg'][2][2] = first_visual
        doc.grids['type'][3][3] = 'a1'
        doc.grids['type'][4][4] = 'b2'
        history = History()
        history.push(doc)

        assert update_special(doc, kind, slot, {'x': 3, 'y': 3}, definitions)
        assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
        assert update_special(doc, kind, slot, {'x': 4, 'y': 4}, definitions)
        assert (doc.grids['type'][3][3], doc.grids['blg'][3][3]) == ('00', '00')
        assert update_special(doc, kind, slot, {'x': 5, 'y': 5}, definitions)
        assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('00', '00')
        assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
        saved = loads_ldf(dumps_ldf(doc))
        assert (saved.grids['type'][3][3], saved.grids['blg'][3][3]) == ('00', '00')
        assert (saved.grids['type'][4][4], saved.grids['blg'][4][4]) == ('00', '00')
        assert history.undo(doc)
        assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == first_visual


def test_sector_move_replaces_building_and_preserves_owner_height():
    doc = LdfDocument(mw=8, mh=8)
    doc.grids['type'][2][2] = '3d'
    doc.grids['type'][4][4] = 'b2'
    doc.grids['blg'][4][4] = '04'
    doc.grids['own'][4][4] = 5
    doc.grids['hgt'][4][4] = 137
    clip = copy_grid_cells(doc, {(2, 2)}, 'sector', ('type', 'blg'))
    original = doc.snapshot()
    history = History()
    history.push(doc)
    assert move_grid_cell(doc, (2, 2), (4, 4), clip)
    assert (doc.grids['type'][4][4], doc.grids['blg'][4][4]) == ('3d', '00')
    assert (doc.grids['type'][2][2], doc.grids['blg'][2][2]) == ('00', '00')
    assert doc.grids['own'][4][4] == 5 and doc.grids['hgt'][4][4] == 137
    assert history.undo(doc) and doc.snapshot() == original


def test_special_key_retains_authored_building_and_requests_its_structure_mesh():
    from map_editor.core.building_defs import BuildingDef
    from map_editor.render.special_scene import special_cells, key_building_sector
    from types import SimpleNamespace

    doc = LdfDocument(mw=8, mh=8)
    slot = add_special(doc, 'item')
    update_special(doc, 'item', slot, {'x': 2, 'y': 2, 'keys': [(4, 4)]})
    doc.grids['blg'][4][4] = '02'
    lib = SimpleNamespace(buildings={2: BuildingDef(2, sec_type=78)})
    member = special_cells(doc, lib)[(4, 4)][1]
    assert member.key == 0 and member.typ == 0xf3
    assert member.building == 2
    assert key_building_sector(member, lib) == 78
    assert key_building_sector(special_cells(doc, lib)[(2, 2)][1], lib) is None


def test_key_sector_combines_key_floor_building_structure_and_mounted_gun():
    """A key may not replace the 3D sector supporting a flak."""
    from types import SimpleNamespace as S
    from map_editor.core.building_defs import BuildingDef
    from map_editor.render.gpu_scene import WorldScene
    from map_editor.render.terrain_mesh import TerrainMesh

    ground = S(vertices=((0, 0, 0), (10, 0, 0), (0, 0, 10)))
    wall = S(vertices=((0, 0, 0), (0, -10, 0), (10, -10, 0)))

    class Library:
        buildings = {2: BuildingDef(2, sec_type=78)}
        assets = S(sdf=S(sectors={0: S(ground=0), 0xf3: S(ground=0), 78: S(ground=0)}))

        def mesh(self, typ):
            return S(faces=[(ground, 0, 0)] + ([(wall, 0, 0)] if typ == 78 else []))

        def building_mesh(self, bid):
            return S(faces=[(wall, 0, 0)])

        def filler_mesh(self, *args):
            return S(faces=[])

        def face_uvs(self, face):
            return ((0, 0), (1, 0), (0, 1))

        def surface_for(self, face):
            return S(kind='solid', indices=b'', width=0, height=0, tracy_mode='none',
                     shade_mode='none', shade_value=0, solid_index=1)

    doc = LdfDocument(mw=6, mh=6)
    slot = add_special(doc, 'item')
    update_special(doc, 'item', slot, {'x': 2, 'y': 2, 'keys': [(3, 3)]})
    doc.grids['blg'][3][3] = '02'
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    scene = WorldScene()
    scene.set_library(Library())
    scene.update(doc, terrain)
    assert ('sector', 0xf3, False) in scene.instances
    assert ('key-building-sector', 78, False) in scene.instances
    assert ('building', 2, False) in scene.instances
    assert len(scene.instances[('key-building-sector', 78, False)][1]) == 1
    assert len(scene.instances[('building', 2, False)][1]) == 1
