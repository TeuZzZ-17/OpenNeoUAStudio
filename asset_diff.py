"""Read-only Source Diff: loose/dev-CD asset family vs SET.BAS embedded copies.

Motivation: real dev-CD vs release mismatches exist (the dev-CD
ST_FLAK1.base maps 101 polygons while the release ST_FLAK1.sklt inside
SET.BAS has 102).  This module detects such differences systematically.

For every skeleton / texture / VANM referenced by a loaded asset family it
locates the loose (dev-CD) file and the embedded SET.BAS resource, decodes
both in memory, and compares them.  Nothing is written or extracted.

Statuses:
  identical          both sides decode and match exactly
  different          both sides decode; content differs but counts line up
  count mismatch     structural counts differ (vertices/polygons/frames...)
  missing loose      only the embedded SET.BAS copy exists
  missing embedded   only the loose file exists
  decode failed      one side exists but could not be decoded
  warning            mapping / consistency finding (kind "mapping")

RGB visual texture diff (V0.5): indexed pixels can differ merely because the
release VBMP remaps the same artwork onto the set palette.  Both sides are
therefore also normalised to RGBA preview pixels (own CMAP when present,
otherwise the set palette; chroma yellow RGB(255,255,0) transparent, same
rule as the engine) and compared visually.  Classification thresholds
(documented, preview-grade):

  RGB_IDENTICAL_MAX_PCT   = 0.5   max % of pixels allowed to differ
  RGB_IDENTICAL_MAX_DELTA = 8     max per-channel delta among those pixels

  - within both limits and indexed pixels equal   -> "visually identical"
  - within both limits but indexed pixels differ  -> "palette/index remap only"
  - beyond limits but indexed pixels equal        -> "same artwork, palette
                                                      recolor" (e.g. a mod's
                                                      recolored STANDARD.PAL)
  - beyond limits and indexed pixels differ       -> "artwork changed"
  - different dimensions                          -> "dimension mismatch"

Caveat: when BOTH the indices and the palette changed (release remap onto a
recolored palette), "artwork changed" may still be a palette-driven
difference; the side-by-side thumbnails are the final judge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from anm_parser import VanmData, parse_anm_file
from asset_family import AssetFamily
from ilbm_parser import IlbmImage, parse_ilbm_file
from setbas_reader import (
    SetBasArchive,
    decode_animation,
    decode_skeleton,
    decode_texture,
)
from sklt_parser import SkltModel, parse_sklt_file

CLASS_FOR_KIND = {
    "skeleton": "sklt.class",
    "texture": "ilbm.class",
    "animation": "bmpanim.class",
}


RGB_IDENTICAL_MAX_PCT = 0.5
RGB_IDENTICAL_MAX_DELTA = 8


@dataclass
class DiffEntry:
    name: str
    kind: str                 # "skeleton" | "texture" | "animation" | "mapping"
    loose: str = "-"          # loose side description (path or "-")
    embedded: str = "-"       # embedded side description
    status: str = ""
    summary: str = ""
    details: list[str] = field(default_factory=list)
    # V0.5 visual texture diff
    visual: str = ""          # visually identical | palette/index remap only |
                              # artwork changed | dimension mismatch |
                              # not compared
    metrics: dict = field(default_factory=dict)
    # RGBA8888 preview buffers for UI thumbnails (not serialised to JSON)
    loose_rgba: bytes | None = None
    embedded_rgba: bytes | None = None
    diff_rgba: bytes | None = None
    thumb_size: tuple[int, int] = (0, 0)

    @property
    def is_difference(self) -> bool:
        return self.status in ("different", "count mismatch")

    @property
    def is_missing(self) -> bool:
        return self.status in ("missing loose", "missing embedded")


@dataclass
class SourceDiff:
    base_path: Path | None = None
    setbas_path: Path | None = None
    entries: list[DiffEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for entry in self.entries:
            totals[entry.status] = totals.get(entry.status, 0) + 1
        return dict(sorted(totals.items()))

    def summary_line(self) -> str:
        counts = self.counts()
        return ", ".join(f"{v} {k}" for k, v in counts.items()) or "no entries"


def _bounds(points) -> tuple | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _fmt_bounds(bounds) -> str:
    if bounds is None:
        return "none"
    return (f"[{bounds[0]:.1f},{bounds[1]:.1f},{bounds[2]:.1f}] .. "
            f"[{bounds[3]:.1f},{bounds[4]:.1f},{bounds[5]:.1f}]")


def _histogram(polygons) -> dict[int, int]:
    hist: dict[int, int] = {}
    for polygon in polygons:
        hist[len(polygon)] = hist.get(len(polygon), 0) + 1
    return dict(sorted(hist.items()))


# --- skeleton diff --------------------------------------------------------------


def diff_skeletons(entry: DiffEntry, loose: SkltModel,
                   embedded: SkltModel) -> None:
    details = entry.details
    mismatch = False
    different = False

    pairs = [
        ("POO2 vertices", len(loose.points), len(embedded.points)),
        ("SEN2 points", len(loose.sensors), len(embedded.sensors)),
        ("POL2 polygons", loose.parsed_polygon_count,
         embedded.parsed_polygon_count),
    ]
    for label, a, b in pairs:
        if a != b:
            mismatch = True
            details.append(f"{label}: loose {a} vs SET.BAS {b}")

    hist_a = _histogram(loose.polygons)
    hist_b = _histogram(embedded.polygons)
    if hist_a != hist_b:
        mismatch = mismatch or (sum(hist_a.values()) != sum(hist_b.values()))
        different = True
        details.append(f"polygon size histogram: loose {hist_a} "
                       f"vs SET.BAS {hist_b}")

    bounds_a = _bounds(loose.points)
    bounds_b = _bounds(embedded.points)
    if bounds_a != bounds_b:
        different = True
        details.append(f"bounds: loose {_fmt_bounds(bounds_a)} vs "
                       f"SET.BAS {_fmt_bounds(bounds_b)}")

    if len(loose.points) == len(embedded.points):
        changed = 0
        max_delta = 0.0
        for a, b in zip(loose.points, embedded.points):
            delta = max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
            if delta > 1e-6:
                changed += 1
                max_delta = max(max_delta, delta)
        if changed:
            different = True
            details.append(f"{changed} vertex coordinate(s) differ "
                           f"(max delta {max_delta:.3f})")

    if loose.parsed_polygon_count == embedded.parsed_polygon_count:
        changed_polys = [
            i for i, (a, b) in enumerate(zip(loose.polygons,
                                             embedded.polygons))
            if list(a) != list(b)
        ]
        if changed_polys:
            different = True
            preview = ", ".join(str(i) for i in changed_polys[:8])
            more = ("..." if len(changed_polys) > 8 else "")
            details.append(f"{len(changed_polys)} polygon(s) have different "
                           f"vertex indices (first: {preview}{more})")
    else:
        extra = embedded.parsed_polygon_count - loose.parsed_polygon_count
        side = "SET.BAS" if extra > 0 else "loose"
        details.append(f"{abs(extra)} extra polygon(s) in the {side} version")
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(loose.polygons,
                                              embedded.polygons))
             if list(a) != list(b)),
            min(loose.parsed_polygon_count, embedded.parsed_polygon_count),
        )
        details.append(f"first differing polygon index: {first_diff}")

    sen_a = _bounds(loose.sensors)
    sen_b = _bounds(embedded.sensors)
    if sen_a != sen_b:
        different = True
        details.append(f"SEN2 bounds: loose {_fmt_bounds(sen_a)} vs "
                       f"SET.BAS {_fmt_bounds(sen_b)}")

    if mismatch:
        entry.status = "count mismatch"
        entry.summary = "; ".join(details[:2])
    elif different:
        entry.status = "different"
        entry.summary = "compatible counts, content differs"
    else:
        entry.status = "identical"
        entry.summary = (f"{len(loose.points)} verts, "
                         f"{loose.parsed_polygon_count} polys, "
                         f"{len(loose.sensors)} SEN2")


# --- texture diff ---------------------------------------------------------------


def _sha1(data) -> str:
    return hashlib.sha1(bytes(data)).hexdigest()[:12] if data else "none"


def _rgb_visual_diff(entry: DiffEntry, loose: IlbmImage, embedded: IlbmImage,
                     set_palette, keep_rgba: bool) -> None:
    """Normalise both sides to RGBA preview pixels and compare visually."""

    rgba_a = loose.to_rgba_bytes(
        set_palette if not loose.palette else None, "chroma"
    )
    rgba_b = embedded.to_rgba_bytes(
        set_palette if not embedded.palette else None, "chroma"
    )
    if rgba_a is None or rgba_b is None:
        entry.visual = "not compared"
        entry.details.append("RGB diff skipped: missing pixel data.")
        return
    if (loose.width, loose.height) != (embedded.width, embedded.height):
        entry.visual = "dimension mismatch"
        return

    total = loose.width * loose.height
    changed = 0
    delta_sum = 0
    delta_max = 0
    alpha_a = 0
    alpha_b = 0
    heat = bytearray(total * 4) if keep_rgba else None

    for i in range(total):
        base = i * 4
        if rgba_a[base + 3] == 0:
            alpha_a += 1
        if rgba_b[base + 3] == 0:
            alpha_b += 1
        delta = max(
            abs(rgba_a[base] - rgba_b[base]),
            abs(rgba_a[base + 1] - rgba_b[base + 1]),
            abs(rgba_a[base + 2] - rgba_b[base + 2]),
            abs(rgba_a[base + 3] - rgba_b[base + 3]),
        )
        if delta:
            changed += 1
            delta_sum += delta
            if delta > delta_max:
                delta_max = delta
        if heat is not None:
            level = min(255, delta * 3)
            heat[base] = level
            heat[base + 1] = level // 6
            heat[base + 2] = level // 6
            heat[base + 3] = 255

    pct = 100.0 * changed / max(1, total)
    entry.metrics = {
        "rgb_diff_pct": round(pct, 2),
        "rgb_avg_delta": round(delta_sum / max(1, changed), 2) if changed else 0.0,
        "rgb_max_delta": delta_max,
        "chroma_transparent_loose": alpha_a,
        "chroma_transparent_embedded": alpha_b,
    }

    indexed_equal = (loose.pixels is not None and embedded.pixels is not None
                     and _sha1(loose.pixels) == _sha1(embedded.pixels))
    visually_same = (pct <= RGB_IDENTICAL_MAX_PCT
                     and delta_max <= RGB_IDENTICAL_MAX_DELTA)
    if visually_same:
        entry.visual = ("visually identical" if indexed_equal
                        else "palette/index remap only")
    elif indexed_equal:
        # Same pixel indices, different rendered colors: the artwork is
        # untouched but the palette itself was recolored (typical of mods).
        entry.visual = "same artwork, palette recolor"
    else:
        entry.visual = "artwork changed"

    entry.details.append(
        f"RGB diff: {pct:.2f}% px differ, avg delta "
        f"{entry.metrics['rgb_avg_delta']}, max {delta_max} -> {entry.visual}"
    )
    if alpha_a != alpha_b:
        entry.details.append(
            f"chroma/alpha transparent px: loose {alpha_a} vs "
            f"SET.BAS {alpha_b}"
        )

    if keep_rgba:
        entry.loose_rgba = rgba_a
        entry.embedded_rgba = rgba_b
        entry.diff_rgba = bytes(heat)
        entry.thumb_size = (loose.width, loose.height)


def diff_textures(entry: DiffEntry, loose: IlbmImage,
                  embedded: IlbmImage, set_palette=None,
                  rgb: bool = True, keep_rgba: bool = True) -> None:
    details = entry.details
    mismatch = False
    different = False

    if (loose.width, loose.height) != (embedded.width, embedded.height):
        mismatch = True
        details.append(f"dimensions: loose {loose.width}x{loose.height} vs "
                       f"SET.BAS {embedded.width}x{embedded.height}")
    if loose.n_planes != embedded.n_planes:
        different = True
        details.append(f"planes: loose {loose.n_planes} vs "
                       f"SET.BAS {embedded.n_planes}")
    if loose.kind != embedded.kind:
        different = True
        details.append(f"container: loose {loose.kind} vs "
                       f"SET.BAS {embedded.kind}")

    pal_a = ("has CMAP" if loose.palette else "no CMAP")
    pal_b = ("has CMAP" if embedded.palette else "no CMAP")
    if pal_a != pal_b:
        different = True
        details.append(f"palette: loose {pal_a} vs SET.BAS {pal_b} "
                       "(embedded VBMPs rely on the set palette)")
    elif loose.palette and embedded.palette:
        hash_a = _sha1(bytes(v for c in loose.palette for v in c))
        hash_b = _sha1(bytes(v for c in embedded.palette for v in c))
        if hash_a != hash_b:
            different = True
            details.append(f"CMAP hash: loose {hash_a} vs SET.BAS {hash_b}")

    if loose.pixels is not None and embedded.pixels is not None:
        hash_a = _sha1(loose.pixels)
        hash_b = _sha1(embedded.pixels)
        if hash_a != hash_b:
            different = True
            changed = (sum(1 for a, b in zip(loose.pixels, embedded.pixels)
                           if a != b)
                       if len(loose.pixels) == len(embedded.pixels) else None)
            if changed is not None:
                pct = 100.0 * changed / max(1, len(loose.pixels))
                details.append(f"pixel data differs: {changed} px "
                               f"({pct:.1f}%) [hash {hash_a} vs {hash_b}]")
            else:
                details.append(f"pixel data differs [hash {hash_a} vs {hash_b}]")
        chroma_a = loose.chroma_transparent_count()
        chroma_b = embedded.chroma_transparent_count()
        if chroma_a != chroma_b:
            details.append(f"chroma-transparent px: loose {chroma_a} vs "
                           f"SET.BAS {chroma_b} (palette-dependent)")

    if rgb:
        _rgb_visual_diff(entry, loose, embedded, set_palette, keep_rgba)

    if mismatch:
        entry.status = "count mismatch"
        entry.summary = details[0]
    elif different:
        entry.status = "different"
        pixels_same = (loose.pixels is not None
                       and embedded.pixels is not None
                       and _sha1(loose.pixels) == _sha1(embedded.pixels))
        if entry.visual == "palette/index remap only":
            entry.summary = "palette/index remap only (visually identical)"
        elif entry.visual == "same artwork, palette recolor":
            entry.summary = (f"same artwork, palette recolor (RGB "
                             f"{entry.metrics.get('rgb_diff_pct', 0)}% differs)")
        elif entry.visual == "artwork changed":
            entry.summary = (f"artwork changed (RGB "
                             f"{entry.metrics.get('rgb_diff_pct', 0)}% differs)")
        elif pixels_same:
            entry.summary = "same pixels, different container/palette"
        else:
            entry.summary = "content differs"
    else:
        entry.status = "identical"
        entry.summary = f"{loose.width}x{loose.height}, pixels match"
        if not entry.visual:
            entry.visual = "visually identical"


# --- VANM diff ------------------------------------------------------------------


def diff_animations(entry: DiffEntry, loose: VanmData,
                    embedded: VanmData) -> None:
    details = entry.details
    mismatch = False
    different = False

    if loose.bitmap_names != embedded.bitmap_names:
        different = True
        details.append(f"bitmaps: loose {loose.bitmap_names} vs "
                       f"SET.BAS {embedded.bitmap_names}")
    if len(loose.texcoord_groups) != len(embedded.texcoord_groups):
        mismatch = True
        details.append(f"UV groups: loose {len(loose.texcoord_groups)} vs "
                       f"SET.BAS {len(embedded.texcoord_groups)}")
    else:
        for i, (a, b) in enumerate(zip(loose.texcoord_groups,
                                       embedded.texcoord_groups)):
            if a != b:
                different = True
                details.append(f"UV group {i}: loose {a} vs SET.BAS {b}")
    if len(loose.frames) != len(embedded.frames):
        mismatch = True
        details.append(f"frames: loose {len(loose.frames)} vs "
                       f"SET.BAS {len(embedded.frames)}")
    else:
        for i, (a, b) in enumerate(zip(loose.frames, embedded.frames)):
            fields_a = (a.frame_time, a.frame_id, a.texcoords_id)
            fields_b = (b.frame_time, b.frame_id, b.texcoords_id)
            if fields_a != fields_b:
                different = True
                details.append(f"frame {i}: loose (time={a.frame_time}, "
                               f"bmp={a.frame_id}, uv={a.texcoords_id}) vs "
                               f"SET.BAS (time={b.frame_time}, "
                               f"bmp={b.frame_id}, uv={b.texcoords_id})")

    if mismatch:
        entry.status = "count mismatch"
        entry.summary = details[0]
    elif different:
        entry.status = "different"
        entry.summary = details[0]
    else:
        entry.status = "identical"
        entry.summary = (f"{len(loose.frames)} frames, "
                         f"{len(loose.texcoord_groups)} UV groups")


# --- mapping diff (BASE ATTS/OLPL vs skeleton) -----------------------------------


def mapping_entries(family: AssetFamily) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    archive = family.setbas_archive

    for fam_obj in family.all_objects():
        base_obj = fam_obj.base_object
        skeleton = fam_obj.skeleton
        name = base_obj.skeleton_name or base_obj.name or "(object)"
        if not base_obj.ades:
            continue

        entry = DiffEntry(name=f"BASE mapping vs {name}", kind="mapping")
        details = entry.details

        atts_total = sum(len(b.atts) for b in base_obj.ades)
        olpl_total = sum(len(b.olpl) for b in base_obj.ades)
        details.append(f"ATTS entries: {atts_total}; OLPL groups: {olpl_total}")

        seen: dict[int, int] = {}
        invalid: list[int] = []
        poly_count = skeleton.parsed_polygon_count if skeleton else None
        block_stats = []
        for block in base_obj.ades:
            label = block.texture.name if block.texture else block.class_id
            ids = [e.poly_id for e in block.atts]
            if ids:
                details.append(
                    f"  {label}: {len(ids)} face(s), polyID "
                    f"{min(ids)}..{max(ids)}"
                )
                block_stats.append({"texture": label, "faces": len(ids),
                                    "min": min(ids), "max": max(ids)})
            for poly_id in ids:
                seen[poly_id] = seen.get(poly_id, 0) + 1
                if poly_count is not None and not (0 <= poly_id < poly_count):
                    invalid.append(poly_id)

        duplicates = {k: v for k, v in seen.items() if v > 1}
        unmapped: list[int] = []
        warn = False
        if invalid:
            warn = True
            details.append(f"invalid polyID references: {sorted(set(invalid))}")
        if duplicates:
            warn = True
            details.append(f"polygons mapped more than once: "
                           f"{dict(sorted(duplicates.items()))}")

        if poly_count is not None:
            unmapped = sorted(set(range(poly_count)) - set(seen))
            if unmapped:
                warn = True
                src = (fam_obj.skeleton_ref.source
                       if fam_obj.skeleton_ref else "?")
                details.append(
                    f"BASE mapping references {atts_total} polygons but "
                    f"selected skeleton ({src}) has {poly_count} polygons; "
                    f"unmapped polygon id(s): {unmapped[:16]}"
                    + ("..." if len(unmapped) > 16 else "")
                )

        entry.metrics = {
            "skeleton": name,
            "poly_count": poly_count,
            "atts_total": atts_total,
            "olpl_total": olpl_total,
            "unmapped": unmapped,
            "invalid": sorted(set(invalid)),
            "duplicates": dict(sorted(duplicates.items())),
            "blocks": block_stats,
        }

        # Compare against the *other* source's skeleton too: this is what
        # catches dev-CD BASE vs release SKLT drift (ST_FLAK1 101 vs 102).
        if archive is not None and base_obj.skeleton_name:
            matches = archive.find(base_obj.skeleton_name, "sklt.class")
            if matches:
                try:
                    other = decode_skeleton(archive, matches[0])
                    if poly_count is not None \
                            and other.parsed_polygon_count != poly_count:
                        warn = True
                        details.append(
                            f"skeleton POL2 differs by source: selected "
                            f"{poly_count} vs SET.BAS "
                            f"{other.parsed_polygon_count}"
                        )
                    if other.parsed_polygon_count != atts_total:
                        warn = True
                        details.append(
                            f"BASE mapping references {atts_total} polygons "
                            f"but SET.BAS skeleton has "
                            f"{other.parsed_polygon_count} polygons"
                        )
                except Exception as exc:
                    details.append(f"SET.BAS skeleton decode failed: {exc}")

        entry.status = "warning" if warn else "identical"
        entry.summary = ("mapping inconsistencies found" if warn
                         else f"all {atts_total} ATTS entries consistent")
        entries.append(entry)
    return entries


# --- family diff ----------------------------------------------------------------


def _loose_path(ref) -> Path | None:
    """Loose-side file for a resolved reference (even when embedded won)."""

    if ref is None:
        return None
    if ref.path is not None:
        return ref.path
    if ref.candidates:
        return ref.candidates[0]
    return None


def _embedded_resource(archive: SetBasArchive | None, name: str, kind: str):
    if archive is None:
        return None
    matches = archive.find(name, CLASS_FOR_KIND[kind])
    return matches[0] if matches else None


def _diff_one(archive: SetBasArchive, name: str, kind: str,
              loose_path: Path | None, embedded_res,
              set_palette=None, rgb: bool = True,
              keep_rgba: bool = True,
              texture_cache: dict | None = None) -> DiffEntry:
    entry = DiffEntry(name=name, kind=kind)
    entry.loose = str(loose_path) if loose_path else "-"
    entry.embedded = (f"SET.BAS:{embedded_res.resource_name}"
                      if embedded_res else "-")

    if loose_path is None and embedded_res is None:
        entry.status = "missing loose"
        entry.summary = "not found in either source"
        return entry
    if loose_path is None:
        entry.status = "missing loose"
        entry.summary = "only the embedded SET.BAS copy exists"
        return entry
    if embedded_res is None:
        entry.status = "missing embedded"
        entry.summary = "no embedded copy in SET.BAS"
        return entry

    try:
        if kind == "skeleton":
            loose_obj = parse_sklt_file(loose_path)
            embedded_obj = decode_skeleton(archive, embedded_res)
            diff_skeletons(entry, loose_obj, embedded_obj)
            chunk_names = {c.name for c in loose_obj.chunks}
            if "POOL" in chunk_names:
                entry.metrics = dict(entry.metrics or {})
                entry.metrics.update({
                    "legacy_v1": True,
                    "plan_chunk": "PLAN" in chunk_names,
                    "points": len(loose_obj.points),
                    "polygons": loose_obj.parsed_polygon_count,
                    "sensors": len(loose_obj.sensors),
                })
                entry.details.append(
                    "loose skeleton is legacy v1 (POOL/POLY/SENS"
                    + ("+PLAN" if "PLAN" in chunk_names else "") + ")"
                )
        elif kind == "texture":
            cache_key = (str(loose_path), embedded_res.index, rgb)
            cached = (texture_cache.get(cache_key)
                      if texture_cache is not None else None)
            if cached is not None:
                entry.status = cached.status
                entry.summary = cached.summary
                entry.visual = cached.visual
                entry.metrics = dict(cached.metrics)
                entry.details = list(cached.details)
                return entry
            loose_obj = parse_ilbm_file(loose_path)
            embedded_obj = decode_texture(archive, embedded_res)
            diff_textures(entry, loose_obj, embedded_obj, set_palette,
                          rgb, keep_rgba)
            if texture_cache is not None:
                texture_cache[cache_key] = entry
        else:
            loose_obj = parse_anm_file(loose_path)
            embedded_obj = decode_animation(archive, embedded_res)
            diff_animations(entry, loose_obj, embedded_obj)
    except Exception as exc:
        entry.status = "decode failed"
        entry.summary = str(exc)
    return entry


def _set_palette_for_diff(family: AssetFamily, diff: SourceDiff):
    """Palette for CMAP-less VBMPs: the family's external palette when it
    loaded one, otherwise the set palette next to the SET.BAS archive
    (Data/SetN/PALETTE/STANDARD.PAL etc.).  Without it the embedded side
    would fall back to grayscale and fake a 100% visual difference."""

    if family.external_palette is not None:
        return family.external_palette
    if family.setbas_path is None:
        return None

    from asset_resolver import AssetResolver
    from ilbm_parser import parse_pal_file

    resolver = AssetResolver([family.setbas_path.parent,
                              family.setbas_path.parent.parent])
    for pal_name in ("STANDARD.PAL", "NORMAL.PAL"):
        ref = resolver.resolve(pal_name, "palette")
        if ref.path is not None:
            palette = parse_pal_file(ref.path)
            if palette:
                diff.warnings.append(
                    f"Using set palette {ref.path} for embedded CMAP-less "
                    "textures in the visual diff."
                )
                return palette
    diff.warnings.append(
        "No set palette found near the SET.BAS archive; CMAP-less embedded "
        "textures fall back to grayscale and their RGB diff is unreliable."
    )
    return None


def diff_family(family: AssetFamily, rgb: bool = True,
                keep_rgba: bool = True,
                palette_override=None,
                texture_cache: dict | None = None) -> SourceDiff:
    """Compare every referenced resource of a loaded family against SET.BAS.

    ``rgb`` enables the visual texture diff; ``keep_rgba`` keeps the RGBA
    preview buffers on the entries for UI thumbnails (disable in batch runs
    to save memory).  ``palette_override`` forces a common palette (e.g. a
    vanilla STANDARD.PAL) for CMAP-less textures on both sides, removing
    palette-recolor noise from the visual diff.
    """

    diff = SourceDiff(base_path=family.base_path,
                      setbas_path=family.setbas_path)
    archive = family.setbas_archive
    if archive is None:
        diff.warnings.append(
            "No SET.BAS provider attached; open one with 'Open SET.BAS' "
            "to compare sources."
        )
        diff.entries.extend(mapping_entries(family))
        return diff

    if palette_override is not None:
        set_palette = palette_override
        diff.warnings.append(
            "Using a user-supplied common palette for CMAP-less textures "
            "(vanilla palette override)."
        )
    else:
        set_palette = _set_palette_for_diff(family, diff)

    for fam_obj in family.all_objects():
        name = fam_obj.base_object.skeleton_name
        if not name:
            continue
        diff.entries.append(_diff_one(
            archive, name, "skeleton",
            _loose_path(fam_obj.skeleton_ref),
            _embedded_resource(archive, name, "skeleton"),
        ))

    for name, ref in family.texture_refs.items():
        diff.entries.append(_diff_one(
            archive, name, "texture", _loose_path(ref),
            _embedded_resource(archive, name, "texture"),
            set_palette, rgb, keep_rgba, texture_cache,
        ))
    for name, ref in family.animation_refs.items():
        diff.entries.append(_diff_one(
            archive, name, "animation", _loose_path(ref),
            _embedded_resource(archive, name, "animation"),
        ))

    diff.entries.extend(mapping_entries(family))
    return diff


# --- serialization ----------------------------------------------------------------


def diff_to_dict(diff: SourceDiff) -> dict:
    return {
        "base_file": str(diff.base_path) if diff.base_path else None,
        "setbas": str(diff.setbas_path) if diff.setbas_path else None,
        "summary": diff.counts(),
        "warnings": list(diff.warnings),
        "visual_summary": _visual_counts(diff),
        "entries": [
            {
                "name": e.name,
                "kind": e.kind,
                "loose": e.loose,
                "embedded": e.embedded,
                "status": e.status,
                "summary": e.summary,
                "visual": e.visual or None,
                "metrics": dict(e.metrics) if e.metrics else None,
                "details": list(e.details),
            }
            for e in diff.entries
        ],
    }


def _visual_counts(diff: SourceDiff) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in diff.entries:
        if entry.kind == "texture" and entry.visual:
            counts[entry.visual] = counts.get(entry.visual, 0) + 1
    return dict(sorted(counts.items()))


def diff_to_markdown_lines(diff: SourceDiff) -> list[str]:
    lines: list[str] = []
    lines.append("## Source Diff (loose/dev-CD vs SET.BAS)")
    lines.append("")
    if diff.setbas_path:
        lines.append(f"- SET.BAS: `{diff.setbas_path}`")
    lines.append(f"- Summary: {diff.summary_line()}")
    visual_counts = _visual_counts(diff)
    if visual_counts:
        lines.append("- Texture visual summary: "
                     + ", ".join(f"{v} {k}" for k, v in visual_counts.items()))
        lines.append(f"- Visual thresholds: <= {RGB_IDENTICAL_MAX_PCT}% pixels "
                     f"and <= {RGB_IDENTICAL_MAX_DELTA} max channel delta "
                     "count as visually identical")
    for warning in diff.warnings:
        lines.append(f"- WARNING: {warning}")
    lines.append("")
    lines.append("| resource | kind | status | visual | summary |")
    lines.append("|---|---|---|---|---|")
    for entry in diff.entries:
        lines.append(f"| {entry.name} | {entry.kind} | {entry.status} "
                     f"| {entry.visual or '-'} | {entry.summary} |")
    lines.append("")
    detailed = [e for e in diff.entries if e.details
                and e.status != "identical"]
    if detailed:
        lines.append("### Differences in detail")
        lines.append("")
        for entry in detailed:
            lines.append(f"- **{entry.name}** ({entry.status})")
            lines.append(f"  - loose: `{entry.loose}`")
            lines.append(f"  - SET.BAS: `{entry.embedded}`")
            for detail in entry.details:
                lines.append(f"  - {detail}")
        lines.append("")
    return lines


# --- CLI ---------------------------------------------------------------------------


def _run_single(base_file: str, setbas: str,
                extra_roots: list[str], rgb: bool = True,
                palette_override=None) -> SourceDiff:
    from asset_family import load_asset_family

    family = load_asset_family(base_file, extra_roots, setbas=setbas)
    return diff_family(family, rgb=rgb, keep_rgba=False,
                       palette_override=palette_override)


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(
        description="Diff loose asset families against SET.BAS (read-only)."
    )
    cli.add_argument("base_file", nargs="?",
                     help="single .base file to diff")
    cli.add_argument("--setbas",
                     help="SET.BAS archive path (required except with "
                          "--census-manifest)")
    cli.add_argument("--base-root",
                     help="scan every .base/.bas under this folder instead")
    cli.add_argument("--root", action="append", default=[],
                     help="extra search root (repeatable)")
    cli.add_argument("--out", help="write a Markdown report here")
    cli.add_argument("--only-differences", action="store_true",
                     help="batch mode: print only files with differences")
    cli.add_argument("--limit", type=int, default=0,
                     help="batch mode: stop after N .base files (0 = all)")
    cli.add_argument("--no-texture-rgb-diff", action="store_true",
                     help="skip the RGB visual texture diff (faster batches)")
    cli.add_argument("--vanilla-pal",
                     help="palette-only ILBM (.PAL) used as common palette "
                          "for CMAP-less textures on both sides")
    cli.add_argument("--census-manifest",
                     help="multi-set census manifest JSON "
                          "(delegates to asset_census; --setbas not needed)")
    cli.add_argument("--json-out", help="census mode: global JSON report path")
    cli.add_argument("--per-set-dir",
                     help="census mode: per-set Markdown detail directory")
    args = cli.parse_args()
    rgb_enabled = not args.no_texture_rgb_diff

    if not args.census_manifest and not args.setbas:
        cli.error("--setbas is required (except with --census-manifest)")

    if args.census_manifest:
        import json

        from asset_census import census_to_json, census_to_markdown, run_census

        manifest = json.loads(Path(args.census_manifest)
                              .read_text(encoding="utf-8"))
        census = run_census(manifest, rgb=rgb_enabled, limit=args.limit,
                            progress=lambda m: print(m, flush=True))
        print("\n=== global ===")
        for key, value in census["global"].items():
            print(f"  {key}: {value}")
        if args.out:
            Path(args.out).write_text(census_to_markdown(census),
                                      encoding="utf-8")
            print(f"wrote {args.out}")
        if args.json_out:
            Path(args.json_out).write_text(census_to_json(census),
                                           encoding="utf-8")
            print(f"wrote {args.json_out}")
        if args.per_set_dir:
            out_dir = Path(args.per_set_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            for s in census["sets"]:
                single = {"generator": census["generator"],
                          "generated_at": census["generated_at"],
                          "global": census["global"], "sets": [s]}
                (out_dir / f"census_{s['id']}.md").write_text(
                    census_to_markdown(single), encoding="utf-8")
        raise SystemExit(0)

    palette_override = None
    if args.vanilla_pal:
        from ilbm_parser import parse_pal_file

        palette_override = parse_pal_file(args.vanilla_pal)
        if palette_override is None:
            cli.error(f"could not read palette from {args.vanilla_pal}")

    lines: list[str] = []

    if args.base_root:
        from asset_family import load_asset_family
        from setbas_reader import read_setbas

        archive = read_setbas(args.setbas)
        root = Path(args.base_root)
        base_files = sorted(
            p for p in root.rglob("*")
            if p.suffix.lower() in (".base", ".bas") and p.is_file()
        )
        if args.limit:
            base_files = base_files[:args.limit]
        print(f"scanning {len(base_files)} .base file(s) under {root}")
        lines.append(f"# Batch source diff: {root} vs {archive.path}")
        lines.append("")
        totals: dict[str, int] = {}
        totals_visual: dict[str, int] = {}
        for base_file in base_files:
            try:
                family = load_asset_family(base_file, args.root,
                                           setbas=archive)
                result = diff_family(family, rgb=rgb_enabled, keep_rgba=False,
                                     palette_override=palette_override)
            except Exception as exc:
                print(f"[ERROR] {base_file.name}: {exc}")
                lines.append(f"- **{base_file.name}**: ERROR {exc}")
                continue
            for status, count in result.counts().items():
                totals[status] = totals.get(status, 0) + count
            for visual, count in _visual_counts(result).items():
                totals_visual[visual] = totals_visual.get(visual, 0) + count
            has_diff = any(e.is_difference or e.status == "warning"
                           for e in result.entries)
            if has_diff or not args.only_differences:
                print(f"{base_file.name}: {result.summary_line()}")
            if has_diff:
                lines.append(f"## {base_file.relative_to(root)}")
                lines.append("")
                lines.extend(diff_to_markdown_lines(result)[2:])
        lines.insert(2, f"Totals: "
                        f"{', '.join(f'{v} {k}' for k, v in sorted(totals.items()))}")
        if totals_visual:
            lines.insert(3, f"Texture visual totals: "
                            f"{', '.join(f'{v} {k}' for k, v in sorted(totals_visual.items()))}")
        print("totals:", totals)
        print("visual:", totals_visual)
    else:
        if not args.base_file:
            cli.error("give a .base file or use --base-root")
        result = _run_single(args.base_file, args.setbas, args.root,
                             rgb_enabled, palette_override)
        lines.append(f"# Source diff: {args.base_file}")
        lines.append("")
        lines.extend(diff_to_markdown_lines(result))
        for entry in result.entries:
            print(f"[{entry.status:16}] {entry.kind:9} {entry.name}: "
                  f"{entry.summary}")
        for warning in result.warnings:
            print(f"WARNING: {warning}")

    if args.out:
        Path(args.out).write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
