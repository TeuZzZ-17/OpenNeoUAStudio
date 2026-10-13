"""Briefing artwork from the editor's real map geometry and indexed textures."""
from __future__ import annotations

import copy
import re
import zlib
from pathlib import Path

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QOpenGLContext

from ..core.ldf_model import DEFAULT_HGT, SECTOR_SIZE
from .camera import IsoCamera
from .terrain_mesh import HEIGHT_UNIT, TerrainMesh
from .map_scene import scene_polygons, render_scene

BRIEFING_SEED = 1977
# Share of the map's own color kept in the MB: the painted retail art is
# paler than the game textures.
BRIEFING_SATURATION = .8
# The DB is the same artwork, duller and darker.
DEBRIEFING_SATURATION = .7
DEBRIEFING_BRIGHTNESS = .8


def briefing_camera(doc):
    # Retail MBPIX scale the complete map footprint to a 302-pixel maximum
    # side. The outer sector ring then becomes the dark border in the artwork.
    columns, rows = doc.mw, doc.mh
    zoom = 302 / (max(columns, rows) * SECTOR_SIZE)
    return IsoCamera(yaw=0, pitch=90, zoom=zoom,
                     width=round(columns * SECTOR_SIZE * zoom),
                     height=round(rows * SECTOR_SIZE * zoom),
                     center=(doc.mw * SECTOR_SIZE / 2, 0, -doc.mh * SECTOR_SIZE / 2))


def _smooth_noise(rng, height, width, cells):
    """Smooth random field in 0..1 with about ``cells`` blobs along the longer side."""

    longest = max(height, width)
    cells_y = max(1, round(cells * height / longest))
    cells_x = max(1, round(cells * width / longest))
    source = rng.random((cells_y + 2, cells_x + 2))
    x = np.linspace(0, cells_x, width, endpoint=False)
    y = np.linspace(0, cells_y, height, endpoint=False)
    x0, y0 = x.astype(int), y.astype(int)
    tx, ty = x - x0, y - y0
    tx, ty = tx * tx * (3 - 2 * tx), ty * ty * (3 - 2 * ty)
    top = source[y0[:, None], x0] * (1 - tx) + source[y0[:, None], x0 + 1] * tx
    bottom = source[(y0 + 1)[:, None], x0] * (1 - tx) + source[(y0 + 1)[:, None], x0 + 1] * tx
    return top * (1 - ty[:, None]) + bottom * ty[:, None]


def _fractal(rng, height, width, cells, octaves, gain=.55):
    """Several noise layers of growing detail, normalised to 0..1."""

    total = 0.0
    amplitude = 1.0
    field = np.zeros((height, width))
    for octave in range(octaves):
        field += amplitude * _smooth_noise(rng, height, width, cells * 2 ** octave)
        total += amplitude
        amplitude *= gain
    return field / total


def _streaks(field, length):
    """Smear a field along one diagonal: the scratched look of the retail art."""

    smeared = np.zeros_like(field)
    for step in range(-length, length + 1):
        smeared += np.roll(np.roll(field, step, 0), -step, 1)
    return smeared / (2 * length + 1)


def _soften(field, radius):
    # Small separable box filter: preserve terrain structures while reducing
    # the stippled look of distant, indexed game textures.
    output = field
    for axis in (0, 1):
        pad = [(0, 0)] * field.ndim
        pad[axis] = (radius, radius)
        padded = np.pad(output, pad, mode='edge')
        output = sum(np.take(padded, range(i, i+field.shape[axis]), axis=axis)
                     for i in range(2*radius+1)) / (2*radius+1)
    return output


def _has_building(value):
    return str(value).strip().lstrip('0') != ''


def _building_marks(luminance, inside, doc, camera, cell_pixels):
    """Pixel centers of the building squares.

    Retail MB mark buildings on a lattice of a third of a sector: the busiest
    lattice cells of the rendered map get a square, and so does the middle of
    every sector that holds a building.
    """

    if cell_pixels < 8:
        return []  # too small for squares with a halo
    height, width = luminance.shape
    centers = set()
    for row in range(1, doc.mh - 1):
        for column in range(1, doc.mw - 1):
            if _has_building(doc.grids['blg'][row][column]):
                centers.add((
                    int(round(width / 2 - ((column + .5) * SECTOR_SIZE - camera.center[0]) * camera.zoom)),
                    int(round(height / 2 + (-(row + .5) * SECTOR_SIZE - camera.center[2]) * camera.zoom))))
    pitch = cell_pixels / 3
    if pitch >= 4:
        detail = np.abs(_soften(luminance, 1) - _soften(luminance, 3))
        columns, rows = int(width // pitch), int(height // pitch)
        energy = np.zeros((rows, columns))
        for j in range(rows):
            for i in range(columns):
                y0, x0 = int(j * pitch), int(i * pitch)
                energy[j, i] = detail[y0:int((j + 1) * pitch), x0:int((i + 1) * pitch)].mean()
        allowed = np.zeros((rows, columns), bool)
        for j in range(rows):
            for i in range(columns):
                allowed[j, i] = inside[int((j + .5) * pitch), int((i + .5) * pitch)]
        if allowed.any() and energy[allowed].max() > 0:
            limit = np.percentile(energy[allowed], 92)
            for j, i in zip(*np.nonzero(allowed & (energy > max(limit, 1e-6)))):
                centers.add((int((i + .5) * pitch), int((j + .5) * pitch)))
    return sorted(centers)


def _draw_building_marks(color, centers, cell_pixels):
    """Bright squares with a dark halo; the square takes the light of its ground."""

    height, width = color.shape[:2]
    size = int(np.clip(round(cell_pixels * .15), 3, 5))
    low, high = size // 2, size - size // 2
    for px, py in centers:
        x0, x1, y0, y1 = px - low, px + high, py - low, py + high
        if x0 < 1 or y0 < 1 or x1 > width - 1 or y1 > height - 1:
            continue
        tile = color[y0:y1, x0:x1].mean(axis=(0, 1))
        color[y0 - 1:y1 + 1, x0 - 1:x1 + 1] *= .5
        color[y0:y1, x0:x1] = np.clip(tile * 1.2 + 55, 0, 255)
        # The retail squares are barely visible: soften the edges.
        patch = color[y0 - 2:y1 + 2, x0 - 2:x1 + 2]
        if patch.shape[0] == size + 4 and patch.shape[1] == size + 4:
            color[y0 - 2:y1 + 2, x0 - 2:x1 + 2] = patch * .45 + _soften(patch, 1) * .55


def style_briefing(image, doc, camera):
    """Turn the rendered map into the dark, grainy planet surface of the retail MB.

    The retail artwork is a painted backdrop rather than a literal top view:
    a dark, speckled ground with bright crystalline patches, a faint sector
    grid, small bright squares for the buildings and a ragged black margin.
    The rendered map still decides the hue and the light/dark layout.
    """

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    height, width = image.height(), image.width()
    rgba = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())[:, :width * 4].reshape(height, width, 4)
    color = rgba[:, :, :3].astype(float)
    luminance = color @ np.array((.299, .587, .114))
    yy, xx = np.indices((height, width))
    wx = -(xx - width / 2) / camera.zoom + camera.center[0]
    wz = (yy - height / 2) / camera.zoom + camera.center[2]
    cols, rows = np.floor(wx / SECTOR_SIZE).astype(int), np.floor(-wz / SECTOR_SIZE).astype(int)
    inside = ((cols >= 1) & (cols < doc.mw - 1) & (rows >= 1) & (rows < doc.mh - 1) & (rgba[:, :, 3] > 0))
    heights = np.asarray(doc.grids['hgt'], dtype=float)
    gx = np.clip(wx / SECTOR_SIZE - .5, 0, doc.mw - 1)
    gz = np.clip(-wz / SECTOR_SIZE - .5, 0, doc.mh - 1)
    x0, z0 = gx.astype(int), gz.astype(int)
    x1, z1 = np.minimum(x0 + 1, doc.mw - 1), np.minimum(z0 + 1, doc.mh - 1)
    tx, tz = gx - x0, gz - z0
    field = ((heights[z0, x0]*(1-tx) + heights[z0, x1]*tx)*(1-tz)
             + (heights[z1, x0]*(1-tx) + heights[z1, x1]*tx)*tz)
    dy, dx = np.gradient(field)
    cell_pixels = camera.zoom * SECTOR_SIZE
    relief = _soften(np.clip(1 + (dx-dy)*cell_pixels*.12, .6, 1.6), 2)

    # Every map gets its own surface: the seed comes from its terrain.
    seed = zlib.crc32(np.ascontiguousarray(heights, np.float64).tobytes()) ^ (doc.mw << 8) ^ doc.mh
    rng = np.random.default_rng(BRIEFING_SEED + seed)
    ground = _fractal(rng, height, width, 5, 6, .62)
    fine = _fractal(rng, height, width, 60, 2, .6)
    grit = _fractal(rng, height, width, 110, 2, .8)
    blotch = _fractal(rng, height, width, 8, 4, .5)
    scratches = _streaks(_fractal(rng, height, width, 16, 5, .68), 4)
    patches = np.clip((scratches - .6) / .08, 0, 1) ** 1.4 * np.clip((ground - .4) / .2, 0, 1)
    # Dark pits and bright crystals: the painted contrast of the retail art.
    dark = np.clip((.5 - ground) / .3, 0, 1) ** 1.5

    scale = max(1, float(np.percentile(luminance[inside], 95))) if inside.any() else 255
    # Painted art has no tile edges: hue and brightness come from a wide blur.
    wide = _soften(_soften(color, 5), 5)
    wide_luma = wide @ np.array((.299, .587, .114))
    broad_map = wide_luma / scale
    mean_map = float(np.mean(luminance[inside])) / 255 if inside.any() else .3
    tone = (.05 + .4 * ground ** 2.4 * (.55 + .45 * fine)) * (1 - .6 * dark)
    tone *= .7 + .6 * np.clip(broad_map, 0, 1.2)
    tone *= relief * (.8 + .4 * blotch)
    # Crinkled, embossed paint: light from one side on the noise itself.
    slope_y, slope_x = np.gradient(_soften(ground * .7 + fine * .3, 1))
    tone *= np.clip(1 + (slope_x - slope_y) * 9, .6, 1.6) * (.85 + .3 * grit)
    tone += patches * (.35 + .45 * fine)
    # Fine detail of the map (buildings, roads) shows through as light speckle.
    tone += np.maximum(0, _soften(luminance, 1) - _soften(luminance, 2)) / scale * .22
    tone *= 1 + (rng.random((height, width)) - .5) * .45
    # Brightness follows the level: a pale set gives a lighter surface.
    if inside.any():
        tone *= np.clip((.09 + .18 * mean_map) / max(float(np.median(tone[inside])), 1e-3), .5, 2)
    tone = np.clip(tone, 0, 1)

    # The hue comes from the map, less saturated like the painted art; the
    # brightest crystals turn white.
    chroma = 1 + (wide / np.maximum(wide_luma[:, :, None], 1.0) - 1) * BRIEFING_SATURATION
    chroma = chroma * (1 + (rng.random((height, width, 3)) - .5) * .24)
    chroma = chroma * (1 - .5 * patches[:, :, None]) + .5 * patches[:, :, None]
    color = chroma * (tone * 255.0)[:, :, None]

    if cell_pixels >= 4:
        line = ((np.minimum(wx % SECTOR_SIZE, SECTOR_SIZE-wx % SECTOR_SIZE) < .5/camera.zoom)
                | (np.minimum((-wz) % SECTOR_SIZE, SECTOR_SIZE-(-wz) % SECTOR_SIZE) < .5/camera.zoom))
        grid = line & (rgba[:, :, 3] > 0)
        color[grid] = color[grid] * .8 + np.array((90, 92, 90)) * (.1 + .14 * fine[grid])[:, None]
        _draw_building_marks(color, _building_marks(luminance, inside, doc, camera, cell_pixels), cell_pixels)

    # Keep the rectangular retail footprint, with a ragged black margin.
    edge = np.minimum.reduce((xx + .5, width - xx - .5, yy + .5, height - yy - .5))
    fade = np.clip((edge - 8 + (blotch - .5) * 22) / 14, 0, 1) ** 1.3
    color *= fade[:, :, None]
    color[rgba[:, :, 3] == 0] = 0
    result = np.full((height, width, 4), 255, np.uint8)
    result[:, :, :3] = np.clip(np.rint(color), 0, 255).astype(np.uint8)
    return QImage(result.data, width, height, width*4, QImage.Format.Format_RGBA8888).copy()


def render_briefing(doc, lib, surface=None):
    doc = copy.deepcopy(doc)
    doc.squads = []  # MB/DB describe terrain and buildings, excluding live actors.
    doc.host_stations = []
    camera = briefing_camera(doc)
    output_camera = camera.copy()
    camera.width *= 4
    camera.height *= 4
    camera.zoom *= 4
    terrain = TerrainMesh()
    terrain.rebuild(doc.grids['hgt'])
    context = QOpenGLContext() if surface is not None else None
    if context is not None:
        context.setFormat(surface.format())
    if context is not None and context.create() and context.makeCurrent(surface):
        from PySide6.QtOpenGL import QOpenGLFramebufferObject
        from .gpu_renderer import GpuRenderer
        from .gpu_scene import WorldScene
        renderer = target = None
        try:
            scene = WorldScene()
            scene.set_library(lib)
            renderer = GpuRenderer()
            renderer.set_scene(scene, scene.update(doc, terrain))
            renderer.set_heights(-(terrain.cells - DEFAULT_HGT) * HEIGHT_UNIT)
            renderer.set_states(np.zeros((doc.mh, doc.mw, 4), np.uint8))
            target = QOpenGLFramebufferObject(camera.width, camera.height)
            renderer.render(camera, camera.width, camera.height, target.handle(),
                            owner_colors={}, grid=False, sky=False, overlays=False,
                            transparent_background=True)
            image = target.toImage()
        finally:
            if renderer is not None:
                renderer.delete()
            del target
            context.doneCurrent()
    else:
        polygons = scene_polygons(lib, doc, terrain, camera)
        image = render_scene(polygons, camera, lib.tables, fast=True).image()
    image = image.scaled(output_camera.width, output_camera.height,
                         Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return style_briefing(image, doc, output_camera)


def available_stem(directory, preferred):
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', preferred).strip(' .') or 'Untitled'
    candidate, number = stem, 2
    while any((Path(directory) / f'{prefix}_{candidate}.png').exists() for prefix in ('Mb', 'Db')):
        candidate = f'{stem}_{number}'
        number += 1
    return candidate


def debriefing_variant(image):
    """The DB of an MB: less saturated and darker, same shapes and margins."""

    db_image = image.convertToFormat(QImage.Format.Format_RGBA8888).copy()
    height, width = db_image.height(), db_image.width()
    pixels = np.frombuffer(db_image.bits(), np.uint8).reshape(
        height, db_image.bytesPerLine())[:, :width * 4].reshape(height, width, 4)
    color = pixels[:, :, :3].astype(float)
    luma = (color @ np.array((.299, .587, .114)))[:, :, None]
    color = (luma + (color - luma) * DEBRIEFING_SATURATION) * DEBRIEFING_BRIGHTNESS
    pixels[:, :, :3] = np.clip(np.rint(color), 0, 255).astype(np.uint8)
    return db_image


def write_briefing_pair(image, directory, stem):
    if not stem or stem.strip(' .') != stem or re.search(r'[<>:"/\\|?*\x00-\x1f]', stem):
        raise ValueError('Choose a filename without path separators or reserved characters.')
    if image.isNull():
        raise ValueError('The generated image is empty.')
    directory = Path(directory)
    paths = tuple(directory / f'{prefix}_{stem}.png' for prefix in ('Mb', 'Db'))
    if any(path.exists() for path in paths):
        raise FileExistsError('That MB/DB name already exists. Choose a new name to preserve the existing artwork.')
    db_image = debriefing_variant(image)
    payloads = []
    for artwork in (image, db_image):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not artwork.save(buffer, 'PNG'):
            raise OSError('Unable to encode the briefing image.')
        payloads.append(bytes(buffer.data()))
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path, data in zip(paths, payloads):
            with path.open('xb') as stream:
                created.append(path)
                stream.write(data)
    except OSError:
        for path in created:
            path.unlink()
        raise
    return paths
