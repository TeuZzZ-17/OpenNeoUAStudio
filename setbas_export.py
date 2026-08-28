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

``export_runtime_loose`` is the runtime-facing mode.  It keeps the archive
logical names and writes only formats already consumed by OpenNeoUA's
``Data/SetN/Loose`` lookup:

    Loose/
      BASE/<NAME>.BASE       standalone copies of named BASE OBJTs
      SKLT/<NAME>.sklt       original FORM SKLT payloads
      ILBM/<NAME>.ILBM       original FORM VBMP/ILBM payloads
      ANM/<NAME>.ANM         original FORM VANM payloads

Unlike the inspection extractor, runtime export never invents duplicate
suffixes: an output collision is reported as ambiguous and left to SET.BAS.

CLI:
    python setbas_export.py SET.BAS --out DIR [--runtime-loose] [--overwrite]
        [--class ilbm.class |
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
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from asset_resolver import AssetResolver, DirectoryIndex
from setbas_reader import SetBasArchive, SetBasError, read_setbas
from verified_io import VerifiedCommitError, commit_verified_files

DEFAULT_CLASS = "ilbm.class"
RUNTIME_LOOSE_CLASSES = {
    "ilbm.class": "texture",
    "sklt.class": "skeleton",
    "bmpanim.class": "animation",
}

_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

# User-facing raw output folders for known EMRS classes (BASet convention).
FRIENDLY_RAW_DIRS = {
    "ilbm.class": "VBMP",
    "sklt.class": "SKLT",
    "bmpanim.class": "ANM",
}


class SetBasExportError(Exception):
    pass


@dataclass
class _RuntimeLooseCandidate:
    row: dict
    data: bytes


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
    """Only the resource filename, dropping any archive logical folders."""

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


def _set_id_from_name(name: str) -> int | None:
    match = re.fullmatch(r"set(\d+)", name, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _archive_set_id(archive_path: Path) -> int | None:
    """Infer SetN only from the canonical ``SetN/OBJECTS/SET.BAS`` shape."""

    archive_path = Path(archive_path)
    if archive_path.parent.name.casefold() != "objects":
        return None
    return _set_id_from_name(archive_path.parent.parent.name)


def infer_runtime_loose_root(archive_path: str | Path) -> Path | None:
    """Return the sibling ``Loose`` root for a canonical SET.BAS path."""

    archive_path = Path(archive_path)
    set_id = _archive_set_id(archive_path)
    if set_id is None or not 1 <= set_id <= 7:
        return None
    return archive_path.parent.parent / "Loose"


def resolve_runtime_loose_root(target: str | Path) -> tuple[Path, int]:
    """Normalize a SetN or Loose target and enforce the runtime Set1-Set7 scope."""

    target = Path(target)
    if target.name.casefold() == "loose":
        loose_root = target
        set_root = target.parent
    else:
        set_root = target
        loose_root = target / "Loose"
    set_id = _set_id_from_name(set_root.name)
    if set_id is None or not 1 <= set_id <= 7:
        raise SetBasExportError(
            "Runtime Loose export requires a Data/SetN/Loose target for "
            "Set1 through Set7 (organized Data/Sets/SetN is also supported)."
        )
    return loose_root, set_id


def _runtime_logical_name(source_name: str) -> tuple[str, str]:
    """Validate a runtime logical path without sanitizing or flattening it."""

    if not source_name:
        return "", "empty logical name"
    if source_name != source_name.strip():
        return "", "leading or trailing whitespace cannot be preserved safely"
    if source_name.startswith(("/", "\\")):
        return "", "absolute logical paths are not supported"
    if ":" in source_name:
        return "", "assign/drive-qualified logical paths are not supported"

    normalized = source_name.replace("\\", "/")
    parts = normalized.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "", "empty, current, or parent path components are unsafe"
        if any(ord(char) < 32 or char in '<>:"|?*' for char in part):
            return "", f"path component {part!r} is not portable"
        if part.endswith((" ", ".")):
            return "", f"path component {part!r} has an unsafe suffix"
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
            return "", f"path component {part!r} is reserved on Windows"
    return "/".join(parts), ""


def _runtime_emrs_output_path(class_name: str, logical_name: str) -> str:
    """Map embedded SET resources into the canonical flat Runtime Loose folders."""

    folder = {
        "ilbm.class": "ILBM",
        "sklt.class": "SKLT",
        "bmpanim.class": "ANM",
    }.get(class_name)
    if folder is None:
        return logical_name
    return f"{folder}/{logical_name.rsplit('/', 1)[-1]}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_manifest_row(*, source_name: str, class_name: str,
                          logical_name: str = "", output_path: str = "",
                          data: bytes | None = None, status: str,
                          reason: str, source_offset: int = -1) -> dict:
    return {
        "source_name": source_name,
        "class": class_name,
        "logical_name": logical_name,
        "output_path": output_path,
        "hash": _sha256(data) if data is not None else "",
        "status": status,
        "reason": reason,
        "source_offset": source_offset,
    }


def _validate_emrs_payload(class_name: str, data: bytes,
                           source_name: str) -> str:
    """Use the existing decoders to validate a runtime-supported payload."""

    if len(data) < 12 or data[:4] != b"FORM":
        return "payload is not an IFF FORM chunk"
    declared = int.from_bytes(data[4:8], "big")
    expected_size = 8 + declared + (declared & 1)
    if expected_size != len(data):
        return (f"FORM size mismatch: header requires {expected_size} bytes, "
                f"payload has {len(data)}")
    form_type = data[8:12]

    if class_name == "sklt.class":
        if form_type != b"SKLT":
            return f"sklt.class requires FORM SKLT, found {form_type!r}"
        from sklt_parser import parse_sklt_bytes
        try:
            parsed = parse_sklt_bytes(data, source_name)
        except Exception as exc:
            return f"existing SKLT parser rejected the payload: {exc}"
        if not any(chunk.form_type == "SKLT" for chunk in parsed.chunks):
            return "existing SKLT parser did not find a FORM SKLT container"
        return ""

    if class_name == "ilbm.class":
        if form_type not in (b"VBMP", b"ILBM"):
            return ("ilbm.class requires FORM VBMP or FORM ILBM, found "
                    f"{form_type!r}")
        from ilbm_parser import parse_ilbm_bytes
        try:
            parsed = parse_ilbm_bytes(data, source_name)
        except Exception as exc:
            return f"existing ILBM/VBMP parser rejected the payload: {exc}"
        if parsed.kind not in ("VBMP", "ILBM"):
            return "existing ILBM/VBMP parser rejected the FORM type"
        if parsed.width <= 0 or parsed.height <= 0 or not parsed.has_body:
            return "texture has no complete positive-size BODY"
        return ""

    if class_name == "bmpanim.class":
        if form_type != b"VANM":
            return f"bmpanim.class requires FORM VANM, found {form_type!r}"
        from anm_parser import parse_anm_bytes
        try:
            parsed = parse_anm_bytes(data, source_name)
        except Exception as exc:
            return f"existing VANM parser rejected the payload: {exc}"
        fatal = [warning for warning in parsed.warnings
                 if "failed" in warning.casefold()
                 or "no data" in warning.casefold()]
        if not parsed.has_form or fatal:
            return ("existing VANM parser rejected the payload"
                    + (f": {fatal[0]}" if fatal else ""))
        return ""

    return f"class {class_name!r} is not supported by Runtime Loose"


def _target_is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _runtime_texture_png_shadows(loose_root: Path, row: dict) -> list[Path]:
    """Existing PNGs win before raw ILBM/VBMP for embedded set textures."""

    if row.get("class") != "ilbm.class":
        return []
    logical_name = row.get("logical_name", "").replace("\\", "/")
    basename = logical_name.rsplit("/", 1)[-1].casefold()
    if basename in {"fx1.ilbm", "fx1.ilb", "fx2.ilbm", "fx2.ilb",
                    "fx3.ilbm", "fx3.ilb"}:
        return []
    slash = logical_name.rfind("/")
    dot = logical_name.rfind(".")
    stem = (logical_name[:dot] if dot > slash else logical_name)
    relative_candidates = []
    for suffix in (".PNG", ".png"):
        png_name = stem.rsplit("/", 1)[-1] + suffix
        relative_candidates.append(f"ILBM/{png_name}")
    found: list[Path] = []
    seen: set[str] = set()
    for relative in relative_candidates:
        candidate = loose_root.joinpath(*relative.split("/"))
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if _target_is_within(loose_root, candidate) and candidate.is_file():
            found.append(candidate)
    return found


def _legacy_three_letter_path(relative: str) -> str:
    slash = relative.rfind("/")
    dot = relative.rfind(".")
    if dot > slash and len(relative) - dot - 1 > 3:
        return relative[:dot + 4]
    return relative


def _existing_runtime_override_paths(loose_root: Path, row: dict) -> list[Path]:
    """All existing paths the runtime could use instead of SET.BAS for a row."""

    output_path = row.get("output_path", "").replace("\\", "/")
    if not output_path:
        return []
    relatives = [output_path, _legacy_three_letter_path(output_path)]
    class_name = row.get("class", "")
    logical_name = row.get("logical_name", "").replace("\\", "/")
    if class_name == "ilbm.class":
        relatives.extend(
            str(path.relative_to(loose_root)).replace("\\", "/")
            for path in _runtime_texture_png_shadows(loose_root, row))
    elif class_name == "base.class" and logical_name:
        base_path = f"BASE/{logical_name}.BASE"
        relatives.extend((base_path, _legacy_three_letter_path(base_path)))

    found: list[Path] = []
    seen: set[str] = set()
    for relative in relatives:
        candidate = loose_root.joinpath(*relative.split("/"))
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if _target_is_within(loose_root, candidate) and candidate.is_file():
            found.append(candidate)
    return found


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


def _plan_runtime_loose(archive: SetBasArchive
                        ) -> tuple[list[dict], list[_RuntimeLooseCandidate]]:
    """Build one collision-aware plan from the shared SET.BAS/Base parsers."""

    rows: list[dict] = []
    candidates: list[_RuntimeLooseCandidate] = []

    for resource in archive.resources:
        payload = (archive.payload_bytes(resource)
                   if resource.payload_source != "none" else None)
        supported = resource.class_id in RUNTIME_LOOSE_CLASSES
        logical_name = ""
        output_path = ""
        path_error = ""
        if supported:
            logical_name, path_error = _runtime_logical_name(
                resource.resource_name)
            if not path_error:
                output_path = _runtime_emrs_output_path(
                    resource.class_id, logical_name)
        base_args = {
            "source_name": resource.resource_name,
            "class_name": resource.class_id,
            "logical_name": logical_name,
            "output_path": output_path,
            "data": payload,
            "source_offset": resource.emrs_offset,
        }
        if resource.error:
            rows.append(_runtime_manifest_row(
                **base_args, status="skipped_invalid",
                reason=resource.error))
            continue
        if resource.payload_source == "none" or payload is None:
            rows.append(_runtime_manifest_row(
                **base_args, status="skipped_invalid",
                reason="archive resource has no payload; SET.BAS fallback kept"))
            continue
        if not supported:
            rows.append(_runtime_manifest_row(
                **base_args, status="skipped_unsupported",
                reason=(f"runtime EMRS override does not support class "
                        f"{resource.class_id!r}; SET.BAS fallback kept")))
            continue

        if path_error:
            rows.append(_runtime_manifest_row(
                **base_args, status="skipped_unsupported",
                reason=f"{path_error}; SET.BAS fallback kept"))
            continue
        payload_error = _validate_emrs_payload(
            resource.class_id, payload, resource.resource_name)
        if payload_error:
            rows.append(_runtime_manifest_row(
                **base_args, status="skipped_invalid",
                reason=f"{payload_error}; SET.BAS fallback kept"))
            continue

        row = _runtime_manifest_row(
            **base_args, status="planned",
            reason="supported exact embedded payload")
        rows.append(row)
        candidates.append(_RuntimeLooseCandidate(row=row, data=payload))

    try:
        from base_mapping_editor import export_base_object_bytes
        from base_parser import parse_base_bytes

        base_asset = parse_base_bytes(
            archive.data, f"SET.BAS:{archive.path.name}")
        if base_asset.root is None:
            rows.append(_runtime_manifest_row(
                source_name=archive.path.name, class_name="base.class",
                status="skipped_invalid",
                reason="SET.BAS has no parseable root BASE object"))
        else:
            for base_object in base_asset.all_objects():
                source_name = (base_object.name or
                               f"<unnamed BASE @ 0x{base_object.source_objt_offset:X}>")
                if not base_object.name:
                    rows.append(_runtime_manifest_row(
                        source_name=source_name, class_name="base.class",
                        status="skipped_unsupported",
                        reason=("unnamed BASE containers have no runtime lookup "
                                "key; SET.BAS fallback kept"),
                        source_offset=base_object.source_objt_offset))
                    continue
                logical_name, path_error = _runtime_logical_name(
                    base_object.name)
                if path_error or "/" in logical_name:
                    rows.append(_runtime_manifest_row(
                        source_name=source_name, class_name="base.class",
                        logical_name=logical_name,
                        status="skipped_unsupported",
                        reason=((path_error or
                                 "BASE object names cannot contain path separators")
                                + "; SET.BAS fallback kept"),
                        source_offset=base_object.source_objt_offset))
                    continue
                output_path = f"BASE/{logical_name}.BASE"
                try:
                    standalone = export_base_object_bytes(
                        archive.data, base_object)
                    verified = parse_base_bytes(
                        standalone, f"Runtime Loose:{output_path}")
                    if verified.root is None \
                            or verified.root.name != base_object.name:
                        raise SetBasExportError(
                            "standalone BASE name verification failed")
                except Exception as exc:
                    rows.append(_runtime_manifest_row(
                        source_name=source_name, class_name="base.class",
                        logical_name=logical_name, output_path=output_path,
                        status="skipped_invalid",
                        reason=f"{exc}; SET.BAS fallback kept",
                        source_offset=base_object.source_objt_offset))
                    continue

                row = _runtime_manifest_row(
                    source_name=source_name, class_name="base.class",
                    logical_name=logical_name, output_path=output_path,
                    data=standalone, status="planned",
                    reason="exact source OBJT wrapped as a standalone BASE",
                    source_offset=base_object.source_objt_offset)
                rows.append(row)
                candidates.append(
                    _RuntimeLooseCandidate(row=row, data=standalone))
    except Exception as exc:
        rows.append(_runtime_manifest_row(
            source_name=archive.path.name, class_name="base.class",
            status="skipped_invalid",
            reason=f"BASE/KIDS parse failed: {exc}; SET.BAS fallback kept"))

    by_output: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["output_path"] and row["class"] in (
                *RUNTIME_LOOSE_CLASSES, "base.class"):
            by_output[row["output_path"].casefold()].append(row)
    for group in by_output.values():
        if len(group) < 2:
            continue
        output_path = group[0]["output_path"]

        def source_label(row: dict) -> str:
            offset = row["source_offset"]
            offset_label = f"0x{offset:X}" if offset >= 0 else "unknown"
            return f"{row['class']}:{row['source_name']}@{offset_label}"

        sources = ", ".join(
            source_label(row) for row in group[:6])
        if len(group) > 6:
            sources += f", ... ({len(group)} total)"
        reason = (f"{len(group)} archive entries map case-insensitively to "
                  f"{output_path}; no candidate selected ({sources}); "
                  "SET.BAS fallback kept")
        for row in group:
            row["status"] = "skipped_ambiguous"
            row["reason"] = reason

    return rows, candidates


def _validate_runtime_row_bytes(row: dict, data: bytes) -> str:
    if row["class"] != "base.class":
        return _validate_emrs_payload(
            row["class"], data, row["source_name"])

    from base_parser import parse_base_bytes
    parsed = parse_base_bytes(data, row["output_path"])
    if parsed.root is None:
        return "standalone BASE parser found no root object"
    if parsed.root.name != row["logical_name"]:
        return (f"standalone BASE NAME {parsed.root.name!r} does not match "
                f"{row['logical_name']!r}")
    return ""


def _resolver_check(loose_root: Path, row: dict,
                    cache: dict[tuple[Path, str], AssetResolver]) -> str:
    output_parts = row["output_path"].replace("\\", "/").split("/")
    class_name = row["class"]
    canonical_folder = {
        "ilbm.class": "ILBM",
        "sklt.class": "SKLT",
        "bmpanim.class": "ANM",
        "base.class": "BASE",
    }.get(class_name)
    if canonical_folder is None or not output_parts \
            or output_parts[0].casefold() != canonical_folder.casefold():
        return f"output is outside canonical {canonical_folder or 'Runtime Loose'} folder"
    resolver_root = loose_root / output_parts[0]
    lookup_name = "/".join(output_parts[1:])
    kind = ("base" if class_name == "base.class"
            else RUNTIME_LOOSE_CLASSES.get(class_name, "any"))

    key = (resolver_root, kind)
    resolver = cache.get(key)
    if resolver is None:
        resolver = AssetResolver([resolver_root])
        cache[key] = resolver
    resolved = resolver.resolve(lookup_name, kind)
    if resolved.status == "ambiguous":
        return (f"shared AssetResolver reports an ambiguity for "
                f"{lookup_name}: {len(resolved.candidates)} candidates")
    if not resolved.found or resolved.path is None:
        return f"shared AssetResolver cannot resolve {lookup_name}"
    expected = loose_root.joinpath(*output_parts)
    try:
        if resolved.path.resolve() != expected.resolve():
            return (f"shared AssetResolver selected {resolved.path}, expected "
                    f"{expected}")
    except OSError as exc:
        return f"could not compare resolved paths: {exc}"
    return ""


def validate_runtime_loose_layout(
        target: str | Path, manifest: dict | str | Path, *,
        planned_payloads: dict[str, bytes] | None = None,
        archive: SetBasArchive | None = None) -> dict:
    """Validate hashes, payloads and shared-resolver lookup for one layout."""

    loose_root, set_id = resolve_runtime_loose_root(target)
    if not isinstance(manifest, dict):
        try:
            manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise SetBasExportError(f"could not read Runtime Loose manifest: {exc}") from exc

    is_plan = planned_payloads is not None
    planned_payloads = planned_payloads or {}
    issues: list[str] = []
    checked = 0
    resolver_checked = 0
    fallback_count = 0
    asset_family_checked = 0
    resolver_cache: dict[tuple[Path, str], AssetResolver] = {}
    DirectoryIndex.clear_cache()

    for row in manifest.get("resources", []):
        status = row.get("status", "")
        output_path = row.get("output_path", "")
        output_file: Path | None = None
        if output_path:
            normalized_output, path_error = _runtime_logical_name(output_path)
            if path_error:
                issues.append(
                    f"{output_path}: invalid manifest output path: {path_error}")
                continue
            output_file = loose_root.joinpath(*normalized_output.split("/"))
            if not _target_is_within(loose_root, output_file):
                issues.append(f"{output_path}: target escapes the Loose root")
                continue
        if status.startswith("skipped_"):
            fallback_count += 1
            if status == "skipped_conflict":
                issues.append(
                    f"{output_path}: existing file conflicts with archive payload")
            elif status in ("skipped_ambiguous", "skipped_invalid") \
                    and output_path:
                shadowing = _existing_runtime_override_paths(loose_root, row)
                if shadowing:
                    issues.append(
                        f"{output_path}: skipped fallback path is still shadowed "
                        "by existing loose file(s): "
                        + ", ".join(str(path) for path in shadowing))
            continue
        if status == "error":
            issues.append(
                f"{output_path or row.get('source_name', '?')}: "
                f"{row.get('reason', 'export error')}")
            continue
        if status not in ("planned", "planned_overwrite", "exported",
                          "already_current"):
            continue
        if not output_path:
            issues.append(f"{row.get('source_name', '?')}: active row has no output path")
            continue

        key = output_path.casefold()
        if status in ("planned", "planned_overwrite"):
            data = planned_payloads.get(key)
            if data is None:
                issues.append(f"{output_path}: planned payload is unavailable")
                continue
        else:
            assert output_file is not None
            if not output_file.is_file() or output_file.is_symlink():
                issues.append(f"{output_path}: output is missing or is a symbolic link")
                continue
            try:
                data = output_file.read_bytes()
            except OSError as exc:
                issues.append(f"{output_path}: could not read output: {exc}")
                continue

        checked += 1
        actual_hash = _sha256(data)
        if actual_hash != row.get("hash", ""):
            issues.append(
                f"{output_path}: SHA-256 mismatch ({actual_hash})")
            continue
        try:
            payload_error = _validate_runtime_row_bytes(row, data)
        except Exception as exc:
            payload_error = f"existing payload parser failed: {exc}"
        if payload_error:
            issues.append(f"{output_path}: {payload_error}")
            continue
        if status in ("exported", "already_current"):
            resolver_error = _resolver_check(
                loose_root, row, resolver_cache)
            if resolver_error:
                issues.append(f"{output_path}: {resolver_error}")
            else:
                resolver_checked += 1

    if archive is not None and not is_plan:
        try:
            from asset_family import load_asset_family

            family = load_asset_family(
                archive.path, extra_roots=[loose_root], setbas=archive)
            if family.root_object is None:
                issues.append(
                    "shared AssetFamily could not reconstruct the SET root")
            relevant_kinds = {
                "skeleton", "texture", "tracy_texture",
                "animation", "anm_bitmap",
            }
            unresolved = [
                dependency for dependency in family.dependencies
                if dependency.kind in relevant_kinds
                and dependency.status in (
                    "ambiguous", "missing", "failed_load")
            ]
            asset_family_checked = sum(
                dependency.kind in relevant_kinds
                for dependency in family.dependencies)
            if unresolved:
                sample = ", ".join(
                    f"{dependency.kind}:{dependency.raw_ref} "
                    f"({dependency.status})"
                    for dependency in unresolved[:8])
                issues.append(
                    f"shared AssetFamily reports {len(unresolved)} unresolved "
                    f"runtime relation(s): {sample}")
        except Exception as exc:
            issues.append(f"shared AssetFamily validation failed: {exc}")

    return {
        "valid": not issues,
        "set_id": set_id,
        "mode": "dry-run plan" if is_plan else "written layout",
        "checked": checked,
        "resolver_checked": resolver_checked,
        "asset_family_checked": asset_family_checked,
        "fallback_count": fallback_count,
        "issues": issues,
    }


def _verify_committed_runtime_outputs(
        loose_root: Path, rows: list[dict]) -> None:
    """Verify every active override while coordinated rollback is available."""

    issues: list[str] = []
    resolver_cache: dict[tuple[Path, str], AssetResolver] = {}
    DirectoryIndex.clear_cache()
    for row in rows:
        if row.get("status") not in ("exported", "already_current"):
            continue
        output_path = str(row.get("output_path", ""))
        normalized, path_error = _runtime_logical_name(output_path)
        if path_error:
            issues.append(f"{output_path}: {path_error}")
            continue
        target = loose_root.joinpath(*normalized.split("/"))
        if not _target_is_within(loose_root, target) \
                or not target.is_file() or target.is_symlink():
            issues.append(
                f"{output_path}: committed output is missing, unsafe, or "
                "a symbolic link")
            continue
        try:
            data = target.read_bytes()
        except OSError as exc:
            issues.append(f"{output_path}: committed output is unreadable: {exc}")
            continue
        if _sha256(data) != row.get("hash", ""):
            issues.append(f"{output_path}: committed SHA-256 mismatch")
            continue
        try:
            payload_error = _validate_runtime_row_bytes(row, data)
        except Exception as exc:
            payload_error = f"existing payload parser failed: {exc}"
        if payload_error:
            issues.append(f"{output_path}: {payload_error}")
            continue
        resolver_error = _resolver_check(
            loose_root, row, resolver_cache)
        if resolver_error:
            issues.append(f"{output_path}: {resolver_error}")
    if issues:
        raise SetBasExportError(
            "coordinated Runtime Loose verification failed:\n"
            + "\n".join(issues[:12]))


def export_runtime_loose(archive: SetBasArchive, target: str | Path, *,
                         dry_run: bool = False, overwrite: bool = False,
                         log=print) -> dict:
    """Export the supported SET.BAS subset to a validated runtime Loose tree."""

    loose_root, set_id = resolve_runtime_loose_root(target)
    source_set_id = _archive_set_id(Path(archive.path))
    if source_set_id is not None and source_set_id != set_id:
        raise SetBasExportError(
            f"source archive belongs to Set{source_set_id}, but target belongs "
            f"to Set{set_id}; refusing a silent cross-SET export")
    if loose_root.exists() and not loose_root.is_dir():
        raise SetBasExportError(f"Loose target is not a directory: {loose_root}")
    if _same_path(loose_root, Path(archive.path)):
        raise SetBasExportError("Loose target must not be the source SET.BAS")

    rows, candidates = _plan_runtime_loose(archive)

    planned_payloads: dict[str, bytes] = {}
    pending_writes: list[tuple[dict, bytes, Path]] = []
    for candidate in candidates:
        row = candidate.row
        if row["status"] != "planned":
            continue
        relative_parts = row["output_path"].replace("\\", "/").split("/")
        out_path = loose_root.joinpath(*relative_parts)
        if not _target_is_within(loose_root, out_path):
            row["status"] = "error"
            row["reason"] = "resolved output escapes the Loose root"
            continue
        if _same_path(out_path, Path(archive.path)):
            row["status"] = "error"
            row["reason"] = "output would overwrite the source SET.BAS"
            continue
        png_shadows = _runtime_texture_png_shadows(loose_root, row)
        if png_shadows:
            row["status"] = "skipped_conflict"
            row["reason"] = (
                "higher-priority runtime PNG already exists and was left "
                "untouched: " + ", ".join(str(path) for path in png_shadows))
            continue
        if out_path.is_symlink():
            row["status"] = "skipped_conflict"
            row["reason"] = "existing symbolic link was not followed or replaced"
            continue

        replacing_existing = False
        if out_path.exists():
            if not out_path.is_file():
                row["status"] = "skipped_conflict"
                row["reason"] = "output path already exists and is not a file"
                continue
            try:
                existing_hash = _sha256(out_path.read_bytes())
            except OSError as exc:
                row["status"] = "error"
                row["reason"] = f"could not inspect existing output: {exc}"
                continue
            if existing_hash == row["hash"]:
                row["status"] = "already_current"
                row["reason"] = "existing Loose file already matches the archive payload"
                continue
            if not overwrite:
                row["status"] = "skipped_conflict"
                row["reason"] = (
                    "different Loose file already exists; enable overwrite "
                    "explicitly to replace it")
                continue
            replacing_existing = True
            if dry_run:
                row["status"] = "planned_overwrite"
                row["reason"] = "dry-run: different existing file would be replaced"
                planned_payloads[row["output_path"].casefold()] = candidate.data
                continue

        if dry_run:
            planned_payloads[row["output_path"].casefold()] = candidate.data
            continue
        row["status"] = "exported"
        row["reason"] = ("replaced an explicitly approved existing Loose file"
                         if replacing_existing
                         else "written from the exact SET.BAS payload")
        pending_writes.append((row, candidate.data, out_path))

    status_counts = dict(sorted(Counter(
        row["status"] for row in rows).items()))
    manifest = {
        "format": "OpenNeoUA Runtime Loose SET",
        "version": 1,
        "source": {
            "name": Path(archive.path).name,
            "path": str(archive.path),
            "sha256": _sha256(archive.data),
        },
        "layout": {
            "set_id": set_id,
            "loose_root": str(loose_root),
            "logical_root": f"Data/Set{set_id}/Loose",
            "folders": ["ILBM", "ANM", "SKLT", "BASE"],
        },
        "hash_algorithm": "sha256",
        "dry_run": dry_run,
        "overwrite": overwrite,
        "summary": {
            "total": len(rows),
            "status_counts": status_counts,
        },
        "resources": rows,
    }
    if dry_run:
        validation = validate_runtime_loose_layout(
            loose_root, manifest,
            planned_payloads=planned_payloads,
            archive=archive)
        manifest["validation"] = validation
        log("Runtime Loose dry-run: nothing written")
    else:
        validation_holder: dict[str, dict] = {}
        manifest["validation"] = {
            "valid": False,
            "mode": "pending coordinated commit",
            "issues": [],
        }
        try:
            with tempfile.TemporaryDirectory(
                    prefix="OpenNeoUAStudio_runtime_loose_") as temp_dir:
                stage_root = Path(temp_dir)
                staged_pairs: list[tuple[Path, Path]] = []
                for row, data, target_path in pending_writes:
                    relative = Path(*row["output_path"].split("/"))
                    staged = stage_root / relative
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(data)
                    if _sha256(staged.read_bytes()) != row["hash"]:
                        raise SetBasExportError(
                            f"staging hash mismatch: {row['output_path']}")
                    staged_pairs.append((staged, target_path))

                def verify_commit() -> None:
                    _verify_committed_runtime_outputs(loose_root, rows)
                    validation = validate_runtime_loose_layout(
                        loose_root, manifest, archive=archive)
                    validation_holder["result"] = validation
                    manifest["validation"] = validation

                warnings = commit_verified_files(
                    staged_pairs, verify=verify_commit)
                for warning in warnings:
                    log(f"warning: {warning}")
        except (OSError, SetBasExportError, VerifiedCommitError) as exc:
            rollback = getattr(exc, "rollback_complete", True)
            suffix = ("; previous Loose files restored"
                      if rollback else "; rollback incomplete")
            raise SetBasExportError(str(exc) + suffix) from exc
        validation = validation_holder["result"]
        log(f"Runtime Loose export verified: {loose_root}")

    skipped = sum(count for status, count in status_counts.items()
                  if status.startswith("skipped_"))
    errors = status_counts.get("error", 0)
    summary = {
        "total": len(rows),
        "exported": status_counts.get("exported", 0),
        "already_current": status_counts.get("already_current", 0),
        "planned": (status_counts.get("planned", 0)
                    + status_counts.get("planned_overwrite", 0)),
        "skipped": skipped,
        "errors": errors,
        "status_counts": status_counts,
        "manifest_json": "",
        "validation": validation,
        "manifest": manifest,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
