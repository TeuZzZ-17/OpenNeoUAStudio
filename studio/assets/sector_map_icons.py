"""Automatic strategic-map icons for external sectors (``map_icon`` in a .sector file).

The game map has five zoom levels: zoom 0 and 1 show one symbol per sector (4 and 8 px), zoom 2, 3
and 4 one symbol per building cell (4, 8 and 16 px; a one-large-cell model covers 3 x 3 cells and
changes with its damage state). The icons are drawn from the model geometry seen from above, in the
tones of the vanilla map tiles, with a transparent background so the map below stays visible.

Only our own models get generated icons; SET models keep their original map symbol.
Plain Python + numpy + Pillow, no Qt, so the Sector Editor and the MCP share it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SECTOR_ICON_SIZES = (4, 8)          # map zoom 0, 1: pixels per sector
CELL_ICON_SIZES = (4, 8, 16)        # map zoom 2, 3, 4: pixels per building cell
CELL = 300.0                        # world size of one building cell
GROUND_HEIGHT = 6.0                 # faces lower than this are ground: left transparent
SUPERSAMPLE = 8

# Tones measured in the vanilla LEGO map tiles (dark to light), and the outline colour.
TONES = ((39, 70, 95), (79, 144, 175), (120, 190, 215))
OUTLINE = (165, 232, 255)


def model_shapes(family_object) -> list[tuple[float, list[tuple[float, float]]]]:
    """``[(height, [(x, z), ...]), ...]``: polygons of a BASE family tree that face up.

    Heights grow upwards (BASE y points down) and are measured from the lowest point of the
    model, so a slightly raised ground plate still counts as ground. Walls (no area seen
    from above) are dropped. Kid positions are added; rotations are ignored, as in static
    sector models.
    """
    raw = []

    def walk(obj, offset):
        transform = getattr(obj.base_object, "transform", None)
        position = getattr(transform, "position", (0.0, 0.0, 0.0)) if transform else (0.0, 0.0, 0.0)
        origin = (offset[0] + position[0], offset[1] + position[1], offset[2] + position[2])
        skeleton = getattr(obj, "skeleton", None)
        if skeleton is not None:
            points = skeleton.points
            for ring in skeleton.polygons:
                if len(ring) < 3 or any(i >= len(points) for i in ring):
                    continue
                corners = [(origin[0] + points[i][0], -(origin[1] + points[i][1]), origin[2] + points[i][2])
                           for i in ring]
                flat = [(x, z) for x, _, z in corners]
                area = 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(flat, flat[1:] + flat[:1])))
                if area > 1.0:
                    raw.append((sum(c[1] for c in corners) / len(corners), flat))
        for kid in obj.kids:
            walk(kid, origin)

    walk(family_object, (0.0, 0.0, 0.0))
    if not raw:
        return []
    # Ground level = the low height with the most area seen from above (the cell's floor
    # plate), not the absolute lowest point: basements and sunk rubble stay below it.
    lowest = min(h for h, _ in raw)
    areas: dict[int, float] = {}
    for height, ring in raw:
        if height <= lowest + 60.0:
            key = int((height - lowest) // 4)
            areas[key] = areas.get(key, 0.0) + _area(ring)
    floor = lowest + 4.0 * max(areas, key=areas.get)
    return [(h - floor, ring) for h, ring in raw]


def _area(ring) -> float:
    return 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])))


def render_icon(shapes, extent: float, size: int, offset=(0.0, 0.0)) -> Image.Image:
    """Top view of ``shapes`` inside a square of half-width ``extent`` world units, ``size`` px.

    Ground faces are left transparent; higher faces get lighter tones and every face a light
    outline, the look of the vanilla map tiles.
    """
    canvas = max(1, size) * SUPERSAMPLE
    scale = canvas / (2.0 * extent)
    tall = sorted((s for s in shapes if s[0] >= GROUND_HEIGHT), key=lambda shape: shape[0])
    rgb = Image.new("RGB", (canvas, canvas))
    mask = Image.new("L", (canvas, canvas))
    lines = Image.new("L", (canvas, canvas))
    if tall:
        top = max(h for h, _ in tall)
        draw_rgb, draw_mask, draw_lines = ImageDraw.Draw(rgb), ImageDraw.Draw(mask), ImageDraw.Draw(lines)
        width = max(1, SUPERSAMPLE // 2)
        for height, ring in tall:
            pixels = [((x + offset[0] + extent) * scale, (extent - (z + offset[1])) * scale) for x, z in ring]
            tone = TONES[min(len(TONES) - 1, int(len(TONES) * height / (top + 1e-6)))]
            draw_rgb.polygon(pixels, fill=tone)
            draw_mask.polygon(pixels, fill=255)
            draw_lines.line(pixels + pixels[:1], fill=255, width=width)
        # Tiny parts (masts, turrets) are widened to about one target pixel so they stay visible.
        grow = SUPERSAMPLE // 4 * 2 + 1
        mask = mask.filter(ImageFilter.MaxFilter(grow))
        rgb = rgb.filter(ImageFilter.MaxFilter(grow))
    pixels = np.asarray(rgb).copy()
    pixels[(np.asarray(lines) > 0) & (np.asarray(mask) > 0)] = OUTLINE
    image = Image.fromarray(np.dstack([pixels, np.asarray(mask)]).astype(np.uint8), "RGBA")
    # Premultiplied reduction: transparent black around buildings must not darken their edges.
    small = image.convert("RGBa").resize((size, size), Image.Resampling.BOX).convert("RGBA")
    # Crisp pixel art: alpha on/off, colours snapped to the map tones.
    data = np.asarray(small).copy()
    data[..., 3] = np.where(data[..., 3] >= 80, 255, 0)
    palette = np.array((*TONES, OUTLINE), dtype=np.int32)
    colours = data[..., :3].astype(np.int32)
    nearest = ((colours[..., None, :] - palette[None, None, :, :]) ** 2).sum(-1).argmin(-1)
    data[..., :3] = palette[nearest]
    data[data[..., 3] == 0, :3] = 0
    return Image.fromarray(data, "RGBA")


def model_icons(shapes, *, single: bool = False) -> list[Image.Image]:
    """Icons for map zoom 2, 3 and 4. A one-large-cell model covers 3 x 3 cells."""
    cells = 3 if single else 1
    return [render_icon(shapes, CELL * cells / 2.0, size * cells) for size in CELL_ICON_SIZES]


def sector_icons(cells, *, single: bool = False) -> list[Image.Image]:
    """Icons for map zoom 0 and 1 from the shapes of each cell (None = nothing drawn).

    ``cells``: 9 shape lists in SET.SDF order (far row first), or 1 for a one-large-cell sector.
    The sector square is 4 cells wide: the building cells sit in the middle, as in game.
    """
    shapes = []
    for index, cell in enumerate(cells):
        if not cell:
            continue
        dx = 0.0 if single else (index % 3 - 1) * CELL
        dz = 0.0 if single else (1 - index // 3) * CELL
        shapes += [(h, [(x + dx, z + dz) for x, z in ring]) for h, ring in cell]
    return [render_icon(shapes, CELL * 2.0, size) for size in SECTOR_ICON_SIZES]


def is_empty(images) -> bool:
    return all(not np.asarray(image)[..., 3].any() for image in images)


def save_icons(images, folder: Path, stem: str) -> list[Path]:
    """Write one PNG per zoom as ``<stem>_<size>.png``; returns the paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for image in images:
        path = folder / f"{stem}_{image.width}.png"
        image.save(path)
        paths.append(path)
    return paths
