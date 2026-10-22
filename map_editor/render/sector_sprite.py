"""Preview sprites of sectors and buildings drawn with the indexed software rasteriser."""

from __future__ import annotations

from dataclasses import dataclass

from .camera import IsoCamera
from .sector_mesh import SectorMesh, SectorMeshLibrary
from ..core.sector_resolver import SECTOR_SIZE

import numpy as np
from PySide6.QtGui import QImage

from studio.rendering.depth import (
    CameraPolygon,
    clip_camera_polygon_near,
    order_camera_polygons,
    order_camera_polygons_fast,
)
from studio.rendering.indexed import (
    IndexedPiece,
    IndexedRasterizer,
    retail_source_face_front_facing,
)

EYE_DISTANCE = 4.0
NEAR = 0.2
BOX_Y = (-520.0, 60.0)


@dataclass
class Sprite:
    image: QImage
    anchor: tuple[float, float]
    ppu: float


def sprite_bounds(cam: IsoCamera):
    half = SECTOR_SIZE / 2 + 40
    pts = []
    for x in (-half, half):
        for y in BOX_Y:
            for z in (-half, half):
                pts.append(cam.to_screen(cam.to_camera((x, y, z), True), (0, 0)))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2


def build_pieces(lib: SectorMeshLibrary, mesh: SectorMesh, cam: IsoCamera,
                 origin: tuple[float, float], fast: bool = False):
    triangles: list[CameraPolygon] = []
    order = 0
    for face_order, (face, ox, oz) in enumerate(mesh.faces):
        verts = [cam.to_camera((v[0] + ox, v[1], v[2] + oz), True)
                 for v in face.vertices]
        if not retail_source_face_front_facing(tuple(verts)):
            continue
        uvs = lib.face_uvs(face)
        valid = len(uvs) == len(verts)
        if not valid:
            uvs = [(0.0, 0.0)] * len(verts)
        depth = max(EYE_DISTANCE - v[2] for v in verts)
        for i in range(2, len(verts)):
            ids = (0, i, i - 1)
            clipped = clip_camera_polygon_near(CameraPolygon(
                tuple(verts[j] for j in ids),
                tuple(tuple(uvs[j]) for j in ids), None, order),
                minimum_distance=NEAR)
            if clipped is None:
                continue
            triangles.append(CameraPolygon(
                clipped.vertices, clipped.attributes,
                (face, valid, face_order, depth), order))
            order += 1
    ordered = (order_camera_polygons_fast(triangles) if fast
               else order_camera_polygons(triangles))
    pieces = []
    for poly in ordered:
        face, valid, face_order, depth = poly.payload
        surface = lib.surface_for(face)
        if surface.kind == "texture" and not valid:
            continue
        screen = tuple(cam.to_screen(v, origin) for v in poly.vertices)
        pieces.append(IndexedPiece(
            source_face_id=id(face), polygon_id=face.poly_id, screen=screen,
            uvs=tuple(tuple(a) for a in poly.attributes),
            camera_vertices=tuple(tuple(v) for v in poly.vertices),
            surface=surface, source_order=face_order, sort_depth=depth))
    return pieces


def fitted_preview_camera(vertices, yaw, pitch, target_size, center=(0, 0, 0)):
    """Fit the complete projected geometry into physical pixels, with a safe one-pixel rim."""
    width, height = (max(4, int(value)) for value in target_size)
    cam = IsoCamera(yaw=yaw, pitch=pitch, center=center, width=width, height=height)
    vertices = np.asarray(vertices, dtype=float).reshape(-1, 3)
    if not len(vertices):
        return cam
    cy, sy, cp, sp = cam._trig()
    projection = np.asarray(((-cy, 0, -sy), (-sp * sy, cp, sp * cy)))
    projected = (vertices - np.asarray(center)) @ projection.T
    low, high = projected.min(axis=0), projected.max(axis=0)
    cam.zoom = min((width - 4) / max(1, high[0] - low[0]),
                   (height - 4) / max(1, high[1] - low[1]))
    cam.pan = tuple(-(low + high) * .5 * cam.zoom)
    return cam


def sprite_input(lib: SectorMeshLibrary, mesh: SectorMesh, yaw: float,
                 pitch: float, ppu: float, fast: bool = False, fit: bool = False,
                 target_size=None):
    cam = IsoCamera(yaw=yaw, pitch=pitch, zoom=ppu)
    if target_size is not None:
        vertices = [(v[0] + ox, v[1], v[2] + oz) for face, ox, oz in mesh.faces for v in face.vertices]
        cam = fitted_preview_camera(vertices, yaw, pitch, target_size)
        width, height = cam.width, cam.height
        origin = (width / 2 + cam.pan[0], height / 2 + cam.pan[1])
        return width, height, origin, build_pieces(lib, mesh, cam, origin, fast)
    if fit and mesh.faces:
        projected = [cam.to_screen(cam.to_camera((v[0]+ox,v[1],v[2]+oz), True), (0,0))
                     for face,ox,oz in mesh.faces for v in face.vertices]
        xs, ys = zip(*projected)
        x0,y0,x1,y1 = min(xs)-3,min(ys)-3,max(xs)+3,max(ys)+3
    else:
        x0, y0, x1, y1 = sprite_bounds(cam)
    width, height = int(np.ceil(x1 - x0)), int(np.ceil(y1 - y0))
    origin = (-x0, -y0)
    pieces = build_pieces(lib, mesh, cam, origin, fast)
    return width, height, origin, pieces


def rasterize_sprite(data, tables, ppu) -> Sprite | None:
    width, height, origin, pieces = data
    if not pieces:
        return None
    result = IndexedRasterizer.render(
        width, height, pieces, tables, background_index=0,
        collect_diagnostics=False, track_polygon_owner=False)
    rgba = result.to_rgba(tables, transparent_background=True)
    image = QImage(rgba, width, height, width * 4,
                   QImage.Format.Format_RGBA8888).copy()
    return Sprite(image, origin, ppu)


