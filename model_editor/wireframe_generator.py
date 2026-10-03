"""Generate a HUD SKL from the current, visible Model Editor scene."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF

from depth_renderer import CameraPolygon, clip_camera_polygon_near
from indexed_renderer import IndexedRasterizer
from sklt_parser import create_minimal_sklt_model, save_sklt_with_poo2_pol2_structure

SOURCE_SIZE = QSize(1024, 1024)


@dataclass
class WireframeProjection:
    points: list[tuple[float, float, float]]
    segments: list[list[int]]

    def save(self, path: str | Path) -> None:
        """Use the existing SKLT writer; one POL2 record per HUD line."""
        if not self.segments:
            raise ValueError("The wireframe contains no visible lines.")
        save_sklt_with_poo2_pol2_structure(
            create_minimal_sklt_model(), self.points, self.segments, path)

    def preview_image(self, size: QSize = QSize(640, 640)) -> QImage:
        """Preview exactly the X/-Z points that will be exported."""
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(8, 12, 17))
        if not self.points:
            return image
        xy = np.array([(p[0], -p[2]) for p in self.points])
        low, high = xy.min(axis=0), xy.max(axis=0)
        span = max(float(max(high-low)), 1e-9)
        scale = min(size.width(), size.height()) * 0.90 / span
        xy = (xy-(low+high)/2)*scale + (size.width()/2, size.height()/2)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(232, 244, 255), 1.3))
        for a, b in self.segments:
            painter.drawLine(QPointF(*xy[a]), QPointF(*xy[b]))
        painter.end()
        return image


def _image_array(image: QImage) -> np.ndarray:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    data = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
        rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4]
    return data.reshape(rgba.height(), rgba.width(), 4).copy()


def _simplify(points, tolerance):
    if len(points) < 3:
        return points
    data = np.array(points, dtype=float)
    keep, pending = {0, len(points)-1}, [(0, len(points)-1)]
    while pending:
        lo, hi = pending.pop()
        first, last = data[lo], data[hi]
        delta = last-first
        length = float(delta @ delta)
        section = data[lo:hi+1]
        if length < 1e-9:
            distances = np.linalg.norm(section-first, axis=1)
        else:
            t = np.clip(((section-first) @ delta)/length, 0, 1)
            distances = np.linalg.norm(section-(first+t[:, None]*delta), axis=1)
        index = int(np.argmax(distances))+lo
        if distances[index-lo] > tolerance:
            keep.add(index)
            pending.extend(((lo, index), (index, hi)))
    return [points[i] for i in sorted(keep)]


def _mask_contours(mask):
    """Trace actual coverage, including disconnected texture islands."""
    boundary = {}
    def add(a, b):
        boundary.setdefault(a, set()).add(b)
    top = mask & ~np.pad(mask[:-1, :], ((1, 0), (0, 0)))
    right = mask & ~np.pad(mask[:, 1:], ((0, 0), (0, 1)))
    bottom = mask & ~np.pad(mask[1:, :], ((0, 1), (0, 0)))
    left = mask & ~np.pad(mask[:, :-1], ((0, 0), (1, 0)))
    for y, x in np.argwhere(top):
        add((int(x), int(y)), (int(x+1), int(y)))
    for y, x in np.argwhere(right):
        add((int(x+1), int(y)), (int(x+1), int(y+1)))
    for y, x in np.argwhere(bottom):
        add((int(x+1), int(y+1)), (int(x), int(y+1)))
    for y, x in np.argwhere(left):
        add((int(x), int(y+1)), (int(x), int(y)))
    directions = {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}
    while boundary:
        start = next(iter(boundary))
        current, chain, incoming = start, [start], None
        while current in boundary:
            options = boundary[current]
            if incoming is None:
                nxt = min(options)
            else:
                def priority(p):
                    direction = directions[(p[0]-current[0], p[1]-current[1])]
                    return {1: 0, 0: 1, 3: 2, 2: 3}[(direction-incoming) % 4]
                nxt = min(options, key=priority)
            options.remove(nxt)
            if not options:
                del boundary[current]
            incoming = directions[(nxt[0]-current[0], nxt[1]-current[1])]
            current = nxt
            chain.append(current)
            if current == start:
                break
        if len(chain) > 4:
            yield chain


def _runs(vertices, flags):
    run = []
    for (a, b), accepted in zip(zip(vertices, vertices[1:]), flags):
        if accepted:
            if not run:
                run.append(a)
            run.append(b)
        elif run:
            yield run
            run = []
    if run:
        yield run


def _geometry_ownership(viewport, pieces, camera, face_ids):
    """Paint the existing BSP pieces as ownership labels, without textures."""
    image = QImage(SOURCE_SIZE, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.black)
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    target = QRectF(0, 0, SOURCE_SIZE.width(), SOURCE_SIZE.height())
    for piece in pieces:
        code = face_ids[id(piece.payload.face)] + 1
        if code > 0xFFFFFF:
            painter.end()
            raise ValueError("Too many source faces for the preview.")
        painter.setBrush(QColor((code >> 16) & 255, (code >> 8) & 255, code & 255))
        painter.drawPolygon(QPolygonF([
            viewport._project(p, target, camera) for p in piece.vertices]))
    painter.end()
    rgb = _image_array(image)[:, :, :3].astype(np.int32)
    return (rgb[:, :, 0] << 16) + (rgb[:, :, 1] << 8) + rgb[:, :, 2] - 1


def generate_wireframe(viewport, preset: str = "Current View", *,
                       respect_transparency: bool = True,
                       simplification: float = 3.0) -> WireframeProjection:
    """Freeze visible scene geometry, project it and remove hidden edge runs."""
    if not viewport._faces:
        raise ValueError("Load a model before generating a wireframe.")
    if not math.isfinite(simplification) or not 0.5 <= simplification <= 12:
        raise ValueError("Contour simplification must be between 0.5 and 12 pixels.")
    original_camera = viewport._camera_state()
    try:
        if preset != "Current View":
            from assembly_viewer import VIEW_PRESET_ANGLES
            if preset not in VIEW_PRESET_ANGLES:
                raise ValueError("Unknown camera preset.")
            viewport.apply_view_preset(preset, SOURCE_SIZE, 100)
        camera = viewport._camera_state()
    finally:
        viewport._set_camera_state(original_camera)
    pieces = viewport.capture_wireframe_geometry(SOURCE_SIZE, camera)
    if not pieces:
        raise ValueError("The current view contains no visible faces.")
    faces = {id(piece.payload.face): piece.payload.face for piece in pieces}
    face_ids = {key: i for i, key in enumerate(faces)}
    target = QRectF(0, 0, SOURCE_SIZE.width(), SOURCE_SIZE.height())
    transparent = set()
    if respect_transparency:
        if viewport._indexed_adapter is None:
            raise ValueError("Texture transparency requires resolved SET textures and palette. "
                             "Turn off Respect texture transparency to use geometry.")
        indexed = viewport._indexed_pieces(target, pieces, camera)
        for piece in indexed:
            if piece.surface.tracy_mode != "none":
                transparent.add(face_ids[piece.source_face_id])
        indexed = [replace(p, polygon_id=face_ids[p.source_face_id]) for p in indexed]
        result = IndexedRasterizer.render(
            SOURCE_SIZE.width(), SOURCE_SIZE.height(), indexed,
            viewport._indexed_adapter.tables, collect_diagnostics=False)
        owner = np.asarray(result.polygon_owner, dtype=np.int32)
        coverage = np.asarray(result.coverage, dtype=bool)
    else:
        owner = _geometry_ownership(viewport, pieces, camera, face_ids)
        coverage = owner >= 0

    edges = {}
    for key, face in faces.items():
        face_id = face_ids[key]
        if face_id in transparent:
            continue
        vertices = tuple(viewport._camera_vertex(p, camera) for p in face.vertices)
        clipped = clip_camera_polygon_near(
            CameraPolygon(vertices, tuple(() for _ in vertices), None),
            minimum_distance=float(camera.get("near_distance", 0.2)))
        if clipped is None:
            continue
        projected = [viewport._project(p, target, camera) for p in clipped.vertices]
        xy = [tuple((p.x(), p.y())) for p in projected]
        for a, b in zip(xy, xy[1:]+xy[:1]):
            if np.linalg.norm(np.array(a)-b) < 0.5:
                continue
            # World-transformed coordinates join seams across material blocks
            # and keep separate assembly instances even when polyIDs repeat.
            edge = tuple(sorted((tuple(round(v, 6) for v in a), tuple(round(v, 6) for v in b))))
            edges.setdefault(edge, set()).add(face_id)

    segments = []
    h, w = owner.shape
    for (a, b), adjacent in edges.items():
        a, b = np.array(a), np.array(b)
        count = max(2, int(math.ceil(np.linalg.norm(b-a)*2))+1)
        # Clip screen samples before allocating; off-screen camera views may
        # project very long source edges at the near plane.
        lo, hi = 0.0, 1.0
        for axis, limit in ((0, w-1), (1, h-1)):
            delta = b[axis]-a[axis]
            if abs(delta) < 1e-9:
                if not 0 <= a[axis] <= limit:
                    hi = -1.0
                    break
            else:
                cuts = sorted((-a[axis]/delta, (limit-a[axis])/delta))
                lo, hi = max(lo, cuts[0]), min(hi, cuts[1])
        if hi <= lo:
            continue
        count = min(4096, max(2, int(count*(hi-lo))+1))
        t = np.linspace(lo, hi, count)
        samples = a+(b-a)*t[:, None]
        pixel = np.rint(samples).astype(int)
        accepted = np.zeros(count, dtype=bool)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                values = owner[np.clip(pixel[:, 1]+dy, 0, h-1), np.clip(pixel[:, 0]+dx, 0, w-1)]
                accepted |= np.isin(values, list(adjacent))
        start = None
        for i, good in enumerate(accepted):
            if good and start is None:
                start = i
            if start is not None and (not good or i == count-1):
                end = i if good else i-1
                if end > start and np.linalg.norm(samples[end]-samples[start]) >= 0.5:
                    segments.append((samples[start], samples[end]))
                start = None

    if transparent:
        # Texture contours contribute only where a visible transparent face
        # extends the silhouette. Opaque hull outlines already come from POL2.
        opaque_mask = (owner >= 0) & ~np.isin(owner, list(transparent))
        padded = np.pad(opaque_mask, 2)
        near_opaque = np.zeros_like(opaque_mask)
        for dy in range(5):
            for dx in range(5):
                near_opaque |= padded[dy:dy+h, dx:dx+w]
        for loop in _mask_contours(coverage):
            flags = []
            for a, b in zip(loop, loop[1:]):
                x, y = np.clip(np.rint((np.array(a)+b)/2).astype(int), (0, 0), (w-1, h-1))
                flags.append(not near_opaque[y, x])
            if flags and not all(flags) and any(flags):
                cut = flags.index(False)
                vertices = loop[:-1]
                vertices = vertices[cut+1:]+vertices[:cut+1]
                loop = vertices+[vertices[0]]
                flags = flags[cut+1:]+flags[:cut+1]
            for run in _runs(loop, flags):
                if len(run) < 5:
                    continue
                simple = _simplify(run, simplification)
                segments.extend((np.array(a), np.array(b)) for a, b in zip(simple, simple[1:])
                                if np.linalg.norm(np.array(a)-b) >= 1)
    if not segments:
        raise ValueError("The current view contains no visible lines.")
    xy = np.array([p for segment in segments for p in segment])
    low, high = xy.min(axis=0), xy.max(axis=0)
    center = (low+high)/2
    scale = 2000.0/max(float(max(high-low)), 1e-9)
    points, lines, positions, seen = [], [], {}, set()
    for segment in segments:
        indices = []
        for xy in segment:
            p = (round(float((xy[0]-center[0])*scale), 4), 0.0,
                 round(float(-(xy[1]-center[1])*scale), 4))
            if p not in positions:
                positions[p] = len(points)
                points.append(p)
            indices.append(positions[p])
        pair = tuple(sorted(indices))
        if indices[0] != indices[1] and pair not in seen:
            seen.add(pair)
            lines.append(indices)
    if len(points) > 65535:
        raise ValueError("The wireframe exceeds the SKLT 16-bit point index limit. "
                         "Increase contour simplification or isolate fewer objects.")
    return WireframeProjection(points, lines)
