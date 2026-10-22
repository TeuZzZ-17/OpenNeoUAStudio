"""Real map boundary meshes must survive CPU and GPU scene construction."""

import numpy as np
import pytest

from .test_special_objects import app, win  # noqa: F401 - shared Qt fixtures
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.ldf_model import LdfDocument
from map_editor.render.camera import IsoCamera
from map_editor.render.gpu_scene import WorldScene
from map_editor.render.map_scene import scene_polygons
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.render.terrain_mesh import TerrainMesh


def test_status_boxes_share_one_row(win):
    layout = win.hover_height.parentWidget().layout()
    # Both widgets are in a nested horizontal layout.
    row = next(layout.itemAt(i).layout() for i in range(layout.count())
               if layout.itemAt(i).layout() is not None
               and layout.itemAt(i).layout().indexOf(win.hover_height) >= 0)
    assert row.indexOf(win.hover_height) == 0
    assert row.indexOf(win.map_issues_toggle) == 1


@pytest.mark.parametrize('resized', [False, True])
def test_set3_boundary_walls_in_both_scenes(app, resized):
    lib = SectorMeshLibrary(SetAssets(3).load())
    doc = LdfDocument(mw=5, mh=5)
    if resized:
        doc.resize(7, 7)
        assert doc.grids['type'][2][6] == 'fd'
        assert doc.grids['type'][6][2] == 'fe'
        # The old border ring is now plain interior, not a wall sector.
        assert doc.grids['type'][2][4] == '00'
        assert doc.grids['type'][4][2] == '00'
    before = doc.snapshot()
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    scene = WorldScene()
    scene.set_library(lib)
    scene.update(doc, terrain)
    cells = ((2, 0, 0xfc), (doc.mw - 1, 2, 0xfd),
             (2, doc.mh - 1, 0xfe), (0, 2, 0xff))
    for col, row, typ in cells:
        geometry, instances = scene.instances['sector', typ, True]
        vertices = np.concatenate((geometry.opaque, geometry.flat))
        assert vertices[:, 1].min() == -2700
        assert row * doc.mw + col in instances[:, 2]
    visible_walls = set()
    for yaw in (-135, -45, 45, 135):
        camera = IsoCamera(yaw=yaw, zoom=.04, width=800, height=800,
                           center=(doc.mw * 600, -1000, -doc.mh * 600))
        visible_walls.update(-p.payload[1] - 1
                             for p in scene_polygons(lib, doc, terrain, camera)
                             if p.payload[1] < 0)
    assert {r * doc.mw + c for c, r, _ in cells} <= visible_walls
    assert doc.snapshot() == before
