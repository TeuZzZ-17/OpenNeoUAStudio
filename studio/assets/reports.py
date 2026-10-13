"""Read-only comparison reports: Source Diff and the multi-set asset census.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

from studio.assets.family import AssetFamily, load_asset_family
from studio.formats.animation import parse_anm_file, VanmData
from studio.formats.images import IlbmImage, parse_ilbm_file, parse_pal_file
from studio.formats.setbas import (
    decode_animation,
    decode_skeleton,
    decode_texture,
    read_setbas,
    SetBasArchive,
    SetBasError,
)
from studio.formats.sklt import parse_sklt_file, SkltModel

# ----------------------------------------------------------------------------
# Multi-set historical census: dev-CD asset roots vs vanilla SET.BAS archives.
# (formerly asset_census.py)
# ----------------------------------------------------------------------------

# --- texture provenance ----------------------------------------------------------


def _provenance_dirs(dev_base_root: Path, extra: list[str]) -> list[tuple[str, Path]]:
    """Candidate (label, dir) pairs: dev set root, its theme subfolders,
    then any manifest-provided extra dirs."""

    dirs: list[tuple[str, Path]] = []
    set_root = dev_base_root.parent  # OBJECTS -> YPA_SET_*
    if set_root.is_dir():
        dirs.append((set_root.name, set_root))
        for child in sorted(set_root.iterdir()):
            if child.is_dir() and child.name.startswith("_"):
                dirs.append((f"{set_root.name}/{child.name}", child))
    for extra_dir in extra:
        path = Path(extra_dir)
        if path.is_dir():
            label = f"{path.parent.name}/{path.name}"
            dirs.append((label, path))
    return dirs


def texture_provenance(archive: SetBasArchive, dev_base_root: Path,
                       extra_dirs: list[str], palette,
                       rgb: bool = True) -> list[dict]:
    """For every release VBMP/texture find the best-matching dev source."""

    results: list[dict] = []
    dirs = _provenance_dirs(dev_base_root, extra_dirs)

    for resource in archive.resources:
        if resource.class_id.lower() != "ilbm.class":
            continue
        record: dict = {
            "release_name": resource.resource_name,
            "source": None,
            "theme": None,
            "classification": "missing source",
            "rgb_diff_pct": None,
            "index_identical_candidates": 0,
            "notes": [],
        }
        try:
            embedded = decode_texture(archive, resource)
        except Exception as exc:
            record["classification"] = "decode failed"
            record["notes"].append(str(exc))
            results.append(record)
            continue

        best = None
        idx_identical = 0
        bare = resource.resource_name.replace("\\", "/").rsplit("/", 1)[-1]
        for label, directory in dirs:
            candidate = directory / bare
            if not candidate.is_file():
                continue
            try:
                loose = parse_ilbm_file(candidate)
            except Exception:
                continue
            entry = DiffEntry(name=bare, kind="texture")
            diff_textures(entry, loose, embedded, palette, rgb=rgb,
                          keep_rgba=False)
            is_idx_identical = entry.visual in (
                "visually identical", "palette/index remap only",
                "same artwork, palette recolor",
            ) or entry.status == "identical"
            if is_idx_identical:
                idx_identical += 1
            score = (0 if is_idx_identical else 1,
                     entry.metrics.get("rgb_diff_pct", 100.0)
                     if entry.metrics else 100.0)
            if best is None or score < best[0]:
                best = (score, label, str(candidate), entry)

        record["index_identical_candidates"] = idx_identical
        if best is not None:
            _score, label, path, entry = best
            record["source"] = path
            record["theme"] = label
            record["rgb_diff_pct"] = (entry.metrics.get("rgb_diff_pct")
                                      if entry.metrics else None)
            if entry.status == "identical":
                record["classification"] = "identical"
            elif entry.visual in ("visually identical",
                                  "palette/index remap only",
                                  "same artwork, palette recolor"):
                record["classification"] = "palette recolor"
            else:
                record["classification"] = "artwork changed"
            if idx_identical > 1:
                record["notes"].append(
                    f"{idx_identical} dev variants are index-identical; "
                    "same-name themes are ambiguous."
                )
        results.append(record)
    return results


# --- per-set census ----------------------------------------------------------------


def _palette_for_set(set_cfg: dict, warnings: list[str]):
    pal_path = set_cfg.get("palette")
    if pal_path:
        palette = parse_pal_file(pal_path)
        if palette:
            return palette
        warnings.append(f"manifest palette unreadable: {pal_path}")
    warnings.append(
        "no manifest palette: the palette found next to SET.BAS will be "
        "used and is NOT verified as vanilla (a modded/recolored palette "
        "skews the visual diff)."
    )
    return None


def census_one_set(set_cfg: dict, rgb: bool = True, limit: int = 0,
                   progress=None) -> dict:
    result: dict = {
        "id": set_cfg.get("id", "?"),
        "dev_base_root": set_cfg.get("dev_base_root"),
        "setbas": set_cfg.get("setbas"),
        "notes": set_cfg.get("notes", ""),
        "warnings": [],
        "files_scanned": 0,
        "errors": [],
        "status_totals": {},
        "visual_totals": {},
        "mapping_anomalies": [],
        "legacy_skeletons": {},
        "decode_failures": [],
        "missing_embedded": set(),
        "unknown_chunks": {},
        "provenance": [],
    }

    try:
        archive = read_setbas(set_cfg["setbas"])
    except (SetBasError, KeyError, OSError) as exc:
        result["errors"].append(f"SET.BAS unusable: {exc}")
        result["missing_embedded"] = []
        return result
    result["setbas_resources"] = len(archive.resources)
    result["setbas_census"] = archive.census()

    palette = _palette_for_set(set_cfg, result["warnings"])
    dev_root = Path(set_cfg["dev_base_root"])
    base_files = sorted(
        p for p in dev_root.rglob("*")
        if p.suffix.lower() in (".base", ".bas") and p.is_file()
    )
    if limit:
        base_files = base_files[:limit]

    texture_cache: dict = {}

    for base_file in base_files:
        if progress:
            progress(f"[{result['id']}] {base_file.name}")
        try:
            family = load_asset_family(base_file, setbas=archive)
            diff = diff_family(family, rgb=rgb, keep_rgba=False,
                               palette_override=palette,
                               texture_cache=texture_cache)
        except Exception as exc:
            result["errors"].append(f"{base_file.name}: {exc}")
            continue
        result["files_scanned"] += 1

        for status, count in diff.counts().items():
            result["status_totals"][status] = \
                result["status_totals"].get(status, 0) + count
        for visual, count in _visual_counts(diff).items():
            result["visual_totals"][visual] = \
                result["visual_totals"].get(visual, 0) + count

        for entry in diff.entries:
            if entry.kind == "mapping" and entry.status == "warning":
                anomaly = dict(entry.metrics or {})
                anomaly["base_file"] = base_file.name
                result["mapping_anomalies"].append(anomaly)
            elif entry.kind == "skeleton" \
                    and (entry.metrics or {}).get("legacy_v1"):
                key = entry.name
                if key not in result["legacy_skeletons"]:
                    metrics = dict(entry.metrics)
                    metrics["geometry_matches_release"] = \
                        entry.status == "identical"
                    result["legacy_skeletons"][key] = metrics
            if entry.status == "decode failed":
                result["decode_failures"].append(
                    f"{base_file.name}: {entry.kind} {entry.name}: "
                    f"{entry.summary}"
                )
            elif entry.status == "missing embedded":
                result["missing_embedded"].add(entry.name)

        if family.base_asset:
            for obj in family.base_asset.all_objects():
                for unknown in obj.unknown_chunks:
                    tag = unknown.split(" at ")[0]
                    result["unknown_chunks"][tag] = \
                        result["unknown_chunks"].get(tag, 0) + 1

    result["missing_embedded"] = sorted(result["missing_embedded"])
    result["provenance"] = texture_provenance(
        archive, dev_root, set_cfg.get("texture_dirs", []), palette, rgb
    )
    return result


def run_census(manifest: dict, rgb: bool = True, limit: int = 0,
               progress=None) -> dict:
    sets = [census_one_set(cfg, rgb, limit, progress)
            for cfg in manifest.get("sets", [])]
    global_totals: dict = {"files_scanned": 0, "status": {}, "visual": {},
                           "mapping_anomalies": 0, "legacy_skeletons": 0,
                           "decode_failures": 0, "errors": 0}
    for s in sets:
        global_totals["files_scanned"] += s.get("files_scanned", 0)
        for k, v in s.get("status_totals", {}).items():
            global_totals["status"][k] = global_totals["status"].get(k, 0) + v
        for k, v in s.get("visual_totals", {}).items():
            global_totals["visual"][k] = global_totals["visual"].get(k, 0) + v
        global_totals["mapping_anomalies"] += len(s.get("mapping_anomalies", []))
        global_totals["legacy_skeletons"] += len(s.get("legacy_skeletons", {}))
        global_totals["decode_failures"] += len(s.get("decode_failures", []))
        global_totals["errors"] += len(s.get("errors", []))
    return {
        "generator": "OpenNeoUA Studio asset_census V0.6",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "global": global_totals,
        "sets": sets,
    }


# --- report writers -----------------------------------------------------------------


def census_to_markdown(census: dict) -> str:
    lines: list[str] = []
    g = census["global"]
    lines.append("# Multi-set historical census: dev CD vs release SET.BAS")
    lines.append("")
    lines.append(f"Generated {census['generated_at']} by {census['generator']}. "
                 "Read-only: no asset was modified or extracted.")
    lines.append("")
    lines.append("## Global summary")
    lines.append("")
    lines.append(f"- asset families scanned: {g['files_scanned']}")
    lines.append("- resource comparisons: "
                 + (", ".join(f"{v} {k}" for k, v in sorted(g["status"].items()))
                    or "none"))
    lines.append("- texture visual: "
                 + (", ".join(f"{v} {k}" for k, v in sorted(g["visual"].items()))
                    or "none"))
    lines.append(f"- mapping anomalies: {g['mapping_anomalies']}")
    lines.append(f"- legacy v1 skeletons: {g['legacy_skeletons']}")
    lines.append(f"- decode failures: {g['decode_failures']}")
    lines.append(f"- scan errors: {g['errors']}")
    lines.append("")

    lines.append("## Mapping holes / ATTS coverage anomalies")
    lines.append("")
    lines.append("| set | base file | skeleton | POL2 | ATTS | unmapped | "
                 "invalid | duplicated |")
    lines.append("|---|---|---|---|---|---|---|---|")
    any_anomaly = False
    for s in census["sets"]:
        for a in s.get("mapping_anomalies", []):
            any_anomaly = True
            lines.append(
                f"| {s['id']} | {a.get('base_file')} | {a.get('skeleton')} "
                f"| {a.get('poly_count')} | {a.get('atts_total')} "
                f"| {a.get('unmapped') or '-'} | {a.get('invalid') or '-'} "
                f"| {a.get('duplicates') or '-'} |"
            )
    if not any_anomaly:
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.append("")

    for s in census["sets"]:
        lines.append(f"## {s['id']}")
        lines.append("")
        lines.append(f"- dev root: `{s.get('dev_base_root')}`")
        lines.append(f"- SET.BAS: `{s.get('setbas')}` "
                     f"({s.get('setbas_resources', '?')} resources "
                     f"{s.get('setbas_census', '')})")
        if s.get("notes"):
            lines.append(f"- notes: {s['notes']}")
        lines.append(f"- families scanned: {s.get('files_scanned', 0)}")
        lines.append("- comparisons: "
                     + (", ".join(f"{v} {k}" for k, v in
                                  sorted(s.get("status_totals", {}).items()))
                        or "none"))
        lines.append("- texture visual: "
                     + (", ".join(f"{v} {k}" for k, v in
                                  sorted(s.get("visual_totals", {}).items()))
                        or "none"))
        for warning in s.get("warnings", []):
            lines.append(f"- WARNING: {warning}")
        for error in s.get("errors", [])[:10]:
            lines.append(f"- ERROR: {error}")
        lines.append("")

        prov = s.get("provenance", [])
        if prov:
            lines.append(f"### {s['id']} texture provenance "
                         f"({len(prov)} release textures)")
            lines.append("")
            lines.append("| release texture | dev source (theme) | "
                         "classification | RGB diff % | notes |")
            lines.append("|---|---|---|---|---|")
            for p in prov:
                lines.append(
                    f"| {p['release_name']} | {p.get('theme') or 'NOT FOUND'} "
                    f"| {p['classification']} "
                    f"| {p.get('rgb_diff_pct') if p.get('rgb_diff_pct') is not None else '-'} "
                    f"| {'; '.join(p.get('notes', [])) or '-'} |"
                )
            lines.append("")

        legacy = s.get("legacy_skeletons", {})
        if legacy:
            lines.append(f"### {s['id']} legacy v1 skeletons ({len(legacy)})")
            lines.append("")
            lines.append("| skeleton | points | polygons | SEN | PLAN chunk | "
                         "matches release |")
            lines.append("|---|---|---|---|---|---|")
            for name, m in sorted(legacy.items()):
                lines.append(
                    f"| {name} | {m.get('points')} | {m.get('polygons')} "
                    f"| {m.get('sensors')} "
                    f"| {'yes' if m.get('plan_chunk') else 'no'} "
                    f"| {'yes' if m.get('geometry_matches_release') else 'NO'} |"
                )
            lines.append("")

        missing = s.get("missing_embedded", [])
        if missing:
            lines.append(f"### {s['id']} dev resources never shipped "
                         f"({len(missing)})")
            lines.append("")
            lines.append(", ".join(f"`{m}`" for m in missing[:60])
                         + (" ..." if len(missing) > 60 else ""))
            lines.append("")

        failures = s.get("decode_failures", [])
        if failures:
            lines.append(f"### {s['id']} decode failures ({len(failures)})")
            lines.append("")
            for failure in failures[:20]:
                lines.append(f"- {failure}")
            lines.append("")

        unknown = s.get("unknown_chunks", {})
        if unknown:
            lines.append(f"### {s['id']} unknown/unhandled chunks in .base")
            lines.append("")
            for tag, count in sorted(unknown.items()):
                lines.append(f"- {tag}: {count}")
            lines.append("")

    return "\n".join(lines)


def census_to_json(census: dict) -> str:
    return json.dumps(census, indent=2, default=str)


# ----------------------------------------------------------------------------
# Read-only Source Diff: loose/dev-CD asset family vs SET.BAS embedded copies.
# (formerly asset_diff.py)
# ----------------------------------------------------------------------------

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

    from studio.exchange.portable import AssetResolver
    from studio.formats.images import parse_pal_file

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
    from studio.assets.family import load_asset_family

    family = load_asset_family(base_file, extra_roots, setbas=setbas)
    return diff_family(family, rgb=rgb, keep_rgba=False,
                       palette_override=palette_override)
