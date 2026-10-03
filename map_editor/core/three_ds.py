"""3DS geometry used by OpenNeoUA: chunk bounds, objects and first diffuse map."""
from dataclasses import dataclass, field
from pathlib import Path
import math
import struct


@dataclass
class Material3DS:
    name: str = ''
    diffuse: tuple = (255, 255, 255)
    texture: str = ''


@dataclass
class Mesh3DS:
    vertices: list = field(default_factory=list)
    faces: list = field(default_factory=list)
    uvs: list = field(default_factory=list)
    materials: dict = field(default_factory=dict)


def read_3ds(path: Path):
    data = path.read_bytes()
    meshes, materials = [], {}

    def chunks(start, end):
        while start < end:
            if end - start < 6:
                raise ValueError('Truncated 3DS chunk')
            tag, size = struct.unpack_from('<HI', data, start)
            if size < 6 or start + size > end:
                raise ValueError('Invalid 3DS chunk size')
            yield tag, start + 6, start + size
            start += size

    def name(start, end):
        stop = data.find(b'\0', start, end)
        if stop < 0:
            raise ValueError('Unterminated 3DS name')
        return data[start:stop].decode('cp1252', errors='replace'), stop + 1

    def array(start, end, fmt):
        if end - start < 2:
            raise ValueError('Truncated 3DS array')
        count = struct.unpack_from('<H', data, start)[0]
        size = struct.calcsize(fmt)
        stop = start + 2 + count * size
        if stop > end:
            raise ValueError('Invalid 3DS array size')
        return [struct.unpack_from(fmt, data, start + 2 + i * size) for i in range(count)], stop

    def mesh(start, end):
        result = Mesh3DS()
        for tag, begin, stop in chunks(start, end):
            if tag == 0x4110:
                values, last = array(begin, stop, '<fff')
                if last != stop or not all(math.isfinite(v) for row in values for v in row):
                    raise ValueError('Invalid 3DS vertices')
                # The engine reads x,z,-y and reverses each polygon's indices.
                result.vertices = [(x, -z, y) for x, y, z in values]
            elif tag == 0x4140:
                result.uvs, last = array(begin, stop, '<ff')
                if last != stop or not all(math.isfinite(v) for row in result.uvs for v in row):
                    raise ValueError('Invalid 3DS UV coordinates')
            elif tag == 0x4120:
                values, last = array(begin, stop, '<HHHH')
                result.faces = [(c, b, a) for a, b, c, _flags in values]
                for subtag, substart, subend in chunks(last, stop):
                    if subtag == 0x4130:
                        label, offset = name(substart, subend)
                        indices, offset = array(offset, subend, '<H')
                        if offset != subend:
                            raise ValueError('Invalid 3DS material group')
                        for (index,) in indices:
                            if index >= len(result.faces):
                                raise ValueError('Invalid 3DS face material index')
                            result.materials[index] = label
        if any(index >= len(result.vertices) for face in result.faces for index in face):
            raise ValueError('Invalid 3DS vertex index')
        if result.uvs and len(result.uvs) != len(result.vertices):
            raise ValueError('3DS UV count differs from vertex count')
        return result

    def material(start, end):
        result = Material3DS()
        for tag, begin, stop in chunks(start, end):
            if tag == 0xA000:
                result.name, _ = name(begin, stop)
            elif tag == 0xA020:
                for subtag, substart, subend in chunks(begin, stop):
                    if subtag in (0x11, 0x12) and subend - substart == 3:
                        result.diffuse = tuple(data[substart:subend])
                    elif subtag in (0x10, 0x13) and subend - substart == 12:
                        values = struct.unpack_from('<fff', data, substart)
                        if not all(math.isfinite(v) for v in values):
                            raise ValueError('Invalid 3DS color')
                        result.diffuse = tuple(round(max(0, min(1, v)) * 255) for v in values)
            elif tag == 0xA200:
                for subtag, substart, subend in chunks(begin, stop):
                    if subtag == 0xA300:
                        result.texture, _ = name(substart, subend)
        return result

    def walk(start, end):
        for tag, begin, stop in chunks(start, end):
            if tag in (0x4D4D, 0x3D3D):
                yield from walk(begin, stop)
            elif tag == 0xAFFF:
                value = material(begin, stop)
                materials[value.name] = value
            elif tag == 0x4000:
                _label, begin = name(begin, stop)
                for subtag, substart, subend in chunks(begin, stop):
                    if subtag == 0x4100:
                        meshes.append(mesh(substart, subend))
        return

    if len(data) < 6 or struct.unpack_from('<H', data)[0] != 0x4D4D:
        raise ValueError('Not a 3DS file')
    list(walk(0, len(data)))
    if not any(mesh.faces for mesh in meshes):
        raise ValueError('3DS file has no faces')
    return meshes, materials
