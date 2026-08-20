"""Extract and convert SET.BAS resources (BASet capability merge).

Built on OpenNeoUA Studio's own ``setbas_reader`` index instead of a parallel
parser.
Strictly read-only for the archive: extraction writes new files into a
separate output folder, mirroring the BASet layout:

    out/
      manifest.json          every EMRS record: class, name, offsets, sha1
      manifest.csv           optional
      raw/
        VBMP/  SKLT/  ANM/   raw payload chunks (FORM header included)
        BASE_KIDS/           optional developer dump (base_kids_export)
      textures_ilbm/         optional standalone ILBM conversion of VBMPs
      textures_png/          optional indexed-PNG conversion of textures

Duplicate resource names get a ``__dupNNN`` suffix exactly like BASet, so
existing tooling that consumes the BASet layout keeps working.

CLI:
    python setbas_export.py SET.BAS --out DIR [--class ilbm.class |
        --all-classes] [--ilbm] [--png] [--export-base-kids-raw] [--metadata]
        [--manifest-csv manifest.csv] [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from setbas_reader import SetBasArchive, SetBasError, read_setbas

DEFAULT_CLASS = "ilbm.class"

# User-facing raw output folders for known EMRS classes (BASet convention).
FRIENDLY_RAW_DIRS = {
    "ilbm.class": "VBMP",
    "sklt.class": "SKLT",
    "bmpanim.class": "ANM",
}


class SetBasExportError(Exception):
    pass


def _same_path(first: Path, second: Path) -> bool:
    try:
        if first.exists() and second.exists():
            return first.samefile(second)
    except OSError:
        pass
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first.absolute() == second.absolute()


def sanitize_component(component: str) -> str:
    component = component.replace("\\", "/").strip()
    component = re.sub(r"[:*?\"<>|]", "_", component)
    component = re.sub(r"[\x00-\x1f]", "_", component)
    component = component.strip(" .")
    return component or "_"


def friendly_raw_dir(class_name: str) -> str:
    return FRIENDLY_RAW_DIRS.get(
        class_name, sanitize_component(class_name.replace("/", "_")))


def flattened_resource_name(resource_name: str) -> str:
    """Only the resource filename, dropping logical folders like Skeleton/."""

    normalized = resource_name.replace("\\", "/")
    parts = [sanitize_component(p) for p in normalized.split("/")
             if p not in ("", ".", "..")]
    return parts[-1] if parts else "unnamed_resource"


def find_external_palette(archive_path: Path) -> Path | None:
    """Set palette next to the archive (Data/SetN/PALETTE/STANDARD.PAL)."""

    for base in (archive_path.parent, archive_path.parent.parent):
        palette_dir = base / "PALETTE"
        if not palette_dir.is_dir():
            continue
        standard = palette_dir / "STANDARD.PAL"
        if standard.is_file():
            return standard
        for candidate in sorted(palette_dir.glob("*.PAL")):
            return candidate
        for candidate in sorted(palette_dir.glob("*.pal")):
            return candidate
    return None


def extract_resource(archive: SetBasArchive, resource,
                     out_path: str | Path) -> Path:
    """Write one EMRS payload (full chunk bytes) to ``out_path``."""

    if resource.error or resource.payload_source == "none":
        raise SetBasExportError(
            f"{resource.resource_name}: no extractable payload "
            f"({resource.error or 'missing payload'})")
    out_path = Path(out_path)
    if _same_path(out_path, Path(archive.path)):
        raise SetBasExportError(
            "The extracted resource must not overwrite the source SET.BAS.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(archive.payload_bytes(resource))
    return out_path


def extract_archive(archive: SetBasArchive, out_dir: str | Path, *,
                    class_name: str = DEFAULT_CLASS,
                    all_classes: bool = False,
                    convert_ilbm: bool = False,
                    convert_png: bool = False,
                    export_base_kids: bool = False,
                    export_metadata: bool = False,
                    manifest_csv: str = "",
                    dry_run: bool = False,
                    log=print) -> dict:
    """Extract EMRS payloads with a BASet-compatible manifest and layout."""

    out_dir = Path(out_dir)
    raw_root = out_dir / "raw"
    ilbm_root = out_dir / "textures_ilbm"
    png_root = out_dir / "textures_png"
    if not dry_run:
        raw_root.mkdir(parents=True, exist_ok=True)

    palette_path = find_external_palette(Path(archive.path)) \
        if (convert_ilbm or convert_png) else None
    palette = None
    if convert_ilbm or convert_png:
        from ilbm_parser import parse_pal_file
        from texture_convert import (BUILTIN_AIR1TXT_CMAP, cmap_to_palette)
        if palette_path is not None:
            palette = parse_pal_file(palette_path)
        if palette is None:
            palette = cmap_to_palette(BUILTIN_AIR1TXT_CMAP)
            log("texture conversion: no set palette found next to the "
                "archive; using the built-in AIR1TXT fallback for VBMPs "
                "without CMAP")
        else:
            log(f"texture conversion: palette for VBMPs: {palette_path}")

    rows: list[dict] = []
    seen: dict[tuple[str, str], int] = defaultdict(int)
    skipped_by_class: Counter = Counter()
    payload_counts: Counter = Counter()
    extracted = 0
    duplicates = 0
    errors = 0
    ilbm_converted = 0
    ilbm_errors = 0
    png_converted = 0
    png_errors = 0

    for resource in archive.resources:
        wanted = (not resource.error
                  and (all_classes or resource.class_id == class_name))
        row = {
            "index": resource.index,
            "class_name": resource.class_id,
            "resource_name": resource.resource_name,
            "emrs_offset": resource.emrs_offset,
            "payload_source": resource.payload_source,
            "payload_tag": resource.payload_tag,
            "payload_form_type": resource.payload_form_type,
            "payload_offset_start": resource.payload_offset,
            "payload_offset_end": resource.payload_offset
            + resource.payload_size,
            "payload_size": resource.payload_size,
            "payload_sha1": "",
            "output_path": "",
            "duplicate_index": 0,
            "extracted": False,
            "error": resource.error,
        }
        if resource.payload_source != "none":
            payload_counts[resource.payload_form_type
                           or resource.payload_tag] += 1
        if resource.error:
            errors += 1
            rows.append(row)
            continue
        if not wanted:
            skipped_by_class[resource.class_id] += 1
            rows.append(row)
            continue
        if resource.payload_source == "none":
            errors += 1
            row["error"] = "missing payload"
            rows.append(row)
            continue

        class_dir = friendly_raw_dir(resource.class_id)
        file_name = flattened_resource_name(resource.resource_name)
        key = (class_dir, file_name)
        dup = seen[key]
        seen[key] += 1
        if dup:
            duplicates += 1
            stem, dot, ext = file_name.rpartition(".")
            file_name = (f"{stem}__dup{dup:03d}.{ext}" if dot
                         else f"{file_name}__dup{dup:03d}")
        out_path = raw_root / class_dir / file_name
        dumped = archive.payload_bytes(resource)
        row.update({
            "payload_sha1": hashlib.sha1(dumped).hexdigest(),
            "output_path": str(out_path.relative_to(out_dir)
                               ).replace("\\", "/"),
            "duplicate_index": dup,
            "extracted": True,
        })
        if not dry_run:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(dumped)

            if (convert_ilbm or convert_png) \
                    and resource.class_id == "ilbm.class":
                try:
                    from ilbm_parser import parse_ilbm_bytes
                    from texture_convert import (
                        ilbm_image_to_png,
                        write_image_as_ilbm,
                    )
                    image = parse_ilbm_bytes(dumped, resource.resource_name)
                    palette_override = (
                        palette if image.palette is None else None)
                    if convert_ilbm:
                        ilbm_path = ilbm_root / (
                            Path(file_name).stem + ".ILBM")
                        try:
                            write_image_as_ilbm(
                                image, ilbm_path, palette_override,
                                source=resource.resource_name)
                            ilbm_converted += 1
                        except Exception as exc:
                            ilbm_errors += 1
                            log(f"[ILBM ERROR] {resource.resource_name}: "
                                f"{exc}")
                    if convert_png:
                        try:
                            png_path = png_root / (
                                Path(file_name).stem + ".PNG")
                            ilbm_image_to_png(
                                image, png_path, palette_override)
                            png_converted += 1
                        except Exception as exc:
                            png_errors += 1
                            log(f"[PNG ERROR] {resource.resource_name}: "
                                f"{exc}")
                except Exception as exc:  # keep extracting on decode issues
                    if convert_ilbm:
                        ilbm_errors += 1
                    if convert_png:
                        png_errors += 1
                    log(f"[TEXTURE DECODE ERROR] "
                        f"{resource.resource_name}: {exc}")
        rows.append(row)
        extracted += 1

    summary = {
        "total": len(archive.resources),
        "extracted": extracted,
        "skipped_by_class": dict(skipped_by_class),
        "payload_counts": dict(sorted(payload_counts.items())),
        "duplicates": duplicates,
        "errors": errors,
        "ilbm_converted": ilbm_converted,
        "ilbm_errors": ilbm_errors,
        "png_converted": png_converted,
        "png_errors": png_errors,
        "manifest_json": "",
        "base_kids": None,
        "metadata": None,
    }

    if dry_run:
        log("dry-run: nothing written")
        return summary

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps({
        "source": str(archive.path),
        "resources": rows,
    }, indent=2), encoding="utf-8")
    summary["manifest_json"] = str(manifest_path)
    log(f"wrote {manifest_path}")

    if manifest_csv:
        csv_path = out_dir / manifest_csv
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            fields = list(rows[0]) if rows else [
                "index", "class_name", "resource_name", "emrs_offset",
                "payload_source", "payload_tag", "payload_form_type",
                "payload_offset_start", "payload_offset_end", "payload_size",
                "payload_sha1", "output_path", "duplicate_index",
                "extracted", "error",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        log(f"wrote {csv_path}")

    if export_base_kids:
        import base_kids_export
        base_summary = base_kids_export.write_raw_base_kids(
            Path(archive.path), raw_root / "BASE_KIDS")
        summary["base_kids"] = base_summary
        log(f"BASE/KIDS raw export: {base_summary['kids_forms_exported']} "
            f"KIDS forms, {base_summary['objt_forms_exported']} OBJT forms")

    if export_metadata:
        import base_kids_export
        meta_summary = base_kids_export.write_outputs(
            Path(archive.path), out_dir / "metadata")
        summary["metadata"] = meta_summary
        log(f"scene metadata exported to {out_dir / 'metadata'}")

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract raw EMRS payloads from SET.BAS "
                    "(BASet capability, OpenNeoUA Studio parsers).")
    parser.add_argument("setbas", help="SET.BAS file (read-only)")
    parser.add_argument("--out", required=True, help="output directory")
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


if __name__ == "__main__":
    raise SystemExit(main())
