"""Markdown / JSON technical reports for an assembled asset family."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from asset_family import AssetFamily, FamilyObject


FORMAT_NOTES = [
    ("CONFIRMED", "SKLT chunks: POO2 = 3 big-endian floats per point; "
     "POOL/SENS = 3 big-endian int16 (legacy v1); SEN2 = 3 BE floats per point "
     "(count = size/12, culling/bounding volume, read-only); POL2 = s32 count, "
     "then per polygon u16 vertex count + u16 indices; POLY (legacy) = s16 "
     "indices with -1 terminators."),
    ("CONFIRMED", "BASE tree: FORM MC2 > FORM OBJT > CLID + FORM BASE "
     "(ROOT/NAME, STRC 62B transform, OBJT skeleton, ADES material blocks, "
     "KIDS nested children with own transforms)."),
    ("CONFIRMED", "amesh ATTS entry = 6 bytes: s16 polyID (explicit skeleton "
     "polygon index), u8 colorVal, u8 shadeVal (color = 1 - shade/256), "
     "u8 tracyVal, u8 pad.  Count = chunk size / 6."),
    ("CONFIRMED", "amesh OLPL: per ATTS entry, s16 count + count*(u8 u, u8 v); "
     "texture-space pixels normalised /256; UV j pairs with polygon vertex j; "
     "triangulated as a fan (0, j, j-1)."),
    ("CONFIRMED", "The chunk ID ATTS is reused by particle.class with a "
     "different payload (emitter parameters); meaning depends on class."),
    ("CONFIRMED", "In HUD .SKL files OLPL has a different layout: u16 count + "
     "u16 indices into OTL2 points (2D outline), not UV bytes."),
    ("CONFIRMED", "ILBM: BMHD 20B, CMAP 256*3, BODY planar bitplanes with "
     "per-row per-plane ByteRun1.  VBMP: HEAD (u16 w,h,flags) + raw 8bpp BODY."),
    ("CONFIRMED", "VANM/ANM: FORM VANM > DATA stream = bitmap class name, "
     "NUL-separated bitmap names, UV outline groups, frames of (s32 time in "
     "1024Hz ticks, s16 bitmapID, s16 uvGroupID).  Texture/material animation "
     "only; play mode (loop/ping-pong) stored in the referencing .base BANI "
     "STRC animType."),
    ("STRONG HYPOTHESIS", "SEN2 is used by the engine as a cheap visibility "
     "pre-check volume (skeleton_func132 transforms SEN before POO when "
     "SEN count < POO/4); commonly but not necessarily 8 points."),
]


def _object_dict(fam_obj: FamilyObject) -> dict:
    base_obj = fam_obj.base_object
    skeleton = fam_obj.skeleton
    entry: dict = {
        "name": base_obj.name,
        "skeleton_reference": base_obj.skeleton_name,
        "skeleton_resolved": fam_obj.skeleton_ref.display_path
        if fam_obj.skeleton_ref and fam_obj.skeleton_ref.found else None,
        "skeleton_source": fam_obj.skeleton_ref.source
        if fam_obj.skeleton_ref and fam_obj.skeleton_ref.source else None,
    }
    if base_obj.transform:
        t = base_obj.transform
        entry["transform"] = {
            "position": list(t.position),
            "scale": list(t.scale),
            "euler_int16": list(t.euler),
            "vis_limit": t.vis_limit,
            "ambient_light": t.ambient_light,
        }
    if skeleton:
        xs = [p[0] for p in skeleton.points] or [0.0]
        ys = [p[1] for p in skeleton.points] or [0.0]
        zs = [p[2] for p in skeleton.points] or [0.0]
        entry["skeleton_stats"] = {
            "poo2_points": len(skeleton.points),
            "pol2_polygons": skeleton.parsed_polygon_count,
            "sen2_points": len(skeleton.sensors),
            "bounds_min": [min(xs), min(ys), min(zs)],
            "bounds_max": [max(xs), max(ys), max(zs)],
            "polygon_vertex_histogram": _vertex_histogram(skeleton.polygons),
        }
    entry["materials"] = []
    for group in fam_obj.materials:
        block = group.block
        entry["materials"].append({
            "label": group.label,
            "texture": group.texture_name,
            "kind": group.kind,
            "faces": len(group.faces),
            "confidence": group.confidence,
            "polflags": block.polflags if block else None,
            "polflags_decoded": block.describe_polflags() if block else None,
            "tracy_mode": block.tracy_mode if block else None,
            "tracy_light": block.tracy_light if block else None,
            "atts_entries": len(block.atts) if block else 0,
            "olpl_groups": len(block.olpl) if block else 0,
            "poly_id_range": (
                [min(e.poly_id for e in block.atts),
                 max(e.poly_id for e in block.atts)]
                if block and block.atts else None
            ),
            "warnings": list(group.warnings),
        })
    if base_obj.embedded:
        entry["embedded_resources"] = [
            {"class": r.class_id, "name": r.resource_name,
             "payload": (r.payload_form_type or r.payload_tag)}
            for r in base_obj.embedded
        ]
    if base_obj.unknown_chunks:
        entry["unknown_chunks"] = list(base_obj.unknown_chunks)
    if fam_obj.kids:
        entry["kids"] = [_object_dict(kid) for kid in fam_obj.kids]
    return entry


def _vertex_histogram(polygons: list[list[int]]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for polygon in polygons:
        key = str(len(polygon))
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: int(kv[0])))


def family_to_dict(family: AssetFamily, diff=None,
                   workbench: dict | None = None) -> dict:
    data: dict = {
        "generator": "OpenNeoUA Studio Asset Assembly Mode",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_file": str(family.base_path) if family.base_path else None,
        "search_roots": list(family.search_roots),
        "objects": [],
        "textures": [],
        "animations": [],
        "manual_overrides": dict(family.overrides),
        "setbas_provider": str(family.setbas_path)
        if family.setbas_path else None,
        "external_palette": str(family.external_palette_path)
        if family.external_palette_path else None,
        "textured_preview_diagnostics": list(family.textured_diagnostics),
        "consistency_checks": [
            {"status": status, "text": text} for status, text in family.checks
        ],
        "warnings": list(family.warnings),
        "format_knowledge": [
            {"confidence": conf, "note": note} for conf, note in FORMAT_NOTES
        ],
    }
    if family.root_object:
        data["objects"].append(_object_dict(family.root_object))

    for name, ref in family.texture_refs.items():
        img = family.textures.get(name)
        tracy_modes = sorted(family.texture_tracy_usage.get(name, set())
                             - {"none"})
        chroma_pixels = 0
        if img is not None and img.has_body:
            chroma_pixels = img.chroma_transparent_count(
                family.external_palette if not img.palette else None
            )
        data["textures"].append({
            "name": name,
            "status": ref.status,
            "source": ref.source or None,
            "path": ref.display_path if ref.found else None,
            "candidates": [str(c) for c in ref.candidates],
            "embedded_candidates": list(ref.embedded_candidates),
            "exists_loose_and_embedded": bool(ref.embedded_available
                                              and ref.candidates),
            "manual_override": family.overrides.get(name),
            "kind": img.kind if img else None,
            "width": img.width if img else None,
            "height": img.height if img else None,
            "planes": img.n_planes if img else None,
            "compression": ("ByteRun1" if img and img.compression else
                            ("none" if img else None)),
            "has_palette": bool(img.palette) if img else None,
            "body_decoded": img.has_body if img else False,
            "tracy_modes_used": tracy_modes,
            "chroma_transparent_pixels": chroma_pixels,
            "transparency_preview": ("chroma+additive" if tracy_modes
                                     else ("chroma" if chroma_pixels else "none")),
            "warnings": list(img.warnings) if img else [],
        })

    for name, ref in family.animation_refs.items():
        anm = family.animations.get(name)
        record: dict = {
            "name": name,
            "status": ref.status,
            "source": ref.source or None,
            "path": ref.display_path if ref.found else None,
            "embedded_candidates": list(ref.embedded_candidates),
        }
        if anm:
            record.update({
                "bitmap_class": anm.bitmap_class,
                "bitmaps": list(anm.bitmap_names),
                "uv_groups": [len(g) for g in anm.texcoord_groups],
                "frames": [
                    {"time_ticks": f.frame_time, "time_ms": round(f.duration_ms, 1),
                     "bitmap": f.frame_id, "uv_group": f.texcoords_id}
                    for f in anm.frames
                ],
                "cycle_ms": round(anm.total_duration_ms, 1),
                "warnings": list(anm.warnings),
            })
        data["animations"].append(record)

    if family.dependencies:
        from base_dependency_resolver import deps_to_dicts, summarize

        data["dependencies"] = {
            "search_root": family.search_root,
            "summary": summarize(family.dependencies),
            "items": deps_to_dicts(family.dependencies),
        }

    if workbench:
        data["polygon_workbench"] = workbench

    if diff is not None:
        from asset_diff import diff_to_dict

        data["source_diff"] = diff_to_dict(diff)

    if family.base_asset and family.base_asset.tree:
        data["chunk_tree"] = family.base_asset.tree.to_dict(preview_bytes=8)
    return data


def _md_object(lines: list[str], entry: dict, depth: int = 0) -> None:
    indent = "  " * depth
    lines.append(f"{indent}- **Object** `{entry.get('name') or '(unnamed)'}`")
    if entry.get("skeleton_reference"):
        lines.append(f"{indent}  - skeleton: `{entry['skeleton_reference']}` -> "
                     f"`{entry.get('skeleton_resolved') or 'NOT FOUND'}`")
    stats = entry.get("skeleton_stats")
    if stats:
        lines.append(f"{indent}  - POO2 {stats['poo2_points']} pts, "
                     f"POL2 {stats['pol2_polygons']} polys, "
                     f"SEN2 {stats['sen2_points']} pts")
        lines.append(f"{indent}  - bounds min {stats['bounds_min']} / "
                     f"max {stats['bounds_max']}")
        lines.append(f"{indent}  - polygon sizes: {stats['polygon_vertex_histogram']}")
    tf = entry.get("transform")
    if tf:
        lines.append(f"{indent}  - transform: pos {tf['position']} "
                     f"scale {tf['scale']} euler {tf['euler_int16']} "
                     f"visLimit {tf['vis_limit']} ambient {tf['ambient_light']}")
    for mat in entry.get("materials", []):
        rng = mat.get("poly_id_range")
        lines.append(f"{indent}  - material `{mat['label']}` [{mat['confidence']}]: "
                     f"{mat['faces']} faces, ATTS {mat['atts_entries']}, "
                     f"OLPL {mat['olpl_groups']}"
                     + (f", polyID {rng[0]}..{rng[1]}" if rng else ""))
        if mat.get("polflags_decoded"):
            lines.append(f"{indent}    - flags: {mat['polflags_decoded']} "
                         f"(raw 0x{mat['polflags']:X})")
        for warning in mat.get("warnings", []):
            lines.append(f"{indent}    - WARNING: {warning}")
    for res in entry.get("embedded_resources", []):
        lines.append(f"{indent}  - embedded: {res['class']} `{res['name']}` "
                     f"({res['payload']})")
    for unknown in entry.get("unknown_chunks", []):
        lines.append(f"{indent}  - unknown chunk: {unknown}")
    for kid in entry.get("kids", []):
        _md_object(lines, kid, depth + 1)


def family_to_markdown(family: AssetFamily, diff=None,
                       workbench: dict | None = None) -> str:
    data = family_to_dict(family, diff, workbench)
    lines: list[str] = []
    lines.append(f"# Asset family report: "
                 f"{Path(data['base_file']).name if data['base_file'] else 'manual'}")
    lines.append("")
    lines.append(f"Generated by OpenNeoUA Studio Asset Assembly Mode, {data['generated_at']}.")
    lines.append("")
    lines.append(f"- Base file: `{data['base_file']}`")
    lines.append("- Search roots:")
    for root in data["search_roots"]:
        lines.append(f"  - `{root}`")
    if data.get("setbas_provider"):
        lines.append(f"- SET.BAS resource provider (read-only): "
                     f"`{data['setbas_provider']}`")
    if data.get("external_palette"):
        lines.append(f"- External palette: `{data['external_palette']}`")
    lines.append("")

    lines.append("## Objects")
    lines.append("")
    for entry in data["objects"]:
        _md_object(lines, entry)
    lines.append("")

    lines.append("## Textures")
    lines.append("")
    lines.append("| name | status | source | size | palette | body | tracy | "
                 "transparency preview | path |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for tex in data["textures"]:
        size = (f"{tex['width']}x{tex['height']}"
                if tex.get("width") is not None else "?")
        tracy = ", ".join(tex.get("tracy_modes_used") or []) or "-"
        lines.append(
            f"| {tex['name']} | {tex['status']} | {tex.get('source') or '-'} "
            f"| {size} "
            f"| {'yes' if tex.get('has_palette') else 'no'} "
            f"| {'decoded' if tex.get('body_decoded') else 'no'} "
            f"| {tracy} | {tex.get('transparency_preview') or 'none'} "
            f"| `{tex.get('path') or '-'}` |"
        )
    lines.append("")

    ambiguous = [t for t in data["textures"] if len(t.get("candidates", [])) > 1]
    if ambiguous:
        lines.append("### Texture candidates (ambiguous references)")
        lines.append("")
        for tex in ambiguous:
            lines.append(f"- `{tex['name']}` -> selected `{tex.get('path')}`")
            for candidate in tex["candidates"]:
                marker = " (selected)" if candidate == tex.get("path") else ""
                lines.append(f"  - `{candidate}`{marker}")
        lines.append("")

    both = [t for t in data["textures"] if t.get("exists_loose_and_embedded")]
    if both:
        lines.append("### Resources present both loose and in SET.BAS")
        lines.append("")
        for tex in both:
            lines.append(f"- `{tex['name']}`: using {tex.get('source')} "
                         f"(`{tex.get('path')}`); embedded alternative: "
                         f"`{', '.join(tex.get('embedded_candidates', []))}`")
        lines.append("")

    if data.get("manual_overrides"):
        lines.append("### Manual session overrides")
        lines.append("")
        for logical, path in data["manual_overrides"].items():
            lines.append(f"- `{logical}` -> `{path}`")
        lines.append("")

    if data.get("textured_preview_diagnostics"):
        lines.append("### Textured preview diagnostics")
        lines.append("")
        for diagnostic in data["textured_preview_diagnostics"]:
            lines.append(f"- {diagnostic}")
        lines.append("")

    if data["animations"]:
        lines.append("## Animations (VANM)")
        lines.append("")
        for anm in data["animations"]:
            lines.append(f"- `{anm['name']}` ({anm['status']}): "
                         f"`{anm.get('path') or 'NOT FOUND'}`")
            if anm.get("frames") is not None:
                lines.append(f"  - bitmaps: {anm.get('bitmaps')}")
                lines.append(f"  - UV groups: {anm.get('uv_groups')} "
                             f"(texture-space bytes /256)")
                for i, frame in enumerate(anm["frames"]):
                    lines.append(f"  - frame {i}: bitmap #{frame['bitmap']}, "
                                 f"UV group #{frame['uv_group']}, "
                                 f"{frame['time_ticks']} ticks "
                                 f"(~{frame['time_ms']} ms)")
                lines.append(f"  - cycle: ~{anm.get('cycle_ms')} ms")
        lines.append("")

    if data.get("dependencies"):
        deps = data["dependencies"]
        lines.append("## Dependencies")
        lines.append("")
        lines.append(f"- search root: `{deps.get('search_root') or '-'}`")
        lines.append("- summary: "
                     + (", ".join(f"{v} {k}"
                                  for k, v in deps["summary"].items())
                        or "none"))
        lines.append("")
        lines.append("| kind | reference | owner | status | resolved / detail |")
        lines.append("|---|---|---|---|---|")
        for item in deps["items"]:
            detail = (item.get("resolved_path")
                      or item.get("error")
                      or (f"{len(item.get('candidates', []))} candidates"
                          if item.get("candidates") else item.get("source")))
            lines.append(f"| {item['kind']} | {item['raw_ref']} "
                         f"| {item.get('owner_node') or '-'} "
                         f"| {item['status']} | `{detail}` |")
        lines.append("")

    if data.get("polygon_workbench"):
        wb = data["polygon_workbench"]
        lines.append("## Polygon Mapping Workbench")
        lines.append("")
        for key, value in wb.items():
            if isinstance(value, list):
                lines.append(f"- {key}:")
                for item in value:
                    lines.append(f"  - {item}")
            else:
                lines.append(f"- {key}: {value}")
        lines.append("")

    if diff is not None:
        from asset_diff import diff_to_markdown_lines

        lines.extend(diff_to_markdown_lines(diff))

    lines.append("## Consistency checks")
    lines.append("")
    for check in data["consistency_checks"]:
        lines.append(f"- [{check['status']}] {check['text']}")
    lines.append("")

    if data["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for warning in data["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Format knowledge used (confidence levels)")
    lines.append("")
    for note in data["format_knowledge"]:
        lines.append(f"- **{note['confidence']}**: {note['note']}")
    lines.append("")
    return "\n".join(lines)


def family_to_json(family: AssetFamily, diff=None,
                   workbench: dict | None = None) -> str:
    return json.dumps(family_to_dict(family, diff, workbench), indent=2)


def save_report(family: AssetFamily, output_path: str | Path,
                diff=None) -> Path:
    """Write a report; format chosen by extension (.md or .json)."""

    path = Path(output_path)
    if path.suffix.lower() == ".json":
        text = family_to_json(family, diff)
    else:
        text = family_to_markdown(family, diff)
    path.write_text(text, encoding="utf-8")
    return path


if __name__ == "__main__":
    import argparse

    from asset_family import load_asset_family

    cli = argparse.ArgumentParser(description="Export an asset family report.")
    cli.add_argument("base_file")
    cli.add_argument("--out", help="output .md or .json path")
    cli.add_argument("--root", action="append", default=[])
    args = cli.parse_args()

    fam = load_asset_family(args.base_file, args.root)
    if args.out:
        target = save_report(fam, args.out)
        print(f"wrote {target}")
    else:
        print(family_to_markdown(fam))
