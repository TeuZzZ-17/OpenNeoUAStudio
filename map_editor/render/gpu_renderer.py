"""OpenGL 3.3 indexed renderer with persistent world geometry and depth picking."""
from __future__ import annotations

import ctypes
import math
import numpy as np
from OpenGL import GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader

from .gpu_scene import STRIDE
from ..core.ldf_model import SECTOR_SIZE

VERTEX = '''#version 330 core
layout(location=0) in vec3 position;
layout(location=1) in vec2 uv;
layout(location=2) in vec4 material;
layout(location=3) in vec3 flags;
layout(location=4) in vec4 heightRefs;
layout(location=5) in vec4 instance;
uniform mat4 camera;
uniform samplerBuffer heights;
uniform ivec2 mapSize;
uniform bool instanced;
out vec2 textureUV;
out vec3 world;
flat out vec4 mat;
flat out vec3 mode;
float heightAt(ivec2 p){
    p=clamp(p,ivec2(0),mapSize-1);
    return texelFetch(heights,p.y*mapSize.x+p.x).r;
}
float cornerAt(ivec2 p){
    return (heightAt(p)+heightAt(p-ivec2(1,0))+heightAt(p-ivec2(0,1))+heightAt(p-ivec2(1,1)))*0.25;
}
void main(){
    world=position;
    textureUV=uv; mat=material; mode=flags;
    if(instanced){
        int id=int(instance.z), kind=int(instance.w);
        ivec2 cell=ivec2(id%mapSize.x,id/mapSize.x);
        if(kind==1 || kind<0) world.y+=texelFetch(heights,id).r;
        else{
            int ref=int(position.y);
            if(ref<4) world.y=heightAt(cell-(kind==3 ? ivec2(1,0) : ivec2(0,1)));
            else if(ref<8) world.y=texelFetch(heights,id).r;
            else world.y=cornerAt(cell+(ref==8 ? (kind==3 ? ivec2(0,1) : ivec2(1,0)) : ivec2(0)));
        }
        world.xz+=instance.xy;
        if(mode.z<0) mode.z=kind<0 ? float(kind) : -float(id+1);
    }else if(heightRefs.x>=0.0) world.y+=all(equal(heightRefs,vec4(heightRefs.x)))
        ? texelFetch(heights,int(heightRefs.x)).r
        : (texelFetch(heights,int(heightRefs.x)).r+texelFetch(heights,int(heightRefs.y)).r+
           texelFetch(heights,int(heightRefs.z)).r+texelFetch(heights,int(heightRefs.w)).r)*0.25;
    gl_Position=camera*vec4(world,1.0);
}
'''

FRAGMENT = '''#version 330 core
in vec2 textureUV;
in vec3 world;
flat in vec4 mat;
flat in vec3 mode;
uniform usampler2DArray textures;
uniform usampler2D shades;
uniform usampler2D tracy;
uniform usampler2D backdrop;
uniform ivec2 mapSize;
uniform bool groundOnly;
layout(location=0) out uint indexOut;
#ifndef FLAT_PASS
layout(location=1) out int cellOut;
layout(location=2) out vec4 edgesOut;
#endif
void main(){
    if(groundOnly && mode.z<0.0) discard;
    uint source=uint(mode.y);
    if(mat.x>=0.0){
        ivec2 size=ivec2(mat.yz);
        ivec2 p=clamp(ivec2(floor(textureUV/256.0*mat.yz)),ivec2(0),size-1);
        source=texelFetch(textures,ivec3(p,int(mat.x)),0).r;
        if(mode.x==1.0 && source==0u) discard;
    }
    if(mat.w>=0.0) source=texelFetch(shades,ivec2(int(source),int(mat.w)),0).r;
    int code=int(mode.z);
    vec2 sector=vec2(world.x,-world.z)/1200.0;
    ivec2 cell=ivec2(floor(sector));
    if(code>0){
        if(any(lessThan(cell,ivec2(0))) || any(greaterThanEqual(cell,mapSize))) discard;
        code=cell.y*mapSize.x+cell.x+1;
    }
#ifdef FLAT_PASS
    uint background=texelFetch(backdrop,ivec2(gl_FragCoord.xy),0).r;
    indexOut=texelFetch(tracy,ivec2(int(source),int(background)),0).r;
#else
    indexOut=source; cellOut=code;
    vec2 distances=fract(sector)*1200.0;
    vec2 derivative=max(vec2(length(vec2(dFdx(world.x),dFdy(world.x))),
                             length(vec2(dFdx(world.z),dFdy(world.z)))),vec2(0.00001));
    edgesOut=vec4(distances.x,1200.0-distances.x,distances.y,1200.0-distances.y)
             /vec4(derivative.x,derivative.x,derivative.y,derivative.y);
#endif
}
'''

SCREEN_VERTEX = '''#version 330 core
out vec2 uv;
void main(){
    vec2 p=vec2((gl_VertexID<<1)&2,gl_VertexID&2);
    uv=p; gl_Position=vec4(p*2.0-1.0,0.0,1.0);
}
'''

SCREEN_FRAGMENT = '''#version 330 core
in vec2 uv;
out vec4 color;
uniform usampler2D indices;
uniform isampler2D cells;
uniform sampler2D edgeMap;
uniform usampler2D states;
uniform sampler2D palette;
uniform sampler2D sky;
uniform samplerBuffer unitStyles;
uniform int unitCount;
uniform vec3 ownerColors[8];
uniform ivec2 mapSize;
uniform int hoverCell;
uniform vec3 cursorColor;
uniform bool showGrid;
uniform bool showSky;
uniform bool showOverlays;
uniform bool transparentBackground;
uniform float pixelScale;
vec4 unitStyle(int code){
    int index=-code-mapSize.x*mapSize.y-1;
    return index>=0 && index<unitCount ? texelFetch(unitStyles,index) : vec4(0);
}
uvec4 stateAt(ivec2 p){
    if(any(lessThan(p,ivec2(0))) || any(greaterThanEqual(p,mapSize))) return uvec4(0);
    return texelFetch(states,p,0);
}
void main(){
    ivec2 p=ivec2(gl_FragCoord.xy);
    int code=texelFetch(cells,p,0).r;
    uint index=texelFetch(indices,p,0).r;
    if(code==0 && index==0u){
        color=transparentBackground ? vec4(0) :
              showSky ? texture(sky,vec2(uv.x,1.0-uv.y)) : vec4(18.0/255.0,22.0/255.0,28.0/255.0,1);
        return;
    }
    color=texelFetch(palette,ivec2(int(index),0),0);
    if(showOverlays && abs(code)<=mapSize.x*mapSize.y && code!=0){
        int id=abs(code)-1;
        if(stateAt(ivec2(id%mapSize.x,id/mapSize.x)).a==2u){
            float grey=dot(color.rgb,vec3(0.299,0.587,0.114));
            color.rgb=mix(vec3(grey),vec3(0.62),0.28);
        }
    }
    if(unitStyle(code).a==3.0){
        float grey=dot(color.rgb,vec3(0.299,0.587,0.114));
        color.rgb=mix(vec3(grey),vec3(0.62),0.28);
    }
    // The visible negative sector pixels are the building silhouette.
    // Keep a yellow inner edge and add a white outer edge only on selection.
    if(showOverlays){
        bool buildingPixel=code<0 && -code<=mapSize.x*mapSize.y;
        uvec4 building=uvec4(0);
        if(buildingPixel){
            int id=-code-1;
            building=stateAt(ivec2(id%mapSize.x,id/mapSize.x));
            buildingPixel=building.a!=0u;
        }
        bool yellowEdge=false;
        bool whiteOuter=false;
        for(int axis=0;axis<4;axis++){
            ivec2 delta=axis==0 ? ivec2(1,0) : axis==1 ? ivec2(-1,0) : axis==2 ? ivec2(0,1) : ivec2(0,-1);
            ivec2 otherPos=clamp(p+delta,ivec2(0),textureSize(cells,0)-1);
            int other=texelFetch(cells,otherPos,0).r;
            if(buildingPixel && other!=code){
                yellowEdge=true;
            }
            if(!buildingPixel && other<0 && -other<=mapSize.x*mapSize.y){
                int id=-other-1;
                uvec4 neighbour=stateAt(ivec2(id%mapSize.x,id/mapSize.x));
                if(neighbour.a==3u) whiteOuter=true;
            }
        }
        if(whiteOuter){color.rgb=vec3(1.0);return;}
        if(yellowEdge){
            color.rgb=building.a==2u ? vec3(0.62) : vec3(1.0,0.80,0.16);
            return;
        }
    }
    if(showOverlays && unitCount>0){
        vec4 own=unitStyle(code);
        for(int d=1;d<=3;d++){
            int step=max(1,int(float(d)*pixelScale));
            for(int axis=0;axis<4;axis++){
                ivec2 offset=axis==0 ? ivec2(step,0) : axis==1 ? ivec2(-step,0) : axis==2 ? ivec2(0,step) : ivec2(0,-step);
                ivec2 q=clamp(p+offset,ivec2(0),textureSize(cells,0)-1);
                int other=texelFetch(cells,q,0).r;
                vec4 neighbour=unitStyle(other);
                if(own.a>0.0 && other!=code && d<=(own.a==2.0 ? 2 : 1)){
                    color.rgb=own.rgb; return;
                }
                if(own.a==0.0 && neighbour.a==2.0 && d<=2){
                    color.rgb=vec3(1.0); return;
                }
            }
        }
    }
    if(code<=0 || !showOverlays) return;
    ivec2 cell=ivec2((code-1)%mapSize.x,(code-1)/mapSize.x);
    uvec4 state=stateAt(cell);
    vec4 e=texelFetch(edgeMap,p,0)/pixelScale;
    float edge=min(min(e.x,e.y),min(e.z,e.w));
    if(showGrid && edge<0.55) color.rgb*=0.65;
    if(state.r>0u && state.r<8u && edge<2.0){
        color.rgb=ownerColors[int(state.r)]; return;
    }
    if(state.g!=0u){
        float boundary=100000.0;
        if(stateAt(cell+ivec2(-1,0)).g==0u) boundary=min(boundary,e.x);
        if(stateAt(cell+ivec2(1,0)).g==0u) boundary=min(boundary,e.y);
        if(stateAt(cell+ivec2(0,-1)).g==0u) boundary=min(boundary,e.z);
        if(stateAt(cell+ivec2(0,1)).g==0u) boundary=min(boundary,e.w);
        if(boundary>3.0 && boundary<4.3 && mod((gl_FragCoord.x+gl_FragCoord.y)/pixelScale,10.0)<6.0)
            color.rgb=cursorColor;
    }
    if(state.b!=0u || code==hoverCell){
        float x=min(e.x,e.y), z=min(e.z,e.w);
        bool bracket=(x>6.0 && x<7.7 && z>6.0 && z<20.0) ||
                     (z>6.0 && z<7.7 && x>6.0 && x<20.0);
        if(bracket) color.rgb=state.b!=0u ? mix(cursorColor,vec3(1),0.35) : cursorColor;
    }
}
'''


def camera_matrix(cam, map_size):
    cy, sy, cp, sp = cam._trig()
    rotation = np.array(((-cy, 0, -sy), (sp*sy, -cp, -sp*cy),
                         (-cp*sy, -sp, cp*cy)), dtype=np.float64)
    if cam.perspective:
        focal = 1 / math.tan(math.radians(cam.fov)/2)
        near, far = 20.0, max(30000.0, sum(map_size)*SECTOR_SIZE*2)
        view = np.eye(4)
        view[:3, :3] = rotation
        view[:3, 3] = -rotation @ np.asarray(cam.center)
        projection = np.array(((focal*cam.height/cam.width,0,0,0),(0,focal,0,0),
                              (0,0,-(far+near)/(far-near),-2*far*near/(far-near)),
                              (0,0,-1,0)))
        return np.asarray(projection @ view, np.float32)
    scale = np.array((2*cam.zoom/cam.width, 2*cam.zoom/cam.height,
                      -1/max(10000, sum(map_size)*SECTOR_SIZE+10000)))
    result = np.eye(4, dtype=np.float32)
    result[:3, :3] = rotation * scale[:, None]
    result[:3, 3] = -(result[:3, :3] @ np.asarray(cam.center))
    result[0, 3] += 2*cam.pan[0]/cam.width
    result[1, 3] -= 2*cam.pan[1]/cam.height
    return result


def _program(vertex, fragment):
    return compileProgram(compileShader(vertex, gl.GL_VERTEX_SHADER),
                          compileShader(fragment, gl.GL_FRAGMENT_SHADER), validate=False)


class Buffer:
    def __init__(self):
        self.vao = int(gl.glGenVertexArrays(1))
        self.vbo = int(gl.glGenBuffers(1))
        self.count = 0
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        for location, count, offset in ((0, 3, 0), (1, 2, 3), (2, 4, 5), (3, 3, 9), (4, 4, 12)):
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribPointer(location, count, gl.GL_FLOAT, False, STRIDE*4,
                                     ctypes.c_void_p(offset*4))

    def upload(self, data):
        self.count = len(data)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)

    def delete(self):
        gl.glDeleteBuffers(1, [self.vbo]); gl.glDeleteVertexArrays(1, [self.vao])


class GpuRenderer:
    def __init__(self, programs=None):
        self.name = gl.glGetString(gl.GL_RENDERER).decode(errors='replace')
        extensions = {gl.glGetStringi(gl.GL_EXTENSIONS, i)
                      for i in range(int(gl.glGetIntegerv(gl.GL_NUM_EXTENSIONS)))}
        self.texture_barrier = b'GL_ARB_texture_barrier' in extensions and bool(gl.glTextureBarrier)
        self.owns_programs = programs is None
        if programs is None:
            programs = (_program(VERTEX, FRAGMENT),
                        _program(VERTEX, FRAGMENT.replace('#version 330 core', '#version 330 core\n#define FLAT_PASS')),
                        _program(SCREEN_VERTEX, SCREEN_FRAGMENT))
        self.opaque_program, self.flat_program, self.screen_program = programs
        self.screen_vao = int(gl.glGenVertexArrays(1))
        self._locations = {}
        self.textures = [int(x) for x in gl.glGenTextures(13)]
        (self.atlas, self.shades, self.tracy, self.palette, self.sky,
         self.states, self.indices, self.ids, self.edges, self.depth, self.backdrop, self.heights, self.unit_styles) = self.textures
        self.fbo = int(gl.glGenFramebuffers(1))
        self.height_buffer = int(gl.glGenBuffers(1))
        self.unit_buffer = int(gl.glGenBuffers(1))
        self._unit_key = None
        self.unit_count = 0
        self.set_unit_styles([])
        self.size = (0, 0)
        self.capacity = (0, 0)
        self.map_size = (1, 1)
        self.material_count = 0
        self.atlas_size = (0, 0, 0)
        self.buffers = {}
        self.flat_buffer = Buffer()
        self.flat_offsets = {}
        self.instance_buffers = {}
        self.scene = None
        self.has_sky = False
        self.draw_calls = 0
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        self._image(self.sky, np.zeros((1, 1, 4), np.uint8), gl.GL_RGBA8, gl.GL_RGBA)
        self.set_states(np.zeros((1, 1, 4), np.uint8))
        self.set_heights(np.zeros((1, 1), np.float32))

    def uniform(self, program, name):
        key = (program, name)
        if key not in self._locations:
            self._locations[key] = gl.glGetUniformLocation(program, name)
        return self._locations[key]

    @staticmethod
    def _bind(texture, unit, target=gl.GL_TEXTURE_2D):
        gl.glActiveTexture(gl.GL_TEXTURE0+unit)
        gl.glBindTexture(target, texture)

    def _image(self, texture, data, internal, fmt, dtype=gl.GL_UNSIGNED_BYTE):
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, data.shape[1], data.shape[0], 0, fmt, dtype, data)

    def set_sky(self, image):
        from PySide6.QtGui import QImage
        self.has_sky = image is not None and not image.isNull()
        if self.has_sky:
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            data = np.frombuffer(image.bits(), np.uint8).reshape(image.height(), image.width(), 4)
            self._image(self.sky, data, gl.GL_RGBA8, gl.GL_RGBA)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)

    def set_states(self, data):
        self.map_size = (data.shape[1], data.shape[0])
        self._image(self.states, data, gl.GL_RGBA8UI, gl.GL_RGBA_INTEGER)

    def set_heights(self, data):
        self.height_data = np.asarray(data, np.float32).reshape(-1)
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, self.height_buffer)
        gl.glBufferData(gl.GL_TEXTURE_BUFFER, self.height_data.nbytes, self.height_data, gl.GL_DYNAMIC_DRAW)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.heights)
        gl.glTexBuffer(gl.GL_TEXTURE_BUFFER, gl.GL_R32F, self.height_buffer)

    def set_scene(self, scene, changed):
        new_scene = scene is not self.scene
        if new_scene:
            changed = set(scene.chunks)
            for opaque in self.buffers.values():
                opaque.delete()
            self.buffers.clear()
            for buffer, data, _ in self.instance_buffers.values():
                buffer.delete(); gl.glDeleteBuffers(1, [data])
            self.instance_buffers.clear()
            self.scene = scene
            self.material_count = 0
            tables = scene.lib.tables
            self._image(self.shades, np.frombuffer(tables.shader_pixels, np.uint8).reshape(256, 256), gl.GL_R8UI, gl.GL_RED_INTEGER)
            self._image(self.tracy, np.frombuffer(tables.tracy_pixels, np.uint8).reshape(256, 256), gl.GL_R8UI, gl.GL_RED_INTEGER)
            palette = np.full((1, 256, 4), 255, np.uint8)
            palette[0, :, :3] = tables.display_palette
            self._image(self.palette, palette, gl.GL_RGBA8, gl.GL_RGBA)
        self._upload_materials(scene.materials)
        groups = getattr(scene, 'instances', {})
        for key in list(self.instance_buffers):
            if key not in groups:
                buffer, data, _ = self.instance_buffers.pop(key)
                buffer.delete(); gl.glDeleteBuffers(1, [data])
        changed_templates = set(groups) if new_scene else getattr(scene, 'changed_templates', set())
        for key in changed_templates:
            if key not in groups:
                continue
            template, instances = groups[key]
            if key not in self.instance_buffers:
                buffer = Buffer(); buffer.upload(template.opaque)
                data = int(gl.glGenBuffers(1))
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, data)
                gl.glEnableVertexAttribArray(5)
                gl.glVertexAttribPointer(5, 4, gl.GL_FLOAT, False, 16, ctypes.c_void_p(0))
                gl.glVertexAttribDivisor(5, 1)
            else:
                buffer, data, _ = self.instance_buffers[key]
                gl.glBindBuffer(gl.GL_ARRAY_BUFFER, data)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, instances.nbytes, instances, gl.GL_DYNAMIC_DRAW)
            self.instance_buffers[key] = (buffer, data, len(instances))
        for key in list(self.buffers):
            if key not in scene.chunks:
                self.buffers.pop(key).delete()
        for key in changed:
            if key not in self.buffers:
                self.buffers[key] = Buffer()
            geometry = scene.chunks[key]
            self.buffers[key].upload(geometry.opaque)
        if changed:
            arrays, offset = [], 0
            self.flat_offsets.clear()
            for key, geometry in scene.chunks.items():
                self.flat_offsets[key] = offset
                arrays.append(geometry.flat)
                offset += len(geometry.flat)
            self.flat_buffer.upload(np.concatenate(arrays) if arrays else np.empty((0, STRIDE), np.float32))

    def _upload_materials(self, materials):
        count = len(materials)
        if count == self.material_count:
            return
        width = max([1] + [s.width for s in materials])
        height = max([1] + [s.height for s in materials])
        old_w, old_h, capacity = self.atlas_size
        self._bind(self.atlas, 0, gl.GL_TEXTURE_2D_ARRAY)
        if width > old_w or height > old_h or count > capacity:
            capacity = max(64, 2**math.ceil(math.log2(max(1, count))))
            if capacity > int(gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS)):
                raise RuntimeError('This set exceeds the GPU texture array capacity')
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_R8UI, width, height, capacity,
                            0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, None)
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            self.atlas_size = (width, height, capacity)
            self.material_count = 0
        for index in range(self.material_count, count):
            s = materials[index]
            if s.kind == 'texture':
                gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, index, s.width, s.height, 1,
                                   gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, np.frombuffer(s.indices, np.uint8))
        self.material_count = count

    def _resize(self, width, height):
        if self.size == (width, height):
            return
        self.size = (width, height)
        old_width, old_height = self.capacity
        if width <= old_width and height <= old_height and width >= old_width//2 and height >= old_height//2:
            return
        width, height = ((width+255)//256)*256, ((height+255)//256)*256
        self.capacity = (width, height)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        attachments = ((self.indices, gl.GL_R8UI, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, gl.GL_COLOR_ATTACHMENT0),
                       (self.ids, gl.GL_R32I, gl.GL_RED_INTEGER, gl.GL_INT, gl.GL_COLOR_ATTACHMENT1),
                       (self.edges, gl.GL_RGBA16F, gl.GL_RGBA, gl.GL_FLOAT, gl.GL_COLOR_ATTACHMENT2),
                       (self.depth, gl.GL_DEPTH_COMPONENT32F, gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, gl.GL_DEPTH_ATTACHMENT))
        for texture, internal, fmt, dtype, attachment in attachments:
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, internal, width, height, 0, fmt, dtype, None)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, attachment, gl.GL_TEXTURE_2D, texture, 0)
        gl.glDrawBuffers(3, [gl.GL_COLOR_ATTACHMENT0, gl.GL_COLOR_ATTACHMENT1, gl.GL_COLOR_ATTACHMENT2])
        if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError('Unable to create the indexed GPU framebuffer')
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.backdrop)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8UI, width, height, 0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, None)

    def _geometry_program(self, program, matrix):
        gl.glUseProgram(program)
        gl.glUniformMatrix4fv(self.uniform(program, 'camera'), 1, True, matrix)
        gl.glUniform2i(self.uniform(program, 'mapSize'), *self.map_size)
        gl.glUniform1i(self.uniform(program, 'instanced'), False)
        for name, tex, unit, target in (('textures', self.atlas, 0, gl.GL_TEXTURE_2D_ARRAY),
                                      ('shades', self.shades, 1, gl.GL_TEXTURE_2D),
                                      ('tracy', self.tracy, 2, gl.GL_TEXTURE_2D),
                                      ('backdrop', self.backdrop, 3, gl.GL_TEXTURE_2D),
                                      ('heights', self.heights, 4, gl.GL_TEXTURE_BUFFER)):
            self._bind(tex, unit, target)
            gl.glUniform1i(self.uniform(program, name), unit)

    def set_unit_styles(self, styles):
        data = np.asarray(styles,np.float32).reshape(-1,4)
        key = data.tobytes()
        if key == self._unit_key:
            return
        self._unit_key, self.unit_count = key,len(data)
        if not len(data):
            data = np.zeros((1,4),np.float32)
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER,self.unit_buffer)
        gl.glBufferData(gl.GL_TEXTURE_BUFFER,data.nbytes,data,gl.GL_DYNAMIC_DRAW)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER,self.unit_styles)
        gl.glTexBuffer(gl.GL_TEXTURE_BUFFER,gl.GL_RGBA32F,self.unit_buffer)

    @staticmethod
    def _transparency_batches(faces, width, height):
        """Keep every overlapping pair in painter order; batch disjoint bounds."""
        layers = np.zeros(((height+1)//2, (width+1)//2), np.int32)
        batches = {}
        for face in sorted(faces, key=lambda x: x[0]):
            _, _, _, _, x, y, right, top = face
            region = layers[y//2:(top+1)//2, x//2:(right+1)//2]
            layer = int(region.max())+1
            region[:] = layer
            batches.setdefault(layer, []).append(face)
        return batches.values()

    def render(self, camera, width, height, target, *, owner_colors, grid=True, sky=True,
               hover=0, pixel_scale=1.0, overlays=True, transparent_background=False,
               cursor_color=(210, 210, 210), clear_view=False):
        self._begin(width, height)
        matrix = camera_matrix(camera, self.map_size)
        self._geometry_program(self.opaque_program, matrix)
        gl.glUniform1i(self.uniform(self.opaque_program, 'groundOnly'), clear_view)
        gl.glUniform1i(self.uniform(self.opaque_program, 'instanced'), True)
        # Stable order also makes coplanar boundary ties independent of Python
        # object addresses, set iteration, or a previously edited sector type.
        ranks = {'sector': 0, 'building': 1, 'filler': 2}
        for key in sorted(self.instance_buffers, key=lambda k: (ranks[k[0]], k[1:])):
            buffer, _, count = self.instance_buffers[key]
            gl.glBindVertexArray(buffer.vao)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, buffer.count, count)
            self.draw_calls += 1
        gl.glUniform1i(self.uniform(self.opaque_program, 'instanced'), False)
        for opaque in self.buffers.values():
            if opaque.count:
                gl.glBindVertexArray(opaque.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, opaque.count)
                self.draw_calls += 1
        # Palette transparency reads the current destination, never alpha-blends RGB.
        transparent = []
        if self.scene is not None:
            for key, geometry in self.scene.chunks.items():
                if not geometry.flat_faces:
                    continue
                vertices = geometry.flat[:, :3].copy()
                refs = geometry.flat[:, 12:16].astype(np.int32)
                attached = refs[:, 0] >= 0
                vertices[attached, 1] += self.height_data[refs[attached]].mean(axis=1)
                clip = vertices @ matrix[:, :3].T + matrix[:, 3]
                projected = clip[:, :3] / np.maximum(clip[:, 3:4], 1e-6)
                projected[:, 0] = (projected[:, 0]+1)*width*.5
                projected[:, 1] = (projected[:, 1]+1)*height*.5
                starts = np.fromiter((f[0] for f in geometry.flat_faces), np.int32)
                counts = np.fromiter((f[1] for f in geometry.flat_faces), np.int32)
                triangles = projected[:, :2].reshape(-1, 3, 2)
                a, b = triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0]
                front = np.logical_or.reduceat(a[:, 0]*b[:, 1]-a[:, 1]*b[:, 0]<0, starts//3)
                lo = np.maximum(0, np.floor(np.minimum.reduceat(projected[:, :2], starts))).astype(int)
                hi = np.minimum((width,height), np.ceil(np.maximum.reduceat(projected[:, :2], starts))).astype(int)
                if camera.perspective:
                    crossing = np.logical_or.reduceat(clip[:, 3] < 20, starts)
                    lo[crossing], hi[crossing] = (0,0), (width,height)
                depths = -np.add.reduceat(projected[:, 2], starts)/counts
                visible = np.flatnonzero(front & np.all(hi>lo,axis=1))
                transparent.extend((float(depths[i]), key, int(starts[i])+self.flat_offsets[key], int(counts[i]),
                                    int(lo[i,0]), int(lo[i,1]), int(hi[i,0]), int(hi[i,1]))
                                   for i in visible)
        if transparent:
            gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
            gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
            gl.glDepthMask(False)
            self._geometry_program(self.flat_program, matrix)
            gl.glUniform1i(self.uniform(self.flat_program, 'groundOnly'), clear_view)
            self._bind(self.indices if self.texture_barrier else self.backdrop, 3)
            # Faces with disjoint screen bounds can share one backdrop copy.
            # Tile dependencies preserve painter order for every overlapping pair,
            # including stacked TRACY surfaces, without one GPU copy per face.
            for faces in self._transparency_batches(transparent, width, height):
                x, y = min(f[4] for f in faces), min(f[5] for f in faces)
                right, top = max(f[6] for f in faces), max(f[7] for f in faces)
                if self.texture_barrier:
                    # ARB_texture_barrier permits in-place palette lookup because
                    # batch rectangles do not overlap; subsequent batches synchronize.
                    gl.glTextureBarrier()
                else:
                    gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, x, y, x, y, right-x, top-y)
                first = np.fromiter((f[2] for f in faces), np.int32)
                counts = np.fromiter((f[3] for f in faces), np.int32)
                gl.glBindVertexArray(self.flat_buffer.vao)
                gl.glMultiDrawArrays(gl.GL_TRIANGLES, first, counts, len(first))
                self.draw_calls += 1
        self._present(target, width, height, owner_colors=owner_colors, grid=grid,
                      sky=sky, hover=hover, pixel_scale=pixel_scale, overlays=overlays,
                      transparent_background=transparent_background,
                      cursor_color=cursor_color)

    def _begin(self, width, height):
        self._resize(width, height)
        self.draw_calls = 0
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, width, height)
        gl.glDisable(gl.GL_BLEND); gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_FRAMEBUFFER_SRGB)
        gl.glDrawBuffers(3, [gl.GL_COLOR_ATTACHMENT0, gl.GL_COLOR_ATTACHMENT1, gl.GL_COLOR_ATTACHMENT2])
        gl.glClearBufferuiv(gl.GL_COLOR, 0, np.zeros(4, np.uint32))
        gl.glClearBufferiv(gl.GL_COLOR, 1, np.zeros(4, np.int32))
        gl.glClearBufferfv(gl.GL_COLOR, 2, np.zeros(4, np.float32))
        gl.glDepthMask(True)
        gl.glClearBufferfv(gl.GL_DEPTH, 0, np.ones(1, np.float32))
        gl.glEnable(gl.GL_DEPTH_TEST); gl.glDepthFunc(gl.GL_LEQUAL)
        gl.glEnable(gl.GL_CULL_FACE); gl.glFrontFace(gl.GL_CW); gl.glCullFace(gl.GL_BACK)

    def _present(self, target, width, height, *, owner_colors, grid=True, sky=True,
                 hover=0, pixel_scale=1.0, overlays=True, transparent_background=False,
                 blend=False, cursor_color=(210, 210, 210)):
        gl.glDepthMask(True)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glViewport(0, 0, width, height)
        gl.glDisable(gl.GL_DEPTH_TEST); gl.glDisable(gl.GL_CULL_FACE)
        program = self.screen_program
        gl.glUseProgram(program)
        for unit, (name, tex) in enumerate((('indices', self.indices), ('cells', self.ids),
                                          ('edgeMap', self.edges), ('states', self.states),
                                          ('palette', self.palette), ('sky', self.sky))):
            self._bind(tex, unit)
            gl.glUniform1i(self.uniform(program, name), unit)
        gl.glUniform2i(self.uniform(program, 'mapSize'), *self.map_size)
        self._bind(self.unit_styles,6,gl.GL_TEXTURE_BUFFER)
        gl.glUniform1i(self.uniform(program,'unitStyles'),6)
        gl.glUniform1i(self.uniform(program,'unitCount'),self.unit_count)
        colors = np.asarray([owner_colors.get(i, (0, 0, 0)) for i in range(8)], np.float32)/255
        if not overlays:
            colors[:] = 0
        gl.glUniform3fv(self.uniform(program, 'ownerColors'), 8, colors)
        gl.glUniform1i(self.uniform(program, 'showGrid'), grid and overlays)
        gl.glUniform1i(self.uniform(program, 'showSky'), sky and self.has_sky)
        gl.glUniform1i(self.uniform(program, 'showOverlays'), overlays)
        gl.glUniform1i(self.uniform(program, 'transparentBackground'), transparent_background)
        gl.glUniform1i(self.uniform(program, 'hoverCell'), hover if overlays else 0)
        gl.glUniform3f(self.uniform(program, 'cursorColor'), *(v / 255 for v in cursor_color))
        gl.glUniform1f(self.uniform(program, 'pixelScale'), pixel_scale)
        if blend:
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glBindVertexArray(self.screen_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0); gl.glUseProgram(0)
        gl.glDisable(gl.GL_BLEND)

    def pick(self, x, y):
        w, h = self.size
        if not (0 <= x < w and 0 <= y < h):
            return 0
        previous = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT1)
        pixel = np.zeros(1, np.int32)
        gl.glReadPixels(int(x), h-1-int(y), 1, 1, gl.GL_RED_INTEGER, gl.GL_INT, pixel)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, previous)
        return int(pixel[0])

    def read_ids(self):
        w, h = self.size
        data = np.empty((h, w), np.int32)
        previous = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT1)
        gl.glReadPixels(0, 0, w, h, gl.GL_RED_INTEGER, gl.GL_INT, data)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, previous)
        return data[::-1].copy()

    def read_indices(self):
        w, h = self.size
        data = np.empty((h, w), np.uint8)
        gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
        previous = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
        gl.glReadPixels(0, 0, w, h, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, data)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, previous)
        return data[::-1].copy()

    def delete(self):
        for buffer, data, _ in self.instance_buffers.values():
            buffer.delete(); gl.glDeleteBuffers(1, [data])
        self.instance_buffers.clear()
        for buffer in self.buffers.values():
            buffer.delete()
        self.buffers.clear()
        self.flat_buffer.delete()
        gl.glDeleteTextures(self.textures)
        gl.glDeleteFramebuffers(1, [self.fbo])
        gl.glDeleteBuffers(1, [self.height_buffer])
        gl.glDeleteBuffers(1, [self.unit_buffer])
        gl.glDeleteVertexArrays(1, [self.screen_vao])
        if self.owns_programs:
            for program in (self.opaque_program, self.flat_program, self.screen_program):
                gl.glDeleteProgram(program)
