"""Derived glTF preview policies; UA data stays in the canonical family.

TRACY preview is anchored to Studio framebuffer index 0 and measured against the full indexed lookup.
VANM playback uses standard STEP transform tracks, never skeletal animation.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from assembly_viewer import AssetViewport, ViewMaterial
from asset_family_package import AssetFamilyPackageError
from texture_convert import image_to_qimage


TICKS_PER_SECOND = 1024
# glTF has no infinite-cycle or scene timeline-range property. Supply repeated
# cycles covering Blender's default timeline, with a bounded common period.
PREVIEW_SECONDS = 60
MAX_COMMON_PERIOD_SECONDS = 120
MAX_TIMELINE_TRANSITIONS = 100_000
# Dark flat-TRACY FX can legitimately resolve to near-black against Studio's
# initial framebuffer, yet a pure global least-squares fit may collapse them
# toward invisibility. Keep a small minimum opacity for such non-identity
# columns so Blender still previews their silhouettes, without the fully opaque
# over-dark result of the previous background-0 bake.
MIN_DARK_TRACY_ALPHA = 0.18


def _linear(channel):
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(channel):
    c = min(1.0, max(0.0, channel))
    return round(255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055))


def _luminance(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def tracy_rgba_table(tables):
    """Project flat TRACY to a deterministic Blender-friendly RGBA table.

    UA flat TRACY is destination-dependent: TRACYRMP maps
    ``[background][source]`` to a new indexed framebuffer value, so no single
    ordinary glTF alpha-blended texture can reproduce it for every possible
    destination color.

    For the editable Blender preview, keep any true identity/background column
    transparent. For non-identity columns, start from the previous global
    least-squares source-over fit, but anchor the preview to Studio's exact
    background-0 result and keep a small visibility floor for very dark FX.
    This avoids both previous failure modes:
    - disappearing dark silhouettes/outlines from an over-transparent fit;
    - fully opaque, over-dark planes from a pure background-0 bake.

    Numeric source zero remains clear. The returned residual still measures the
    approximation over all 256 possible destination indices, so ONUA3D
    continues to report honestly how non-representable a particular TRACY
    source is in standard glTF.
    """
    palette = [tuple(_linear(c) for c in rgb) for rgb in tables.display_palette]
    mean_b = [sum(rgb[k] for rgb in palette) / 256 for k in range(3)]
    variance = sum((rgb[k] - mean_b[k]) ** 2 for rgb in palette for k in range(3))
    result, errors = [], []
    for source in range(256):
        outputs = [palette[tables.tracy_index(background, source)]
                   for background in range(256)]
        transparent_column = (
            source == 0 or
            all(tables.tracy_index(background, source) == background
                for background in range(256))
        )
        if transparent_column:
            rgb = (0, 0, 0)
            alpha = 0
            encoded = (0.0, 0.0, 0.0)
        else:
            mean_o = [sum(rgb[k] for rgb in outputs) / 256 for k in range(3)]
            covariance = sum((background[k] - mean_b[k]) * (output[k] - mean_o[k])
                             for background, output in zip(palette, outputs)
                             for k in range(3))
            alpha_fit = 1.0 - (min(1.0, max(0.0, covariance / variance)) if variance else 0.0)
            anchor = outputs[0]
            alpha_floor = MIN_DARK_TRACY_ALPHA * max(0.0, 1.0 - _luminance(anchor))
            alpha_linear_min = max(anchor)
            alpha_value = min(1.0, max(alpha_fit, alpha_floor, alpha_linear_min))
            rgb = tuple(_srgb(channel / alpha_value) for channel in anchor)
            alpha = round(255 * alpha_value)
            encoded = tuple(_linear(c) for c in rgb)
        result.append((*rgb, alpha))
        a = alpha / 255.0
        errors.append(math.sqrt(sum(
            (a * encoded[k] + (1.0 - a) * background[k]
             - outputs[background_index][k]) ** 2
            for background_index, background in enumerate(palette)
            for k in range(3)) / 768))
    return result, errors


def surface_image(image, surface, tables, tracy_projection=None):
    """Reuse indexed decoding, replacing only the derived PNG color table."""
    rgba, residuals = [], []
    for raw in range(256):
        source = (tables.shade_index(surface.shade_value, raw)
                  if surface.shade_mode != "none" else raw)
        if surface.tracy_mode == "flat":
            colors, errors = tracy_projection
            color = colors[source]
            residuals.append(errors[source])
        else:
            color = (*tables.display_palette[source],
                     0 if surface.tracy_mode == "clear" and raw == 0 else 255)
        rgba.append(color)
    qimage = image_to_qimage(image, tables.display_palette)
    qimage.setColorTable([(a << 24) | (r << 16) | (g << 8) | b for r, g, b, a in rgba])
    used = sorted(set(image.pixels))
    metadata = {
        "tracy": surface.tracy_mode, "shade_mode": surface.shade_mode,
        "shade_row": surface.shade_value, "map_mode": surface.map_mode,
        "alpha": "TRACYRMP anchored source-over fit with dark-FX visibility floor" if residuals else
                 "numeric source zero" if surface.tracy_mode == "clear" else "opaque",
    }
    if residuals:
        metadata["linear_rgb_rmse_max_used_index"] = max(residuals[i] for i in used)
        metadata["linear_rgb_rmse_mean_used_index"] = sum(residuals[i] for i in used) / len(used)
    return qimage, metadata


@dataclass
class VanmTimeline:
    # (start tick, frame index) for one exact cycle or a terminal prefix.
    starts: list[tuple[int, int]]
    period: int | None

    def transitions(self, duration):
        if self.period is None:
            return [(t, frame) for t, frame in self.starts if t <= duration]
        rows = []
        for offset in range(0, duration + 1, self.period):
            rows.extend((offset + t, frame) for t, frame in self.starts if offset + t <= duration)
            if len(rows) > MAX_TIMELINE_TRANSITIONS:
                raise AssetFamilyPackageError("VANM preview exceeds the documented timeline transition limit")
        return rows


def vanm_timeline(animation, anim_type=0):
    if not animation.frames:
        raise AssetFamilyPackageError("VANM has no frames")
    material = ViewMaterial(label=animation.source_name, anim_type=anim_type,
                            anim_frames=[(f.frame_time, f.frame_id, f.texcoords_id)
                                         for f in animation.frames])
    state, tick, starts, seen = (0, 1), 0, [], set()
    while state not in seen:
        seen.add(state)
        frame = animation.frames[state[0]]
        if not (0 <= frame.frame_id < len(animation.bitmap_names) and
                0 <= frame.texcoords_id < len(animation.texcoord_groups)):
            raise AssetFamilyPackageError(f"VANM frame {state[0]} has invalid bitmap/UV IDs")
        starts.append((tick, state[0]))
        if frame.frame_time <= 0:
            # Same halt semantics as Studio's _advance_animation; no invented fps.
            return VanmTimeline(starts, None)
        tick += frame.frame_time
        # The existing viewer owns loop/ping-pong semantics, including held ends.
        state = AssetViewport._next_frame(None, material, *state)
    return VanmTimeline(starts, tick)


def preview_duration(timelines):
    period = 1
    for timeline in timelines:
        if timeline.period:
            period = math.lcm(period, timeline.period)
    minimum = PREVIEW_SECONDS * TICKS_PER_SECOND
    if period <= MAX_COMMON_PERIOD_SECONDS * TICKS_PER_SECOND:
        return max(period, math.ceil(minimum / period) * period), True
    return MAX_COMMON_PERIOD_SECONDS * TICKS_PER_SECOND, False
