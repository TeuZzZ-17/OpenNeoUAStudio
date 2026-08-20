"""Multi-set historical census: dev-CD asset roots vs vanilla SET.BAS archives.

Driven by a JSON manifest:

    {
      "sets": [
        {
          "id": "SET1",
          "dev_base_root": "...\\SET1_3_6\\YPA_SET_BORING\\OBJECTS",
          "setbas": "...\\UA_RC1\\DATA\\SET1\\OBJECTS\\SET.BAS",
          "palette": "...\\UA_RC1\\DATA\\SET1\\PALETTE\\STANDARD.PAL",
          "texture_dirs": ["...optional extra provenance dirs..."],
          "notes": "optional free text"
        }
      ]
    }

``palette`` is optional; without it the palette next to the SET.BAS is
auto-resolved and the report flags it as "not verified as vanilla".
``texture_dirs`` extends the provenance search (theme subfolders of the dev
set root are always scanned automatically).

Everything is read-only and in-memory; only the requested report files are
written.  Each SET.BAS is parsed once; texture pair diffs are cached per
set, so repeated references across hundreds of .base files are cheap.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from asset_diff import (
    DiffEntry,
    SourceDiff,
    _visual_counts,
    diff_family,
    diff_textures,
)
from asset_family import load_asset_family
from ilbm_parser import parse_ilbm_file, parse_pal_file
from setbas_reader import SetBasArchive, SetBasError, decode_texture, read_setbas


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
    lines.append(f"- resource comparisons: "
                 + (", ".join(f"{v} {k}" for k, v in sorted(g["status"].items()))
                    or "none"))
    lines.append(f"- texture visual: "
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
        lines.append(f"- comparisons: "
                     + (", ".join(f"{v} {k}" for k, v in
                                  sorted(s.get("status_totals", {}).items()))
                        or "none"))
        lines.append(f"- texture visual: "
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


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(
        description="Multi-set dev-CD vs SET.BAS census (read-only)."
    )
    cli.add_argument("manifest", help="census manifest JSON")
    cli.add_argument("--out", help="global Markdown report path")
    cli.add_argument("--json-out", help="global JSON report path")
    cli.add_argument("--per-set-dir",
                     help="write per-set Markdown details into this directory")
    cli.add_argument("--limit", type=int, default=0,
                     help="max .base files per set (0 = all)")
    cli.add_argument("--no-texture-rgb-diff", action="store_true")
    cli.add_argument("--quiet", action="store_true")
    args = cli.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    progress = None if args.quiet else (lambda msg: print(msg, flush=True))
    census = run_census(manifest, rgb=not args.no_texture_rgb_diff,
                        limit=args.limit, progress=progress)

    print("\n=== global ===")
    for key, value in census["global"].items():
        print(f"  {key}: {value}")
    for s in census["sets"]:
        print(f"  {s['id']}: {s.get('files_scanned', 0)} files, "
              f"anomalies {len(s.get('mapping_anomalies', []))}, "
              f"errors {len(s.get('errors', []))}")

    if args.out:
        Path(args.out).write_text(census_to_markdown(census), encoding="utf-8")
        print(f"wrote {args.out}")
    if args.json_out:
        Path(args.json_out).write_text(census_to_json(census), encoding="utf-8")
        print(f"wrote {args.json_out}")
    if args.per_set_dir:
        out_dir = Path(args.per_set_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in census["sets"]:
            single = {"generator": census["generator"],
                      "generated_at": census["generated_at"],
                      "global": census["global"], "sets": [s]}
            path = out_dir / f"census_{s['id']}.md"
            path.write_text(census_to_markdown(single), encoding="utf-8")
            print(f"wrote {path}")
