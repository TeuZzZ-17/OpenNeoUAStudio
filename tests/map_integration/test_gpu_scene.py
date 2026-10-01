import numpy as np
import pytest
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.ldf_model import LdfDocument
from map_editor.render.terrain_mesh import TerrainMesh
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.render.gpu_scene import WorldScene


@pytest.fixture(scope='module')
def library():
    return SectorMeshLibrary(SetAssets(1).load())


def test_gpu_instances_reuse_templates_and_only_rebuild_changed_neighbours(library):
    doc=LdfDocument(mw=30,mh=30)
    terrain=TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
    scene=WorldScene(); scene.set_library(library)
    scene.update(doc,terrain)
    assert scene.rebuilt_cells==900
    assert sum(len(g.opaque) for g in scene.chunks.values())==0
    assert sum(len(data) for _,data in scene.instances.values())>900
    assert len(scene.instances)<30
    doc.grids['hgt'][5][5]+=1
    terrain.rebuild(doc.grids['hgt'])
    assert not scene.update(doc,terrain)
    assert scene.rebuilt_cells==900
    doc.grids['type'][5][5]='05'
    changed=scene.update(doc,terrain)
    assert changed=={(0,0)}
    assert scene.rebuilt_cells==909
    assert scene.changed_templates
    fresh=WorldScene(); fresh.set_library(library); fresh.update(doc,terrain)
    # Incremental updates retain exactly the instance inventory of a full build.
    def inventory(world):
        return sorted((t.opaque.tobytes(), sorted(map(tuple,data)))
                      for t,data in world.instances.values())
    assert inventory(scene)==inventory(fresh)
    assert np.array_equal(scene.chunks[0,0].flat,fresh.chunks[0,0].flat)


def test_gpu_resizing_and_preview_library_switch_leave_no_old_instances(library):
    scene=WorldScene(); scene.set_library(library)
    doc=LdfDocument(mw=7,mh=7)
    terrain=TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
    scene.update(doc,terrain)
    scene.clear()
    doc=LdfDocument(mw=5,mh=5)
    terrain.rebuild(doc.grids['hgt']); scene.update(doc,terrain)
    assert len(scene.cells)==25
    assert all(np.all(data[:,2]<25) for _,data in scene.instances.values())
    for w,h in ((5,7),(7,5)):
        doc=LdfDocument(mw=w,mh=h)
        terrain.rebuild(doc.grids['hgt']); scene.update(doc,terrain)
        assert set(scene.cells)=={(c,r) for r in range(h) for c in range(w)}
    previews=WorldScene(); previews.set_library(library)
    previews.preview(5)
    assert len(previews.chunks[0,0].opaque)>0 and not previews.instances
    other=SectorMeshLibrary(SetAssets(2).load())
    previews.set_library(other)
    assert not previews.chunks and not previews._previews and not previews.materials
