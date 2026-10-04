from __future__ import annotations

from dataclasses import dataclass, replace
import math

import numpy as np
from PySide6.QtGui import QImage

from .sector_mesh import SectorMeshLibrary

from depth_renderer import CameraPolygon, order_camera_polygons, order_camera_polygons_fast, clip_camera_polygon_near
from indexed_renderer import IndexedPiece, IndexedRasterizer, retail_source_face_front_facing

from ..core.ldf_model import DEFAULT_HGT, SECTOR_SIZE
from .terrain_mesh import HEIGHT_UNIT
from .squad_scene import squad_members, host_members


def _clip(vertices, uvs, axis, bound, positive):
    """Taglia un poligono e le sue UV sul confine di un settore."""
    out, attributes = [], []
    previous, prev_uv = vertices[-1], uvs[-1]
    previous_distance = (previous[axis] - bound) * (1 if positive else -1)
    for vertex, uv in zip(vertices, uvs):
        distance = (vertex[axis] - bound) * (1 if positive else -1)
        if (distance >= 0) != (previous_distance >= 0):
            t = previous_distance / (previous_distance - distance)
            out.append(tuple(a + (b - a) * t for a, b in zip(previous, vertex)))
            attributes.append(tuple(a + (b - a) * t for a, b in zip(prev_uv, uv)))
        if distance >= 0:
            out.append(vertex)
            attributes.append(uv)
        previous, prev_uv, previous_distance = vertex, uv, distance
    return out, attributes


def ground_regions(vertices, uvs, width, height):
    """Assegna i raccordi alle celle per picking e bordi, preservando geometria e UV."""
    first_col = max(0, int(math.floor(min(v[0] for v in vertices) / SECTOR_SIZE)))
    last_col = min(width - 1, int(math.floor((max(v[0] for v in vertices) - 1e-6) / SECTOR_SIZE)))
    first_row = max(0, int(math.floor(-max(v[2] for v in vertices) / SECTOR_SIZE)))
    last_row = min(height - 1, int(math.floor((-min(v[2] for v in vertices) - 1e-6) / SECTOR_SIZE)))
    for row in range(first_row, last_row + 1):
        for col in range(first_col, last_col + 1):
            points, attributes = vertices, uvs
            for axis, bound, positive in (
                    (0, col * SECTOR_SIZE, True), (0, (col + 1) * SECTOR_SIZE, False),
                    (2, -(row + 1) * SECTOR_SIZE, True), (2, -row * SECTOR_SIZE, False)):
                if not points:
                    break
                points, attributes = _clip(points, attributes, axis, bound, positive)
            if len(points) >= 3:
                yield col, row, points, attributes


@dataclass
class SceneFrame:
    rgba: np.ndarray
    cell_ids: np.ndarray

    def image(self):
        height, width = self.rgba.shape[:2]
        return QImage(self.rgba.data, width, height, width * 4,
                      QImage.Format.Format_RGBA8888).copy()

    def pick(self, x, y, width):
        x, y = int(x), int(y)
        if not (0 <= y < self.cell_ids.shape[0] and 0 <= x < self.cell_ids.shape[1]):
            return None
        code = int(self.cell_ids[y, x])
        if not code:
            return None
        cell = abs(code) - 1
        return cell % width, cell // width


def scene_polygons(lib, doc, terrain, cam):
    """Settori e slurp reali, con quote e disposizione identiche a RenderSector."""
    polygons = []
    surfaces = {}
    order = 0
    type_values, building_values = {}, {}

    def parse(values, cache, invalid):
        output = []
        for line in values:
            row = []
            for value in line:
                text = str(value)
                if text not in cache:
                    try:
                        cache[text] = int(text, 16)
                    except (TypeError, ValueError):
                        cache[text] = invalid
                row.append(cache[text])
            output.append(row)
        return output

    types = parse(doc.grids['type'], type_values, -1)
    buildings = parse(doc.grids['blg'], building_values, 0)
    meshes = {t: lib.mesh(t) for t in type_values.values()}
    extras = {b: lib.building_mesh(b) for b in building_values.values() if b}
    bounds = [mesh.bounds for mesh in (*meshes.values(), *extras.values())]
    low_y = float(np.min(-(terrain.cells - DEFAULT_HGT) * HEIGHT_UNIT)) + min(b[1] for b in bounds)
    high_y = float(np.max(-(terrain.cells - DEFAULT_HGT) * HEIGHT_UNIT)) + max(b[4] for b in bounds)
    padding = max(750.0, *(abs(b[axis]) for b in bounds for axis in (0, 2, 3, 5)))
    projected = [cam.screen_to_ground(x, y, height) for height in (low_y, high_y)
                 for x in (0, cam.width) for y in (0, cam.height)]
    projected = [p for p in projected if p is not None]
    if projected and not cam.perspective:
        c0 = max(0, math.floor((min(p[0] for p in projected) - padding) / SECTOR_SIZE))
        c1 = min(doc.mw, math.ceil((max(p[0] for p in projected) + padding) / SECTOR_SIZE))
        r0 = max(0, math.floor((-max(p[2] for p in projected) - padding) / SECTOR_SIZE))
        r1 = min(doc.mh, math.ceil((-min(p[2] for p in projected) + padding) / SECTOR_SIZE))
    else:
        c0, c1, r0, r1 = 0, doc.mw, 0, doc.mh

    def append_mesh(mesh, col, row, *, filler=False, position=None, actor_code=None):
        nonlocal order
        wx, wy, wz = position if position is not None else terrain.cell_center(col, row)
        if filler:
            wy = 0.0
        for face, ox, oz in mesh.faces:
            source = order
            ground = actor_code is None and (filler or all(abs(v[1]) < 1e-6 for v in face.vertices))
            if actor_code is None and (col in (0, doc.mw - 1) or row in (0, doc.mh - 1)) and not ground:
                continue
            vertices = [(v[0] + ox + wx, v[1] + wy, v[2] + oz + wz) for v in face.vertices]
            source_camera = tuple(cam.to_camera(v) for v in vertices)
            screen = [cam.to_screen(v) for v in source_camera]
            if not cam.perspective and (max(p[0] for p in screen) < 0 or min(p[0] for p in screen) >= cam.width
                    or max(p[1] for p in screen) < 0 or min(p[1] for p in screen) >= cam.height):
                continue
            if not retail_source_face_front_facing(source_camera, camera_distance=4 if cam.perspective else 1e9):
                continue
            uvs = lib.face_uvs(face)
            if len(uvs) != len(vertices):
                continue
            # Una camera ortografica richiede UV lineari, senza correzione prospettica.
            surface = lib.surface_for(face)
            if id(surface) not in surfaces:
                surfaces[id(surface)] = replace(surface, map_mode="depth" if cam.perspective else "linear")
            surface = surfaces[id(surface)]
            # Le tessere e i raccordi rettangolari restano un unico poligono:
            # il raster condiviso esegue già il fan, dopo il taglio fra celle.
            rectangle = (ground and len(vertices) == 4
                         and len({v[0] for v in vertices}) == 2
                         and len({v[2] for v in vertices}) == 2
                         and surface.tracy_mode == 'none'
                         and all(abs(uvs[0][axis] + uvs[2][axis]
                                     - uvs[1][axis] - uvs[3][axis]) < 1e-8
                                 for axis in (0, 1)))
            fans = ((0, 3, 2, 1),) if rectangle else (
                (0, index, index - 1) for index in range(2, len(vertices)))
            for fan in fans:
                points = [vertices[i] for i in fan]
                attrs = [tuple(uvs[i]) for i in fan]
                regions = ground_regions(points, attrs, doc.mw, doc.mh) if ground else (
                    (col, row, points, attrs),)
                for c, r, world, coordinates in regions:
                    camera = tuple(cam.to_camera(v) for v in world)
                    code = actor_code if actor_code is not None else (r * doc.mw + c + 1) * (1 if ground else -1)
                    polygon = CameraPolygon(
                        camera, tuple(coordinates),
                        (surface, code, source), order)
                    if cam.perspective:
                        polygon = clip_camera_polygon_near(polygon, minimum_distance=20*cam.k)
                    if polygon is not None:
                        polygons.append(polygon)
                    order += 1

    def surface_type(col, row):
        desc = lib.assets.sdf.sectors.get(types[row][col])
        return desc.ground if desc is not None else 0

    for row in range(r0, r1):
        for col in range(c0, c1):
            append_mesh(meshes[types[row][col]], col, row)
            # Conserva la visualizzazione dei blg già presenti nei documenti.
            building = buildings[row][col]
            if building:
                append_mesh(extras[building], col, row)
            for vertical in (False, True):
                if (vertical and col == 0) or (not vertical and row == 0):
                    continue
                c, r = (col - 1, row) if vertical else (col, row - 1)
                filler = lib.filler_mesh(surface_type(c, r), surface_type(col, row),
                                         vertical, terrain.filler_heights(col, row, vertical))
                append_mesh(filler, col, row, filler=True)
    if doc.squads or doc.host_stations:
        from itertools import chain
        for member in chain(squad_members(doc, terrain, lib), host_members(doc, terrain, lib)):
            code = -(doc.mw * doc.mh + member.squad + 1)
            append_mesh(lib.actor_mesh(member.vehicle), 0, 0,
                        position=member.position, actor_code=code)
    return polygons


def render_scene(polygons, cam, tables, fast=False, preview_codes=()):
    ordered = (order_camera_polygons_fast(polygons) if fast else
               order_camera_polygons(polygons, eye=(0.0, 0.0, 4 if cam.perspective else 1e9)))
    pieces = []
    for poly in ordered:
        surface, code, source = poly.payload
        pieces.append(IndexedPiece(
            source_face_id=source, polygon_id=code,
            screen=tuple(cam.to_screen(v) for v in poly.vertices),
            uvs=poly.attributes, camera_vertices=poly.vertices,
            surface=surface, source_order=poly.source_order,
            sort_depth=-poly.mean_z()))
    result = IndexedRasterizer.render(cam.width, cam.height, pieces, tables,
                                      collect_diagnostics=False, track_polygon_owner=True)
    rgba = np.frombuffer(result.to_rgba(tables), dtype=np.uint8).reshape(
        cam.height, cam.width, 4).copy()
    rgba[~result.coverage, 3] = 0
    # Il raster condiviso usa -1 per lo sfondo; qui zero significa nessuna cella.
    cells = result.polygon_owner.copy()
    cells[~result.coverage] = 0
    if preview_codes:
        mask = np.isin(cells, preview_codes)
        grey = rgba[mask, :3] @ np.array((.299, .587, .114))
        rgba[mask, :3] = np.round(grey[:, None] * .72 + 255 * .62 * .28).astype(np.uint8)
    return SceneFrame(rgba, cells)
