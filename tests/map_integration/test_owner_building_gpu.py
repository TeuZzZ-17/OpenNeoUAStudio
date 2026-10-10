"""GPU regression for ownership borders over a building sector."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication

from map_editor.render.gpu_renderer import GpuRenderer, gl
from map_editor.render.gpu_viewport import gl_format


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('actor_preview', [None, 'squad', 'host', 'special'])
def test_owner_color_and_actor_draft_grayscale_are_independent(app, actor_preview):
    surface = QOffscreenSurface()
    surface.setFormat(gl_format())
    surface.create()
    if not surface.isValid():
        pytest.skip("OpenGL offscreen surface is unavailable")

    context = QOpenGLContext()
    context.setFormat(gl_format())
    if not context.create() or not context.isValid():
        surface.destroy()
        pytest.skip("OpenGL context is unavailable")
    actual = context.format()
    if ((actual.majorVersion(), actual.minorVersion()) < (3, 3)
            or actual.profile() != gl_format().profile()):
        surface.destroy()
        pytest.skip("OpenGL 3.3 Core is unavailable")

    previous_context = QOpenGLContext.currentContext()
    previous_surface = previous_context.surface() if previous_context else None
    if not context.makeCurrent(surface):
        surface.destroy()
        pytest.skip("OpenGL context could not be made current")

    renderer = None
    target = target_texture = 0
    width, height = 8, 4
    owner_color = (240, 12, 36)
    palette_color = (42, 76, 104)
    try:
        renderer = GpuRenderer()
        renderer._resize(width, height)

        # A building is encoded in state.a; its visible terrain sector pixels
        # still have positive cell IDs. Negative IDs represent the building
        # silhouette and must keep the normal palette color.
        states = np.array([[[6, 0, 0, 1]]], dtype=np.uint8)
        renderer.set_states(states)
        palette = np.array([[[0, 0, 0, 0], [*palette_color, 255]]],
                           dtype=np.uint8).reshape(1, 2, 4)
        renderer._image(renderer.palette, palette, gl.GL_RGBA8, gl.GL_RGBA)

        indices = np.ones((height, width), dtype=np.uint8)
        cells = np.ones((height, width), dtype=np.int32)
        cells[:, width // 2:] = -1
        if actor_preview is not None:
            # Actor IDs follow terrain, squads, then hosts. A special after one
            # squad and host therefore uses -4 and style entry two.
            if actor_preview == 'special':
                cells[:, :width // 3] = -2
                cells[:, width // 3:2 * width // 3] = -3
                cells[:, 2 * width // 3:] = -4
                flags = (1, 1, 3)
            else:
                cells[:, :width // 2] = -2
                cells[:, width // 2:] = -3
                flags = (3, 1) if actor_preview == 'squad' else (1, 3)
            renderer.set_unit_styles([(1, 0, 0, flag) for flag in flags])
        edges = np.ones((height, width, 4), dtype=np.float32)

        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.indices)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, indices)
        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.ids)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RED_INTEGER, gl.GL_INT, cells)
        gl.glBindTexture(gl.GL_TEXTURE_2D, renderer.edges)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                           gl.GL_RGBA, gl.GL_FLOAT, edges)

        target_texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, target_texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        target = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, target_texture, 0)
        gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
        assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE

        gl.glDisable(gl.GL_FRAMEBUFFER_SRGB)
        renderer._present(
            target, width, height, owner_colors={6: owner_color}, grid=False,
            sky=False, hover=0, overlays=actor_preview is None)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE, pixels)

        if actor_preview is None:
            assert tuple(pixels[1, 1, :3]) == owner_color
            assert tuple(pixels[1, width - 2, :3]) == palette_color
        else:
            draft_x, existing_x = ((width-2, 1) if actor_preview in ('host', 'special')
                                   else (1, width-2))
            draft = pixels[1, draft_x, :3]
            assert max(draft) == min(draft)
            assert tuple(pixels[1, existing_x, :3]) == palette_color
    finally:
        if renderer is not None:
            renderer.delete()
        if target:
            gl.glDeleteFramebuffers(1, [target])
        if target_texture:
            gl.glDeleteTextures(1, [target_texture])
        if previous_context is not None and previous_surface is not None:
            previous_context.makeCurrent(previous_surface)
        else:
            context.doneCurrent()
        surface.destroy()




def test_real_special_scene_ids_and_gpu_selected_and_drag_contours(app):
    from types import SimpleNamespace

    from map_editor.core.asset_bridge import SetAssets
    from map_editor.core.ldf_model import DEFAULT_HGT, LdfDocument
    from map_editor.core.special_objects import add_special, update_special
    from map_editor.render.camera import IsoCamera
    from map_editor.render.sector_mesh import SectorMeshLibrary
    from map_editor.render.special_scene import SPECIAL_COLORS, actor_code, scene_object_styles, special_cells, special_members
    from map_editor.render.terrain_mesh import HEIGHT_UNIT, TerrainMesh
    from map_editor.render.gpu_scene import WorldScene

    surface = QOffscreenSurface()
    surface.setFormat(gl_format())
    surface.create()
    if not surface.isValid():
        pytest.skip("OpenGL offscreen surface is unavailable")

    context = QOpenGLContext()
    context.setFormat(gl_format())
    if not context.create() or not context.isValid():
        surface.destroy()
        pytest.skip("OpenGL context is unavailable")
    actual = context.format()
    if ((actual.majorVersion(), actual.minorVersion()) < (3, 3)
            or actual.profile() != gl_format().profile()):
        surface.destroy()
        pytest.skip("OpenGL 3.3 Core is unavailable")

    previous_context = QOpenGLContext.currentContext()
    previous_surface = previous_context.surface() if previous_context else None
    if not context.makeCurrent(surface):
        surface.destroy()
        pytest.skip("OpenGL context could not be made current")

    renderer = target = target_texture = 0
    try:
        lib = SectorMeshLibrary(SetAssets(1).load())
        doc = LdfDocument(mw=9, mh=9)
        positions = {'gate': (3, 3), 'item': (4, 4), 'gem': (5, 5)}
        actions = [{'target_type': 'modify_vehicle', 'id': 1,
                    'param': 'enable', 'val': '1'}]
        for kind, (x, y) in positions.items():
            slot = add_special(doc, kind)
            if kind == 'gem':
                doc.grids['type'][y][x] = '05'
                values = {'x': x, 'y': y, 'actions': actions}
            else:
                values = {'x': x, 'y': y}
            update_special(doc, kind, slot, values)

        terrain = TerrainMesh()
        terrain.rebuild(doc.grids['hgt'])
        scene = WorldScene()
        scene.set_library(lib)
        changed = scene.update(doc, terrain)
        members = list(special_members(doc, lib))
        codes = {member.kind: actor_code(doc, index)
                 for index, member in enumerate(members)}
        assert set(codes) == {'gate', 'item', 'gem'}

        renderer = GpuRenderer()
        width, height = 640, 480
        renderer.set_scene(scene, changed)
        renderer.set_heights(-(terrain.cells - DEFAULT_HGT) * HEIGHT_UNIT)
        renderer.set_states(np.zeros((doc.mh, doc.mw, 4), np.uint8))
        renderer._resize(width, height)

        target_texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, target_texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height,
                        0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        target = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, target_texture, 0)
        gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
        assert gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE

        camera = IsoCamera(yaw=0, pitch=90, zoom=.12, width=width, height=height,
                           center=(5400, 0, -5400))
        view = SimpleNamespace(doc=doc, lib=lib, owner_colors={}, dragged_codes=set(),
                               selected_special=None, selected_host=-1, squad_overlay=None)

        def render(*, clear_view=False):
            styles = [(*(color / 255 for color in row[:3]), row[3])
                      for row in scene_object_styles(view)]
            renderer.set_unit_styles(styles)
            renderer.render(camera, width, height, target, owner_colors={},
                            grid=False, sky=False, overlays=True,
                            clear_view=clear_view)
            ids = renderer.read_ids()
            previous = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, target)
            gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
            pixels = np.empty((height, width, 4), dtype=np.uint8)
            gl.glReadPixels(0, 0, width, height, gl.GL_RGBA,
                            gl.GL_UNSIGNED_BYTE, pixels)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, previous)
            return ids, pixels[::-1].copy()

        ids, pixels = render()
        for kind, code in codes.items():
            assert np.any(ids == code), f'{kind} actor code {code} did not reach the GPU raster'
            edge_color = np.asarray(SPECIAL_COLORS[kind], np.uint8)
            assert np.any(np.all(pixels[:, :, :3] == edge_color, axis=2)), (
                f'{kind} actor edge color was not drawn')

        ground_ids, _ = render(clear_view=True)
        assert np.any(ground_ids > 0), 'Clear View removed the terrain cells'
        assert not np.any(ground_ids < 0), 'Clear View retained non-ground GPU geometry'
        restored_ids, _ = render()
        assert all(np.any(restored_ids == code) for code in codes.values()), (
            'turning Clear View off did not restore actor geometry')

        gate_code = codes['gate']
        view.selected_special = ('gate', 1, -1)
        selected_ids, selected_pixels = render()
        gate_mask = selected_ids == gate_code
        assert np.any(gate_mask)
        near_gate = np.zeros(gate_mask.shape, dtype=bool)
        for distance in (1, 2):
            near_gate[distance:, :] |= gate_mask[:-distance, :]
            near_gate[:-distance, :] |= gate_mask[distance:, :]
            near_gate[:, distance:] |= gate_mask[:, :-distance]
            near_gate[:, :-distance] |= gate_mask[:, distance:]
        white = np.all(selected_pixels[:, :, :3] >= 250, axis=2)
        assert np.any(white & near_gate & ~gate_mask), 'selected gate has no white contour'

        view.selected_special = None
        view.dragged_codes = {gate_code}
        dragged_ids, dragged_pixels = render()
        dragged_mask = dragged_ids == gate_code
        assert np.any(dragged_mask)
        actor_rgb = dragged_pixels[dragged_mask, :3]
        grayscale = np.max(actor_rgb, axis=1) == np.min(actor_rgb, axis=1)
        assert grayscale.mean() > .9, 'dragged gate pixels are not rendered in grayscale'

        # Special actor IDs include the squad/host count. Updating the same
        # WorldScene after an actor is added or removed must re-encode every
        # special cell so raster picking continues to resolve the right slot.
        doc.squads.append(dict(owner=1, veh=1, num=1, x=6, y=6,
                               hidden=False, useable=False))
        changed = scene.update(doc, terrain)
        renderer.set_scene(scene, changed)
        shifted_members = list(special_members(doc, lib))
        shifted_codes = {member.kind: actor_code(doc, index)
                         for index, member in enumerate(shifted_members)
                         if member.key < 0}
        assert shifted_codes == {kind: code - 1 for kind, code in codes.items()}
        shifted_ids, _ = render()
        for kind, code in shifted_codes.items():
            assert np.any(shifted_ids == code), f'{kind} ID was not re-encoded after adding squad'

        doc.squads.clear()
        changed = scene.update(doc, terrain)
        renderer.set_scene(scene, changed)
        restored_ids, _ = render()
        for kind, code in codes.items():
            assert np.any(restored_ids == code), f'{kind} ID was not restored after removing squad'

        # A special in the old outer map area must be dropped cleanly when a
        # resize shrinks that area away; the next update must not revisit its
        # old coordinate or retain it in the scene's special lookup.
        edge_slot = add_special(doc, 'gate')
        update_special(doc, 'gate', edge_slot, {'x': 7, 'y': 7})
        terrain.rebuild(doc.grids['hgt'])
        scene.update(doc, terrain)
        assert (7, 7) in special_cells(doc, lib)
        doc.resize(5, 5, remove_out_of_bounds=True)
        terrain.rebuild(doc.grids['hgt'])
        changed = scene.update(doc, terrain)
        renderer.set_scene(scene, changed)
        assert (7, 7) not in scene.cells
        assert (7, 7) not in scene._specials
        assert all(0 <= col < doc.mw and 0 <= row < doc.mh
                   for col, row in scene.cells)
        assert all(0 <= col < doc.mw and 0 <= row < doc.mh
                   for col, row in scene._specials)
    finally:
        if renderer:
            renderer.delete()
        if target:
            gl.glDeleteFramebuffers(1, [target])
        if target_texture:
            gl.glDeleteTextures(1, [target_texture])
        if previous_context is not None and previous_surface is not None:
            previous_context.makeCurrent(previous_surface)
        else:
            context.doneCurrent()
        surface.destroy()
