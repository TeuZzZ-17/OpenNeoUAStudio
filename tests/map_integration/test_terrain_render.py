import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from map_editor.core.asset_bridge import SetAssets
from map_editor.core.factions import load_owner_colors
from map_editor.core.ldf_model import DEFAULT_HGT, LdfDocument, load_ldf, save_ldf
from map_editor.render.camera import IsoCamera
from map_editor.render.map_scene import ground_regions, render_scene, scene_polygons
from map_editor.render.sector_mesh import SectorMeshLibrary
from map_editor.render.terrain_mesh import TerrainMesh


@pytest.fixture(scope="module")
def library():
    app = QApplication.instance() or QApplication([])
    return app, SectorMeshLibrary(SetAssets(1).load())


def test_subtiles_share_edges_and_cover_runtime_plateau(library):
    _, lib = library
    faces = lib.mesh(0).faces
    rectangles = [(min(v[0] + x for v in f.vertices), max(v[0] + x for v in f.vertices),
                   min(v[2] + z for v in f.vertices), max(v[2] + z for v in f.vertices))
                  for f, x, z in faces]
    assert len(rectangles) == 9
    assert sorted(set((x0, x1) for x0, x1, z0, z1 in rectangles)) == [
        (-450, -150), (-150, 150), (150, 450)]
    assert sum((x1 - x0) * (z1 - z0) for x0, x1, z0, z1 in rectangles) == 900 * 900


def test_heights_and_slurp_vertices_match_runtime_after_save(library, tmp_path):
    _, lib = library
    doc = LdfDocument(mw=7, mh=7)
    for col, row, value in ((2, 2, 129), (3, 2, 133), (2, 3, 135), (3, 3, 143)):
        doc.grids['hgt'][row][col] = value
    path = tmp_path / "slopes.LDF"
    save_ldf(doc, path)
    terrain = TerrainMesh()
    terrain.rebuild(load_ldf(path).grids['hgt'])
    assert terrain.cell_y(3, 3) == -1600
    assert terrain.cell_center(3, 3) == (4200, -1600, -4200)
    assert terrain.corners[3, 3] == -800
    for vertical, first in ((False, -600), (True, -800)):
        values = terrain.filler_heights(3, 3, vertical)
        assert values[:4] == (first,) * 4
        assert values[4:8] == (-1600,) * 4
        assert values[9] == -800
        mesh = lib.filler_mesh(0, 1, vertical, values)
        template = lib.assets.family.root_object.kids[2].kids[36 * int(vertical) + 1]
        for face, _, _ in mesh.faces:
            for vertex, index in zip(face.vertices, template.skeleton.polygons[face.poly_id]):
                raw = template.skeleton.points[index]
                assert vertex == (raw[0], values[index], raw[2])
        assert {lib.surface_for(face).name for face, _, _ in mesh.faces}


def test_render_has_no_ground_gaps_and_owner_keeps_all_textures_unchanged(library):
    _, lib = library
    doc = LdfDocument(mw=7, mh=7)
    doc.grids['type'][3][3] = '05'
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    cam = IsoCamera(width=800, height=600, zoom=.065, center=(4200, 0, -4200))
    frame = render_scene(scene_polygons(lib, doc, terrain, cam), cam, lib.tables)
    for x in range(1250, 7200, 100):
        for z in range(1250, 7200, 100):
            screen, _ = cam.world_to_screen((x, 0, -z))
            sx, sy = map(int, screen)
            assert frame.cell_ids[sy, sx] != 0
    before = frame.rgba.copy()
    doc.grids['own'][3][3] = 6
    image = frame.image()
    after = np.frombuffer(image.bits(), np.uint8).reshape(600, 800, 4)
    assert np.array_equal(before, after)
    assert np.any(frame.cell_ids == -(3 * doc.mw + 3 + 1))


def test_split_ground_preserves_uvs_on_sector_boundary():
    vertices = [(1150., 0., -1300.), (1250., 0., -1300.), (1200., -100., -1400.)]
    regions = list(ground_regions(vertices, [(0, 0), (100, 0), (50, 100)], 5, 5))
    assert {c for c, r, v, uv in regions} == {0, 1}
    for c, r, vertices, uvs in regions:
        for vertex, uv in zip(vertices, uvs):
            assert uv[0] == pytest.approx(vertex[0] - 1150)
            assert uv[1] == pytest.approx(-vertex[2] - 1300)


def test_owner_colors_follow_game_world_ini():
    colors = load_owner_colors()
    assert colors[2] == (0, 179, 66)
    assert colors[3] == (232, 232, 232)
    assert colors[4] == (255, 171, 28)
    assert colors[5] == (73, 73, 73)
    assert colors[6] == (255, 0, 0)
