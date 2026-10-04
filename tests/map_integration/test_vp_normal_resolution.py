from types import SimpleNamespace

import pytest

from map_editor import bootstrap
from map_editor.core import asset_bridge
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.vehicle_defs import VehicleVisual
from map_editor.render.sector_mesh import SectorMeshLibrary


@pytest.mark.parametrize('loose_set', [False, True])
def test_positional_vp_ignores_external_name_collision_but_keeps_set_override(tmp_path, monkeypatch, loose_set):
    data = tmp_path / 'Data'
    set_dir = data / 'Sets' / 'Set1'
    set_dir.mkdir(parents=True)
    genesis = data / 'Models' / 'Base' / 'YingGenesis' / 'VP_KUFO.BASE'
    genesis.parent.mkdir(parents=True)
    genesis.write_bytes(b'Genesis fixture')
    local = set_dir / 'Objects' / 'VP_KUFO.BASE'
    if loose_set:
        local.parent.mkdir()
        local.write_bytes(b'Normal override fixture')
    normal = object()
    external = object()
    override = object()
    monkeypatch.setattr(bootstrap, 'game_data_dir', lambda: data)
    monkeypatch.setattr(asset_bridge, 'load_asset_family', lambda path, *args:
                        SimpleNamespace(root_object=override if path == local else external))
    assets = SetAssets.__new__(SetAssets)
    assets.set_dir = set_dir
    assets.archive = None
    assets._by_name = {'vp_kufo.base': normal}
    assets._loose = {}
    assets._loose_index = None
    assets._loose_families = {}
    # Resolve the generic BASE first to exercise cache isolation as well.
    assert assets.base_object('VP_KUFO') is (override if loose_set else external)
    assert assets.base_object('VP_KUFO', set_only=True) is (override if loose_set else normal)


def test_normal_vp_and_missing_external_base_use_set_scoped_lookup():
    library = SectorMeshLibrary.__new__(SectorMeshLibrary)
    library._vehicle_meshes = {}
    library._vp_names = ['dummy.base', 'normal.base']
    library.vehicles = {23: VehicleVisual(vp_normal=1),
                        24: VehicleVisual(vp_normal=1, base_normal='Data/Models/Base/missing.base')}
    library._visual_path = lambda path: None
    requests = []
    face = object()

    def faces(name, *, set_only=False):
        requests.append((name, set_only))
        return [face]

    library._base_faces = faces
    assert library.vehicle_mesh(23).faces == [(face, 0, 0)]
    assert library.vehicle_mesh(24).faces == [(face, 0, 0)]
    assert requests == [('normal.base', True), ('normal.base', True)]


def test_vp_faces_do_not_reuse_external_geometry_or_family_cache():
    normal = SimpleNamespace(owner_path='normal')
    normal.iter_tree = lambda: [normal]
    external = SimpleNamespace(owner_path='genesis')
    family = SimpleNamespace(root_object=external)
    normal_face, external_face = SimpleNamespace(mapped=True), object()
    loader = SimpleNamespace(_faces=[])
    loader._load_object = lambda *args, **kwargs: loader._faces.append(normal_face)
    library = SectorMeshLibrary.__new__(SectorMeshLibrary)
    library._base_cache = {}
    library._material_index = {}
    library._loader = loader
    library._family_faces = lambda *args: [external_face]
    library.assets = SimpleNamespace(
        base_object=lambda name, set_only=False: normal if set_only else external,
        _key=lambda name: name.casefold(), _loose_families={'same.base': family}, family=object())
    assert library._base_faces('same.base') == [external_face]
    assert library._base_faces('same.base', set_only=True) == [normal_face]
    assert library._base_faces('same.base') == [external_face]
