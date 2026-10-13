"""Developer command line tools of the Studio libraries.

Each library used to carry its own ``if __name__ == "__main__"`` block. They live here now,
so the library modules stay import-only.

    python tools/dev_cli.py <tool> [arguments of that tool]
    python tools/dev_cli.py --help
"""

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

# Run from the repository root no matter where the script is started.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.assets.dependencies import DependencyProfile
from studio.assets.family import load_asset_family
from studio.assets.mapping import (
    eligible_blocks,
    MappingIndex,
    plan_copy_style,
    plan_planar,
    save_repaired_base,
)
from studio.assets.reports import (
    _run_single,
    _visual_counts,
    census_to_json,
    census_to_markdown,
    diff_family,
    diff_to_markdown_lines,
    run_census,
)
from studio.exchange.portable import (
    AssetResolver,
    TYPE_HINTS,
)
from studio.formats.animation import (
    parse_anm_file,
)
from studio.formats.base_format import (
    build_parser,
    ExportError,
    parse_base_file,
    print_summary,
    write_outputs,
)
from studio.formats.images import (
    _iter_inputs,
    convert_png_to_ilbm,
    convert_to_png,
    convert_vbmp_to_ilbm,
    parse_ilbm_file,
    read_iff_file,
    TextureConvertError,
)
from studio.formats.setbas import read_setbas, SetBasError
from studio.assets.setbas_export import DEFAULT_CLASS, export_runtime_loose, extract_archive, SetBasExportError


def run_iff_reader():
    import argparse
    import json

    cli = argparse.ArgumentParser(description="Dump the IFF chunk tree of a file.")
    cli.add_argument("file", help="asset file to inspect (read-only)")
    cli.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = cli.parse_args()

    parsed = read_iff_file(args.file)
    if args.json:
        print(json.dumps(parsed.to_dict(), indent=2))
    else:
        print(parsed.dump_text())
        for warning in parsed.warnings:
            print(f"WARNING: {warning}")


def run_asset_diff():
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
        from studio.formats.images import parse_pal_file

        palette_override = parse_pal_file(args.vanilla_pal)
        if palette_override is None:
            cli.error(f"could not read palette from {args.vanilla_pal}")

    lines: list[str] = []

    if args.base_root:
        from studio.assets.family import load_asset_family
        from studio.formats.setbas import read_setbas

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


def run_dependency_profile():
    profile = DependencyProfile()
    print(f"profile: {profile.path}")
    print(f"auto_apply: {profile.auto_apply}")
    print(f"choices: {len(profile)}")
    for choice in profile._choices.values():
        marker = " [STALE]" if choice.stale else ""
        print(f"  {choice.root_base} / {choice.owner_node} / {choice.kind} "
              f"/ {choice.raw_ref} -> {choice.chosen_path}{marker}")


def run_setbas_reader():
    import argparse

    cli = argparse.ArgumentParser(
        description="Inspect a SET.BAS archive (strictly read-only)."
    )
    cli.add_argument("setbas")
    cli.add_argument("--find", help="look up a logical resource name")
    cli.add_argument("--list", action="store_true", help="list all resources")
    args = cli.parse_args()

    parsed = read_setbas(args.setbas)
    print(f"{parsed.path}: {len(parsed.resources)} EMRS resources")
    print("classes:", parsed.census())
    print("payloads:", parsed.payload_census())
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")

    if args.find:
        for resource in parsed.find(args.find):
            print(f"  {resource.class_id} {resource.resource_name} "
                  f"({resource.display_payload}, {resource.payload_size} bytes "
                  f"@ 0x{resource.payload_offset:X}, {resource.payload_source})")
    if args.list:
        for resource in parsed.resources:
            print(f"  [{resource.index:4}] {resource.class_id:14} "
                  f"{resource.resource_name:32} {resource.display_payload:5} "
                  f"{resource.payload_size:8} bytes  {resource.error}")


def main_texture_convert(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenNeoUA Studio texture conversions (merged from BASet).")
    sub = parser.add_subparsers(dest="command", required=True)

    to_png = sub.add_parser("to-png", help="ILBM/VBMP -> indexed PNG")
    to_png.add_argument("inputs", nargs="+", help="files or folders")
    to_png.add_argument("--out", help="output PNG (single input file only)")
    to_png.add_argument("--out-dir", help="output folder")
    to_png.add_argument("--pal", help=".PAL palette for VBMP inputs")
    to_png.add_argument("--palette-ilbm", help="reference ILBM with CMAP")

    p2i = sub.add_parser("png-to-ilbm", help="edited PNG -> ILBM (template)")
    p2i.add_argument("png", help="edited PNG file")
    p2i.add_argument("--template", required=True, help="template ILBM")
    p2i.add_argument("--out", required=True, help="output ILBM path")

    v2i = sub.add_parser("vbmp-to-ilbm", help="raw VBMP -> standalone ILBM")
    v2i.add_argument("inputs", nargs="+", help="files or folders")
    v2i.add_argument("--out", help="output ILBM (single input file only)")
    v2i.add_argument("--out-dir", help="output folder")
    v2i.add_argument("--pal", help=".PAL palette file")
    v2i.add_argument("--palette-ilbm", help="reference ILBM with CMAP")

    args = parser.parse_args(argv)
    try:
        if args.command == "png-to-ilbm":
            result = convert_png_to_ilbm(args.png, args.template, args.out)
            note = f" [{result.warning}]" if result.warning else ""
            print(f"[OK] {result.source} -> {result.output} "
                  f"({result.width}x{result.height}){note}")
            return 0

        suffixes = ((".ilbm", ".ilb", ".iff", ".lbm", ".vbmp")
                    if args.command == "to-png" else (".vbmp", ".ilbm", ".ilb"))
        files = _iter_inputs(args.inputs, suffixes)
        if not files:
            print("no input files found", file=sys.stderr)
            return 1
        if args.out and len(files) != 1:
            parser.error("--out needs exactly one input file; use --out-dir")
        if not args.out and not args.out_dir:
            parser.error("--out or --out-dir is required")

        converter = (convert_to_png if args.command == "to-png"
                     else convert_vbmp_to_ilbm)
        new_suffix = ".png" if args.command == "to-png" else ".ILBM"
        converted = 0
        errors = 0
        for path in files:
            out = (Path(args.out) if args.out
                   else Path(args.out_dir) / (path.stem + new_suffix))
            try:
                result = converter(path, out, args.pal, args.palette_ilbm)
            except TextureConvertError as exc:
                errors += 1
                print(f"[ERROR] {path.name}: {exc}")
                continue
            converted += 1
            note = f" [{result.warning}]" if result.warning else ""
            print(f"[OK] {path} -> {result.output} "
                  f"({result.width}x{result.height}){note}")
        print(f"\nSummary: {converted} converted, {errors} error(s)")
        return 0 if converted and not errors else 1
    except TextureConvertError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


def run_texture_convert():
    raise SystemExit(main_texture_convert())


def run_asset_census():
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


def run_base_mapping_editor():
    import argparse

    from studio.assets.family import load_asset_family

    cli = argparse.ArgumentParser(
        description="Inspect/repair BASE ATTS coverage holes (writes only "
                    "to a new file via --repair/--out)."
    )
    cli.add_argument("base_file")
    cli.add_argument("--repair", type=int, metavar="POLYID",
                     help="polygon to repair")
    cli.add_argument("--block", type=int, default=0,
                     help="target material block index (planar method)")
    cli.add_argument("--copy-from", type=int, metavar="POLYID",
                     help="copy mapping style from this polygon instead")
    cli.add_argument("--out", help="output .base path (never the original)")
    cli.add_argument("--deps", action="store_true",
                     help="print the dependency report and exit")
    args = cli.parse_args()

    family = load_asset_family(args.base_file)
    if args.deps:
        from studio.assets.dependencies import print_report

        print_report(family)
        raise SystemExit(0)
    fam_obj = next((o for o in family.all_objects() if o.skeleton), None)
    if fam_obj is None:
        raise SystemExit("no skeleton-bearing object in this family")
    mapping = MappingIndex(fam_obj)
    print(f"{args.base_file}: {mapping.poly_count} polygons, "
          f"unmapped={mapping.unmapped}, duplicates={mapping.duplicates}, "
          f"invalid={mapping.invalid}")
    for index, block in eligible_blocks(fam_obj):
        tex = block.texture.name if block.texture else "-"
        print(f"  block #{index}: {tex} ({len(block.atts)} entries)")

    if args.repair is not None:
        if mapping.status(args.repair) != "unmapped":
            raise SystemExit(f"polygon #{args.repair} is not unmapped "
                             f"({mapping.status(args.repair)})")
        if args.copy_from is not None:
            plan = plan_copy_style(fam_obj, args.repair, args.copy_from,
                                   mapping)
        else:
            plan = plan_planar(fam_obj, args.repair, args.block, mapping)
        for line in plan.describe():
            print(line)
        if args.out:
            for note in save_repaired_base(family, fam_obj, [plan], args.out):
                print(note)


def run_ilbm_parser():
    import argparse

    cli = argparse.ArgumentParser(description="Inspect an ILBM/VBMP file (read-only).")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_ilbm_file(args.file)
    print(f"{parsed.source_name}: {parsed.kind} {parsed.width}x{parsed.height} "
          f"planes={parsed.n_planes} compression={parsed.compression} "
          f"palette={'yes' if parsed.palette else 'no'} "
          f"body={'decoded' if parsed.has_body else 'absent'}")
    if parsed.pixels:
        used = sorted(set(parsed.pixels))
        print(f"indices used: {len(used)} (min {used[0]}, max {used[-1]})")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")


def run_asset_family():
    import argparse

    cli = argparse.ArgumentParser(
        description="Assemble and check an asset family from a .base file."
    )
    cli.add_argument("base_file")
    cli.add_argument("--root", action="append", default=[],
                     help="extra search root (repeatable)")
    cli.add_argument("--deps", action="store_true",
                     help="print the full dependency report")
    args = cli.parse_args()

    fam = load_asset_family(args.base_file, args.root)
    if args.deps:
        from studio.assets.dependencies import print_report

        print_report(fam)
        raise SystemExit(0)
    print(f"== {fam.base_path} ==")
    for obj in fam.all_objects():
        skl = obj.base_object.skeleton_name
        print(f"object: skeleton={skl!r} "
              f"resolved={obj.skeleton_ref.path if obj.skeleton_ref else None}")
        for group in obj.materials:
            print(f"  material {group.label}: {len(group.faces)} faces "
                  f"[{group.confidence}]")
    print("\nchecks:")
    for status, text in fam.checks:
        print(f"  [{status}] {text}")
    if fam.warnings:
        print("\nwarnings:")
        for warning in fam.warnings:
            print(f"  - {warning}")


def run_base_parser():
    import argparse

    cli = argparse.ArgumentParser(description="Inspect a .base file (read-only).")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_base_file(args.file)
    print(f"== {parsed.source_path} ==")
    if parsed.tree:
        print(parsed.tree.dump_text())
    for obj in parsed.all_objects():
        print(f"\nObject name={obj.name!r} skeleton={obj.skeleton_name!r}")
        if obj.transform:
            t = obj.transform
            print(f"  pos={t.position} scale={t.scale} euler={t.euler} "
                  f"visLimit={t.vis_limit} ambient={t.ambient_light}")
        for i, block in enumerate(obj.ades):
            tex = block.texture.name if block.texture else "<none>"
            print(f"  ADES[{i}] {block.class_id}: texture={tex} "
                  f"ATTS={len(block.atts)} OLPL={len(block.olpl)} "
                  f"[{block.describe_polflags()}]")
        for res in obj.embedded:
            print(f"  EMRS {res.class_id} {res.resource_name} "
                  f"({res.payload_tag} {res.payload_form_type})")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")


def main_base_kids(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setbas = Path(args.setbas).resolve()
    out_dir = Path(args.out).resolve()

    if not setbas.is_file():
        print(f"error: SET.BAS not found: {setbas}", file=sys.stderr)
        return 2

    try:
        summary = write_outputs(setbas, out_dir)
    except (ExportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(summary)
    return 0


def run_base_kids_export():
    raise SystemExit(main_base_kids())


def run_anm_parser():
    import argparse

    cli = argparse.ArgumentParser(description="Inspect a VANM/ANM file.")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_anm_file(args.file)
    print(f"{parsed.source_name}: {parsed.summary()}")
    for i, frame in enumerate(parsed.frames):
        print(f"  frame {i}: bitmap#{frame.frame_id} uv#{frame.texcoords_id} "
              f"{frame.frame_time} ticks (~{frame.duration_ms:.1f} ms)")
    for i, group in enumerate(parsed.texcoord_groups):
        print(f"  uv group {i}: {group}")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")


def run_asset_resolver():
    import argparse

    cli = argparse.ArgumentParser(description="Resolve an asset reference.")
    cli.add_argument("root", help="asset root directory (or a file inside it)")
    cli.add_argument("name", help="logical resource name, e.g. Skeleton/BP_FLAK1.sklt")
    cli.add_argument("--kind", default="any",
                     choices=sorted(TYPE_HINTS.keys()))
    args = cli.parse_args()

    resolver = AssetResolver([args.root])
    res = resolver.resolve(args.name, args.kind)
    print(f"{res.logical_name}: {res.status}")
    if res.path:
        print(f"  -> {res.path}")
    for candidate in res.candidates:
        print(f"  candidate: {candidate}")


def main_setbas_export(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract inspection data or a runtime-ready Loose tree "
                    "from SET.BAS using OpenNeoUA Studio parsers.")
    parser.add_argument("setbas", help="SET.BAS file (read-only)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument(
        "--runtime-loose", action="store_true",
        help=("write the supported archive subset to Data/SetN/Loose "
              "instead of the BASet inspection layout"))
    parser.add_argument(
        "--overwrite", action="store_true",
        help="runtime mode only: replace different existing Loose files")
    parser.add_argument("--class", dest="class_name", default=DEFAULT_CLASS,
                        help=f"EMRS class to extract (default {DEFAULT_CLASS})")
    parser.add_argument("--all-classes", action="store_true",
                        help="extract every EMRS class")
    parser.add_argument("--png", action="store_true",
                        help="also convert extracted textures to indexed PNG")
    parser.add_argument("--ilbm", action="store_true",
                        help="also convert extracted VBMP textures to ILBM")
    parser.add_argument("--export-base-kids-raw", action="store_true",
                        help="developer dump of raw BASE/KIDS chunks (slow)")
    parser.add_argument("--metadata", action="store_true",
                        help="export BASE/KIDS scene metadata JSON")
    parser.add_argument("--manifest-csv", default="",
                        help="optional CSV manifest filename")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        archive = read_setbas(args.setbas)
    except SetBasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.runtime_loose:
        try:
            summary = export_runtime_loose(
                archive, args.out, dry_run=args.dry_run,
                overwrite=args.overwrite)
        except SetBasExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("\nRuntime Loose summary:")
        print(f"  manifest rows: {summary['total']}")
        print(f"  exported: {summary['exported']}")
        print(f"  already current: {summary['already_current']}")
        print(f"  planned: {summary['planned']}")
        print(f"  skipped to fallback: {summary['skipped']}")
        print(f"  errors: {summary['errors']}")
        print(f"  layout valid: {summary['validation']['valid']}")
        for issue in summary["validation"]["issues"][:12]:
            print(f"  validation issue: {issue}")
        return 1 if (summary["errors"]
                     or not summary["validation"]["valid"]) else 0

    summary = extract_archive(
        archive, args.out,
        class_name=args.class_name,
        all_classes=args.all_classes,
        convert_ilbm=args.ilbm,
        convert_png=args.png,
        export_base_kids=args.export_base_kids_raw,
        export_metadata=args.metadata,
        manifest_csv=args.manifest_csv,
        dry_run=args.dry_run,
    )
    print("\nSummary:")
    print(f"  total EMRS found: {summary['total']}")
    print(f"  extracted: {summary['extracted']}")
    for name, count in summary["skipped_by_class"].items():
        print(f"  skipped {name}: {count}")
    for name, count in summary["payload_counts"].items():
        print(f"  payload {name}: {count}")
    print(f"  duplicates: {summary['duplicates']}")
    print(f"  errors: {summary['errors']}")
    if summary["ilbm_converted"] or summary["ilbm_errors"]:
        print(f"  ilbm converted: {summary['ilbm_converted']} "
              f"({summary['ilbm_errors']} error(s))")
    if summary["png_converted"] or summary["png_errors"]:
        print(f"  png converted: {summary['png_converted']} "
              f"({summary['png_errors']} error(s))")
    return 1 if summary["errors"] else 0


def run_setbas_export():
    raise SystemExit(main_setbas_export())


TOOLS = {
    'iff': run_iff_reader,
    'diff': run_asset_diff,
    'profile': run_dependency_profile,
    'setbas': run_setbas_reader,
    'texture-convert': run_texture_convert,
    'census': run_asset_census,
    'mapping': run_base_mapping_editor,
    'ilbm': run_ilbm_parser,
    'family': run_asset_family,
    'base': run_base_parser,
    'base-kids': run_base_kids_export,
    'anm': run_anm_parser,
    'resolver': run_asset_resolver,
    'setbas-export': run_setbas_export,
}


def main(argv=None):
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: python tools/dev_cli.py <tool> [arguments]")
        print("tools:", ", ".join(sorted(TOOLS)))
        return 0
    if args[0] not in TOOLS:
        print(f"unknown tool {args[0]!r}; available: {', '.join(sorted(TOOLS))}")
        return 2
    sys.argv = [f"dev_cli {args[0]}", *args[1:]]
    result = TOOLS[args[0]]()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
