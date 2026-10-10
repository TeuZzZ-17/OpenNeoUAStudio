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


def _smooth_noise(rng, height, width, cells):
    source = rng.random((cells + 1, cells + 1))
    x = np.linspace(0, cells, width)
    y = np.linspace(0, cells, height)
    x0, y0 = np.minimum(x.astype(int), cells - 1), np.minimum(y.astype(int), cells - 1)
    tx, ty = x - x0, y - y0
    return ((source[y0[:, None], x0] * (1-tx) + source[y0[:, None], x0+1] * tx) * (1-ty[:, None])
            + (source[(y0+1)[:, None], x0] * (1-tx) + source[(y0+1)[:, None], x0+1] * tx) * ty[:, None])


def _retail_palette(doc):
    from .. import bootstrap
    from ..core.game_installation import GameInstallation
    from ..core.resource_catalog import preview_image
    install = bootstrap.installation() or GameInstallation.suggest(bootstrap.game_data_dir())
    directory = install.folder('briefings')
    candidates = []
    for field in ('mbmap', 'dbmap'):
        name = Path(str(doc.lvl_info.get(field, '')).replace('\\', '/')).name
        if re.fullmatch(r'[md]b_\d+\.(png|iff)', name, re.I):
            candidates.append(directory / name)
    candidates.extend(directory / name for name in ('Mb_01.png', 'MB_01.IFF', 'Db_01.png', 'DB_01.IFF'))
    for path in candidates:
        if path.is_file():
            image = preview_image(path, None)
            if image is not None and not image.isNull():
                rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
                pixels = np.frombuffer(rgba.bits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(-1, 4)[:, :3]
                luminance = pixels @ np.array((.299, .587, .114))
                order = np.argsort(luminance)
                # Only colour statistics are reused; all geography comes from the edited map.
                bins = np.array_split(order[luminance[order] > 3], 24)
                if all(len(bucket) for bucket in bins):
                    return np.vstack((np.zeros(3), [pixels[bucket].mean(axis=0) for bucket in bins]))
    return None


def style_briefing(image, doc, camera, reference_palette=None):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    height, width = image.height(), image.width()
    rgba = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())[:, :width * 4].reshape(height, width, 4)
    color = rgba[:, :, :3].astype(float)
    luminance = color @ np.array((.299, .587, .114))
    yy, xx = np.indices((height, width))
    wx = (xx - width / 2) / camera.zoom + camera.center[0]
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
    relief = np.clip(1 + (dx-dy)*cell_pixels*.12, .25, 2.6)
    rng = np.random.default_rng(1977)
    grain = rng.random((height, width))
    mottling = _smooth_noise(rng, height, width, 12)
    local = (np.roll(luminance,1,0) + np.roll(luminance,-1,0) + np.roll(luminance,1,1) + np.roll(luminance,-1,1)) / 4
    scale = max(1, float(np.percentile(luminance[inside], 95))) if inside.any() else 255
    tone = np.clip(luminance / scale, 0, 1) ** 2.4 * .72
    tone *= relief * (.86 + grain*.28) * (.68 + mottling*.55)
    tone *= np.clip(1 + (luminance-local) / max(scale, 1)*1.5, .55, 1.6)
    tone = np.clip(tone, 0, 1)
    if reference_palette is not None:
        # Retail artwork has a narrow, dark terrain palette and bright relief accents.
        palette = np.asarray(reference_palette, float)
        coordinates = np.linspace(0, 1, len(palette))
        color = np.stack([np.interp(tone, coordinates, palette[:, channel]) for channel in range(3)], axis=2)
    else:
        color = (color*.5 + luminance[:, :, None]*.5) * (.3 + tone*.6)[:, :, None]
    if cell_pixels >= 4:
        line = ((np.minimum(wx % SECTOR_SIZE, SECTOR_SIZE-wx % SECTOR_SIZE) < .4/camera.zoom)
                | (np.minimum((-wz) % SECTOR_SIZE, SECTOR_SIZE-(-wz) % SECTOR_SIZE) < .4/camera.zoom))
        color[line & inside] = color[line & inside]*.65 + np.array((45, 52, 55))*.35
    # Keep the rectangular retail footprint, with a ragged dark margin rather than a round vignette.
    edge = np.minimum.reduce((gx-.5, doc.mw-1.5-gx, gz-.5, doc.mh-1.5-gz))*cell_pixels
    fade = np.clip((edge + (mottling-.5)*10) / max(8, cell_pixels*.75), 0, 1)
    color *= fade[:, :, None]
    color[~inside] = 0
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
    return style_briefing(image, doc, output_camera, _retail_palette(doc))


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
    db_image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = np.frombuffer(db_image.bits(), np.uint8).reshape(db_image.height(), db_image.bytesPerLine())[:, :db_image.width()*4].reshape(db_image.height(), db_image.width(), 4)
    pixels[:, :, :3] = np.rint(pixels[:, :, :3].astype(float)*.72).astype(np.uint8)
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
