"""Texture pictures for the Studio MCP: build a picture from layers, map it to a set palette, write an ILBM.

Urban Assault textures are 8-bit pictures (usually 256 x 256) whose colours are indices into the set palette
(Palette/STANDARD.PAL). Every picture made here is mapped to that palette before it is shown or written, so the
preview shows the colours the game will use. Writing reuses Studio's ILBM writer (``write_image_as_ilbm``).
"""

from __future__ import annotations

import inspect
import io
import random
import re
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from studio.exchange.portable import find_existing_path
from studio.formats.images import IlbmImage, parse_ilbm_bytes, parse_ilbm_file, parse_pal_file, write_image_as_ilbm
from studio.formats.setbas import decode_texture
from studio_mcp import core

DEFAULT_SIZE = (256, 256)
ILBM_SUFFIXES = (".ilbm", ".ilb", ".iff")
LAYERS: dict = {}


def layer(name: str):
    def register(function):
        LAYERS[name] = function
        return function
    return register


# --- palettes and decoding --------------------------------------------------------------


def set_palette(set_name: str) -> list[tuple[int, int, int]]:
    """The 256 colours of a set (Palette/STANDARD.PAL)."""
    folder = Path(core._set_info(set_name)["set_bas"]).parents[1]
    palettes = find_existing_path(folder, "Palette")
    path = find_existing_path(palettes, "STANDARD.PAL") if palettes else None
    palette = parse_pal_file(path) if path else None
    if not palette:
        raise core.McpToolError(f"{folder.name} has no Palette/STANDARD.PAL")
    return list(palette)[:256]


def indexed_to_rgb(image: IlbmImage, palette) -> Image.Image:
    colours = np.asarray((list(image.palette or palette) + [(0, 0, 0)] * 256)[:256], dtype=np.uint8)
    pixels = np.frombuffer(image.pixels, dtype=np.uint8).reshape(image.height, image.width)
    return Image.fromarray(colours[pixels], "RGB")


def decode_ilbm(spec: str, set_name: str) -> IlbmImage:
    """An indexed texture from 'set3:CITY1.ILBM' (a set's own texture) or an .ILBM file path."""
    match = re.fullmatch(r"(set\d+|\d+):(.+)", spec.strip(), re.I)
    if match:
        archive = core._setbas(match[1])[0]
        resource = next((r for r in archive.find(match[2].strip(), "ilbm.class") if r.decodable), None)
        if resource is None:
            raise core.McpToolError(f"{match[1]} has no texture {match[2]!r} (see studio_set_textures)")
        image = decode_texture(archive, resource)
        if image.palette is None:
            image.palette = set_palette(match[1])
        return image
    path = core.resolve_existing(spec)
    image = parse_ilbm_file(path)
    if image.pixels is None:
        raise core.McpToolError(f"{path.name} has no picture")
    if image.palette is None:
        image.palette = set_palette(set_name)
    return image


def load_picture(spec: str, set_name: str) -> Image.Image:
    """Any picture as RGB: a set texture ('set3:CITY1.ILBM'), an ILBM file or a PNG/JPG/BMP file."""
    if re.fullmatch(r"(set\d+|\d+):.+", spec.strip(), re.I) or Path(spec).suffix.lower() in ILBM_SUFFIXES:
        image = decode_ilbm(spec, set_name)
        return indexed_to_rgb(image, image.palette)
    path = core.resolve_existing(spec)
    try:
        return Image.open(path).convert("RGB")
    except OSError as exc:
        raise core.McpToolError(f"{path.name} cannot be read as a picture: {exc}") from exc


def to_indices(picture: Image.Image, palette, allow_index0: bool = False) -> bytes:
    """Nearest palette colour of every pixel. Index 0 is left out by default: in UA it is the see-through colour
    of masked materials, so ordinary pixels must never land on it."""
    colours = np.asarray(palette, dtype=np.int32)
    first = 0 if allow_index0 else 1
    rgb = np.asarray(picture.convert("RGB"), dtype=np.int32).reshape(-1, 3)
    unique, inverse = np.unique(rgb, axis=0, return_inverse=True)
    nearest = np.empty(len(unique), dtype=np.int64)
    for start in range(0, len(unique), 2048):   # in chunks: a noisy picture has tens of thousands of colours
        chunk = unique[start:start + 2048]
        distance = ((chunk[:, None, :] - colours[None, first:, :]) ** 2).sum(axis=2)
        nearest[start:start + 2048] = distance.argmin(axis=1) + first
    return nearest[inverse.reshape(-1)].astype(np.uint8).tobytes()


def ilbm_bytes(pixels: bytes, width: int, height: int, palette, name: str = "texture") -> bytes:
    """A standalone FORM ILBM written and verified by Studio's own writer."""
    image = IlbmImage(source_name=name, kind="ILBM", width=width, height=height, palette=list(palette),
                      pixels=pixels)
    with tempfile.TemporaryDirectory(prefix="onua-texture-") as folder:
        stage = Path(folder) / "texture.ilbm"
        write_image_as_ilbm(image, stage, palette_override=list(palette))
        data = stage.read_bytes()
    parse_ilbm_bytes(data, name)
    return data


def reindex(image: IlbmImage, palette) -> bytes:
    """The same picture with the colours of another palette (identical palettes keep the indices)."""
    source = list(image.palette or palette)[:256]
    if source == list(palette)[:256]:
        return image.pixels
    return to_indices(indexed_to_rgb(image, source), palette, allow_index0=False)


# --- building a picture from layers -----------------------------------------------------


def _colour(value, default=(128, 128, 128)) -> tuple[int, int, int]:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
            raise core.McpToolError(f"colour {value!r}: use '#rrggbb' or [r, g, b]")
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    if len(value) != 3:
        raise core.McpToolError(f"colour {value!r}: use '#rrggbb' or [r, g, b]")
    return tuple(max(0, min(255, int(c))) for c in value)


def _rect(value, picture) -> tuple[int, int, int, int]:
    if value is None:
        return 0, 0, picture.width, picture.height
    x0, y0, x1, y1 = (int(v) for v in value)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


@layer("fill")
def _fill(picture, set_name, color=None, rect=None):
    """Paint a rectangle (default: everything) in one colour."""
    ImageDraw.Draw(picture).rectangle(_rect(rect, picture), fill=_colour(color))


@layer("gradient")
def _gradient(picture, set_name, start=None, end=None, direction="vertical", rect=None):
    """Blend from `start` to `end` colour, top to bottom (vertical) or left to right (horizontal)."""
    x0, y0, x1, y1 = _rect(rect, picture)
    a, b = np.asarray(_colour(start, (90, 90, 90)), float), np.asarray(_colour(end, (40, 40, 40)), float)
    w, h = x1 - x0, y1 - y0
    t = np.linspace(0, 1, h if direction == "vertical" else w)
    band = (a + (b - a) * t[:, None]).astype(np.uint8)
    block = np.repeat(band[:, None, :], w, axis=1) if direction == "vertical" else np.repeat(band[None], h, axis=0)
    picture.paste(Image.fromarray(block, "RGB"), (x0, y0))


@layer("noise")
def _noise(picture, set_name, amount: float = 12, grain: int = 1, seed: int = 0, rect=None):
    """Random light/dark speckle (amount = colour change, grain = speckle size in pixels): concrete, rust, grit."""
    x0, y0, x1, y1 = _rect(rect, picture)
    rng = np.random.default_rng(seed)
    grain = max(1, int(grain))
    small = rng.normal(0, float(amount), ((y1 - y0 + grain - 1) // grain, (x1 - x0 + grain - 1) // grain))
    field = np.kron(small, np.ones((grain, grain)))[:y1 - y0, :x1 - x0]
    area = np.asarray(picture.crop((x0, y0, x1, y1)), float) + field[..., None]
    picture.paste(Image.fromarray(np.clip(area, 0, 255).astype(np.uint8), "RGB"), (x0, y0))


@layer("image")
def _image(picture, set_name, source=None, crop=None, at=None, size=None, tile: bool = False, opacity: float = 1.0):
    """Paste a picture: a set texture ('set3:CITY1.ILBM'), an ILBM or a PNG/JPG (e.g. a generated image).
    crop = [x0,y0,x1,y1] of the source, at = [x,y], size = [w,h] (default: the whole texture), tile = repeat."""
    if not source:
        raise core.McpToolError("image layer: give `source`")
    part = load_picture(str(source), set_name)
    if crop is not None:
        part = part.crop(_rect(crop, part))
    x, y = (int(v) for v in (at or (0, 0)))
    w, h = (int(v) for v in (size or ((picture.width - x, picture.height - y) if not tile else part.size)))
    part = part.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)
    spots = [(x, y)]
    if tile:
        spots = [(tx, ty) for ty in range(y, picture.height, part.height) for tx in range(x, picture.width, part.width)]
    for spot in spots:
        if opacity >= 1:
            picture.paste(part, spot)
        else:
            box = (spot[0], spot[1], spot[0] + part.width, spot[1] + part.height)
            picture.paste(Image.blend(picture.crop(box), part, float(opacity)), spot)


@layer("tint")
def _tint(picture, set_name, color=None, strength: float = 0.5, rect=None):
    """Colour the area like a filter: keeps light and shadow, moves the hue towards `color`."""
    box = _rect(rect, picture)
    area = picture.crop(box)
    grey = np.asarray(area.convert("L"), float)[..., None] / 255.0
    toned = np.asarray(_colour(color), float)[None, None, :] * grey * 1.25
    mixed = np.asarray(area, float) * (1 - strength) + np.clip(toned, 0, 255) * strength
    picture.paste(Image.fromarray(mixed.astype(np.uint8), "RGB"), box[:2])


@layer("adjust")
def _adjust(picture, set_name, brightness: float = 1.0, contrast: float = 1.0, saturation: float = 1.0, rect=None):
    """Brightness / contrast / saturation multipliers (1 = unchanged, 0.7 = darker or duller)."""
    box = _rect(rect, picture)
    area = picture.crop(box)
    area = ImageEnhance.Brightness(area).enhance(float(brightness))
    area = ImageEnhance.Contrast(area).enhance(float(contrast))
    area = ImageEnhance.Color(area).enhance(float(saturation))
    picture.paste(area, box[:2])


@layer("rect")
def _rect_layer(picture, set_name, rect=None, color=None, outline=None, width: int = 1):
    """A filled and/or outlined rectangle (door, sign, vent, hatch)."""
    ImageDraw.Draw(picture).rectangle(_rect(rect, picture), fill=_colour(color) if color is not None else None,
                                      outline=_colour(outline) if outline is not None else None, width=int(width))


@layer("lines")
def _lines(picture, set_name, cols: int = 4, rows: int = 4, color=None, width: int = 1, rect=None,
           highlight=None):
    """Panel seams: a grid of `cols` x `rows` plates. `highlight` draws a light edge beside every seam (bevel)."""
    x0, y0, x1, y1 = _rect(rect, picture)
    draw = ImageDraw.Draw(picture)
    dark = _colour(color, (30, 30, 30))
    for i in range(1, int(cols)):
        x = x0 + round(i * (x1 - x0) / int(cols))
        draw.line([(x, y0), (x, y1 - 1)], fill=dark, width=int(width))
        if highlight is not None:
            draw.line([(x + width, y0), (x + width, y1 - 1)], fill=_colour(highlight), width=1)
    for j in range(1, int(rows)):
        y = y0 + round(j * (y1 - y0) / int(rows))
        draw.line([(x0, y), (x1 - 1, y)], fill=dark, width=int(width))
        if highlight is not None:
            draw.line([(x0, y + width), (x1 - 1, y + width)], fill=_colour(highlight), width=1)


@layer("windows")
def _windows(picture, set_name, rect=None, cols: int = 6, rows: int = 8, margin=None, glass=None, frame=None,
             lit: float = 0.0, lit_color=None, seed: int = 0):
    """A facade grid of windows: `cols` x `rows` windows in `rect`, `margin` = [x, y] wall between them,
    `glass` / `frame` colours, `lit` = share of lit windows (0..1, night look) in `lit_color`."""
    x0, y0, x1, y1 = _rect(rect, picture)
    draw = ImageDraw.Draw(picture)
    cw, ch = (x1 - x0) / int(cols), (y1 - y0) / int(rows)
    mx, my = (float(v) for v in (margin or (cw * 0.3, ch * 0.35)))
    rng = random.Random(seed)
    for r in range(int(rows)):
        for c in range(int(cols)):
            box = (round(x0 + c * cw + mx / 2), round(y0 + r * ch + my / 2),
                   round(x0 + (c + 1) * cw - mx / 2) - 1, round(y0 + (r + 1) * ch - my / 2) - 1)
            fill = _colour(lit_color, (210, 190, 110)) if rng.random() < float(lit) else _colour(glass, (35, 45, 55))
            draw.rectangle(box, fill=fill, outline=_colour(frame) if frame is not None else None)


@layer("bricks")
def _bricks(picture, set_name, rect=None, brick=None, mortar=None, color=None, jitter: float = 18, seed: int = 0):
    """Brick or stone courses: `brick` = [w, h] in pixels, every other row shifted by half a brick."""
    x0, y0, x1, y1 = _rect(rect, picture)
    bw, bh = (int(v) for v in (brick or (24, 10)))
    base = np.asarray(_colour(color, (120, 80, 60)), float)
    draw = ImageDraw.Draw(picture)
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=_colour(mortar, (70, 65, 60)))
    rng = random.Random(seed)
    for row, y in enumerate(range(y0, y1, bh)):
        shift = (bw // 2) if row % 2 else 0
        for x in range(x0 - shift, x1, bw):
            tone = tuple(int(max(0, min(255, v + rng.uniform(-jitter, jitter)))) for v in base)
            draw.rectangle((max(x0, x + 1), y + 1, min(x1 - 1, x + bw - 2), min(y1 - 1, y + bh - 2)), fill=tone)


@layer("stripes")
def _stripes(picture, set_name, rect=None, count: int = 6, color=None, width: int = 8, direction: str = "diagonal"):
    """Warning or road stripes: `count` stripes of `width` pixels, 'diagonal', 'vertical' or 'horizontal'."""
    x0, y0, x1, y1 = _rect(rect, picture)
    layer_image = picture.crop((x0, y0, x1, y1))
    draw = ImageDraw.Draw(layer_image)
    w, h = layer_image.size
    step = (w + h if direction == "diagonal" else (w if direction == "vertical" else h)) / max(1, int(count))
    for i in range(int(count) + 1):
        p = i * step
        if direction == "vertical":
            draw.rectangle((p, 0, p + width, h), fill=_colour(color, (200, 170, 40)))
        elif direction == "horizontal":
            draw.rectangle((0, p, w, p + width), fill=_colour(color, (200, 170, 40)))
        else:
            draw.polygon([(p, 0), (p + width, 0), (p + width - h, h), (p - h, h)], fill=_colour(color, (200, 170, 40)))
    picture.paste(layer_image, (x0, y0))


@layer("dirt")
def _dirt(picture, set_name, amount: float = 0.35, color=None, blotches: int = 40, size: int = 24, seed: int = 0,
          rect=None):
    """Soft stains: `blotches` blurred spots of about `size` pixels in `color` (soot, rust, moss)."""
    x0, y0, x1, y1 = _rect(rect, picture)
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    draw = ImageDraw.Draw(mask)
    rng = random.Random(seed)
    for _ in range(int(blotches)):
        cx, cy = rng.uniform(0, x1 - x0), rng.uniform(0, y1 - y0)
        r = rng.uniform(0.4, 1.0) * int(size)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=int(255 * float(amount)))
    mask = mask.filter(ImageFilter.GaussianBlur(int(size) / 3))
    stain = Image.new("RGB", mask.size, _colour(color, (30, 25, 20)))
    area = picture.crop((x0, y0, x1, y1))
    picture.paste(Image.composite(stain, area, mask), (x0, y0))


@layer("blur")
def _blur(picture, set_name, radius: float = 1.0, rect=None):
    """Soften the area (radius in pixels)."""
    box = _rect(rect, picture)
    picture.paste(picture.crop(box).filter(ImageFilter.GaussianBlur(float(radius))), box[:2])


@layer("flip")
def _flip(picture, set_name, axis: str = "x"):
    """Mirror the whole picture: 'x' = left/right, 'y' = top/bottom."""
    flipped = picture.transpose(Image.Transpose.FLIP_LEFT_RIGHT if axis == "x" else Image.Transpose.FLIP_TOP_BOTTOM)
    picture.paste(flipped)


@layer("rotate")
def _rotate(picture, set_name, turns: int = 1):
    """Turn the whole picture by quarter turns (clockwise)."""
    picture.paste(picture.rotate(-90 * int(turns), expand=False))


def build_picture(layers: list[dict] | None, size=None, set_name: str = "set1", source: str = "") -> Image.Image:
    """Start from `source` (or mid grey) and apply the layers in order."""
    width, height = (int(v) for v in (size or DEFAULT_SIZE))
    if not (8 <= width <= 1024 and 8 <= height <= 1024):
        raise core.McpToolError("size must be between 8 and 1024 pixels (UA textures are 256 x 256)")
    picture = (load_picture(source, set_name).resize((width, height), Image.Resampling.LANCZOS)
               if source else Image.new("RGB", (width, height), (128, 128, 128)))
    for number, raw in enumerate(layers or [], start=1):
        arguments = dict(raw)
        name = arguments.pop("op", None)
        if name not in LAYERS:
            raise core.McpToolError(f"layer #{number}: unknown op {name!r}. Known: {sorted(LAYERS)}")
        try:
            LAYERS[name](picture, set_name, **arguments)
        except TypeError as exc:
            raise core.McpToolError(f"layer #{number} ({name}): {exc}") from exc
    return picture


def preview_png(pixels: bytes, width: int, height: int, palette, scale: int = 2, uv_grid: bool = False) -> bytes:
    """The indexed picture as PNG, enlarged; uv_grid marks every 32 UV units (UVs are 0..255 on a 256 texture)."""
    image = IlbmImage(width=width, height=height, pixels=pixels, palette=list(palette))
    picture = indexed_to_rgb(image, palette).resize((width * scale, height * scale), Image.Resampling.NEAREST)
    if uv_grid:
        draw = ImageDraw.Draw(picture)
        for uv in range(0, 257, 32):
            x, y = uv * width * scale / 256, uv * height * scale / 256
            draw.line([(x, 0), (x, picture.height)], fill=(255, 0, 200), width=1)
            draw.line([(0, y), (picture.width, y)], fill=(255, 0, 200), width=1)
            draw.text((x + 2, 2), str(uv), fill=(255, 255, 255))
            draw.text((2, y + 2), str(uv), fill=(255, 255, 255))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def layers_help() -> str:
    lines = []
    for name, function in sorted(LAYERS.items()):
        params = [f"{p}={v.default!r}" for p, v in list(inspect.signature(function).parameters.items())[2:]]
        lines.append(f"- {name}({', '.join(params)})\n    {' '.join((function.__doc__ or '').split())}")
    return "\n".join(lines)


# --- tools: preview and write a texture beside a model ---------------------------------------


def make(target: str, layers: list[dict] | None, size=None, set_name: str = "set1", source: str = "",
         overwrite: bool = False) -> tuple[dict, bytes]:
    """Write a new texture file (in the palette of `set_name`) under a writable folder, e.g. beside a model."""
    from studio_mcp import history
    path = core.ensure_writable(target)
    if path.suffix.lower() not in (".ilbm", ".ilb"):
        raise core.McpToolError("the texture file must end in .ILBM, e.g. Models/Base/New/Tower_Intact/NXTOW.ILBM")
    if path.exists() and not overwrite:
        raise core.McpToolError(f"{path} already exists (overwrite=true replaces it; studio_undo brings it back)")
    palette = set_palette(set_name)
    picture = build_picture(layers, size, set_name, source)
    pixels = to_indices(picture, palette)
    data = ilbm_bytes(pixels, picture.width, picture.height, palette, path.name)
    history.snapshot(path, [path], "make_texture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    info = {"texture": path.name, "file": str(path), "size": [picture.width, picture.height],
            "palette": set_name, "use": f"set_texture texture='{path.name}' on a model in this folder"}
    return info, preview_png(pixels, picture.width, picture.height, palette)


def preview(layers: list[dict] | None = None, size=None, set_name: str = "set1", source: str = "",
            uv_grid: bool = False) -> tuple[dict, bytes]:
    """Show a texture (a set texture, a file, or layers on top) in the set palette, without writing anything."""
    palette = set_palette(set_name)
    if source and not layers and not size:
        try:
            image = decode_ilbm(source, set_name) if (re.fullmatch(r"(set\d+|\d+):.+", source.strip(), re.I)
                                                      or Path(source).suffix.lower() in ILBM_SUFFIXES) else None
        except core.McpToolError:
            image = None
        if image is not None:
            pixels = reindex(image, palette)
            return ({"source": source, "size": [image.width, image.height], "palette": set_name},
                    preview_png(pixels, image.width, image.height, palette, uv_grid=uv_grid))
    picture = build_picture(layers, size, set_name, source)
    pixels = to_indices(picture, palette)
    return ({"size": [picture.width, picture.height], "palette": set_name, "layers": len(layers or [])},
            preview_png(pixels, picture.width, picture.height, palette, uv_grid=uv_grid))
