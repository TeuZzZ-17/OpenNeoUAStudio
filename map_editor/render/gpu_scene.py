"""Camera-independent geometry. Only changed cells rebuild their GPU chunks."""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
from .squad_scene import squad_members, host_members

CHUNK_SIZE = 8
STRIDE = 16


@dataclass
class Geometry:
    opaque: np.ndarray
    flat: np.ndarray
    # One vertex range per original flat-TRACY face, keeping its fan together.
    flat_faces: list
    instances: dict = field(default_factory=dict)
    template_key: tuple = ()


EMPTY = np.empty((0, STRIDE), dtype=np.float32)


class WorldScene:
    def __init__(self):
        self.lib = None
        self.materials = []
        self._material_keys = {}
        self._templates = {}
        self._previews = {}
        self.cells = {}
        self._cell_keys = {}
        self._row_keys = {}
        self.chunks = {}
        self.instances = {}
        self.changed_templates = set()
        self.rebuilt_cells = 0
        self._size = None
        self._squad_key = None

    def set_library(self, lib):
        if lib is self.lib:
            return
        self.lib = lib
        self.materials.clear()
        self._material_keys.clear()
        self._templates.clear()
        self._previews.clear()
        self.clear()

    def clear(self):
        self.cells.clear()
        self._cell_keys.clear()
        self._row_keys.clear()
        self.chunks.clear()
        self.instances.clear()
        self._size = None
        self._squad_key = None

    def preview(self, typ, building_id=None, vehicle_id=None):
        key = (typ, building_id, vehicle_id)
        if key not in self._previews:
            mesh = (self.lib.actor_mesh(vehicle_id) if vehicle_id is not None else
                    self.lib.building_preview(building_id) if building_id is not None
                    else self.lib.mesh(typ))
            t = self._template(('preview', *key), mesh)
            opaque, flat = t.opaque.copy(), t.flat.copy()
            for array in (opaque, flat):
                array[:, :3] += (600, 0, -600)
            self._previews[key] = Geometry(opaque, flat, t.flat_faces)
        self.chunks = {(0, 0): self._previews[key]}
        return {(0, 0)}

    def _material(self, face):
        s = self.lib.surface_for(face)
        key = (s.kind, s.indices, s.width, s.height)
        index = self._material_keys.get(key)
        if index is None:
            index = len(self.materials)
            self._material_keys[key] = index
            self.materials.append(s)
        return index, s

    def _template(self, key, mesh, ground_only=False, filler=False):
        if key in self._templates:
            return self._templates[key]
        opaque, transparent, ranges = [], [], []
        flat_count = 0
        for face, ox, oz in mesh.faces:
            ground = filler or all(abs(v[1]) < 1e-6 for v in face.vertices)
            if ground_only and not ground:
                continue
            uv = self.lib.face_uvs(face)
            if len(uv) != len(face.vertices):
                continue
            index, surface = self._material(face)
            mode = {'none': 0, 'clear': 1, 'flat': 2}[surface.tracy_mode]
            shade = surface.shade_value if surface.shade_mode != 'none' else -1
            attrs = (index if surface.kind == 'texture' else -1,
                     surface.width, surface.height, shade, mode,
                     surface.solid_index or 0, 1 if ground else -1)
            vertices = [(v[0] + ox, v[1], v[2] + oz, *u, *attrs, 0, 0, 0, 0)
                        for v, u in zip(face.vertices, uv)]
            triangles = [vertices[j] for i in range(2, len(vertices))
                         for j in (0, i - 1, i)]
            if mode == 2:
                count = len(triangles)
                ranges.append((flat_count, count))
                flat_count += count
                transparent.extend(triangles)
            else:
                opaque.extend(triangles)
        result = Geometry(np.asarray(opaque, dtype=np.float32).reshape(-1, STRIDE),
                          np.asarray(transparent, dtype=np.float32).reshape(-1, STRIDE), ranges)
        result.template_key = key
        self._templates[key] = result
        return result

    @staticmethod
    def _number(value, fallback=0):
        try:
            return int(str(value), 16)
        except (ValueError, TypeError):
            return fallback

    def _cell(self, doc, terrain, col, row):
        typ = self._number(doc.grids['type'][row][col], -1)
        border = col in (0, doc.mw - 1) or row in (0, doc.mh - 1)
        key = ('sector', typ, border)
        template = self._templates.get(key)
        if template is None:
            template = self._template(key, self.lib.mesh(typ), ground_only=border)
        x, _, z = terrain.cell_center(col, row)
        cell_ref = (row * doc.mw + col,) * 4
        parts = [(template, (x, 0, z), cell_ref, False)]
        building = self._number(doc.grids['blg'][row][col])
        if building:
            key = ('building', building, border)
            template = self._templates.get(key)
            if template is None:
                template = self._template(key, self.lib.building_mesh(building), ground_only=border)
            parts.append((template, (x, 0, z), cell_ref, False))
        desc = self.lib.assets.sdf.sectors.get(typ)
        current_ground = desc.ground if desc else 0
        for vertical in (False, True):
            if (vertical and col == 0) or (not vertical and row == 0):
                continue
            c, r = (col - 1, row) if vertical else (col, row - 1)
            neighbour = self.lib.assets.sdf.sectors.get(self._number(doc.grids['type'][r][c], -1))
            other_ground = neighbour.ground if neighbour else 0
            key = ('filler', other_ground, current_ground, vertical)
            template = self._templates.get(key)
            if template is None:
                # Encode POO2 height indices once, then deform arrays directly.
                template = self._template(key, self.lib.filler_mesh(
                    other_ground, current_ground, vertical, tuple(range(10))), filler=True)
            def corner(cx, rz):
                return tuple(min(doc.mh-1, max(0, rr))*doc.mw + min(doc.mw-1, max(0, cc))
                             for rr in (rz-1, rz) for cc in (cx-1, cx))
            refs = None
            if len(template.flat):
                end = corner(col, row+1) if vertical else corner(col+1, row)
                start = corner(col, row)
                refs = ((r*doc.mw+c,) * 4,) * 4 + (cell_ref,) * 4 + (end, start)
            parts.append((template, (x, 0, z), refs, 3 if vertical else 2))
        flat, faces, instances = [], [], {}
        flat_offset = 0
        for template, offset, refs, filler in parts:
            if len(template.opaque):
                # A template is uploaded once. A sector only contributes its origin,
                # height-cell ID and filler orientation to the instance stream.
                kind = filler or 1
                instances[template.template_key] = (template, np.asarray(
                    [(offset[0], offset[2], row*doc.mw+col, kind)], np.float32))
            if not len(template.flat):
                continue
            a = template.flat.copy()
            if filler:
                a[:, 12:16] = np.asarray(refs)[a[:, 1].astype(np.int32)]
                a[:, 1] = 0
            else:
                a[:, 12:16] = refs
            a[:, :3] += offset
            a[a[:, 11] < 0, 11] = -(row * doc.mw + col + 1)
            flat.append(a)
            for start, count in template.flat_faces:
                faces.append((flat_offset + start, count))
            flat_offset += len(a)
        return Geometry(EMPTY, np.concatenate(flat) if flat else EMPTY, faces, instances)

    def update(self, doc, terrain, dirty=None):
        """Return changed chunks; height/filler neighbours are included locally."""
        if not self.cells or self._size != (doc.mw, doc.mh):
            affected = {(c, r) for r in range(doc.mh) for c in range(doc.mw)}
            self.clear()
            self._size = (doc.mw, doc.mh)
        else:
            if dirty is None:
                rows = [r for r in range(doc.mh) if self._row_keys.get(r) !=
                        (doc.grids['type'][r], doc.grids['blg'][r], doc.mw, doc.mh)]
                dirty = {(c, r) for r in rows for c in range(doc.mw)
                         if self._cell_keys.get((c, r)) !=
                         (doc.grids['type'][r][c], doc.grids['blg'][r][c], doc.mw, doc.mh)}
            affected = {(c + dx, r + dz) for c, r in dirty
                        for dx in (-1, 0, 1) for dz in (-1, 0, 1)
                        if 0 <= c + dx < doc.mw and 0 <= r + dz < doc.mh}
        chunks = set()
        for c, r in affected:
            self.cells[c, r] = self._cell(doc, terrain, c, r)
            self._cell_keys[c, r] = (doc.grids['type'][r][c], doc.grids['blg'][r][c], doc.mw, doc.mh)
            chunks.add((c // CHUNK_SIZE, r // CHUNK_SIZE))
        self.rebuilt_cells += len(affected)
        for r in {r for _, r in affected}:
            self._row_keys[r] = (doc.grids['type'][r][:], doc.grids['blg'][r][:], doc.mw, doc.mh)
        changed_templates = set()
        for cx, cz in chunks:
            old = self.chunks.get((cx, cz))
            if old:
                changed_templates.update(old.instances)
            flat, ranges, offset = [], [], 0
            instances = {}
            for r in range(cz * CHUNK_SIZE, min(doc.mh, (cz + 1) * CHUNK_SIZE)):
                for c in range(cx * CHUNK_SIZE, min(doc.mw, (cx + 1) * CHUNK_SIZE)):
                    cell = self.cells[c, r]
                    if len(cell.flat):
                        flat.append(cell.flat)
                    ranges.extend((start + offset, count) for start, count in cell.flat_faces)
                    offset += len(cell.flat)
                    for key, (template, data) in cell.instances.items():
                        instances.setdefault(key, (template, []))[1].append(data)
            groups = {key: (t, np.concatenate(arrays)) for key, (t, arrays) in instances.items()}
            changed_templates.update(groups)
            self.chunks[cx, cz] = Geometry(EMPTY, np.concatenate(flat) if flat else EMPTY, ranges, groups)
        for key in changed_templates:
            groups = [chunk.instances[key] for chunk in self.chunks.values() if key in chunk.instances]
            if groups:
                self.instances[key] = (groups[0][0], np.concatenate([g[1] for g in groups]))
            else:
                self.instances.pop(key, None)
        self.changed_templates = changed_templates
        # Squad geometry uses exact world positions rather than cell instances.
        squad_key = (repr(doc.squads), repr(doc.host_stations), terrain.cells.tobytes(),
                     repr(doc.grids['type'])) if doc.squads or doc.host_stations else ()
        if squad_key != self._squad_key:
            key = (-1, -1)
            if doc.squads or doc.host_stations or key in self.chunks:
                self.chunks[key] = self._squads(doc, terrain)
                chunks.add(key)
            self._squad_key = squad_key
        return chunks

    def _squads(self, doc, terrain):
        opaque, flat, ranges = [], [], []
        flat_offset = 0
        from itertools import chain
        for member in chain(squad_members(doc, terrain, self.lib), host_members(doc)):
            template = self._template(('vehicle', member.vehicle), self.lib.actor_mesh(member.vehicle))
            for source, arrays in ((template.opaque, opaque), (template.flat, flat)):
                if not len(source):
                    continue
                data = source.copy()
                data[:, :3] += member.position
                data[:, 11] = -(doc.mw * doc.mh + member.squad + 1)
                data[:, 12:16] = -1
                arrays.append(data)
            ranges.extend((start + flat_offset, count) for start, count in template.flat_faces)
            flat_offset += len(template.flat)
        return Geometry(np.concatenate(opaque) if opaque else EMPTY,
                        np.concatenate(flat) if flat else EMPTY, ranges)
