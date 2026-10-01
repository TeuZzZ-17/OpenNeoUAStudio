"""Native Qt/OpenGL regression probes; no desktop interaction or game writes.

Run separately from pytest (whose software UI tests use QT_QPA_PLATFORM=offscreen).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from OpenGL import GL
from PySide6.QtCore import Qt, QPointF, QEvent
from PySide6.QtGui import QMouseEvent, QOpenGLContext, QOffscreenSurface, QImage, QColor
from PySide6.QtOpenGL import QOpenGLFramebufferObject
from PySide6.QtWidgets import QApplication

from .. import bootstrap
from ..core.asset_bridge import SetAssets
from ..core.ldf_model import LdfDocument, DEFAULT_HGT, load_ldf
from ..render.camera import IsoCamera
from ..render.terrain_mesh import TerrainMesh, HEIGHT_UNIT
from ..render.sector_mesh import SectorMeshLibrary
from ..render.map_scene import scene_polygons, render_scene
from ..render.gpu_scene import WorldScene, Geometry, EMPTY
from ..render.gpu_renderer import GpuRenderer, camera_matrix
from ..render.gpu_viewport import gl_format
from .benchmark_gpu import summary


def camera_check():
    rng = np.random.default_rng(21)
    max_error = 0
    for yaw, pitch in ((-45, 35.26), (87, 8), (210, 62), (0, 90)):
        cam = IsoCamera(yaw=yaw, pitch=pitch, zoom=.037, width=1234, height=817,
                        center=(12000, -700, -32000), pan=(72, -83))
        points = rng.uniform(-40000, 40000, (100, 3))
        matrix = camera_matrix(cam, (64, 64))
        projected = points @ matrix[:3, :3].T + matrix[:3, 3]
        actual = np.column_stack(((projected[:, 0]+1)*cam.width*.5,
                                   (1-projected[:, 1])*cam.height*.5))
        expected = np.asarray([cam.world_to_screen(p)[0] for p in points])
        max_error = max(max_error, float(np.max(abs(actual-expected))))
    assert max_error < .001, max_error
    return {'camera_max_error_pixels': max_error}


def palette_check(renderer, target):
    """Assert palette shade/clear/stacked TRACY and opaque depth ordering."""
    values = np.arange(256, dtype=np.uint8)
    shade = (values[None, :].astype(int)+values[:, None]) % 256
    tracy = shade.copy()
    tables = SimpleNamespace(shader_pixels=shade.astype(np.uint8).tobytes(),
                             tracy_pixels=tracy.astype(np.uint8).tobytes(),
                             display_palette=np.repeat(values[:, None], 3, axis=1))
    material = SimpleNamespace(kind='texture', width=2, height=1, indices=bytes((0, 11)))
    def quad(y, source, mode=0, texture=False, shade=-1):
        points = [(0,y,0), (1200,y,0), (1200,y,-1200),
                  (0,y,0), (1200,y,-1200), (0,y,-1200)]
        return np.asarray([(*v, v[0]/1200*256, 0, 0 if texture else -1,
                            2, 1, shade, mode, source, 1, 0, 0, 0, 0)
                           for v in points], np.float32)
    camera = IsoCamera(yaw=0, pitch=90, zoom=.25, width=500, height=360,
                       center=(600,0,-600))
    records = []
    for name, opaque, flat, expected in (
            ('stacked_tracy', quad(0,10), np.concatenate((quad(-10,20,2), quad(-20,30,2))), (60,60)),
            ('opaque_occludes_tracy', np.concatenate((quad(0,10),quad(-40,70))),
             np.concatenate((quad(-10,20,2),quad(-20,30,2))), (70,70)),
            ('clear_then_shade', np.concatenate((quad(0,10),quad(-40,0,1,True,5))), EMPTY, (10,16))):
        faces = [(i,6) for i in range(0,len(flat),6)]
        scene = SimpleNamespace(lib=SimpleNamespace(tables=tables), materials=[material],
                                chunks={(0,0):Geometry(opaque,flat,faces)})
        renderer.set_scene(scene,{(0,0)})
        renderer.set_states(np.zeros((1,1,4),np.uint8))
        renderer.set_heights(np.zeros((1,1),np.float32))
        renderer.render(camera,500,360,target.handle(),owner_colors={},overlays=False)
        pixels = renderer.read_indices()
        actual = (int(pixels[180,170]),int(pixels[180,330]))
        assert actual == expected, (name,actual,expected)
        records.append(name)
    return records


def parity_check(renderer, target):
    results = []
    for number in range(1, 7):
        lib = SectorMeshLibrary(SetAssets(number).load())
        doc = LdfDocument(mw=7, mh=7, set_number=number)
        types = sorted(lib.assets.sdf.sectors)
        for row in range(1,6):
            for col in range(1,6):
                doc.grids['type'][row][col] = f'{types[(row*5+col)%len(types)]:02x}'
                doc.grids['hgt'][row][col] += (row+col)%4
        doc.normalize_border_heights()
        terrain = TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
        scene = WorldScene(); scene.set_library(lib)
        renderer.set_scene(scene,scene.update(doc,terrain))
        renderer.set_states(np.zeros((7,7,4),np.uint8))
        renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
        for yaw,pitch in ((-45,35.26),(70,57),(0,90)):
            camera = IsoCamera(yaw=yaw,pitch=pitch,zoom=.028,width=500,height=360,
                               center=(4200,-300,-4200),pan=(17,-13))
            renderer.render(camera,500,360,target.handle(),owner_colors={},overlays=False)
            ids = renderer.read_ids()
            indices = renderer.read_indices()
            cpu = render_scene(scene_polygons(lib,doc,terrain,camera),camera,lib.tables)
            coverage = np.count_nonzero((ids!=0)!=(cpu.cell_ids!=0))
            picking = np.count_nonzero(ids!=cpu.cell_ids)
            # GPU top-left edge rules can choose the adjacent triangle at exact ties.
            assert coverage < 30, (number,yaw,coverage)
            assert picking < 150, (number,yaw,picking)
            mask = (ids == cpu.cell_ids) & (ids != 0)
            palette = np.asarray(lib.tables.display_palette, np.uint8)
            rgb_difference = float(np.mean(np.any(palette[indices][mask] != cpu.rgba[:, :, :3][mask], axis=1)))
            results.append(dict(set=number,yaw=yaw,pitch=pitch,
                                coverage_difference=int(coverage),picking_difference=int(picking),
                                rgb_difference_fraction=rgb_difference))
    return results


def annotation_check(renderer, target):
    lib=SectorMeshLibrary(SetAssets(1).load())
    doc=LdfDocument(mw=5,mh=5)
    doc.grids['type'][2][2]='05'
    terrain=TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
    scene=WorldScene(); scene.set_library(lib)
    renderer.set_scene(scene,scene.update(doc,terrain))
    renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
    states=np.zeros((5,5,4),np.uint8); states[2,2,0]=6
    renderer.set_states(states)
    sky=QImage(4,4,QImage.Format.Format_RGBA8888); sky.fill(QColor(18,52,86))
    renderer.set_sky(sky)
    camera=IsoCamera(width=500,height=360,zoom=.046,center=(3000,0,-3000))
    def pixels(**options):
        renderer.render(camera,500,360,target.handle(),owner_colors={6:(255,0,0)},grid=False,**options)
        image=target.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        return np.frombuffer(image.bits(),np.uint8).reshape(360,500,4).copy()
    base=pixels(overlays=False)
    owned=pixels()
    ids=renderer.read_ids()
    changes=np.any(base!=owned,axis=2)
    assert changes.any() and not np.any(changes & (ids!=13))
    assert np.all(owned[changes,:3]==(255,0,0))
    assert np.count_nonzero(changes)<np.count_nonzero(ids==13)/3
    assert np.any(ids<0) and not np.any(changes & (ids<0))
    states[2,2,1:3]=1; renderer.set_states(states)
    marked=pixels(hover=13)
    assert np.array_equal(marked[changes],owned[changes]), 'Selection covered ownership borders'
    assert tuple(marked[0,0,:3])==(18,52,86)
    dark=pixels(sky=False)
    assert tuple(dark[0,0,:3])==(18,22,28)
    assert renderer.pick(0,0)==0
    y,x=np.argwhere(ids==13)[0]
    assert renderer.pick(int(x),int(y))==13
    renderer.set_sky(None)
    return ['owner_borders_only','buildings_occlude_borders','selection_preserves_owners',
            'sky_on_off','depth_picking']


def transparency_batch_check(renderer,target):
    lib=SectorMeshLibrary(SetAssets(1).load())
    doc=load_ldf(str(bootstrap.game_data_dir()/'Levels/Single/L0101.LDF'))
    terrain=TerrainMesh(); terrain.rebuild(doc.grids['hgt'])
    scene=WorldScene(); scene.set_library(lib)
    renderer.set_scene(scene,scene.update(doc,terrain))
    renderer.set_heights(-(terrain.cells-DEFAULT_HGT)*HEIGHT_UNIT)
    renderer.set_states(np.zeros((doc.mh,doc.mw,4),np.uint8))
    batch=renderer._transparency_batches
    barrier=renderer.texture_barrier
    records=[]
    for yaw,pitch in ((-45,35.26),(0,90),(81,56)):
        cam=IsoCamera(yaw=yaw,pitch=pitch,width=500,height=360,zoom=.028,
                      center=(doc.mw*600,0,-doc.mh*600))
        renderer.texture_barrier=False
        renderer._transparency_batches=lambda faces,w,h: ((f,) for f in sorted(faces,key=lambda f:f[0]))
        renderer.render(cam,500,360,target.handle(),owner_colors={},overlays=False)
        reference=renderer.read_indices()
        renderer._transparency_batches=batch
        # Opaque ties must not depend on allocation/edit insertion order.
        renderer.instance_buffers=dict(reversed(list(renderer.instance_buffers.items())))
        for enabled in (False,barrier):
            renderer.texture_barrier=enabled
            renderer.render(cam,500,360,target.handle(),owner_colors={},overlays=False)
            actual=renderer.read_indices()
            difference=int(np.count_nonzero(actual!=reference))
            assert difference==0, ('TRACY batch ordering',yaw,enabled,difference)
            records.append(dict(yaw=yaw,barrier=enabled,differing_pixels=difference))
    return records


def ui_check(app, output):
    from ..ui.main_window import MainWindow
    from ..ui.dialogs import LevelInfoDialog
    from ..tools.brush import BrushMode
    win = MainWindow(load_ldf(str(bootstrap.game_data_dir()/'Levels/Single/L0101.LDF')))
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.resize(1600,1000); win.show(); app.processEvents(); win._icons.stop()
    view = win.view
    assert view.renderer is not None and not view._software_fallback
    assert win.renderer_badge.text() == 'GPU'
    def frame():
        view._canvas.repaint()
        view._canvas.makeCurrent(); GL.glFinish(); view._canvas.doneCurrent()
        assert not view._software_fallback
    frame()
    def measure(action, count=40):
        times=[]
        for i in range(count):
            start=time.perf_counter(); action(i); frame()
            times.append((time.perf_counter()-start)*1000)
        return summary(times)
    def mouse(kind,x,y,button=Qt.MouseButton.NoButton,buttons=Qt.MouseButton.NoButton):
        event=QMouseEvent(kind,QPointF(x,y),QPointF(x,y),button,buttons,Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view,event)
    def drag(button):
        mouse(QEvent.Type.MouseButtonPress,400,300,button,button)
        result=measure(lambda i: mouse(QEvent.Type.MouseMove,400+i*2,300+i%5,buttons=button))
        view._canvas.makeCurrent(); before=view.renderer.read_indices(); view._canvas.doneCurrent()
        mouse(QEvent.Type.MouseButtonRelease,480,300,button)
        frame()
        view._canvas.makeCurrent(); after=view.renderer.read_indices(); view._canvas.doneCurrent()
        assert np.array_equal(before,after), 'Release changed the rendered geometry or proportions'
        return result
    result={'renderer':view.renderer_name,'viewport':[view.width(),view.height()],
            'rotate_ms':drag(Qt.MouseButton.RightButton),
            'pan_ms':drag(Qt.MouseButton.MiddleButton)}
    built=view.world_scene.rebuilt_cells
    result['zoom_ms']=measure(lambda i:setattr(view.camera,'zoom',.032+(i%20)*.001))
    result['hover_pick_ms']=measure(lambda i:mouse(QEvent.Type.MouseMove,350+i*3,320))
    win.set_tool('owner'); win.sel_owner=6
    def owner(i):
        win._pressed(3+i%8,5,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        win._released(3+i%8,5,Qt.KeyboardModifier.NoModifier)
    result['owner_paint_ms']=measure(owner)
    win.set_tool('terrain'); win.brush.radius=5; win.brush.strength=1
    win.brush.begin_stroke(win.doc,7,7)
    def terrain(i):
        touched=win.brush.apply(win.doc,7,7,BrushMode.RAISE if i%20<10 else BrushMode.LOWER)
        view.terrain_changed(touched)
    result['wide_terrain_ms']=measure(terrain)
    assert view.world_scene.rebuilt_cells==built, 'Camera/owner/height action rebuilt geometry'
    win.set_tool('sector')
    def sector(i):
        win.sel_typ=5 if i%2 else 1
        win._pressed(6,6,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
        win._released(6,6,Qt.KeyboardModifier.NoModifier)
    result['sector_stamp_ms']=measure(sector,20)
    result['undo_redo_ms']=measure(lambda i:win.undo() if i%2==0 else win.redo(),20)
    win.set_tool('terrain')
    win._pressed(7,7,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    win._released(7,7,Qt.KeyboardModifier.NoModifier); frame()
    built=view.world_scene.rebuilt_cells
    result['height_undo_redo_ms']=measure(lambda i:win.undo() if i%2==0 else win.redo(),20)
    assert view.world_scene.rebuilt_cells==built
    result['view_toggles_ms']=measure(lambda i:(view.set_grid(i%2==0),view.set_sky_visible(i%2==0)))
    result['resize_ms']=measure(lambda i:win.resize(1550+i%4*20,950+i%3*20),20)
    lib=win._lib()
    icon_times=[]
    for typ in list(lib.assets.sdf.sectors)[:24]:
        start=time.perf_counter(); sprite=view.render_icon(lib,typ,1.1)
        assert sprite and not sprite.image.isNull()
        icon_times.append((time.perf_counter()-start)*1000)
    result['uncached_gpu_icon_ms']=summary(icon_times)
    start=time.perf_counter(); dialog=LevelInfoDialog(win.doc,win)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
    dialog.show(); app.processEvents()
    result['level_info_open_ms']=(time.perf_counter()-start)*1000
    deadline=time.monotonic()+10
    while dialog._preview_jobs and time.monotonic()<deadline:
        app.processEvents(); time.sleep(.005)
    assert not dialog._preview_jobs
    assert any(not item.icon().isNull() for item in dialog.sky_items.values())
    dialog.close()
    win.set_tool('sector'); win._icons.stop()
    for _ in range(10):
        win._make_icon(); win._icons.stop()
    frame()
    if output:
        win.grab().save(str(output.with_suffix('.png')))
    result['frame_submission_ms']=summary(view.frame_ms)
    win.dirty=False; win.close(); view._canvas.cleanup()
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    app=QApplication.instance() or QApplication([])
    surface=QOffscreenSurface(); surface.setFormat(gl_format()); surface.create()
    context=QOpenGLContext(); context.setFormat(gl_format())
    assert context.create() and context.makeCurrent(surface), 'Use the native Qt platform'
    renderer=GpuRenderer(); target=QOpenGLFramebufferObject(500,360)
    results=camera_check()
    results['palette_checks']=palette_check(renderer,target)
    barrier = renderer.texture_barrier
    renderer.texture_barrier = False
    results['palette_copy_fallback_checks'] = palette_check(renderer,target)
    renderer.texture_barrier = barrier
    results['texture_barrier'] = barrier
    results['parity']=parity_check(renderer,target)
    results['annotations']=annotation_check(renderer,target)
    results['transparency_batches']=transparency_batch_check(renderer,target)
    renderer.delete(); del target; context.doneCurrent()
    results['ui']=ui_check(app,args.output)
    args.output.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2),flush=True)


if __name__=='__main__':
    main()
