"""Briefing artwork from the editor's real map geometry and indexed textures."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QOpenGLContext

from ..core.ldf_model import DEFAULT_HGT, SECTOR_SIZE
from .camera import IsoCamera
from .terrain_mesh import HEIGHT_UNIT, TerrainMesh
from .map_scene import scene_polygons, render_scene


def briefing_camera(doc):
    # Retail MBPIX scale the complete map footprint to a 302-pixel maximum
    # side. The outer sector ring then becomes the dark border in the artwork.
    columns, rows = doc.mw, doc.mh
    zoom = 302 / (max(columns, rows) * SECTOR_SIZE)
    return IsoCamera(yaw=0, pitch=90, zoom=zoom,
                     width=round(columns * SECTOR_SIZE * zoom),
                     height=round(rows * SECTOR_SIZE * zoom),
                     center=(doc.mw * SECTOR_SIZE / 2, 0, -doc.mh * SECTOR_SIZE / 2))


def style_briefing(image, doc, camera):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    height, width = image.height(), image.width()
    pixels = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())[:, :width * 4]
    rgba = pixels.reshape(height, width, 4)
    color = rgba[:, :, :3].astype(np.float64)
    luminance = color @ np.array((.299, .587, .114))
    color = color * .8 + luminance[:, :, None] * .2
    # Keep the installation's terrain colours, lifting dark indexed textures
    # into the readable, muted relief range of the original briefing art.
    tone = 255 * .72 * (luminance / 255) ** .72
    color *= (tone / np.maximum(luminance, 1))[:, :, None]
    yy, xx = np.indices((height, width))
    wx = (xx - width / 2) / camera.zoom + camera.center[0]
    wz = (yy - height / 2) / camera.zoom + camera.center[2]
    cols, rows = np.floor(wx / SECTOR_SIZE).astype(int), np.floor(-wz / SECTOR_SIZE).astype(int)
    inside = ((cols >= 1) & (cols < doc.mw - 1) & (rows >= 1) & (rows < doc.mh - 1)
              & (rgba[:, :, 3] > 0))
    heights = np.asarray(doc.grids['hgt'], dtype=float)
    # Bilinear height field gives continuous relief instead of block shading.
    gx = np.clip(wx / SECTOR_SIZE - .5, 0, doc.mw - 1)
    gz = np.clip(-wz / SECTOR_SIZE - .5, 0, doc.mh - 1)
    x0, z0 = gx.astype(int), gz.astype(int)
    x1, z1 = np.minimum(x0 + 1, doc.mw - 1), np.minimum(z0 + 1, doc.mh - 1)
    tx, tz = gx - x0, gz - z0
    field = ((heights[z0, x0] * (1-tx) + heights[z0, x1] * tx) * (1-tz)
             + (heights[z1, x0] * (1-tx) + heights[z1, x1] * tx) * tz)
    dy, dx = np.gradient(field)
    slope = camera.zoom * SECTOR_SIZE
    color *= np.clip(1 + (dx - dy) * slope * .06, .3, 1.85)[:, :, None]
    noise = np.random.default_rng(1977).random((height, width))
    color *= (.85 + noise * .30)[:, :, None]
    color[:, ::4] *= .95
    cell_pixels = camera.zoom * SECTOR_SIZE
    if cell_pixels >= 4:
        line = ((np.minimum(wx % SECTOR_SIZE, SECTOR_SIZE - wx % SECTOR_SIZE) < .45 / camera.zoom)
                | (np.minimum((-wz) % SECTOR_SIZE, SECTOR_SIZE - (-wz) % SECTOR_SIZE) < .45 / camera.zoom))
        color[line & inside] = color[line & inside] * .88 + np.array((75, 80, 73)) * .12
    radial = ((xx - (width - 1) / 2) / (width / 2)) ** 2 + ((yy - (height - 1) / 2) / (height / 2)) ** 2
    edge = np.minimum.reduce((xx, width - 1 - xx, yy, height - 1 - yy))
    fade = np.clip((1.65 - radial ** .85), .03, 1)
    fade *= np.clip((edge + noise * 5) / 14, 0, 1)
    color *= fade[:, :, None]
    color[~inside] = 0
    result = np.full((height, width, 4), 255, np.uint8)
    result[:, :, :3] = np.clip(np.rint(color), 0, 255).astype(np.uint8)
    return QImage(result.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def render_briefing(doc, lib, surface=None):
    doc = copy.deepcopy(doc)
    doc.squads = []  # MB/DB describe the level; live units and editor marks are excluded.
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


def write_briefing_pair(image, directory, stem):
    if not stem or stem.strip(' .') != stem or re.search(r'[<>:"/\\|?*\x00-\x1f]', stem):
        raise ValueError('Choose a filename without path separators or reserved characters.')
    if image.isNull():
        raise ValueError('The generated image is empty.')
    directory = Path(directory)
    paths = tuple(directory / f'{prefix}_{stem}.png' for prefix in ('Mb', 'Db'))
    if any(path.exists() for path in paths):
        raise FileExistsError('That MB/DB name already exists. Choose a new name to preserve the existing artwork.')
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise OSError('Unable to encode the briefing image.')
    data = bytes(buffer.data())
    directory.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for path in paths:
            with path.open('xb') as stream:
                created.append(path)
                stream.write(data)
    except OSError:
        for path in created:
            path.unlink()
        raise
    return paths
