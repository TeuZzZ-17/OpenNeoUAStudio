"""Dependency model for a .base asset family (UI-free).

Collects every detectable reference of a parsed family — skeletons per
object (including nested KIDS children), textures, tracy textures, VANM
animations and their frame bitmaps, embedded EMRS resources, recursively
decoded particle life-stage materials and unknown chunks — into flat
:class:`AssetDependency` records with
clear statuses:

    auto_loaded        resolved unambiguously and loaded
    kept_for_session   user-chosen (manual override / SET.BAS forced)
    trial_loaded       user-chosen, marked as a trial by the UI
    resolved           present and understood but nothing to load
                       (e.g. embedded EMRS payloads, inline KIDS nodes)
    ambiguous          multiple plausible candidates; NOT auto-loaded
    skipped            user chose to ignore it for this session
    missing            no candidate found anywhere
    unsupported_loader found/parsed but OpenNeoUAStudio has no loader for it
    failed_load        a loader exists but raised; error recorded

Everything is read-only; the collector never touches disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AssetDependency:
    kind: str                      # skeleton | texture | tracy_texture |
                                   # animation | anm_bitmap | embedded |
                                   # child_base | unknown_chunk
    raw_ref: str
    source: str = ""               # block/field that references it
    owner_node: str | None = None  # object path for KIDS context
    resolved_path: Path | None = None
    status: str = "unresolved"
    candidates: list[Path] = field(default_factory=list)
    error: str | None = None
    resolution_source: str = ""
    resolution_rule: str = ""

    def display_path(self) -> str:
        if self.resolved_path is not None:
            return str(self.resolved_path)
        return "-"


def _status_from_ref(family, ref, loaded: bool, name: str) -> tuple[str, str]:
    """(status, error) for a resolver reference + load outcome."""

    error = family.load_errors.get(name, "")
    if ref is None:
        return "missing", ""
    if ref.status == "missing":
        return "missing", error
    if ref.status == "ambiguous" and ref.path is None:
        return "ambiguous", ""
    if not loaded and error:
        return "failed_load", error
    if ref.status in ("manual", "manual (SET.BAS)"):
        return "kept_for_session", ""
    if ref.status in ("found", "setbas", "ambiguous"):
        return ("auto_loaded" if loaded else "failed_load"), error
    return ref.status, error


def _dep_from_ref(family, name: str, kind: str, source: str,
                  owner: str | None, ref, loaded: bool) -> AssetDependency:
    status, error = _status_from_ref(family, ref, loaded, name)
    dep = AssetDependency(kind=kind, raw_ref=name, source=source,
                          owner_node=owner, status=status,
                          error=error or None)
    if ref is not None:
        dep.candidates = list(ref.candidates)
        dep.resolution_source = ref.source
        dep.resolution_rule = getattr(ref, "resolution_rule", "")
        if ref.found and ref.path is not None:
            dep.resolved_path = ref.path
        elif ref.found:
            dep.resolved_path = None
            dep.source += f" [{ref.display_path}]"
    return dep


def collect_dependencies(family) -> list[AssetDependency]:
    """Build the flat dependency list for a loaded family."""

    deps: list[AssetDependency] = []
    seen_textures: set[str] = set()
    seen_anims: set[str] = set()

    def iter_visual_blocks(block, source: str):
        yield block, source
        for stage_index, stage in enumerate(block.particle_stages):
            yield from iter_visual_blocks(
                stage, f"{source} PTCL stage #{stage_index}")

    def walk(fam_obj, label: str) -> None:
        base_obj = fam_obj.base_object

        if base_obj.skeleton_name:
            deps.append(_dep_from_ref(
                family, base_obj.skeleton_name, "skeleton",
                "OBJT sklt.class NAME", label,
                fam_obj.skeleton_ref, fam_obj.skeleton is not None,
            ))

        for block_index, root_block in enumerate(base_obj.ades):
            for block, source in iter_visual_blocks(
                    root_block, f"ADES block #{block_index}"):
                for tex, tex_kind in (
                        (block.texture, "texture"),
                        (block.tracy_texture, "tracy_texture")):
                    if tex is None or not tex.name:
                        continue
                    if tex.kind == "bmpanim":
                        if tex.name not in seen_anims:
                            seen_anims.add(tex.name)
                            ref = family.animation_refs.get(tex.name)
                            deps.append(_dep_from_ref(
                                family, tex.name, "animation",
                                f"{source} BANI", label,
                                ref, tex.name in family.animations,
                            ))
                    elif tex.name not in seen_textures:
                        seen_textures.add(tex.name)
                        ref = family.texture_refs.get(tex.name)
                        deps.append(_dep_from_ref(
                            family, tex.name, tex_kind,
                            f"{source} CIBO NAM2", label,
                            ref, tex.name in family.textures,
                        ))

        for res in base_obj.embedded:
            supported = res.class_id.lower() in (
                "ilbm.class", "sklt.class", "bmpanim.class"
            )
            has_payload = res.payload_size > 0
            if not has_payload and family.setbas_archive is not None:
                has_payload = any(
                    candidate.decodable for candidate in
                    family.setbas_archive.find(
                        res.resource_name, res.class_id)
                )
            deps.append(AssetDependency(
                kind="embedded", raw_ref=res.resource_name,
                source=f"EMBD EMRS {res.class_id} "
                       f"({res.payload_form_type or res.payload_tag})",
                owner_node=label,
                status=("resolved" if supported and has_payload else
                        "missing" if supported else "unsupported_loader"),
                resolution_source=(
                    "inline BASE" if res.payload_size > 0 else
                    "SET.BAS" if has_payload else ""),
                resolution_rule=(
                    "embedded EMRS payload in the owning BASE"
                    if res.payload_size > 0 else
                    "unique attached archive payload"
                    if has_payload else
                    "embedded reference has no available payload"),
            ))

        for unknown in base_obj.unknown_chunks:
            deps.append(AssetDependency(
                kind="unknown_chunk", raw_ref=unknown,
                source="FORM BASE child", owner_node=label,
                status="unsupported_loader",
                resolution_source="inline BASE",
                resolution_rule=(
                    "unknown chunk retained by the conservative BASE parser"),
            ))

        for kid_index, kid in enumerate(fam_obj.kids):
            kid_label = f"{label}/kid[{kid_index}]"
            deps.append(AssetDependency(
                kind="child_base",
                raw_ref=(kid.base_object.skeleton_name
                         or kid.base_object.name or f"kid[{kid_index}]"),
                source="FORM KIDS (inline child base object)",
                owner_node=label,
                status="resolved",
                resolution_source="inline BASE",
                resolution_rule="ordered FORM KIDS child object",
            ))
            walk(kid, kid_label)

    if family.root_object is not None:
        walk(family.root_object, "root")

    # VANM frame bitmaps resolved family-wide
    for anm_name, anm in family.animations.items():
        for bitmap_name in anm.bitmap_names:
            if bitmap_name in seen_textures:
                continue
            seen_textures.add(bitmap_name)
            ref = family.texture_refs.get(bitmap_name)
            deps.append(_dep_from_ref(
                family, bitmap_name, "anm_bitmap",
                f"VANM {anm_name} bitmap list", "family",
                ref, bitmap_name in family.textures,
            ))

    palette_ref = getattr(family, "external_palette_ref", None)
    if palette_ref is not None:
        deps.append(_dep_from_ref(
            family, palette_ref.logical_name, "palette",
            "Retail indexed SET profile", "family", palette_ref,
            getattr(family, "external_palette", None) is not None,
        ))
    for profile_name, logical_name in (
            ("shader", "REMAP/SHADERMP.ILB"),
            ("tracy", "REMAP/TRACYRMP.ILB")):
        ref = getattr(family, "indexed_profile_refs", {}).get(profile_name)
        if ref is not None:
            deps.append(_dep_from_ref(
                family, logical_name,
                "shader_remap" if profile_name == "shader"
                else "tracy_remap",
                "Retail indexed SET profile", "family", ref,
                ref.path is not None,
            ))

    return deps


def summarize(deps: list[AssetDependency]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for dep in deps:
        counts[dep.status] = counts.get(dep.status, 0) + 1
    return dict(sorted(counts.items()))


def print_report(family) -> None:
    deps = family.dependencies
    print(f"root base:   {family.base_path}")
    print(f"search root: {family.search_root}")
    counts = summarize(deps)
    print(f"dependencies: {len(deps)} total "
          f"({', '.join(f'{v} {k}' for k, v in counts.items()) or 'none'})")
    for dep in deps:
        owner = f" [{dep.owner_node}]" if dep.owner_node else ""
        print(f"  [{dep.status:18}] {dep.kind:14} {dep.raw_ref}{owner}")
        if dep.resolved_path:
            print(f"      -> {dep.resolved_path}")
        for candidate in dep.candidates if dep.status == "ambiguous" else []:
            print(f"      ? {candidate}")
        if dep.error:
            print(f"      ! {dep.error}")


def deps_to_dicts(deps: list[AssetDependency]) -> list[dict]:
    return [
        {
            "kind": d.kind,
            "raw_ref": d.raw_ref,
            "source": d.source,
            "owner_node": d.owner_node,
            "status": d.status,
            "resolved_path": str(d.resolved_path) if d.resolved_path else None,
            "candidates": [str(c) for c in d.candidates],
            "error": d.error,
            "resolution_source": d.resolution_source,
            "resolution_rule": d.resolution_rule,
        }
        for d in deps
    ]
