"""Studio uses the Map Editor's indexed textures, remaps and GPU framebuffer.

The existing Retail clipping/BSP pass supplies ordered pieces. Keeping that
order also preserves intersecting geometry and palette destination transparency.
"""
from __future__ import annotations

from types import SimpleNamespace
import numpy as np
from OpenGL import GL as gl

from map_editor.render.gpu_renderer import GpuRenderer, VERTEX, FRAGMENT, SCREEN_VERTEX, SCREEN_FRAGMENT, _program
from map_editor.render.gpu_scene import Geometry, EMPTY, STRIDE


STUDIO_VERTEX = '''#version 330 core
layout(location=0) in vec3 position;
layout(location=1) in vec2 uv;
layout(location=2) in vec4 material;
layout(location=3) in vec3 flags;
layout(location=4) in vec4 heightRefs;
out vec2 textureUV;
out vec3 world;
flat out vec4 mat;
flat out vec3 mode;
void main(){
    textureUV=uv; world=position; mat=material; mode=flags;
    gl_Position=vec4(position*heightRefs.x,heightRefs.x);
}
'''
STUDIO_FRAGMENT = FRAGMENT.replace(
    '#ifndef FLAT_PASS\nlayout(location=1) out int cellOut;\nlayout(location=2) out vec4 edgesOut;\n#endif',
    'layout(location=1) out int cellOut;\n#ifndef FLAT_PASS\nlayout(location=2) out vec4 edgesOut;\n#endif')
STUDIO_FRAGMENT = STUDIO_FRAGMENT.replace(
    'indexOut=texelFetch(tracy,ivec2(int(source),int(background)),0).r;',
    'indexOut=texelFetch(tracy,ivec2(int(source),int(background)),0).r; cellOut=-1;')


def piece_geometry(pieces, width, height):
    """Screen-space GPU fans; clip W retains each material's UV interpolation."""
    materials, keys, vertices, ranges = [], {}, [], []
    for piece in pieces:
        surface = piece.surface
        key = (surface.kind, surface.indices, surface.width, surface.height)
        index = keys.get(key)
        if index is None:
            index = len(materials)
            keys[key] = index
            materials.append(surface)
        mode = {'none': 0, 'clear': 1, 'flat': 2}[surface.tracy_mode]
        shade = surface.shade_value if surface.shade_mode != 'none' else -1
        attrs = (index if surface.kind == 'texture' else -1, surface.width,
                 surface.height, shade, mode, surface.solid_index or 0, -1)
        uv = piece.uvs or ((0.,0.),)*len(piece.screen)
        data = []
        for (x,y), coordinates, camera in zip(piece.screen, uv, piece.camera_vertices):
            w = max(.2, 4-camera[2]) if surface.map_mode == 'depth' else 1.
            data.append((2*x/width-1, 1-2*y/height, 0, *coordinates, *attrs, w,w,w,w))
        start = len(vertices)
        vertices.extend(data[j] for i in range(2,len(data)) for j in (0,i-1,i))
        count = len(vertices)-start
        if ranges and ranges[-1][2] == mode and mode != 2:
            old = ranges.pop()
            ranges.append((old[0],old[1]+count,mode))
        else:
            ranges.append((start,count,mode))
    return np.asarray(vertices,np.float32).reshape(-1,STRIDE), materials, ranges


class StudioGpuRenderer(GpuRenderer):
    def __init__(self):
        programs = (_program(STUDIO_VERTEX,STUDIO_FRAGMENT),
                    _program(STUDIO_VERTEX,STUDIO_FRAGMENT.replace(
                        '#version 330 core','#version 330 core\n#define FLAT_PASS')),
                    _program(SCREEN_VERTEX,SCREEN_FRAGMENT))
        super().__init__(programs)
        self.owns_programs = True
        self._tables = None

    def render_pieces(self, pieces, tables, width, height, target, *, blend=True):
        if any(piece.distance_fade for piece in pieces):
            raise ValueError('Distance-fade diagnostics require the exact software rasterizer')
        data, materials, ranges = piece_geometry(pieces,width,height)
        # Texture storage is retained across camera frames and reused by material content.
        if self._tables is not tables:
            self._tables = tables
            self._studio_scene = SimpleNamespace(lib=SimpleNamespace(tables=tables),
                materials=materials,chunks={(0,0):Geometry(data,EMPTY,[])},instances={})
        scene = self._studio_scene
        scene.materials = materials
        scene.chunks[0,0] = Geometry(data,EMPTY,[])
        # Material numbering can change after source-face culling or VANM playback.
        signature = tuple((s.kind,s.indices,s.width,s.height) for s in materials)
        if signature != getattr(self,'_materials_signature',None):
            self.material_count = 0
            self._materials_signature = signature
        self.set_scene(scene,{(0,0)})
        self._begin(width,height)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        for start,count,mode in ranges:
            program = self.flat_program if mode == 2 else self.opaque_program
            gl.glDrawBuffers(2 if mode == 2 else 3,
                [gl.GL_COLOR_ATTACHMENT0,gl.GL_COLOR_ATTACHMENT1,gl.GL_COLOR_ATTACHMENT2][:2 if mode == 2 else 3])
            self._geometry_program(program,np.eye(4,dtype=np.float32))
            if mode == 2:
                self._bind(self.backdrop,3)
                gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
                gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D,0,0,0,0,0,width,height)
            gl.glBindVertexArray(self.buffers[0,0].vao)
            gl.glDrawArrays(gl.GL_TRIANGLES,start,count)
            self.draw_calls += 1
        self._present(target,width,height,owner_colors={},overlays=False,sky=False,
                      transparent_background=True,blend=blend)
        return {'backend':'OpenGL 3.3 indexed','draw_calls':self.draw_calls,
                'triangles':len(data)//3,'renderer':self.name}
