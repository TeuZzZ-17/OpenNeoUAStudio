"""Indexed-colour raster backend, the AssetFamily adapter and its GPU framebuffer backend.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
import operator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Hashable, Iterable, Sequence

import numpy as np
from OpenGL import GL as gl

from map_editor.render.gpu_renderer import (
    _program,
    FRAGMENT,
    GpuRenderer,
    SCREEN_FRAGMENT,
    SCREEN_VERTEX,
)
from map_editor.render.gpu_scene import EMPTY, Geometry, STRIDE
from studio.formats.images import parse_ilbm_file

# ----------------------------------------------------------------------------
# Source-faithful indexed-colour raster backend for OpenNeoUAStudio.
# (formerly indexed_renderer.py)
# ----------------------------------------------------------------------------

try:
    import numpy as _np
except ImportError:  # pragma: no cover - optional acceleration only
    _np = None


ACCELERATION_BACKEND = "numpy" if _np is not None else "python"


MAX_SAFE_PIXELS = 4096 * 4096 if _np is not None else 1024 * 1024


_CAMERA_DISTANCE = 4.0


_MINIMUM_DEPTH = 0.2


_EPS = 1e-10


RGB = tuple[int, int, int]


Point2 = tuple[float, float]


Point3 = tuple[float, float, float]


def _u8(value, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be an integer")
    try:
        value = operator.index(value)
    except TypeError as exc:
        raise TypeError(f"{label} must be an integer") from exc
    if not 0 <= value <= 255:
        raise ValueError(f"{label} must be in the range 0..255")
    return value


def _grid_bytes(values, width: int, height: int, label: str) -> bytes:
    if hasattr(values, "pixels"):
        decoded_width = int(getattr(values, "width", width))
        decoded_height = int(getattr(values, "height", height))
        if (decoded_width, decoded_height) != (width, height):
            raise ValueError(
                f"{label} must be {width}x{height}, got "
                f"{decoded_width}x{decoded_height}")
        values = values.pixels
    if values is None:
        raise ValueError(f"{label} has no pixel data")
    if isinstance(values, (bytes, bytearray, memoryview)):
        raw = bytes(values)
    elif _np is not None and isinstance(values, _np.ndarray):
        array = _np.asarray(values)
        if array.dtype.kind not in "uib":
            raise TypeError(f"{label} must contain integer indices")
        if array.size and (int(array.min()) < 0 or int(array.max()) > 255):
            raise ValueError(f"{label} indices must be in 0..255")
        raw = _np.asarray(array, dtype=_np.uint8).reshape(-1).tobytes()
    else:
        try:
            outer = list(values)
        except TypeError as exc:
            raise TypeError(f"{label} must be an iterable") from exc
        flat: list[int] = []
        if len(outer) == height and any(not isinstance(item, int) for item in outer):
            for row in outer:
                row_values = list(row)
                if len(row_values) != width:
                    raise ValueError(f"{label} rows must contain {width} values")
                flat.extend(_u8(item, label) for item in row_values)
        else:
            flat.extend(_u8(item, label) for item in outer)
        raw = bytes(flat)
    expected = width * height
    if len(raw) != expected:
        raise ValueError(f"{label} needs {expected} pixels, got {len(raw)}")
    return raw


def _palette(value) -> tuple[RGB, ...]:
    entries = list(value)
    if len(entries) != 256:
        raise ValueError(f"palette needs 256 entries, got {len(entries)}")
    result: list[RGB] = []
    for index, entry in enumerate(entries):
        channels = list(entry)
        if len(channels) != 3:
            raise ValueError(f"palette entry {index} must be RGB")
        result.append(tuple(_u8(channel, f"palette entry {index}") for channel in channels))
    return tuple(result)


def _points(values, dimensions: int, label: str):
    result = []
    for index, point in enumerate(values):
        coords = tuple(float(value) for value in point)
        if len(coords) != dimensions or not all(math.isfinite(value) for value in coords):
            raise ValueError(f"{label}[{index}] must contain {dimensions} finite coordinates")
        result.append(coords)
    return tuple(result)


def retail_source_face_front_facing(
        camera_vertices, camera_distance: float = _CAMERA_DISTANCE) -> bool:
    """Return the retail whole-source-face pre-clip visibility decision."""

    points = _points(camera_vertices, 3, "camera_vertices")
    if len(points) < 3:
        return False
    transformed = [
        (float(x), float(y), float(camera_distance - z))
        for x, y, z in points[:3]
    ]
    first = tuple(
        transformed[1][axis] - transformed[0][axis] for axis in range(3))
    second = tuple(
        transformed[2][axis] - transformed[1][axis] for axis in range(3))
    normal = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    return sum(
        normal[axis] * transformed[0][axis] for axis in range(3)) >= 0.0


@dataclass(frozen=True)
class IndexedTables:
    """Palette plus the retail 256x256 SHADERMP/TRACYRMP lookup tables."""

    palette: tuple[RGB, ...]
    shader_pixels: bytes = field(repr=False)
    tracy_pixels: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "palette", _palette(self.palette))
        object.__setattr__(self, "shader_pixels", _grid_bytes(
            self.shader_pixels, 256, 256, "SHADERMP"))
        object.__setattr__(self, "tracy_pixels", _grid_bytes(
            self.tracy_pixels, 256, 256, "TRACYRMP"))

    @classmethod
    def from_decoded(cls, palette, shader_pixels, tracy_pixels) -> "IndexedTables":
        return cls(tuple(palette), shader_pixels, tracy_pixels).validate_ua_profile()

    def validate_ua_profile(self) -> "IndexedTables":
        """Fail closed when tables do not match known UA axis invariants."""

        shader = self.shader_pixels
        tracy = self.tracy_pixels
        if any(shader[255 * 256 + source] != 0 for source in range(256)):
            raise ValueError("SHADERMP UA profile failed: terminal row is not black")
        if any(shader[shade * 256] != 0 for shade in range(256)):
            raise ValueError("SHADERMP UA profile failed: source index zero is not stable")
        for source, expected in ((1, 1), (5, 5), (6, 0), (255, 255)):
            if shader[source] != expected:
                raise ValueError(
                    f"SHADERMP UA profile failed at [0][{source}]: "
                    f"{shader[source]} != {expected}")

        # Source-derived span_lnf orientation: TRACYRMP[background][raw_source].
        if any(tracy[13 * 256 + source] != source for source in range(256)):
            raise ValueError("TRACYRMP UA profile failed: background row 13 is not source identity")
        for source in (8, 10, 11, 12, 14, 15):
            if any(tracy[background * 256 + source] != source
                   for background in range(256)):
                raise ValueError(
                    f"TRACYRMP UA profile failed: opaque source column {source} changed")
        for background in range(256):
            expected = 0 if background == 13 else background
            if tracy[background * 256] != expected:
                raise ValueError(
                    "TRACYRMP UA profile failed: transparent source column 0 "
                    f"over background {background}")
        known = {
            (31, 220): 212,
            (220, 31): 200,
            (13, 255): 255,
            (255, 13): 177,
            (156, 185): 54,
            (159, 185): 106,
        }
        for (background, source), expected in known.items():
            actual = tracy[background * 256 + source]
            if actual != expected:
                raise ValueError(
                    f"TRACYRMP UA profile failed at [{background}][{source}]: "
                    f"{actual} != {expected}")
        first_156 = tracy[156]
        first_159 = tracy[159]
        if (tracy[first_156 * 256 + 159],
                tracy[first_159 * 256 + 156]) != (207, 191):
            raise ValueError(
                "TRACYRMP UA profile failed: non-commutative 156/159 "
                "layer-order tripwire did not match")
        return self

    @property
    def display_palette(self) -> tuple[RGB, ...]:
        # The retail framebuffer treats palette index zero as black/clear.
        return ((0, 0, 0),) + self.palette[1:]

    def shade_index(self, shade: int, source: int) -> int:
        return self.shader_pixels[_u8(shade, "shade") * 256 + _u8(source, "source")]

    def tracy_index(self, background: int, source: int) -> int:
        return self.tracy_pixels[
            _u8(background, "TRACY background") * 256 + _u8(source, "TRACY source")]


@dataclass(frozen=True)
class IndexedSurface:
    name: str
    kind: str  # texture | solid
    indices: bytes | Sequence[int] | None = field(repr=False)
    width: int
    height: int
    solid_index: int | None
    shade_mode: str  # none | gradient
    shade_value: int
    tracy_mode: str  # none | clear | flat
    map_mode: str  # linear | depth

    def __post_init__(self) -> None:
        if self.kind not in ("texture", "solid"):
            raise ValueError("surface kind must be texture or solid")
        if self.shade_mode not in ("none", "gradient"):
            raise ValueError("shade mode must be none or gradient")
        if self.tracy_mode not in ("none", "clear", "flat"):
            raise ValueError("TRACY mode must be none, clear, or flat")
        if self.map_mode not in ("linear", "depth"):
            raise ValueError("map mode must be linear or depth")
        object.__setattr__(self, "shade_value", _u8(self.shade_value, "shade value"))
        if self.kind == "solid":
            if self.solid_index is None:
                raise ValueError("solid surfaces need solid_index")
            object.__setattr__(self, "solid_index", _u8(self.solid_index, "solid index"))
            object.__setattr__(self, "indices", None)
            object.__setattr__(self, "width", 0)
            object.__setattr__(self, "height", 0)
        else:
            width, height = int(self.width), int(self.height)
            if width <= 0 or height <= 0:
                raise ValueError("texture dimensions must be positive")
            object.__setattr__(self, "indices", _grid_bytes(
                self.indices, width, height, f"texture {self.name}"))
            object.__setattr__(self, "width", width)
            object.__setattr__(self, "height", height)


@dataclass(frozen=True)
class IndexedPiece:
    source_face_id: Hashable
    polygon_id: int
    screen: tuple[Point2, ...]
    uvs: tuple[Point2, ...]
    camera_vertices: tuple[Point3, ...]
    surface: IndexedSurface
    source_order: int = 0
    sort_depth: float = 0.0
    # AREA_FLAG_DPTHFADE is evaluated from radial camera distance at each
    # source vertex, matching skeleton.cpp.  Distances stay in asset units;
    # the rasterizer only interpolates the resulting shade contribution.
    distance_fade: bool = False
    fade_start: float = 0.0
    fade_length: float = 600.0
    vertex_distances: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "screen", _points(self.screen, 2, "screen"))
        object.__setattr__(self, "camera_vertices", _points(
            self.camera_vertices, 3, "camera_vertices"))
        object.__setattr__(self, "uvs", _points(self.uvs, 2, "uvs") if self.uvs else ())
        if len(self.screen) < 3 or len(self.camera_vertices) != len(self.screen):
            raise ValueError("piece geometry is incomplete")
        if self.surface.kind == "texture" and len(self.uvs) != len(self.screen):
            raise ValueError("textured pieces need one UV per vertex")
        object.__setattr__(self, "polygon_id", int(self.polygon_id))
        object.__setattr__(self, "source_order", int(self.source_order))
        depth = float(self.sort_depth)
        if not math.isfinite(depth):
            raise ValueError("sort_depth must be finite")
        object.__setattr__(self, "sort_depth", depth)
        object.__setattr__(self, "distance_fade", bool(self.distance_fade))
        fade_start = float(self.fade_start)
        fade_length = float(self.fade_length)
        if not math.isfinite(fade_start):
            raise ValueError("fade_start must be finite")
        if not math.isfinite(fade_length) or fade_length <= 0.0:
            raise ValueError("fade_length must be finite and positive")
        distances = tuple(float(value) for value in self.vertex_distances)
        if distances and (
                len(distances) != len(self.screen)
                or any(not math.isfinite(value) or value < 0.0
                       for value in distances)):
            raise ValueError(
                "vertex_distances must provide one finite non-negative "
                "distance per vertex")
        if self.distance_fade and not distances:
            raise ValueError(
                "distance-faded pieces need vertex_distances")
        object.__setattr__(self, "fade_start", fade_start)
        object.__setattr__(self, "fade_length", fade_length)
        object.__setattr__(self, "vertex_distances", distances)


@dataclass(frozen=True)
class IndexedRenderResult:
    width: int
    height: int
    indices: object = field(repr=False)
    coverage: object = field(repr=False)
    polygon_owner: object = field(repr=False)
    stats: dict[str, object]

    def to_rgba(self, tables: IndexedTables, transparent_background: bool = True) -> bytes:
        palette = tables.display_palette
        if _np is not None and isinstance(self.indices, _np.ndarray):
            palette_rgba = _np.empty((256, 4), dtype=_np.uint8)
            palette_rgba[:, :3] = _np.asarray(palette, dtype=_np.uint8)
            palette_rgba[:, 3] = 255
            rgba = palette_rgba[self.indices]
            if transparent_background:
                rgba[:, :, 3] = self.coverage
                rgba[:, :, 3] *= 255
            return rgba.tobytes()
        out = bytearray(self.width * self.height * 4)
        for y in range(self.height):
            for x in range(self.width):
                index = int(self.indices[y][x])
                r, g, b = palette[index]
                covered = bool(self.coverage[y][x])
                a = 255 if covered or not transparent_background else 0
                offset = (y * self.width + x) * 4
                out[offset:offset + 4] = bytes((r, g, b, a))
        return bytes(out)


def _barycentric(px: float, py: float, tri: tuple[Point2, Point2, Point2]):
    (ax, ay), (bx, by), (cx, cy) = tri
    denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denom) <= _EPS:
        return None
    w0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denom
    w1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denom
    w2 = 1.0 - w0 - w1
    if w0 < -1e-8 or w1 < -1e-8 or w2 < -1e-8:
        return None
    return (w0, w1, w2)


def _sample(value: float, size: int) -> int:
    coordinate = int(math.floor(value / 256.0 * size))
    return min(size - 1, max(0, coordinate))


class IndexedRasterizer:
    """Deterministic indexed rasterizer for the shared Textured renderer."""

    acceleration_backend = ACCELERATION_BACKEND
    max_safe_pixels = MAX_SAFE_PIXELS

    @staticmethod
    def _retail_pass_order(pieces: tuple[IndexedPiece, ...]):
        indexed = list(enumerate(pieces))
        opaque = [item for item in indexed if item[1].surface.tracy_mode == "none"]
        transparent = [item for item in indexed if item[1].surface.tracy_mode != "none"]
        transparent.sort(
            key=lambda item: (item[1].sort_depth, item[1].source_order, item[0]),
            reverse=True,
        )
        return opaque + transparent

    @staticmethod
    def render(width: int, height: int, pieces: Iterable[IndexedPiece],
               tables: IndexedTables, background_index: int = 0,
               flat_tracy_destination_override: int | None = None, *,
               collect_diagnostics: bool = True,
               track_polygon_owner: bool = True,
               background_indices: bytes | None = None) -> IndexedRenderResult:
        if isinstance(width, bool) or isinstance(height, bool):
            raise TypeError("render dimensions must be integers")
        width, height = operator.index(width), operator.index(height)
        if width <= 0 or height <= 0:
            raise ValueError("render dimensions must be positive")
        if width * height > MAX_SAFE_PIXELS:
            raise ValueError(
                f"indexed render {width}x{height} exceeds safe limit "
                f"{MAX_SAFE_PIXELS} pixels")
        if not isinstance(tables, IndexedTables):
            raise TypeError("tables must be IndexedTables")
        background = _u8(background_index, "background index")
        if background_indices is not None:
            if not isinstance(background_indices, bytes):
                raise TypeError("background indices must be bytes")
            if len(background_indices) != width * height:
                raise ValueError("background indices must match render dimensions")
        forced = (None if flat_tracy_destination_override is None else
                  _u8(flat_tracy_destination_override, "forced TRACY destination"))
        ordered = tuple(pieces)
        if not all(isinstance(piece, IndexedPiece) for piece in ordered):
            raise TypeError("pieces must contain IndexedPiece values")

        schedule = IndexedRasterizer._retail_pass_order(ordered)
        has_transparent = any(
            piece.surface.tracy_mode != "none" for piece in ordered)
        if _np is not None:
            framebuffer = _np.full((height, width), background, dtype=_np.uint8)
            if background_indices is not None:
                framebuffer = _np.frombuffer(
                    background_indices, dtype=_np.uint8).reshape(height, width).copy()
            coverage = _np.zeros((height, width), dtype=bool)
            owner = (
                _np.full((height, width), -1, dtype=_np.int32)
                if track_polygon_owner else None)
            opaque_order = (
                _np.full((height, width), -1, dtype=_np.int32)
                if has_transparent else None)

            flat_face_ids: list[Hashable] = []
            flat_face_set: set[Hashable] = set()
            for _input_order, piece in schedule:
                if (piece.surface.tracy_mode == "flat"
                        and piece.source_face_id not in flat_face_set):
                    flat_face_set.add(piece.source_face_id)
                    flat_face_ids.append(piece.source_face_id)
            if not flat_face_ids:
                flat_seen_bits = None
                flat_bits = {}
                flat_sparse = None
            elif len(flat_face_ids) <= 64:
                bit_dtype = (
                    _np.uint8 if len(flat_face_ids) <= 8 else
                    _np.uint16 if len(flat_face_ids) <= 16 else
                    _np.uint32 if len(flat_face_ids) <= 32 else _np.uint64
                )
                flat_seen_bits = _np.zeros((height, width), dtype=bit_dtype)
                flat_bits = {
                    face_id: bit_dtype(1 << index)
                    for index, face_id in enumerate(flat_face_ids)
                }
                flat_sparse = None
            else:
                # Assets with more than 64 flat-TRACY source faces are rare;
                # retain exact behaviour without allocating an unbounded mask.
                flat_seen_bits = None
                flat_bits = {}
                flat_sparse = {face_id: set() for face_id in flat_face_ids}
            shader = _np.frombuffer(
                tables.shader_pixels, dtype=_np.uint8).reshape(256, 256)
            tracy = _np.frombuffer(
                tables.tracy_pixels, dtype=_np.uint8).reshape(256, 256)
        else:
            framebuffer = [[background] * width for _ in range(height)]
            if background_indices is not None:
                framebuffer = [list(background_indices[y * width:(y + 1) * width])
                               for y in range(height)]
            coverage = [[False] * width for _ in range(height)]
            owner = (
                [[-1] * width for _ in range(height)]
                if track_polygon_owner else None)
            opaque_order = (
                [[-1] * width for _ in range(height)]
                if has_transparent else None)
            flat_seen: dict[Hashable, set[int]] = {}

        counters = Counter()
        for input_order, piece in schedule:
            counters["ordered_pieces"] += 1
            for index in range(2, len(piece.screen)):
                ids = (0, index, index - 1)
                tri_screen = tuple(piece.screen[i] for i in ids)
                tri_uvs = tuple(piece.uvs[i] for i in ids) if piece.uvs else ()
                tri_camera = tuple(piece.camera_vertices[i] for i in ids)
                tri_distances = (
                    tuple(piece.vertex_distances[i] for i in ids)
                    if piece.distance_fade else ())
                counters["fan_triangles"] += 1
                if _np is not None:
                    IndexedRasterizer._triangle_numpy(
                        framebuffer, coverage, owner, opaque_order,
                        flat_seen_bits, flat_bits.get(piece.source_face_id),
                        (None if flat_sparse is None else
                         flat_sparse.get(piece.source_face_id)),
                        piece, input_order, tri_screen, tri_uvs, tri_camera,
                        tri_distances,
                        shader, tracy, counters, forced, width, height,
                    )
                else:
                    face_seen = (
                        flat_seen.setdefault(piece.source_face_id, set())
                        if piece.surface.tracy_mode == "flat" else None)
                    IndexedRasterizer._triangle_python(
                        framebuffer, coverage, owner, opaque_order,
                        face_seen, piece, input_order,
                        tri_screen, tri_uvs, tri_camera, tri_distances,
                        tables, counters, forced,
                        width, height,
                    )

        if _np is not None:
            covered_pixels = int(_np.count_nonzero(coverage))
        else:
            covered_pixels = sum(1 for row in coverage for value in row if value)
        raw = (
            framebuffer.tobytes()
            if collect_diagnostics and _np is not None else
            bytes(value for row in framebuffer for value in row)
            if collect_diagnostics else None)
        stats = dict(counters)
        stats.update({
            "covered_pixels": covered_pixels,
            "index_buffer_sha256": (
                hashlib.sha256(raw).hexdigest() if raw is not None else ""),
            "diagnostics_collected": bool(collect_diagnostics),
            "shader_lookup_axis_order": "SHADERMP[shade][source]",
            "tracy_lookup_axis_order": "TRACYRMP[background][raw_source]",
            "initial_framebuffer_index": background,
            "flat_tracy_destination_mode": (
                "live_framebuffer" if forced is None else "forced_diagnostic"),
            "flat_tracy_forced_destination_index": forced,
            "source_faithful_frame_clear": background == 0,
            "source_faithful_flat_destination_reads": forced is None,
            "canonical_retail_destination_policy": background == 0 and forced is None,
            "transparent_replay_rule": (
                "opaque BSP pass, then transparent whole-face depth LIFO"),
        })
        return IndexedRenderResult(width, height, framebuffer, coverage, owner, stats)

    @staticmethod
    def _get(grid, y: int, x: int):
        return grid[y][x]

    @staticmethod
    def _set(grid, y: int, x: int, value):
        grid[y][x] = value

    @staticmethod
    def _triangle_numpy(framebuffer, coverage, owner, opaque_order,
                        flat_seen_bits, flat_face_bit, sparse_seen,
                        piece: IndexedPiece, input_order: int,
                        screen, uvs, camera, distances, shader, tracy,
                        counters, forced,
                        width: int, height: int) -> None:
        left = max(0, math.floor(min(point[0] for point in screen)))
        right = min(width - 1, math.ceil(max(point[0] for point in screen)))
        top = max(0, math.floor(min(point[1] for point in screen)))
        bottom = min(height - 1, math.ceil(max(point[1] for point in screen)))
        if left > right or top > bottom:
            return

        (ax, ay), (bx, by), (cx, cy) = screen
        denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denom) <= _EPS:
            return
        px = _np.arange(left, right + 1, dtype=_np.float64)[None, :] + 0.5
        py = _np.arange(top, bottom + 1, dtype=_np.float64)[:, None] + 0.5
        w0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denom
        w1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denom
        w2 = 1.0 - w0 - w1
        mask = (w0 >= -1e-8) & (w1 >= -1e-8) & (w2 >= -1e-8)
        if not bool(mask.any()):
            return

        surface = piece.surface
        if surface.map_mode == "depth":
            counters["depth_mapped_triangles"] += 1
        else:
            counters["linear_mapped_triangles"] += 1

        if flat_face_bit is not None:
            seen_view = flat_seen_bits[top:bottom + 1, left:right + 1]
            seen_mask = _np.bitwise_and(seen_view, flat_face_bit) != 0
            skipped = int(_np.count_nonzero(mask & seen_mask))
            if skipped:
                counters["flat_tracy_duplicate_samples_skipped"] += skipped
                mask &= ~seen_mask
            if not bool(mask.any()):
                return
        elif sparse_seen is not None:
            ys, xs = _np.nonzero(mask)
            linear = (top + ys).astype(_np.int64) * width + (left + xs)
            fresh = _np.fromiter(
                (int(value) not in sparse_seen for value in linear),
                dtype=bool, count=linear.size)
            skipped = int(_np.count_nonzero(~fresh))
            if skipped:
                counters["flat_tracy_duplicate_samples_skipped"] += skipped
                mask[ys[~fresh], xs[~fresh]] = False
            if not bool(mask.any()):
                return

        if surface.kind == "solid":
            raw_source = _np.full(mask.shape, int(surface.solid_index), dtype=_np.uint8)
        else:
            mapped = (w0, w1, w2)
            if surface.map_mode == "depth":
                depth = _np.asarray([
                    max(_MINIMUM_DEPTH, _CAMERA_DISTANCE - vertex[2])
                    for vertex in camera
                ], dtype=_np.float64)
                c0, c1, c2 = w0 / depth[0], w1 / depth[1], w2 / depth[2]
                total = c0 + c1 + c2
                valid_depth = total > _EPS
                mask &= valid_depth
                if not bool(mask.any()):
                    return
                total = _np.where(valid_depth, total, 1.0)
                mapped = (c0 / total, c1 / total, c2 / total)
                counters["depth_mapped_samples"] += int(_np.count_nonzero(mask))
            else:
                counters["linear_mapped_samples"] += int(_np.count_nonzero(mask))
            uv = _np.asarray(uvs, dtype=_np.float64)
            u = mapped[0] * uv[0, 0] + mapped[1] * uv[1, 0] + mapped[2] * uv[2, 0]
            v = mapped[0] * uv[0, 1] + mapped[1] * uv[1, 1] + mapped[2] * uv[2, 1]
            sx = _np.floor(u / 256.0 * surface.width).astype(_np.int64)
            sy = _np.floor(v / 256.0 * surface.height).astype(_np.int64)
            sx = _np.clip(sx, 0, surface.width - 1)
            sy = _np.clip(sy, 0, surface.height - 1)
            texture = _np.frombuffer(surface.indices, dtype=_np.uint8).reshape(
                surface.height, surface.width)
            raw_source = texture[sy, sx]
            if surface.tracy_mode == "clear":
                zero_mask = raw_source == 0
                skipped = int(_np.count_nonzero(mask & zero_mask))
                if skipped:
                    counters["clear_source_zero_skipped"] += skipped
                    mask &= ~zero_mask
                if not bool(mask.any()):
                    return

        if piece.distance_fade:
            fade_vertices = _np.clip(
                (_np.asarray(distances, dtype=_np.float64)
                 - piece.fade_start) / piece.fade_length,
                0.0, 1.0)
            fade_value = (
                w0 * fade_vertices[0]
                + w1 * fade_vertices[1]
                + w2 * fade_vertices[2])
            fade_row = _np.floor(
                _np.clip(fade_value, 0.0, 1.0) * 255.0 + 0.5
            ).astype(_np.int16)
            base_row = (
                int(surface.shade_value)
                if surface.shade_mode != "none" else 0)
            shade_rows = _np.clip(base_row + fade_row, 0, 255)
            composed_source = shader[shade_rows, raw_source]
            fade_samples = int(_np.count_nonzero(mask))
            counters["distance_fade_triangles"] += 1
            counters["distance_fade_samples"] += fade_samples
            counters["distance_fade_saturated_samples"] += int(
                _np.count_nonzero(mask & (shade_rows == 255)))
        elif surface.shade_mode != "none":
            composed_source = shader[int(surface.shade_value), raw_source]
        else:
            composed_source = raw_source

        framebuffer_sub = framebuffer[top:bottom + 1, left:right + 1]
        coverage_sub = coverage[top:bottom + 1, left:right + 1]
        owner_sub = (
            owner[top:bottom + 1, left:right + 1]
            if owner is not None else None)
        opaque_order_sub = (
            opaque_order[top:bottom + 1, left:right + 1]
            if opaque_order is not None else None)

        if surface.tracy_mode != "none":
            occluded = opaque_order_sub > input_order
            count = int(_np.count_nonzero(mask & occluded))
            if count:
                counters["transparent_samples_occluded"] += count
                mask &= ~occluded
            if not bool(mask.any()):
                return

        if surface.tracy_mode == "flat":
            current = framebuffer_sub
            lookup_background = (
                current if forced is None
                else _np.full(current.shape, forced, dtype=_np.uint8))
            output = tracy[lookup_background, composed_source]
            samples = int(_np.count_nonzero(mask))
            counters["flat_tracy_samples"] += samples
            counters["flat_tracy_source_zero_samples"] += int(
                _np.count_nonzero(mask & (raw_source == 0)))
            changed = mask & (output != current)
            counters["flat_tracy_changed_samples"] += int(_np.count_nonzero(changed))
            framebuffer_sub[mask] = output[mask]
            coverage_sub[changed] = True
            if flat_face_bit is not None:
                seen_view[mask] |= flat_face_bit
            elif sparse_seen is not None:
                ys, xs = _np.nonzero(mask)
                ids = (top + ys).astype(_np.int64) * width + (left + xs)
                sparse_seen.update(int(value) for value in ids.tolist())
            return

        output = composed_source
        samples = int(_np.count_nonzero(mask))
        counters["opaque_or_clear_samples"] += samples
        framebuffer_sub[mask] = output[mask]
        coverage_sub[mask] = True
        if owner_sub is not None:
            owner_sub[mask] = piece.polygon_id
        if surface.tracy_mode == "none" and opaque_order_sub is not None:
            opaque_order_sub[mask] = input_order

    @staticmethod
    def _triangle_python(framebuffer, coverage, owner, opaque_order, face_seen,
                         piece: IndexedPiece, input_order: int,
                         screen, uvs, camera, distances, tables, counters,
                         forced,
                         width: int, height: int) -> None:
        left = max(0, math.floor(min(point[0] for point in screen)))
        right = min(width - 1, math.ceil(max(point[0] for point in screen)))
        top = max(0, math.floor(min(point[1] for point in screen)))
        bottom = min(height - 1, math.ceil(max(point[1] for point in screen)))
        if left > right or top > bottom:
            return
        surface = piece.surface
        if surface.map_mode == "depth":
            counters["depth_mapped_triangles"] += 1
        else:
            counters["linear_mapped_triangles"] += 1
        if piece.distance_fade:
            counters["distance_fade_triangles"] += 1
            fade_values = tuple(
                min(1.0, max(
                    0.0,
                    (distance - piece.fade_start) / piece.fade_length))
                for distance in distances)
        else:
            fade_values = ()

        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                weights = _barycentric(x + 0.5, y + 0.5, screen)
                if weights is None:
                    continue
                pixel_key = y * width + x
                if face_seen is not None and pixel_key in face_seen:
                    counters["flat_tracy_duplicate_samples_skipped"] += 1
                    continue

                if surface.kind == "solid":
                    raw_source = int(surface.solid_index)
                else:
                    mapped_weights = weights
                    if surface.map_mode == "depth":
                        corrected = [
                            weight / max(_MINIMUM_DEPTH, _CAMERA_DISTANCE - vertex[2])
                            for weight, vertex in zip(weights, camera)
                        ]
                        total = sum(corrected)
                        if total <= _EPS:
                            continue
                        mapped_weights = tuple(value / total for value in corrected)
                        counters["depth_mapped_samples"] += 1
                    else:
                        counters["linear_mapped_samples"] += 1
                    u = sum(weight * uv[0] for weight, uv in zip(mapped_weights, uvs))
                    v = sum(weight * uv[1] for weight, uv in zip(mapped_weights, uvs))
                    sx = _sample(u, surface.width)
                    sy = _sample(v, surface.height)
                    raw_source = surface.indices[sy * surface.width + sx]
                    # Retail clear-TRACY span skips numeric source zero only.
                    if surface.tracy_mode == "clear" and raw_source == 0:
                        counters["clear_source_zero_skipped"] += 1
                        continue

                if piece.distance_fade:
                    interpolated = sum(
                        weight * value
                        for weight, value in zip(weights, fade_values))
                    fade_row = int(math.floor(
                        min(1.0, max(0.0, interpolated)) * 255.0
                        + 0.5))
                    base_row = (
                        surface.shade_value
                        if surface.shade_mode != "none" else 0)
                    shade_row = min(255, base_row + fade_row)
                    composed_source = tables.shade_index(
                        shade_row, raw_source)
                    counters["distance_fade_samples"] += 1
                    if shade_row == 255:
                        counters["distance_fade_saturated_samples"] += 1
                elif surface.shade_mode != "none":
                    composed_source = tables.shade_index(
                        surface.shade_value, raw_source)
                else:
                    composed_source = raw_source

                if surface.tracy_mode != "none" and int(
                        IndexedRasterizer._get(opaque_order, y, x)) > input_order:
                    counters["transparent_samples_occluded"] += 1
                    continue

                if surface.tracy_mode == "flat":
                    background = int(IndexedRasterizer._get(framebuffer, y, x))
                    lookup_background = background if forced is None else forced
                    output = tables.tracy_index(
                        lookup_background, composed_source)
                    counters["flat_tracy_samples"] += 1
                    if raw_source == 0:
                        counters["flat_tracy_source_zero_samples"] += 1
                    if output != background:
                        counters["flat_tracy_changed_samples"] += 1
                        IndexedRasterizer._set(coverage, y, x, True)
                    IndexedRasterizer._set(framebuffer, y, x, output)
                    if face_seen is not None:
                        face_seen.add(pixel_key)
                    continue

                output = composed_source
                counters["opaque_or_clear_samples"] += 1
                IndexedRasterizer._set(framebuffer, y, x, output)
                IndexedRasterizer._set(coverage, y, x, True)
                if owner is not None:
                    IndexedRasterizer._set(owner, y, x, piece.polygon_id)
                if surface.tracy_mode == "none" and opaque_order is not None:
                    IndexedRasterizer._set(opaque_order, y, x, input_order)


# ----------------------------------------------------------------------------
# Adapter from OpenNeoUAStudio AssetFamily data to the optional indexed renderer.
# (formerly indexed_family_adapter.py)
# ----------------------------------------------------------------------------

_RETAIL_IGNORED_POLFLAG_BITS = 0x001 | 0x008 | 0x100


_RETAIL_NNN_CODE = 0x00


_RETAIL_LINEAR_CODES = frozenset((0x02, 0x32, 0x42, 0x72, 0x82))


_RETAIL_DEPTH_CODES = frozenset((0x06, 0x36, 0x46, 0x76))


_RETAIL_GRADIENT_CODES = frozenset((0x32, 0x36, 0x72, 0x76))


_RETAIL_CLEAR_CODES = frozenset((0x42, 0x46, 0x72, 0x76))


_RETAIL_FLAT_TRACY_CODE = 0x82


_RETAIL_MATERIAL_CODES = frozenset(
    (_RETAIL_NNN_CODE,) + tuple(_RETAIL_LINEAR_CODES) + tuple(_RETAIL_DEPTH_CODES)
)


_AMBIGUOUS_TEXTURE = object()


VANILLA_GAMEPLAY_VIS_LIMIT = 1400.0


VANILLA_FADE_LENGTH = 600.0


VANILLA_GAMEPLAY_FADE_START = (
    VANILLA_GAMEPLAY_VIS_LIMIT - VANILLA_FADE_LENGTH)


class IndexedFamilyAdapterError(RuntimeError):
    pass


class UnsupportedIndexedMaterialError(IndexedFamilyAdapterError):
    pass


class IndexedSourceError(IndexedFamilyAdapterError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _material_code(polflags: Any) -> int:
    return int(polflags) & ~_RETAIL_IGNORED_POLFLAG_BITS


def validate_retail_polygon_flags(polflags: int) -> int:
    """Return the proven retail dispatch code, or explain an unsupported material."""
    code = _material_code(polflags)
    if code not in _RETAIL_MATERIAL_CODES:
        raise UnsupportedIndexedMaterialError(
            f"unsupported retail polygon flags 0x{polflags:x} "
            f"(dispatch code 0x{code:x})")
    return code


def _constant_shade_row(raw_shade: int) -> int:
    return (253 * raw_shade + 0x180) >> 8


def _as_byte(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise UnsupportedIndexedMaterialError(
            f"{label} must be an integer in the range 0..255")
    try:
        decoded = operator.index(value)
    except TypeError as exc:
        raise UnsupportedIndexedMaterialError(
            f"{label} must be an integer in the range 0..255") from exc
    if not 0 <= decoded <= 255:
        raise UnsupportedIndexedMaterialError(
            f"{label} is outside the range 0..255: {decoded}")
    return decoded


def _candidate_set_roots(family) -> list[Path]:
    roots: list[Path] = []

    def add(value) -> None:
        if not value:
            return
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError, TypeError):
            return
        if path.is_file():
            path = path.parent
        candidates = [path]
        if path.name.casefold() in ("palette", "remap", "objects"):
            candidates.insert(0, path.parent)
        # Search only a few ancestors; never recurse through unrelated trees.
        candidates.extend(list(path.parents)[:4])
        for candidate in candidates:
            if candidate not in roots:
                roots.append(candidate)

    add(getattr(family, "external_palette_path", None))
    add(getattr(family, "setbas_path", None))
    add(getattr(family, "base_path", None))
    for root in getattr(family, "search_roots", ()):
        add(root)
    return roots


def _find_named(root: Path, folder: str, stems: tuple[str, ...]) -> Path | None:
    if not root.is_dir():
        return None
    try:
        directory = next(
            (entry for entry in root.iterdir()
             if entry.is_dir() and entry.name.casefold() == folder.casefold()),
            None,
        )
    except OSError:
        return None
    if directory is None:
        return None
    wanted = {name.casefold() for name in stems}
    try:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if entry.is_file() and entry.name.casefold() in wanted:
                return entry.resolve()
    except OSError:
        return None
    return None


def _find_profile(family) -> tuple[Path, Path, Path]:
    palette_path = getattr(family, "external_palette_path", None)
    palette_path = Path(palette_path).resolve() if palette_path else None
    palette_memory = getattr(family, "external_palette", None)
    if palette_memory is None:
        raise IndexedSourceError("no resolved 256-entry SET palette is available")

    canonical_refs = getattr(family, "indexed_profile_refs", {}) or {}
    if canonical_refs:
        shader_ref = canonical_refs.get("shader")
        tracy_ref = canonical_refs.get("tracy")
        failures = []
        for label, ref in (("SHADERMP", shader_ref),
                           ("TRACYRMP", tracy_ref)):
            if ref is None or ref.path is None or ref.status == "ambiguous":
                status = getattr(ref, "status", "missing")
                rule = getattr(ref, "resolution_rule", "unresolved")
                failures.append(f"{label}: {status} ({rule})")
        if failures:
            raise IndexedSourceError(
                "the palette-selected SET has no unique complete REMAP "
                "profile: " + "; ".join(failures))
        if palette_path is None or not palette_path.is_file():
            raise IndexedSourceError(
                "the canonical palette provenance has no source file")
        return palette_path, shader_ref.path.resolve(), tracy_ref.path.resolve()

    roots = _candidate_set_roots(family)
    if palette_path is not None and palette_path.parent.name.casefold() == "palette":
        preferred = palette_path.parent.parent
        shader = _find_named(
            preferred, "REMAP", ("SHADERMP.ILB", "SHADERMP.ILBM"))
        tracy = _find_named(
            preferred, "REMAP", ("TRACYRMP.ILB", "TRACYRMP.ILBM"))
        if shader is not None and tracy is not None:
            return palette_path, shader, tracy
        if shader is not None or tracy is not None:
            raise IndexedSourceError(
                "the SET selected by the external palette has an incomplete "
                "REMAP pair; refusing to mix remap tables from another root")
        roots = [preferred] + [root for root in roots if root != preferred]

    complete: list[tuple[Path, Path, Path]] = []
    for root in roots:
        shader = _find_named(root, "REMAP", ("SHADERMP.ILB", "SHADERMP.ILBM"))
        tracy = _find_named(root, "REMAP", ("TRACYRMP.ILB", "TRACYRMP.ILBM"))
        candidate_palette = palette_path
        if candidate_palette is None or not candidate_palette.is_file():
            candidate_palette = _find_named(
                root, "PALETTE", ("STANDARD.PAL", "NORMAL.PAL"))
        if candidate_palette is not None and shader is not None and tracy is not None:
            complete.append((candidate_palette.resolve(), shader, tracy))

    # Prefer the exact SET containing the already-selected palette.
    if palette_path is not None:
        matching = [profile for profile in complete if profile[0] == palette_path]
        if matching:
            complete = matching
    if not complete:
        raise IndexedSourceError(
            "matching PALETTE + REMAP/SHADERMP + REMAP/TRACYRMP were not found "
            "inside the bounded SET roots")

    # Multiple identical profiles are harmless; conflicting ones are not.
    unique: dict[tuple[str, str, str], tuple[Path, Path, Path]] = {}
    for profile in complete:
        hashes = tuple(_sha256(path) for path in profile)
        unique.setdefault(hashes, profile)
    if len(unique) > 1:
        raise IndexedSourceError(
            "palette/SET origin is ambiguous: multiple complete profiles have "
            "different palette or remap hashes")
    return next(iter(unique.values()))


class IndexedFamilyAdapter:
    """Resolve an AssetFamily into exact indexed surfaces and source metadata."""

    def __init__(self, family) -> None:
        self.family = family
        palette_path, shader_path, tracy_path = _find_profile(family)
        self.palette_path = palette_path
        self.shader_path = shader_path
        self.tracy_path = tracy_path

        palette = getattr(family, "external_palette", None)
        if palette is None or len(palette) != 256:
            raise IndexedSourceError("resolved external palette must contain 256 colors")
        shader = parse_ilbm_file(shader_path)
        tracy = parse_ilbm_file(tracy_path)
        self.tables = IndexedTables.from_decoded(palette, shader, tracy)
        self.source_hashes = {
            "palette": _sha256(palette_path),
            "shader": _sha256(shader_path),
            "tracy": _sha256(tracy_path),
        }
        self.source_info = {
            "palette": {
                "path": str(palette_path),
                "sha256": self.source_hashes["palette"],
                "provenance": getattr(
                    getattr(family, "external_palette_ref", None),
                    "provenance", {}),
            },
            "shader": {
                "path": str(shader_path),
                "sha256": self.source_hashes["shader"],
                "provenance": getattr(
                    getattr(family, "indexed_profile_refs", {}).get(
                        "shader"), "provenance", {}),
            },
            "tracy": {
                "path": str(tracy_path),
                "sha256": self.source_hashes["tracy"],
                "provenance": getattr(
                    getattr(family, "indexed_profile_refs", {}).get(
                        "tracy"), "provenance", {}),
            },
            "tracy_lookup_axis_order": "TRACYRMP[background][raw_source]",
            "shade_model": (
                "raw 0..2 bypass SHADERMP; 3..253 uses "
                "(253*raw+0x180)>>8; 254..255 becomes opaque index 0"),
        }

        self.block_map: dict[tuple[str, int], Any] = {}
        self.fade_profiles: dict[tuple[str, int], dict[str, float]] = {}
        unsupported = []
        depth_fade = []
        for obj in family.all_objects():
            owner = str(getattr(obj, "owner_path", "root"))
            for block_index, group in enumerate(getattr(obj, "materials", ())):
                block = getattr(group, "block", None)
                if block is None:
                    continue
                self.block_map[(owner, block_index)] = block
                code = _material_code(getattr(block, "polflags", 0))
                if code not in _RETAIL_MATERIAL_CODES:
                    unsupported.append({
                        "owner": owner,
                        "block": block_index,
                        "polflags": int(getattr(block, "polflags", 0)),
                        "dispatch_code": code,
                    })
                if bool(getattr(block, "depth_fade", False)):
                    depth_fade.append(f"{owner} block {block_index}")
                    self.fade_profiles[(owner, block_index)] = {
                        "vis_limit": VANILLA_GAMEPLAY_VIS_LIMIT,
                        "fade_start": VANILLA_GAMEPLAY_FADE_START,
                        "fade_length": VANILLA_FADE_LENGTH,
                    }
        self.source_info["unsupported_material_codes"] = unsupported
        self.source_info["depth_fade_blocks"] = depth_fade
        self.source_info["area_distance_fade_profiles"] = {
            f"{owner} block {block}": dict(profile)
            for (owner, block), profile in self.fade_profiles.items()
        }
        self._texture_lookup_revision: tuple[int, int] | None = None
        self._texture_exact: dict[str, Any] = {}
        self._texture_basename: dict[str, Any] = {}
        self._surface_cache: dict[tuple[Any, ...], IndexedSurface] = {}
        self._solid_surfaces = {
            "palette-index-0": IndexedSurface(
                "palette-index-0", "solid", None, 0, 0, 0,
                "none", 0, "none", "linear"),
            "palette-index-0-black-shade": IndexedSurface(
                "palette-index-0-black-shade", "solid", None, 0, 0, 0,
                "none", 0, "none", "linear"),
        }
        # Keep the adapter usable when unsupported blocks exist elsewhere in
        # the family. Exact rendering fails closed only if a visible polygon
        # actually resolves to one of those combinations.
        self.available = True
        self.availability_reason = ""

    @classmethod
    def try_create(cls, family):
        try:
            adapter = cls(family)
        except Exception as exc:
            # Textured rendering is exact/fail-closed. Non-textured inspection
            # modes remain available when its indexed resources are invalid.
            return None, str(exc)
        if not adapter.available:
            return None, adapter.availability_reason
        return adapter, ""

    def block_for(self, owner: str, block_index: int):
        return self.block_map.get((str(owner), int(block_index)))

    def distance_fade_profile(
            self, face, enabled: bool = False
            ) -> dict[str, float] | None:
        """Return the recovered retail gameplay AREA fade profile.

        The BASE/AREA data only marks eligible geometry with
        ``AREA_FLAG_DPTHFADE``.  The normal Urban Assault world runtime
        supplies visLimit=1400 and fadeLength=600, therefore fadeStart=800.
        The viewer exposes only an opt-in toggle; no editable preview profile
        is accepted here.
        """

        if not enabled:
            return None
        key = (str(getattr(face, "owner", "root")),
               int(getattr(face, "block_index", -1)))
        profile = self.fade_profiles.get(key)
        return dict(profile) if profile is not None else None

    def active_texture_name(self, material, frame_index: int) -> str:
        frames = list(getattr(material, "anim_frames", ()) or ())
        names = list(getattr(material, "anim_bitmap_names", ()) or ())
        if frames:
            frame_index = max(0, min(int(frame_index), len(frames) - 1))
            _duration, image_id, _uv_id = frames[frame_index]
            if 0 <= int(image_id) < len(names):
                return str(names[int(image_id)])
        return str(getattr(material, "label", ""))

    def active_uvs(self, material, frame_index: int):
        frames = list(getattr(material, "anim_frames", ()) or ())
        groups = list(getattr(material, "anim_uv_groups", ()) or ())
        if not frames:
            return ()
        frame_index = max(0, min(int(frame_index), len(frames) - 1))
        _duration, _image_id, uv_id = frames[frame_index]
        if 0 <= int(uv_id) < len(groups):
            return tuple(tuple(pair) for pair in groups[int(uv_id)])
        return ()

    def _texture(self, name: str):
        textures = getattr(self.family, "textures", {})
        revision = (id(textures), len(textures))
        if revision != self._texture_lookup_revision:
            exact: dict[str, Any] = {}
            basename: dict[str, Any] = {}
            for key, value in textures.items():
                normalized = str(key).replace("\\", "/").casefold()
                exact[normalized] = value
                base = Path(normalized).name
                previous = basename.get(base)
                if previous is None or previous is value:
                    basename[base] = value
                else:
                    basename[base] = _AMBIGUOUS_TEXTURE
            self._texture_exact = exact
            self._texture_basename = basename
            self._texture_lookup_revision = revision
            self._surface_cache.clear()
        wanted = str(name).replace("\\", "/").casefold()
        image = self._texture_exact.get(wanted)
        if image is None:
            image = self._texture_basename.get(Path(wanted).name)
            if image is _AMBIGUOUS_TEXTURE:
                image = None
        if image is None or getattr(image, "pixels", None) is None:
            raise UnsupportedIndexedMaterialError(
                f"raw indexed texture {name!r} is unavailable")
        return image

    def resolve_surface(self, face, material, frame_index: int = 0) -> IndexedSurface:
        owner = str(getattr(face, "owner", "root"))
        block_index = int(getattr(face, "block_index", -1))
        block = self.block_for(owner, block_index)
        if block is None:
            raise UnsupportedIndexedMaterialError(
                f"no exact material block for {owner} block {block_index}")
        if not bool(getattr(face, "mapped", True)):
            raise UnsupportedIndexedMaterialError(
                f"polygon {getattr(face, 'poly_id', -1)} has no ATTS mapping")

        polflags = int(getattr(block, "polflags", 0))
        code = validate_retail_polygon_flags(polflags)

        # NNN is the retail zero-span path; ATTS color is not consulted.
        if code == _RETAIL_NNN_CODE:
            return self._solid_surfaces["palette-index-0"]

        map_mode = "depth" if code in _RETAIL_DEPTH_CODES else "linear"
        tracy_mode = (
            "flat" if code == _RETAIL_FLAT_TRACY_CODE else
            "clear" if code in _RETAIL_CLEAR_CODES else "none")
        shade_mode = "gradient" if code in _RETAIL_GRADIENT_CODES else "none"
        shade_source = getattr(face, "shade", None)
        if shade_source is None:
            shade_source = getattr(block, "shade_val", 0)
        raw_shade = _as_byte(shade_source, "shade value")
        shade_value = 0
        if shade_mode == "gradient" and raw_shade <= 2:
            shade_mode = "none"
        elif shade_mode == "gradient" and raw_shade >= 254:
            return self._solid_surfaces["palette-index-0-black-shade"]
        elif shade_mode == "gradient":
            shade_value = _constant_shade_row(raw_shade)

        name = self.active_texture_name(material, frame_index)
        image = self._texture(name)
        pixels = getattr(image, "pixels", None)
        cache_key = (
            id(image), id(pixels), name, int(image.width), int(image.height),
            shade_mode, shade_value, tracy_mode, map_mode,
        )
        cached = self._surface_cache.get(cache_key)
        if cached is not None:
            return cached
        surface = IndexedSurface(
            name=name,
            kind="texture",
            indices=pixels,
            width=int(image.width),
            height=int(image.height),
            solid_index=None,
            shade_mode=shade_mode,
            shade_value=shade_value,
            tracy_mode=tracy_mode,
            map_mode=map_mode,
        )
        self._surface_cache[cache_key] = surface
        return surface


# ----------------------------------------------------------------------------
# Studio uses the Map Editor's indexed textures, remaps and GPU framebuffer.
# (formerly gpu_indexed_backend.py)
# ----------------------------------------------------------------------------

STUDIO_VERTEX = '''#version 330 core
layout(location=0) in vec3 position;
layout(location=1) in vec2 uv;
layout(location=2) in vec4 material;
layout(location=3) in vec3 flags;
layout(location=4) in vec4 heightRefs;
out vec2 textureUV;
out vec3 world;
flat out vec4 mat;
flat out vec3 mode;
void main(){
    textureUV=uv; world=position; mat=material; mode=flags;
    gl_Position=vec4(position*heightRefs.x,heightRefs.x);
}
'''


STUDIO_FRAGMENT = FRAGMENT.replace(
    '#ifndef FLAT_PASS\nlayout(location=1) out int cellOut;\nlayout(location=2) out vec4 edgesOut;\n#endif',
    'layout(location=1) out int cellOut;\n#ifndef FLAT_PASS\nlayout(location=2) out vec4 edgesOut;\n#endif')


STUDIO_FRAGMENT = STUDIO_FRAGMENT.replace(
    'indexOut=texelFetch(tracy,ivec2(int(source),int(background)),0).r;',
    'indexOut=texelFetch(tracy,ivec2(int(source),int(background)),0).r; '
    'if(indexOut==background) discard; cellOut=-1;')


def piece_geometry(pieces, width, height):
    """Screen-space GPU fans; clip W retains each material's UV interpolation."""
    materials, keys, vertices, ranges = [], {}, [], []
    for piece in pieces:
        surface = piece.surface
        key = (surface.kind, surface.indices, surface.width, surface.height)
        index = keys.get(key)
        if index is None:
            index = len(materials)
            keys[key] = index
            materials.append(surface)
        mode = {'none': 0, 'clear': 1, 'flat': 2}[surface.tracy_mode]
        shade = surface.shade_value if surface.shade_mode != 'none' else -1
        attrs = (index if surface.kind == 'texture' else -1, surface.width,
                 surface.height, shade, mode, surface.solid_index or 0, -1)
        uv = piece.uvs or ((0.,0.),)*len(piece.screen)
        data = []
        for (x,y), coordinates, camera in zip(piece.screen, uv, piece.camera_vertices):
            w = max(.2, 4-camera[2]) if surface.map_mode == 'depth' else 1.
            data.append((2*x/width-1, 1-2*y/height, 0, *coordinates, *attrs, w,w,w,w))
        start = len(vertices)
        vertices.extend(data[j] for i in range(2,len(data)) for j in (0,i-1,i))
        count = len(vertices)-start
        if ranges and ranges[-1][2] == mode and mode != 2:
            old = ranges.pop()
            ranges.append((old[0],old[1]+count,mode))
        else:
            ranges.append((start,count,mode))
    return np.asarray(vertices,np.float32).reshape(-1,STRIDE), materials, ranges


class StudioGpuRenderer(GpuRenderer):
    def __init__(self):
        programs = (_program(STUDIO_VERTEX,STUDIO_FRAGMENT),
                    _program(STUDIO_VERTEX,STUDIO_FRAGMENT.replace(
                        '#version 330 core','#version 330 core\n#define FLAT_PASS')),
                    _program(SCREEN_VERTEX,SCREEN_FRAGMENT.replace(
                        'if(code==0 && index==0u)', 'if(code==0)')))
        super().__init__(programs)
        self.owns_programs = True
        self._tables = None

    def render_pieces(self, pieces, tables, width, height, target, *, blend=True,
                      background_indices=None):
        if background_indices is not None and len(background_indices) != width * height:
            raise ValueError('Background indices must match render dimensions')
        if any(piece.distance_fade for piece in pieces):
            raise ValueError('Distance-fade diagnostics require the exact software rasterizer')
        data, materials, ranges = piece_geometry(pieces,width,height)
        # Texture storage is retained across camera frames and reused by material content.
        if self._tables is not tables:
            self._tables = tables
            self._studio_scene = SimpleNamespace(lib=SimpleNamespace(tables=tables),
                materials=materials,chunks={(0,0):Geometry(data,EMPTY,[])},instances={})
        scene = self._studio_scene
        scene.materials = materials
        scene.chunks[0,0] = Geometry(data,EMPTY,[])
        # Material numbering can change after source-face culling or VANM playback.
        signature = tuple((s.kind,s.indices,s.width,s.height) for s in materials)
        if signature != getattr(self,'_materials_signature',None):
            self.material_count = 0
            self._materials_signature = signature
        self.set_scene(scene,{(0,0)})
        self._begin(width,height)
        if background_indices is not None:
            # Seed TRACY with the scene behind the model; untouched pixels stay clear.
            backdrop = np.frombuffer(background_indices, np.uint8).reshape(height,width)
            self._bind(self.indices,0)
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D,0,0,0,width,height,
                              gl.GL_RED_INTEGER,gl.GL_UNSIGNED_BYTE,
                              np.ascontiguousarray(backdrop[::-1]))
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_CULL_FACE)
        for start,count,mode in ranges:
            program = self.flat_program if mode == 2 else self.opaque_program
            gl.glDrawBuffers(2 if mode == 2 else 3,
                [gl.GL_COLOR_ATTACHMENT0,gl.GL_COLOR_ATTACHMENT1,gl.GL_COLOR_ATTACHMENT2][:2 if mode == 2 else 3])
            self._geometry_program(program,np.eye(4,dtype=np.float32))
            if mode == 2:
                self._bind(self.backdrop,3)
                gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
                gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D,0,0,0,0,0,width,height)
            gl.glBindVertexArray(self.buffers[0,0].vao)
            gl.glDrawArrays(gl.GL_TRIANGLES,start,count)
            self.draw_calls += 1
        self._present(target,width,height,owner_colors={},overlays=False,sky=False,
                      transparent_background=True,blend=blend)
        return {'backend':'OpenGL 3.3 indexed','draw_calls':self.draw_calls,
                'triangles':len(data)//3,'renderer':self.name}
