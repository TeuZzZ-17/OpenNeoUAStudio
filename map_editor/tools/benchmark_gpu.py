"""Run with a native Qt platform: python -m map_editor.tools.benchmark_gpu."""
from __future__ import annotations
import argparse
import json
import time
from pathlib import Path
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QOpenGLContext, QOffscreenSurface, QImage
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from OpenGL import GL

from .. import bootstrap
from ..core.asset_bridge import SetAssets
from ..core.ldf_model import LdfDocument, load_ldf, DEFAULT_HGT
from ..core.factions import load_owner_colors
from ..render.camera import IsoCamera
from ..render.terrain_mesh import TerrainMesh, HEIGHT_UNIT
from ..render.sector_mesh import SectorMeshLibrary
from ..render.map_scene import scene_polygons, render_scene
from ..render.gpu_scene import WorldScene
from ..render.gpu_renderer import GpuRenderer
from ..render.gpu_viewport import gl_format


def summary(values):
    return {name: float(np.percentile(values, p)) for name, p in
            (('median', 50), ('p95', 95), ('max', 100))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preview', type=Path)
    parser.add_argument('--frames', type=int, default=60)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    surface = QOffscreenSurface(); surface.setFormat(gl_format()); surface.create()
    context = QOpenGLContext(); context.setFormat(gl_format())
    if not context.create() or not context.makeCurrent(surface):
        raise RuntimeError('A native OpenGL 3.3 context is required (not QT_QPA_PLATFORM=offscreen)')
    renderer = GpuRenderer()
    lib = SectorMeshLibrary(SetAssets(1).load())
    target = QOpenGLFramebufferObject(1000, 700)
    owners = load_owner_colors()
    cases = [('flat30', LdfDocument(mw=30, mh=30)),
             ('L0101', load_ldf(str(bootstrap.game_data_dir()/'Levels/Single/L0101.LDF')))]
    hill = LdfDocument(mw=15, mh=15)
    for r in range(5, 10):
        for c in range(5, 10):
            hill.grids['hgt'][r][c] += 8
    hill.grids['type'][7][7] = '05'
    cases.append(('hill', hill))
    results = {'gpu': renderer.name, 'size': [1000, 700], 'cases': []}
    for name, doc in cases:
        if lib.assets.set_number != doc.set_number:
            lib = SectorMeshLibrary(SetAssets(doc.set_number).load())
        terrain = TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
        scene = WorldScene(); scene.set_library(lib)
        start = time.perf_counter(); changed = scene.update(doc, terrain)
        build_ms = (time.perf_counter()-start)*1000
        renderer.set_scene(scene, changed)
        renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
        states = np.zeros((doc.mh, doc.mw, 4), np.uint8)
        renderer.set_states(states)
        cam = IsoCamera(width=1000, height=700, zoom=650/(max(doc.mw,doc.mh)*1200*.75),
                        center=(doc.mw*600, 0, -doc.mh*600))
        renderer.render(cam, 1000, 700, target.handle(), owner_colors=owners, grid=False)
        GL.glFinish()
        ids = renderer.read_ids(); indices = renderer.read_indices()
        fixed_timings=[]
        for _ in range(20):
            start=time.perf_counter()
            renderer.render(cam,1000,700,target.handle(),owner_colors=owners,grid=False)
            GL.glFinish(); fixed_timings.append((time.perf_counter()-start)*1000)
        start=time.perf_counter(); polygons=scene_polygons(lib,doc,terrain,cam)
        cpu_scene_ms=(time.perf_counter()-start)*1000
        start=time.perf_counter(); cpu=render_scene(polygons,cam,lib.tables)
        cpu_raster_ms=(time.perf_counter()-start)*1000
        pal=np.asarray(lib.tables.display_palette,np.uint8)
        rgb=pal[indices]
        valid=(ids!=0)&(cpu.cell_ids!=0)
        interior=valid.copy()
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
            interior &= np.roll(ids,(dy,dx),(0,1))==ids
            interior &= np.roll(cpu.cell_ids,(dy,dx),(0,1))==cpu.cell_ids
        differences=np.any(rgb!=cpu.rgba[:,:,:3],axis=2)
        record={'name':name,'build_ms':build_ms,'cpu_scene_ms':cpu_scene_ms,
                'same_camera_gpu_frame_ms':summary(fixed_timings),
                'cpu_raster_ms':cpu_raster_ms,'coverage_pixels':int(np.count_nonzero(ids)),
                'coverage_difference_pixels':int(np.count_nonzero((ids!=0)!=(cpu.cell_ids!=0))),
                'id_difference_pixels':int(np.count_nonzero(ids!=cpu.cell_ids)),
                'interior_rgb_difference_fraction':float(np.mean(differences[interior])),
                'materials':len(scene.materials),'chunks':len(scene.chunks),
                'transparent_faces':sum(len(g.flat_faces) for g in scene.chunks.values())}
        if args.preview:
            args.preview.mkdir(parents=True,exist_ok=True)
            rgba=np.empty((700,1000,4),np.uint8); rgba[:,:,:3]=rgb; rgba[:,:,3]=255
            QImage(rgba.data,1000,700,4000,QImage.Format.Format_RGBA8888).save(str(args.preview/(name+'-gpu.png')))
            cpu.image().save(str(args.preview/(name+'-cpu.png')))
        timings=[]
        for i in range(args.frames):
            cam.yaw=-45+i*1.3; cam.pitch=25+(i%35); cam.zoom*=1.001
            start=time.perf_counter()
            renderer.render(cam,1000,700,target.handle(),owner_colors=owners)
            GL.glFinish(); timings.append((time.perf_counter()-start)*1000)
        record['camera_frame_ms']=summary(timings)
        timings=[]
        for i in range(args.frames):
            start=time.perf_counter()
            terrain.cells[2:-2,2:-2]=DEFAULT_HGT+i%20
            renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
            renderer.render(cam,1000,700,target.handle(),owner_colors=owners)
            GL.glFinish(); timings.append((time.perf_counter()-start)*1000)
        record['height_edit_frame_ms']=summary(timings)
        record['geometry_cells_built']=scene.rebuilt_cells
        results['cases'].append(record)
        print(json.dumps(record),flush=True)
    args.output.write_text(json.dumps(results,indent=2),encoding='utf-8')
    renderer.delete()


if __name__ == '__main__':
    main()
