"""Large-map and dense-geometry timing, using local installed assets."""
import argparse
import json
import time
import numpy as np
from pathlib import Path
from OpenGL import GL
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QOpenGLContext, QOffscreenSurface
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from ..core.asset_bridge import SetAssets
from ..core.ldf_model import LdfDocument, DEFAULT_HGT
from ..render.sector_mesh import SectorMeshLibrary
from ..render.terrain_mesh import TerrainMesh, HEIGHT_UNIT
from ..render.gpu_scene import WorldScene
from ..render.gpu_renderer import GpuRenderer
from ..render.gpu_viewport import gl_format
from ..render.camera import IsoCamera
from .benchmark_gpu import summary


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    app=QApplication([])
    surface=QOffscreenSurface(); surface.setFormat(gl_format()); surface.create()
    context=QOpenGLContext(); context.setFormat(gl_format())
    assert context.create() and context.makeCurrent(surface)
    renderer=GpuRenderer(); target=QOpenGLFramebufferObject(1920,1080)
    lib=SectorMeshLibrary(SetAssets(1).load())
    transparent_types = [typ for typ in lib.assets.sdf.sectors
                         if any(lib.surface_for(f).tracy_mode == 'flat'
                                for f, _, _ in lib.mesh(typ).faces)]
    print('Sectors with TRACY:', transparent_types, flush=True)
    results=[]
    for size,dense in ((64,False),(128,False),(255,False),(30,True)):
        doc=LdfDocument(mw=size,mh=size)
        if dense:
            for row in range(1,size-1):
                for col in range(1,size-1):
                    doc.grids['type'][row][col]=f'{transparent_types[(col+row)%len(transparent_types)]:02x}'
        terrain=TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
        scene=WorldScene(); scene.set_library(lib)
        start=time.perf_counter(); changed=scene.update(doc,terrain)
        renderer.set_scene(scene,changed)
        renderer.set_states(np.zeros((size,size,4),np.uint8))
        renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
        build_ms=(time.perf_counter()-start)*1000
        camera=IsoCamera(width=1920,height=1080,zoom=1000/(size*1200*.75),
                         center=(size*600,0,-size*600))
        times=[]; gpu_times=[]
        query=int(GL.glGenQueries(1)[0])
        for i in range(30):
            camera.yaw=-45+i*1.7; camera.pitch=35+i*.5
            start=time.perf_counter()
            GL.glBeginQuery(GL.GL_TIME_ELAPSED,query)
            renderer.render(camera,1920,1080,target.handle(),owner_colors={})
            GL.glEndQuery(GL.GL_TIME_ELAPSED)
            GL.glFinish(); times.append((time.perf_counter()-start)*1000)
            gpu_times.append(float(GL.glGetQueryObjectuiv(query,GL.GL_QUERY_RESULT))/1e6)
        record=dict(size=size,dense=dense,build_ms=build_ms,frame_ms=summary(times[2:]),
                    flat_faces=sum(len(g.flat_faces) for g in scene.chunks.values()),
                    draw_calls=renderer.draw_calls, gpu_ms=summary(gpu_times[2:]),
                    vertices=(sum(len(g.flat) for g in scene.chunks.values())+
                              sum(len(t.opaque)*len(data) for t,data in scene.instances.values())),
                    texture_barrier=renderer.texture_barrier)
        if not dense:
            terrain_times=[]; sector_times=[]; undo_times=[]
            cell=size//2
            for i in range(12):
                start=time.perf_counter(); snapshot=doc.snapshot()
                doc.grids['hgt'][cell][cell]+=1
                terrain.rebuild(doc.grids['hgt'])
                renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
                renderer.render(camera,1920,1080,target.handle(),owner_colors={})
                GL.glFinish(); terrain_times.append((time.perf_counter()-start)*1000)
                start=time.perf_counter(); doc.restore(snapshot)
                terrain.rebuild(doc.grids['hgt'])
                renderer.set_scene(scene,scene.update(doc,terrain))
                renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
                renderer.render(camera,1920,1080,target.handle(),owner_colors={})
                GL.glFinish(); undo_times.append((time.perf_counter()-start)*1000)
                start=time.perf_counter(); doc.snapshot()
                doc.grids['type'][cell][cell]='05' if i%2 else '01'
                renderer.set_scene(scene,scene.update(doc,terrain,[(cell,cell)]))
                renderer.render(camera,1920,1080,target.handle(),owner_colors={})
                GL.glFinish(); sector_times.append((time.perf_counter()-start)*1000)
            record.update(terrain_ms=summary(terrain_times),undo_ms=summary(undo_times),
                          sector_ms=summary(sector_times))
        GL.glDeleteQueries(1,[query])
        results.append(record); print(json.dumps(record),flush=True)
    args.output.write_text(json.dumps(results,indent=2),encoding='utf-8')
    renderer.delete(); del target


if __name__=='__main__':
    main()
