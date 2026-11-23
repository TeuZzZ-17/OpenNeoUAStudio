"""Automatic strategic-map icons for external sectors (``map_icon`` in a .sector file).

The game map has five zoom levels: zoom 0 and 1 show one symbol per sector (4 and 8 px), zoom 2, 3
and 4 one symbol per building cell (4, 8 and 16 px; a one-large-cell model covers 3 x 3 cells and
changes with its damage state). The icons are generated as lightweight SVGs from the model
geometry seen from above, in the tones of the vanilla map tiles, with a transparent background so
the engine's SVG loader (StatusIconLoad / IMG_LoadSizedSVG_RW) renders them crisply at each zoom.

Only our own models get generated icons; SET models keep their original map symbol.
Plain Python, no Qt, so the Sector Editor and the MCP share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

SECTOR_ICON_SIZES = (4, 8)          # map zoom 0, 1: pixels per sector
CELL_ICON_SIZES = (4, 8, 16)        # map zoom 2, 3, 4: pixels per building cell
CELL = 300.0                        # world size of one building cell
GROUND_HEIGHT = 6.0                 # faces lower than this are ground: left transparent
VIEWBOX_SIZE = 64.0

# Tones measured in the vanilla LEGO map tiles (dark to light), and the outline colour.
TONES = ("#27465f", "#4f90af", "#78bed7")
OUTLINE = "#a5e8ff"


@dataclass(frozen=True)
class SvgIcon:
    """Crisp resolution-independent SVG icon sized for one strategic-map zoom."""

    width: int
    height: int
    svg: str
    has_shapes: bool = True

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.svg, encoding="utf-8")
        return target


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


def render_icon(shapes, extent: float, size: int, offset=(0.0, 0.0)) -> SvgIcon:
    """Top-view SVG of ``shapes`` inside a square of half-width ``extent`` world units, ``size`` px.

    Ground faces are left transparent; higher faces get lighter tones and every face a light
    outline, the look of the vanilla map tiles.
    """
    px = max(1, int(size))
    scale = VIEWBOX_SIZE / (2.0 * extent)
    tall = sorted((s for s in shapes if s[0] >= GROUND_HEIGHT), key=lambda shape: shape[0])
    # Tune stroke weight for the target pixel size so tiny zoom icons stay clear without bloating large zooms.
    stroke_width = max(1.6, min(6.0, VIEWBOX_SIZE * 0.75 / px))
    elements = []
    if tall:
        top = max(h for h, _ in tall)
        for height, ring in tall:
            pts = " ".join(
                f"{(x + offset[0] + extent) * scale:.2f},{(extent - (z + offset[1])) * scale:.2f}"
                for x, z in ring
            )
            tone = TONES[min(len(TONES) - 1, int(len(TONES) * height / (top + 1e-6)))]
            elements.append(
                f'  <polygon points="{pts}" fill="{tone}" stroke="{OUTLINE}" '
                f'stroke-width="{stroke_width:.2f}" stroke-linejoin="round"/>'
            )
    body = "\n".join(elements)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {int(VIEWBOX_SIZE)} {int(VIEWBOX_SIZE)}">\n'
        f"{body}\n" if body else
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {int(VIEWBOX_SIZE)} {int(VIEWBOX_SIZE)}">\n'
    ) + "</svg>\n"
    return SvgIcon(width=px, height=px, svg=svg, has_shapes=bool(tall))


def model_icons(shapes, *, single: bool = False) -> list[SvgIcon]:
    """Icons for map zoom 2, 3 and 4. A one-large-cell model covers 3 x 3 cells."""
    cells = 3 if single else 1
    return [render_icon(shapes, CELL * cells / 2.0, size * cells) for size in CELL_ICON_SIZES]


def sector_icons(cells, *, single: bool = False) -> list[SvgIcon]:
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
    def _empty_item(item) -> bool:
        if isinstance(item, SvgIcon):
            return not item.has_shapes
        return not np.asarray(item)[..., 3].any()
    return all(_empty_item(image) for image in images)


def save_icons(images, folder: Path, stem: str) -> list[Path]:
    """Write one SVG per zoom as ``<stem>_<size>.svg``; returns the paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for image in images:
        ext = ".svg" if isinstance(image, SvgIcon) else ".png"
        path = folder / f"{stem}_{image.width}{ext}"
        image.save(path)
        paths.append(path)
    return paths
