"""Complete Urban Assault asset families and their portable package workflow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import copy
import hashlib
import json
from pathlib import Path
import struct
from typing import Iterable

from studio.exchange.portable import (
    AssetFamilyPackageError,
    AssetResolver,
    DirectoryIndex,
    find_existing_path,
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    normalize_logical_name,
    ResolvedFile,
    same_path,
)
from studio.formats.animation import parse_anm_file, VanmData
from studio.formats.base_format import (
    AmeshBlock, AttsEntry, BaseAsset, BaseObject, TextureRef, parse_base_file,
)
from studio.formats.images import IlbmImage, Palette, parse_ilbm_file, parse_pal_file
from studio.formats.setbas import (
    decode_animation,
    decode_skeleton,
    decode_texture,
    read_setbas,
    SetBasArchive,
    SetBasError,
)
from studio.formats.sklt import parse_sklt_file, sen2_points_for_poo2, SkltModel

# ----------------------------------------------------------------------------
# Assembles a complete Urban Assault asset family from a .base file.
# (formerly asset_family.py)
# ----------------------------------------------------------------------------

# override values with this prefix select an embedded SET.BAS resource
SETBAS_OVERRIDE_PREFIX = "setbas:"


@dataclass
class MaterialGroup:
    """Polygons of one skeleton drawn with one texture/material block."""

    label: str                       # texture name or synthetic label
    texture_name: str = ""
    kind: str = ""                   # "ilbm" | "bmpanim" | other
    block: AmeshBlock | None = None
    # Per entry: (skeleton polygon index, UV list in polygon vertex order,
    # shade value 0..255).  UVs are texture-space 0..255 bytes; the runtime
    # divides by 256 (CONFIRMED, amesh.cpp).
    faces: list[tuple[int, list[tuple[int, int]], int]] = field(default_factory=list)
    confidence: str = "CONFIRMED"
    warnings: list[str] = field(default_factory=list)


@dataclass
class FamilyObject:
    """One base.class object with its resolved skeleton and materials."""

    base_object: BaseObject
    skeleton_ref: ResolvedFile | None = None
    skeleton: SkltModel | None = None
    materials: list[MaterialGroup] = field(default_factory=list)
    kids: list["FamilyObject"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Stable object identity used by tree/viewport selection ("root",
    # "root/kid[3]", "root/kid[3]/kid[0]", ...)
    owner_path: str = "root"

    @property
    def display_name(self) -> str:
        skel = self.base_object.skeleton_name
        if skel:
            return skel.replace("\\", "/").rsplit("/", 1)[-1]
        return self.base_object.name or self.owner_path

    def iter_tree(self):
        yield self
        for kid in self.kids:
            yield from kid.iter_tree()


@dataclass
class AssetFamily:
    base_path: Path | None = None
    base_asset: BaseAsset | None = None
    root_object: FamilyObject | None = None
    textures: dict[str, IlbmImage] = field(default_factory=dict)
    texture_refs: dict[str, ResolvedFile] = field(default_factory=dict)
    animations: dict[str, VanmData] = field(default_factory=dict)
    animation_refs: dict[str, ResolvedFile] = field(default_factory=dict)
    # logical texture name -> set of tracy modes of the blocks using it
    texture_tracy_usage: dict[str, set] = field(default_factory=dict)
    # Modern OpenNeoUA remaps FX1/FX2/FX3 to the set's HI/ALPHA PNGs when
    # destination blending is available.  Paths are keyed by lowercase
    # logical bitmap name and consumed only by flat-tracy preview materials.
    effect_override_paths: dict[str, Path] = field(default_factory=dict)
    # session-only manual resolver overrides actually in effect
    overrides: dict[str, str] = field(default_factory=dict)
    # optional embedded resource provider (read-only SET.BAS archive)
    setbas_archive: SetBasArchive | None = None
    setbas_path: Path | None = None
    # logical name (lower) -> True when the user forced the SET.BAS source
    setbas_overrides: dict[str, bool] = field(default_factory=dict)
    external_palette: Palette | None = None
    external_palette_path: Path | None = None
    external_palette_ref: ResolvedFile | None = None
    # Canonical same-root SET profile resolution used by renderer, package
    # export and dependency provenance.
    indexed_profile_refs: dict[str, ResolvedFile] = field(default_factory=dict)
    search_roots: list[str] = field(default_factory=list)
    search_root: str = ""            # primary root (the .base parent dir)
    warnings: list[str] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)  # (status, text)
    # human-readable reasons why the textured preview is incomplete
    textured_diagnostics: list[str] = field(default_factory=list)
    # logical name -> load error message (for failed_load dependency status)
    load_errors: dict[str, str] = field(default_factory=dict)
    # flat dependency records (base_dependency_resolver.AssetDependency)
    dependencies: list = field(default_factory=list)

    def all_objects(self) -> list[FamilyObject]:
        return list(self.root_object.iter_tree()) if self.root_object else []

    def check(self, ok: bool | None, text: str) -> None:
        status = "OK" if ok else ("WARN" if ok is None else "FAIL")
        self.checks.append((status, text))

    def diag(self, text: str) -> None:
        if text not in self.textured_diagnostics:
            self.textured_diagnostics.append(text)


def _setbas_matches(family: AssetFamily, name: str,
                    class_id: str) -> list:
    """All matching embedded resources; callers must handle ambiguity."""

    if family.setbas_archive is None:
        return []
    return family.setbas_archive.find(name, class_id)


def _setbas_find(family: AssetFamily, name: str, class_id: str):
    """The unique matching embedded resource, never an arbitrary first."""

    matches = _setbas_matches(family, name, class_id)
    if len(matches) > 1:
        warning = (
            f"{name}: {len(matches)} embedded resources match in "
            f"{family.setbas_archive.path.name}; none was selected."
        )
        if warning not in family.warnings:
            family.warnings.append(warning)
        return None
    return matches[0] if len(matches) == 1 else None


def _wants_setbas(family: AssetFamily, name: str) -> bool:
    key = normalize_logical_name(name).lower()
    if family.setbas_overrides.get(key):
        return True
    bare = key.rsplit("/", 1)[-1]
    return bool(family.setbas_overrides.get(bare))


def _is_setbas_family(family: AssetFamily) -> bool:
    """True when the family is the SET.BAS archive itself, not a loose BASE."""

    return (family.base_path is not None and family.setbas_path is not None
            and same_path(Path(family.base_path), Path(family.setbas_path)))


def _only_foreign_model_files(family: AssetFamily, ref: ResolvedFile) -> bool:
    """True when every loose match lies in another model's folder under Models/Base."""

    if _is_setbas_family(family) or family.base_path is None:
        return False
    own = Path(family.base_path).resolve().parent
    container = next((p for p in own.parents
                      if p.name.casefold() == "base"
                      and p.parent.name.casefold() == "models"), None)
    if container is None:
        return False
    found = [Path(ref.path)] if ref.path is not None else list(ref.candidates)
    if not found:
        return False
    for item in found:
        try:
            resolved = Path(item).resolve()
        except OSError:
            return False
        inside_own = own == resolved.parent or own in resolved.parents
        inside_container = container in resolved.parents
        if inside_own or not inside_container:
            return False
    return True


def _resolve_with_setbas(family: AssetFamily, resolver: AssetResolver,
                         name: str, kind: str, class_id: str) -> ResolvedFile:
    """Three-way resolution: manual override > loose file > SET.BAS embedded.

    Mirrors the engine's precedence (loose overrides embedded,
    embed.cpp/IsSetLooseEmrsOverrideClass) with the extra user-forced
    "setbas:" override on top.
    """

    embedded_matches = _setbas_matches(family, name, class_id)
    embedded = embedded_matches[0] if len(embedded_matches) == 1 else None
    embedded_labels = [
        f"SET.BAS:{resource.resource_name} (EMRS #{resource.index})"
        for resource in embedded_matches]

    if _wants_setbas(family, name):
        loose = resolver.resolve(name, kind)
        if embedded is not None:
            ref = ResolvedFile(
                logical_name=name, status="manual (SET.BAS)",
                source="SET.BAS",
                resolution_rule="explicit unique SET.BAS override")
        elif embedded_matches:
            ref = ResolvedFile(
                logical_name=name, status="ambiguous", source="SET.BAS",
                resolution_rule=(
                    "explicit SET.BAS override has multiple embedded matches"))
        else:
            ref = ResolvedFile(
                logical_name=name, status="missing", source="SET.BAS",
                resolution_rule=(
                    "explicit SET.BAS override has no embedded match"))
        ref.embedded_available = bool(embedded_matches)
        ref.embedded_candidates = embedded_labels
        ref.candidates = loose.candidates
        return ref

    ref = resolver.resolve(name, kind)
    if embedded_matches:
        ref.embedded_available = True
        ref.embedded_candidates = embedded_labels

    # A loose BASE only sees its own folder and the SET.BAS: same-named files
    # inside other models' folders are not engine search paths, so they must
    # not hide the embedded copy (or be taken in its place).
    if (embedded is not None and ref.status != "manual"
            and _only_foreign_model_files(family, ref)):
        ref.path = None
        ref.status = "setbas"
        ref.source = "SET.BAS"
        ref.resolution_rule = ("unique embedded copy: the loose files found are "
                               "in other models' folders, not engine search paths")
        return ref

    if ref.path is not None:
        ref.source = "manual" if ref.status == "manual" else "loose"
        if embedded is not None and ref.status != "manual":
            family.warnings.append(
                f"{name}: exists both loose ({ref.path}) and embedded in "
                f"{family.setbas_archive.path.name}; loose wins (engine "
                "precedence). Use Asset Dependencies to compare sources."
            )
    elif ref.status == "ambiguous" and not (
            embedded is not None and _is_setbas_family(family) and not any(
                part.casefold() == "loose"
                for candidate in ref.candidates for part in candidate.parts)):
        # A plausible loose override must not be bypassed by silently using
        # SET.BAS; the user first has to disambiguate the higher-priority
        # loose source. For SET.BAS itself the engine only replaces resources
        # from SetN/Loose, so same-named files elsewhere in the set (vanilla
        # HI/ALPHA, BETA, GAMMA FX textures) fall through to the embedded copy.
        pass
    elif embedded is not None:
        ref.status = "setbas"
        ref.source = "SET.BAS"
        ref.resolution_rule = "unique embedded fallback after loose miss"
    elif embedded_matches:
        ref.status = "ambiguous"
        ref.source = "SET.BAS"
        ref.resolution_rule = "multiple embedded fallback candidates"
        _setbas_find(family, name, class_id)  # emit the visible warning once
    return ref


def _load_texture(family: AssetFamily, resolver: AssetResolver, name: str) -> None:
    if not name or name in family.texture_refs:
        return
    ref = _resolve_with_setbas(family, resolver, name, "texture", "ilbm.class")
    # Engine order for an external BASE: ILBM beside the BASE, then a PNG with the same
    # name, then SET.BAS. The FX1/FX2/FX3 names are remapped by the engine and skip this.
    if (ref.path is None and ref.status in ("missing", "setbas")
            and not _is_setbas_family(family)
            and name.casefold() not in ("fx1.ilbm", "fx2.ilbm", "fx3.ilbm")):
        png = resolver.resolve(name.rsplit(".", 1)[0] + ".PNG", "texture")
        if png.path is not None and png.status in ("found", "manual"):
            png.logical_name = name
            png.resolution_rule = "PNG fallback: no ILBM beside the BASE (engine order)"
            ref = png
    family.texture_refs[name] = ref

    if ref.status in ("setbas", "manual (SET.BAS)"):
        embedded = _setbas_find(family, name, "ilbm.class")
        try:
            img = decode_texture(family.setbas_archive, embedded)
            family.textures[name] = img
            family.warnings.extend(f"{name}: {w}" for w in img.warnings)
        except Exception as exc:
            family.warnings.append(
                f"Texture {name} (SET.BAS embedded) failed to decode: {exc}"
            )
        return

    if ref.status == "ambiguous" and ref.path is None:
        family.warnings.append(
            f"Texture {name}: {len(ref.candidates)} plausible candidates - "
            "not auto-loaded (same-named ILBMs from other themes would look "
            "wrong). Trial-load one in Asset Dependencies."
        )
        return
    if ref.path is None:
        where = ("in the search roots or the SET.BAS archive"
                 if family.setbas_archive is not None else "in the search roots")
        family.warnings.append(f"Texture not found {where}: {name}")
        return
    try:
        img = parse_ilbm_file(ref.path)
        family.textures[name] = img
        family.warnings.extend(f"{name}: {w}" for w in img.warnings)
    except Exception as exc:  # defensive: never crash the family load
        family.load_errors[name] = str(exc)
        family.warnings.append(f"Texture {name} failed to decode: {exc}")


def _load_animation(family: AssetFamily, resolver: AssetResolver, name: str) -> None:
    if not name or name in family.animation_refs:
        return
    ref = _resolve_with_setbas(family, resolver, name, "animation",
                               "bmpanim.class")
    family.animation_refs[name] = ref

    anm = None
    if ref.status in ("setbas", "manual (SET.BAS)"):
        embedded = _setbas_find(family, name, "bmpanim.class")
        try:
            anm = decode_animation(family.setbas_archive, embedded)
        except Exception as exc:
            family.warnings.append(
                f"Animation {name} (SET.BAS embedded) failed to parse: {exc}"
            )
            return
    elif ref.status == "ambiguous" and ref.path is None:
        family.warnings.append(
            f"Animation {name}: {len(ref.candidates)} plausible candidates - "
            "not auto-loaded. Trial-load one in Asset Dependencies."
        )
        return
    elif ref.path is None:
        where = ("in the search roots or the SET.BAS archive"
                 if family.setbas_archive is not None else "in the search roots")
        family.warnings.append(f"Animation not found {where}: {name}")
        return
    else:
        try:
            anm = parse_anm_file(ref.path)
        except Exception as exc:
            family.load_errors[name] = str(exc)
            family.warnings.append(f"Animation {name} failed to parse: {exc}")
            return

    family.animations[name] = anm
    family.warnings.extend(f"{name}: {w}" for w in anm.warnings)
    for bitmap_name in anm.bitmap_names:
        _load_texture(family, resolver, bitmap_name)


def _build_materials(fam_obj: FamilyObject, family: AssetFamily) -> None:
    skeleton = fam_obj.skeleton
    poly_count = len(skeleton.polygons) if skeleton else 0
    parsed_polys = skeleton.polygons if skeleton else []

    for index, block in enumerate(fam_obj.base_object.ades):
        tex_name = block.texture.name if block.texture else ""
        kind = block.texture.kind if block.texture else ""
        label = tex_name or f"{block.class_id or 'material'} #{index}"
        group = MaterialGroup(label=label, texture_name=tex_name,
                              kind=kind, block=block)

        if not block.atts:
            group.confidence = "UNKNOWN"
            group.warnings.append("Block has no decoded ATTS entries.")
            fam_obj.materials.append(group)
            continue

        for i, entry in enumerate(block.atts):
            uvs = block.olpl[i] if i < len(block.olpl) else []
            if skeleton and not (0 <= entry.poly_id < poly_count):
                group.warnings.append(
                    f"ATTS entry {i} references polygon {entry.poly_id}, but the "
                    f"skeleton has {poly_count} polygons; entry skipped."
                )
                continue
            if skeleton and uvs and entry.poly_id < len(parsed_polys):
                nvert = len(parsed_polys[entry.poly_id])
                if len(uvs) != nvert:
                    group.warnings.append(
                        f"ATTS entry {i}: OLPL group has {len(uvs)} UVs but "
                        f"polygon {entry.poly_id} has {nvert} vertices."
                    )
            group.faces.append((entry.poly_id, uvs, entry.shade_val))

        fam_obj.materials.append(group)
        family.warnings.extend(f"{label}: {w}" for w in group.warnings)


def family_object_has_animation(family: AssetFamily, obj: FamilyObject) -> bool:
    """Use the same animation availability rule in both resource browsers."""
    for child in obj.iter_tree():
        for block in child.base_object.ades:
            a = block.particle_attributes
            if (a is not None and a.birth_rate > 0 and a.lifetime > 0
                    and a.context_stop_generation > a.context_start_generation
                    and any(s.texture for s in block.particle_stages)):
                return True
            if block.class_id == "particle.class":
                continue
            for stage in block.iter_visual_blocks():
                texture = stage.texture
                animation = (family.animations.get(texture.name)
                             if texture and texture.kind == "bmpanim" else None)
                if animation is not None and animation.frames:
                    return True
    return False


def animation_preview_family(family: AssetFamily, name: str) -> AssetFamily:
    """Show an unassigned VANM on a preview-only quad using its own UVs."""
    animation = family.animations[name]
    if (not animation.frames or not animation.texcoord_groups
            or any(not 0 <= frame.frame_id < len(animation.bitmap_names)
                   or not 0 <= frame.texcoords_id < len(animation.texcoord_groups)
                   for frame in animation.frames)):
        raise ValueError(f"{name}: animation has no complete frames or UV groups.")
    preview = copy.copy(family)
    preview.base_path = None
    preview.base_asset = None
    preview.warnings = []
    preview.textured_diagnostics = []
    preview.checks = []
    texture = TextureRef(class_id="bmpanim.class", kind="bmpanim", name=name)
    block = AmeshBlock(
        class_id="area.class", polflags=0x7A,
        texture=texture, atts=[AttsEntry(0, 0, 0, 0, 0)])
    uv = animation.texcoord_groups[animation.frames[0].texcoords_id]
    if len(uv) not in (3, 4):
        raise ValueError(f"{name}: standalone preview requires 3 or 4 UV corners.")
    width = max((u for u, _ in uv), default=255) - min(
        (u for u, _ in uv), default=0)
    height = max((v for _, v in uv), default=255) - min(
        (v for _, v in uv), default=0)
    aspect = max(1, width) / max(1, height)
    skeleton = SkltModel(source_name=name)
    skeleton.points = [(-aspect, -1.0, 0.0), (-aspect, 1.0, 0.0),
                       (aspect, 1.0, 0.0), (aspect, -1.0, 0.0)]
    skeleton.polygons = [list(range(len(uv)))]
    obj = FamilyObject(BaseObject(name=name, ades=[block]), skeleton=skeleton)
    preview.root_object = obj
    _build_materials(obj, preview)
    return preview


def rebuild_materials(fam_obj: FamilyObject, family: AssetFamily) -> None:
    """Rebind MaterialGroups after an in-memory ADES grow/shrink operation."""

    warning_count = len(family.warnings)
    fam_obj.materials.clear()
    _build_materials(fam_obj, family)
    # Runtime editing should not duplicate load-time diagnostics each time a
    # topology command is applied, undone or redone.
    del family.warnings[warning_count:]


def _load_family_object(base_obj: BaseObject, family: AssetFamily,
                        resolver: AssetResolver,
                        owner_path: str = "root") -> FamilyObject:
    fam_obj = FamilyObject(base_object=base_obj, owner_path=owner_path)

    if base_obj.skeleton_name:
        ref = _resolve_with_setbas(family, resolver, base_obj.skeleton_name,
                                   "skeleton", "sklt.class")
        fam_obj.skeleton_ref = ref
        if ref.status in ("setbas", "manual (SET.BAS)"):
            embedded = _setbas_find(family, base_obj.skeleton_name, "sklt.class")
            try:
                fam_obj.skeleton = decode_skeleton(family.setbas_archive,
                                                   embedded)
                family.warnings.extend(
                    f"{embedded.resource_name}: {w}"
                    for w in fam_obj.skeleton.warnings
                )
            except Exception as exc:
                family.warnings.append(
                    f"Skeleton {base_obj.skeleton_name} (SET.BAS embedded) "
                    f"failed to parse: {exc}"
                )
        elif ref.status == "ambiguous" and ref.path is None:
            family.warnings.append(
                f"Skeleton {base_obj.skeleton_name}: "
                f"{len(ref.candidates)} plausible candidates - not "
                "auto-loaded. Trial-load one in Asset Dependencies."
            )
        elif ref.path is None:
            where = ("in the search roots or the SET.BAS archive"
                     if family.setbas_archive is not None
                     else "in the search roots")
            family.warnings.append(
                f"Skeleton not found {where}: {base_obj.skeleton_name}"
            )
        else:
            try:
                fam_obj.skeleton = parse_sklt_file(ref.path)
                family.warnings.extend(
                    f"{ref.path.name}: {w}" for w in fam_obj.skeleton.warnings
                )
            except Exception as exc:
                family.load_errors[base_obj.skeleton_name] = str(exc)
                family.warnings.append(
                    f"Skeleton {base_obj.skeleton_name} failed to parse: {exc}"
                )

    for root_block in base_obj.ades:
        for block in root_block.iter_visual_blocks():
            for tex in (block.texture, block.tracy_texture):
                if tex is None or not tex.name:
                    continue
                if tex.kind == "bmpanim":
                    _load_animation(family, resolver, tex.name)
                else:
                    _load_texture(family, resolver, tex.name)
                family.texture_tracy_usage.setdefault(tex.name, set()).add(
                    block.tracy_mode
                )
                if block.tracy_mode == "mapped":
                    family.diag(
                        f"{tex.name}: tracy 'mapped' present but not previewed "
                        "yet (second-texture transparency is not "
                        "implemented)."
                    )

    _build_materials(fam_obj, family)

    for index, kid in enumerate(base_obj.kids):
        fam_obj.kids.append(_load_family_object(
            kid, family, resolver, f"{owner_path}/kid[{index}]"
        ))
    return fam_obj


def _find_external_palette(family: AssetFamily, resolver: AssetResolver) -> None:
    needs_palette = any(
        img.pixels is not None and img.palette is None
        for img in family.textures.values()
    )
    # The Retail indexed renderer needs the SET palette even when every ILBM
    # carries its own CMAP.  Resolve the package/SET-local PALETTE path first
    # so a broader compatibility root cannot silently supply another SET's
    # profile.
    palette_names = ("PALETTE/NORMAL.PAL", "PALETTE/STANDARD.PAL")
    selected_ref = None

    # Explicit session overrides remain authoritative.  Otherwise evaluate
    # NORMAL/STANDARD as aliases within each root before considering the next
    # root, so a later Set's NORMAL.PAL cannot beat this Set's STANDARD.PAL.
    override_refs = [
        resolver.resolve(name, "palette") for name in palette_names]
    explicit_refs = [
        ref for ref in override_refs
        if ref.resolution_rule.startswith("explicit session override")]
    if explicit_refs:
        selected_ref = explicit_refs[0]
    else:
        for root in resolver.roots:
            scoped = AssetResolver([root])
            refs = [scoped.resolve(name, "palette")
                    for name in palette_names]
            selected_ref = None
            for ref in refs:
                if ref.path is not None:
                    selected_ref = ref
                    break
                if ref.status == "ambiguous":
                    family.warnings.append(
                        f"Palette {ref.logical_name} is ambiguous inside "
                        f"{root}; none was selected.")
                    family.external_palette_ref = ref
                    return
            if selected_ref is not None:
                break

    if selected_ref is not None:
        if selected_ref.path is None:
            family.external_palette_ref = selected_ref
            family.warnings.append(
                f"Palette override {selected_ref.logical_name} is missing; "
                "none was selected.")
            return
        palette = parse_pal_file(selected_ref.path)
        if palette:
            family.external_palette = palette
            family.external_palette_path = selected_ref.path
            family.external_palette_ref = selected_ref
            profile_root = (
                selected_ref.source_root
                or selected_ref.path.parent.parent)
            if profile_root.name.casefold() == "palette":
                profile_root = profile_root.parent
            profile_resolver = AssetResolver([profile_root])
            family.indexed_profile_refs = {
                "shader": profile_resolver.resolve(
                    "REMAP/SHADERMP.ILB", "texture"),
                "tracy": profile_resolver.resolve(
                    "REMAP/TRACYRMP.ILB", "texture"),
            }
            family.warnings.append(
                f"Using external palette {selected_ref.path.name} for "
                "textures and Retail indexed rendering."
            )
            return
    if needs_palette:
        family.warnings.append(
            "Some textures have no CMAP and no external .PAL was found; "
            "grayscale preview will be used."
        )


def _run_checks(family: AssetFamily) -> None:
    for fam_obj in family.all_objects():
        base_obj = fam_obj.base_object
        name = base_obj.skeleton_name or base_obj.name or "<object>"
        skeleton = fam_obj.skeleton

        if base_obj.skeleton_name:
            family.check(fam_obj.skeleton_ref is not None
                         and fam_obj.skeleton_ref.found,
                         f"Skeleton resolved: {base_obj.skeleton_name}")
        if skeleton is None:
            continue

        poly_count = skeleton.parsed_polygon_count
        atts_total = sum(len(b.atts) for b in base_obj.ades)
        covered = set()
        for block in base_obj.ades:
            covered.update(e.poly_id for e in block.atts)

        # OLPL exists on disk only for amesh.class blocks; area.class blocks
        # take UVs from the texture's OTL2 outline or from the VANM frame at
        # runtime, so they are excluded from the OLPL/ATTS comparison.
        amesh_blocks = [b for b in base_obj.ades
                        if (b.class_id or "").lower() == "amesh.class"]
        amesh_atts = sum(len(b.atts) for b in amesh_blocks)
        amesh_olpl = sum(len(b.olpl) for b in amesh_blocks)
        area_blocks = [
            b for b in base_obj.ades
            if (b.class_id or "").lower() == "area.class"]
        particle_blocks = [
            b for b in base_obj.ades
            if (b.class_id or "").lower() == "particle.class"]

        family.check(True, f"{name}: POL2 polygons = {poly_count}, "
                           f"POO2 points = {len(skeleton.points)}, "
                           f"SEN2 points = {len(skeleton.sensors)}")
        if not atts_total:
            family.check(None, f"{name}: total ATTS entries {atts_total} "
                               f"vs POL2 {poly_count}")
        elif atts_total == poly_count:
            family.check(True, f"{name}: total ATTS entries {atts_total} "
                               f"vs POL2 {poly_count}")
        elif atts_total < poly_count:
            # Shipped assets exist with fewer ATTS entries than skeleton
            # polygons (e.g. ST_FLAK1 #77 and several expansion VP_*).  The
            # engine iterates the ATTS list, so unmapped polygons are simply
            # never drawn - a data property, not a loader error.
            family.check(None,
                         f"{name}: total ATTS entries {atts_total} vs POL2 "
                         f"{poly_count} ({poly_count - atts_total} unmapped "
                         "polygons shipped in the original asset; the engine "
                         "draws only mapped faces)")
        else:
            family.check(False,
                         f"{name}: total ATTS entries {atts_total} vs POL2 "
                         f"{poly_count} (more entries than polygons)")
        if amesh_blocks:
            if amesh_olpl == amesh_atts:
                family.check(True, f"{name}: amesh OLPL groups {amesh_olpl} "
                                   f"vs amesh ATTS {amesh_atts}")
            else:
                # Shipped assets exist with empty/short OLPL chunks (the VP_*
                # vis-protos inside SET.BAS).  The engine's chunk reads clamp
                # at the boundary and return count 0 (fsmgr readS16B inits
                # val = 0), so those faces get no valid UVs in-game either -
                # a data quirk of the original files, not a loader error.
                family.check(None,
                             f"{name}: amesh OLPL groups {amesh_olpl} vs "
                             f"ATTS {amesh_atts} (short/empty OLPL chunk "
                             "shipped in the original asset; the engine "
                             "reads zero-count groups, so these faces have "
                             "no valid UVs)")
        if area_blocks:
            family.check(True,
                         f"{name}: {len(area_blocks)} area.class block(s) use "
                         "texture-outline/VANM UVs (no OLPL chunk by design)")
        if particle_blocks:
            stage_count = sum(
                len(block.particle_stages) for block in particle_blocks)
            family.check(
                True,
                f"{name}: {len(particle_blocks)} particle.class emitter(s), "
                f"{stage_count} nested visual life stage(s) decoded")
        if atts_total:
            family.check(
                len(covered) == atts_total,
                f"{name}: ATTS polyIDs unique ({len(covered)} unique of {atts_total})",
            )
            missing = poly_count - len(covered & set(range(poly_count)))
            family.check(
                missing == 0 if atts_total >= poly_count else None,
                f"{name}: polygons without material assignment: {missing}",
            )

    for tex_name, ref in family.texture_refs.items():
        if ref.status == "ambiguous" and ref.path is None:
            family.check(None, f"Texture ambiguous (not auto-loaded): "
                               f"{tex_name} "
                               f"({len(ref.candidates)} candidates)")
        else:
            family.check(ref.found, f"Texture resolved: {tex_name}")
        img = family.textures.get(tex_name)
        if img is not None:
            family.check(img.has_body or img.is_palette_only,
                         f"Texture decoded: {tex_name} "
                         f"({img.kind} {img.width}x{img.height})")
    for anm_name, ref in family.animation_refs.items():
        if ref.status == "ambiguous" and ref.path is None:
            family.check(None, f"Animation ambiguous (not auto-loaded): "
                               f"{anm_name}")
        else:
            family.check(ref.found, f"Animation resolved: {anm_name}")


def _propagate_anm_tracy_usage(family: AssetFamily) -> None:
    """VANM bitmaps inherit the tracy modes of the blocks using the ANM."""

    for anm_name, anm in family.animations.items():
        modes = family.texture_tracy_usage.get(anm_name)
        if not modes:
            continue
        for bitmap_name in anm.bitmap_names:
            family.texture_tracy_usage.setdefault(bitmap_name, set()).update(modes)


def _find_engine_effect_overrides(family: AssetFamily) -> None:
    """Locate the same HI/ALPHA FX PNG overrides used by modern OpenNeoUA."""

    effect_names = {
        name for name in family.textures
        if Path(name.replace("\\", "/")).name.lower()
        in ("fx1.ilbm", "fx2.ilbm", "fx3.ilbm")
    }
    if not effect_names:
        return

    roots: list[Path] = []
    for value in ([family.base_path, family.setbas_path]
                  + [Path(p) for p in family.search_roots]):
        if value is None:
            continue
        root = Path(value)
        if root.is_file():
            root = root.parent
        for candidate in (root, *list(root.parents)[:5]):
            if candidate not in roots:
                roots.append(candidate)

    for logical_name in effect_names:
        stem = Path(logical_name.replace("\\", "/")).stem
        for root in roots:
            override = find_existing_path(root, "HI", "ALPHA", f"{stem}.PNG")
            if override is not None and override.is_file():
                family.effect_override_paths[logical_name.lower()] = override
                break


def _collect_textured_diagnostics(family: AssetFamily) -> None:
    """Explain, in user terms, everything that degrades the textured preview."""

    for name, ref in family.texture_refs.items():
        if ref.status == "ambiguous" and ref.path is None:
            family.diag(f"{name}: ambiguous ({len(ref.candidates)} candidates) "
                        "- not auto-loaded; trial-load one in the "
                        "Asset Dependencies.")
            continue
        if not ref.found:
            family.diag(f"{name}: texture missing - assign it manually in the "
                        "Asset Dependencies.")
            continue
        if ref.status == "ambiguous" and ref.path is not None:
            family.diag(f"{name}: ambiguous ({len(ref.candidates)} candidates) "
                        f"- using {ref.path.name} from "
                        f"{ref.path.parent.name}; pick the right one in the "
                        "Asset Dependencies.")
        img = family.textures.get(name)
        if img is None:
            family.diag(f"{name}: decode failed - material color fallback.")
        elif not img.has_body and not img.is_palette_only:
            family.diag(f"{name}: no BODY pixels - material color fallback.")
        elif img.pixels is not None and img.palette is None:
            if family.external_palette is not None:
                pass  # external PAL applied; colors should be right
            else:
                family.diag(f"{name}: no CMAP and no external .PAL - "
                            "grayscale preview.")

    for anm_name, ref in family.animation_refs.items():
        if not ref.found:
            family.diag(f"{anm_name}: animation file missing.")
            continue
        anm = family.animations.get(anm_name)
        if anm is None:
            continue
        for bitmap_name in anm.bitmap_names:
            bitmap_ref = family.texture_refs.get(bitmap_name)
            if bitmap_ref is None or not bitmap_ref.found:
                family.diag(f"{anm_name}: frame bitmap {bitmap_name} missing.")

    for fam_obj in family.all_objects():
        for group in fam_obj.materials:
            block = group.block
            if block is None:
                continue
            if block.texture and block.texture.kind == "ilbm" and group.faces:
                missing_uv = sum(1 for _pid, uvs, _s in group.faces if not uvs)
                if missing_uv:
                    family.diag(
                        f"{group.label}: {missing_uv} face(s) without UVs - "
                        "drawn with material color."
                    )


def _attach_setbas(family: AssetFamily, setbas,
                   roots: list[Path | str]) -> None:
    """Attach a SET.BAS provider (path or pre-parsed archive), read-only."""

    if setbas is None:
        return
    if isinstance(setbas, SetBasArchive):
        family.setbas_archive = setbas
    else:
        try:
            family.setbas_archive = read_setbas(setbas)
        except SetBasError as exc:
            family.warnings.append(f"SET.BAS parse failed: {exc}")
            return
    family.setbas_path = family.setbas_archive.path
    family.warnings.extend(
        f"{family.setbas_path.name}: {w}"
        for w in family.setbas_archive.warnings
    )
    # The set folder next to SET.BAS is a natural search root
    # (Data/SetN/OBJECTS -> Data/SetN, engine loose layout).
    roots.append(family.setbas_path.parent)
    roots.append(family.setbas_path.parent.parent)


def _split_overrides(family: AssetFamily,
                     overrides: dict[str, Path | str] | None
                     ) -> dict[str, Path]:
    """Separate "setbas:" overrides from file-path overrides."""

    file_overrides: dict[str, Path] = {}
    for name, value in (overrides or {}).items():
        family.overrides[name] = str(value)
        text = str(value)
        if text.lower().startswith(SETBAS_OVERRIDE_PREFIX):
            key = normalize_logical_name(name).lower()
            family.setbas_overrides[key] = True
        else:
            file_overrides[name] = Path(value)
    return file_overrides


def load_asset_family(base_path: str | Path,
                      extra_roots: list[str | Path] | None = None,
                      overrides: dict[str, Path | str] | None = None,
                      setbas: SetBasArchive | str | Path | None = None,
                      *, isolated_root: str | Path | None = None
                      ) -> AssetFamily:
    """Mode A: open a .base file and assemble the referenced family.

    ``overrides`` maps logical resource names (e.g. "BODEN2.ILBM") to file
    paths chosen by the user, or to "setbas:<name>" to force the embedded
    SET.BAS resource; they win over directory search.  ``setbas`` attaches a
    read-only SET.BAS archive as fallback resource provider (loose files
    still win, matching the engine's precedence).  Session-only: nothing is
    written to disk.
    """

    family = AssetFamily()
    path = Path(base_path)
    family.base_path = path

    if isolated_root is not None:
        isolated = Path(isolated_root).resolve()
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(isolated)
        except ValueError as exc:
            raise ValueError(
                f"BASE {resolved_path} is outside isolated root {isolated}"
            ) from exc
        roots: list[Path | str] = [isolated]
        if extra_roots:
            raise ValueError(
                "isolated AssetFamily loading does not accept extra roots")
        if setbas is not None:
            provider_path = (
                setbas.path if isinstance(setbas, SetBasArchive)
                else Path(setbas))
            try:
                provider_path.resolve().relative_to(isolated)
            except ValueError as exc:
                raise ValueError(
                    "isolated AssetFamily loading cannot use an external "
                    "SET.BAS provider") from exc
    else:
        roots = [path.parent, path.parent.parent]
        for extra in extra_roots or []:
            roots.append(extra)
    _attach_setbas(family, setbas, roots)
    file_overrides = _split_overrides(family, overrides)
    resolver = AssetResolver(
        roots, file_overrides, prefer_earliest_root=True)
    family.search_roots = [str(r) for r in resolver.roots]
    family.search_root = str(path.parent)

    try:
        family.base_asset = parse_base_file(path)
    except Exception as exc:
        family.warnings.append(f"Failed to parse {path.name}: {exc}")
        return family
    family.warnings.extend(family.base_asset.warnings)

    # A .base that embeds EMRS resources (SET.BAS-style) is its own resource
    # provider: attach it so its KIDS resolve embedded skeletons/textures
    # without loose files.  An explicitly supplied provider wins.
    if family.setbas_archive is None and family.base_asset.root is not None \
            and family.base_asset.root.embedded:
        try:
            family.setbas_archive = read_setbas(path)
            family.setbas_path = path
            roots.append(path.parent.parent)
            family.warnings.append(
                f"{path.name} embeds "
                f"{len(family.setbas_archive.resources)} EMRS resources: "
                "using the file itself as its resource provider."
            )
        except SetBasError as exc:
            family.warnings.append(
                f"{path.name}: EMRS self-provider scan failed: {exc}"
            )

    if family.base_asset.root is not None:
        family.root_object = _load_family_object(
            family.base_asset.root, family, resolver
        )

    _propagate_anm_tracy_usage(family)
    _find_engine_effect_overrides(family)
    _find_external_palette(family, resolver)
    _collect_textured_diagnostics(family)
    _run_checks(family)

    from studio.assets.dependencies import collect_dependencies

    family.dependencies = collect_dependencies(family)
    return family


def load_manual_family(sklt_path: str | Path | None,
                       texture_paths: list[str | Path],
                       anm_paths: list[str | Path],
                       base_path: str | Path | None = None,
                       extra_roots: list[str | Path] | None = None,
                       overrides: dict[str, Path | str] | None = None,
                       setbas: SetBasArchive | str | Path | None = None
                       ) -> AssetFamily:
    """Mode B: user picks the family members by hand.

    If a .base is given it drives the assembly exactly like Mode A (the
    hand-picked files' directories are added as search roots).  Without a
    .base there is no ATTS/OLPL mapping, so the skeleton is shown with
    geometry only and textures/animations are listed for inspection.
    """

    roots: list[str | Path] = []
    for candidate in [sklt_path, *texture_paths, *anm_paths]:
        if candidate:
            roots.append(Path(candidate).parent)
    for extra in extra_roots or []:
        roots.append(extra)

    if base_path is not None:
        return load_asset_family(base_path, roots, overrides, setbas)

    family = AssetFamily()
    _attach_setbas(family, setbas, roots)
    file_overrides = _split_overrides(family, overrides)
    resolver = AssetResolver(roots, file_overrides)
    family.search_roots = [str(r) for r in resolver.roots]

    if sklt_path is not None:
        source_path = Path(sklt_path)
        fake = BaseObject()
        fake.skeleton_name = source_path.name
        fam_obj = FamilyObject(
            base_object=fake,
            skeleton_ref=ResolvedFile(
                logical_name=source_path.name,
                status="manual",
                path=source_path,
                source="manual",
            ),
        )
        try:
            fam_obj.skeleton = parse_sklt_file(source_path)
            family.warnings.extend(
                f"{Path(sklt_path).name}: {w}" for w in fam_obj.skeleton.warnings
            )
        except Exception as exc:
            family.warnings.append(f"Skeleton failed to parse: {exc}")
        family.root_object = fam_obj
        family.warnings.append(
            "Manual family without .base: no ATTS/OLPL material mapping is "
            "available, geometry preview only."
        )

    for tex in texture_paths:
        _load_texture(family, resolver, Path(tex).name)
    for anm in anm_paths:
        _load_animation(family, resolver, Path(anm).name)

    _propagate_anm_tracy_usage(family)
    _find_engine_effect_overrides(family)
    _find_external_palette(family, resolver)
    _collect_textured_diagnostics(family)
    _run_checks(family)

    from studio.assets.dependencies import collect_dependencies

    family.dependencies = collect_dependencies(family)
    return family


# ----------------------------------------------------------------------------
# Canonical portable package workflow for a complete :mod:`asset_family`.
# (formerly asset_family_package.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class PackageEntry:
    logical_reference: str
    asset_class: str
    resolved_source: str
    exported_path: str
    dependency_relation: str
    owner_node: str = "root"
    sha256: str = ""
    status: str = "exported"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PackageValidation:
    root: Path
    manifest_path: Path | None = None
    valid: bool = False
    manifest: dict | None = None
    family: AssetFamily | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _storage_float(value: float) -> float:
    """Canonical IEEE-754 value used by BASE/SKLT on-disk structures."""

    return struct.unpack(">f", struct.pack(">f", float(value)))[0]


def _storage_value(value):
    if isinstance(value, float):
        return _storage_float(value)
    if isinstance(value, dict):
        return {key: _storage_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_storage_value(item) for item in value]
    return value


def _block_snapshot(block) -> dict:
    def texture_snapshot(texture):
        if texture is None:
            return None
        return {
            "class": texture.class_id,
            "kind": texture.kind,
            "name": texture.name,
            "outline_uvs": [list(uv) for uv in texture.outline_uvs],
            "anim_type": texture.anim_type,
        }

    return {
        "class": block.class_id,
        "payload_form_type": block.payload_form_type,
        "ade_flags": block.ade_flags,
        "ade_point_id": block.ade_point_id,
        "ade_poly_id": block.ade_poly_id,
        "area_flags": block.area_flags,
        "polflags": block.polflags,
        "color": block.color_val,
        "tracy": block.tracy_val,
        "shade": block.shade_val,
        "texture": texture_snapshot(block.texture),
        "tracy_texture": texture_snapshot(block.tracy_texture),
        "atts": [
            [entry.poly_id, entry.color_val, entry.shade_val,
             entry.tracy_val, entry.pad]
            for entry in block.atts],
        "olpl": [[list(uv) for uv in group] for group in block.olpl],
        "particle_stages": [
            _block_snapshot(stage) for stage in block.particle_stages],
    }


def _object_snapshot(obj: FamilyObject, *, path: str,
                     animation_names: set[str],
                     texture_names: set[str]) -> dict:
    blocks = []
    for block in obj.base_object.ades:
        blocks.append(_block_snapshot(block))
        for visual in block.iter_visual_blocks():
            for texture in (visual.texture, visual.tracy_texture):
                if texture is None or not texture.name:
                    continue
                if texture.kind == "bmpanim":
                    animation_names.add(texture.name)
                else:
                    texture_names.add(texture.name)
    skeleton = obj.skeleton
    transform = obj.base_object.transform
    sensor_points = (
        sen2_points_for_poo2(skeleton, skeleton.points)
        if skeleton is not None else ())
    return {
        "path": path,
        "name": obj.base_object.name,
        "skeleton_name": obj.base_object.skeleton_name,
        "transform": (
            _storage_value(asdict(transform))
            if transform is not None else None),
        "ades": blocks,
        "embedded": [
            [resource.class_id, resource.resource_name,
             resource.payload_tag, resource.payload_form_type,
             resource.payload_size]
            for resource in obj.base_object.embedded],
        "unknown_chunks": list(obj.base_object.unknown_chunks),
        "skeleton": None if skeleton is None else {
            "points": [_storage_value(point) for point in skeleton.points],
            "polygons": [list(poly) for poly in skeleton.polygons],
            "sensors": [_storage_value(point) for point in sensor_points],
        },
        "kids": [
            _object_snapshot(
                kid, path=f"{path}/kid[{index}]",
                animation_names=animation_names,
                texture_names=texture_names)
            for index, kid in enumerate(obj.kids)],
    }


def family_semantic_snapshot(
        family: AssetFamily,
        root_object: FamilyObject | None = None) -> dict:
    """Stable semantic tree used by export/import round-trip validation."""

    root = root_object or family.root_object
    if root is None:
        raise AssetFamilyPackageError("asset family has no BASE root object")
    animation_names: set[str] = set()
    texture_names: set[str] = set()
    object_tree = _object_snapshot(
        root, path="root", animation_names=animation_names,
        texture_names=texture_names)

    animations = {}
    for name in sorted(animation_names, key=str.casefold):
        animation = next((value for key, value in family.animations.items()
                          if key.casefold() == name.casefold()), None)
        if animation is None:
            animations[name] = None
            continue
        texture_names.update(animation.bitmap_names)
        animations[name] = {
            "bitmap_class": animation.bitmap_class,
            "bitmap_names": list(animation.bitmap_names),
            "texcoord_groups": [
                [list(uv) for uv in group]
                for group in animation.texcoord_groups],
            "frames": [
                [frame.frame_time, frame.frame_id, frame.texcoords_id]
                for frame in animation.frames],
        }

    textures = {}
    for name in sorted(texture_names, key=str.casefold):
        image = next((value for key, value in family.textures.items()
                      if key.casefold() == name.casefold()), None)
        if image is None:
            textures[name] = None
            continue
        palette = (getattr(image, "palette", None)
                   or family.external_palette)
        palette_bytes = (
            bytes(channel for rgb in palette for channel in rgb)
            if palette is not None else None)
        textures[name] = {
            "width": image.width,
            "height": image.height,
            "pixels_sha256": _sha256_bytes(image.pixels),
            "palette_sha256": _sha256_bytes(palette_bytes),
        }

    dependency_graph = []
    for path, object_snapshot in _walk_snapshot_objects(object_tree):
        skeleton_name = object_snapshot["skeleton_name"]
        if skeleton_name:
            dependency_graph.append([
                path, "skeleton", skeleton_name,
                "OBJT sklt.class NAME"])
        for block_index, block in enumerate(object_snapshot["ades"]):
            for slot in ("texture", "tracy_texture"):
                texture = block[slot]
                if texture is None or not texture["name"]:
                    continue
                kind = ("animation" if texture["kind"] == "bmpanim"
                        else slot)
                dependency_graph.append([
                    path, kind, texture["name"],
                    f"ADES block #{block_index} {slot}"])
    for animation_name, animation in animations.items():
        if animation is None:
            continue
        for bitmap in animation["bitmap_names"]:
            dependency_graph.append([
                "family", "anm_bitmap", bitmap,
                f"VANM {animation_name} bitmap list"])
    dependency_graph.sort(
        key=lambda item: tuple(str(value).casefold() for value in item))
    return {
        "object_tree": object_tree,
        "animations": animations,
        "textures": textures,
        "dependency_graph": dependency_graph,
    }


def _walk_snapshot_objects(root: dict):
    yield root["path"], root
    for kid in root["kids"]:
        yield from _walk_snapshot_objects(kid)


def semantic_sha256(snapshot: dict) -> str:
    payload = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_package_manifest(
        package_root: str | Path, entry_base: str | Path,
        source_family: AssetFamily, entries: Iterable[PackageEntry], *,
        root_object: FamilyObject | None = None,
        source_name: str | None = None) -> tuple[Path, dict]:
    """Write a manifest after all staged package files have been verified."""

    root = Path(package_root).resolve()
    entry = Path(entry_base)
    if entry.is_absolute():
        try:
            entry = entry.resolve().relative_to(root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                "package entry BASE is outside the package root") from exc
    entry_path = (root / entry).resolve()
    try:
        entry_path.relative_to(root)
    except ValueError as exc:
        raise AssetFamilyPackageError(
            "package entry BASE escapes the package root") from exc
    if not entry_path.is_file():
        raise AssetFamilyPackageError(
            f"package entry BASE is missing: {entry_path}")

    normalized_entries = []
    seen: set[str] = set()
    for item in entries:
        relative = Path(item.exported_path)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                f"manifest output escapes package root: {relative}") from exc
        key = relative.as_posix().casefold()
        if key in seen:
            raise AssetFamilyPackageError(
                f"duplicate package output path: {relative}")
        seen.add(key)
        if item.status == "exported" and not path.is_file():
            raise AssetFamilyPackageError(
                f"exported package file is missing: {relative}")
        encoded = item.to_dict()
        if item.status == "exported":
            encoded["sha256"] = sha256_file(path)
        normalized_entries.append(encoded)

    snapshot = family_semantic_snapshot(source_family, root_object)
    manifest = {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "source_name": source_name or (
            str(source_family.base_path) if source_family.base_path else ""),
        "entry_base": entry.as_posix(),
        "semantic_sha256": semantic_sha256(snapshot),
        "semantic_snapshot": snapshot,
        "entries": normalized_entries,
    }
    target = root / MANIFEST_NAME
    target.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    return target, manifest


def _load_manifest(root: Path, manifest_relative: str | Path) -> dict:
    relative = Path(manifest_relative)
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AssetFamilyPackageError(
            "package manifest escapes the package root") from exc
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetFamilyPackageError(
            f"could not read {relative.as_posix()}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AssetFamilyPackageError("package manifest root must be an object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise AssetFamilyPackageError("not a Complete Asset Family manifest")
    if manifest.get("version") != MANIFEST_VERSION:
        raise AssetFamilyPackageError(
            f"unsupported package manifest version: "
            f"{manifest.get('version')!r}")
    if not isinstance(manifest.get("entries"), list):
        raise AssetFamilyPackageError("package manifest entries must be a list")
    return manifest


def validate_family_package(
        root: str | Path, *,
        manifest_relative: str | Path = MANIFEST_NAME
        ) -> PackageValidation:
    """Validate hashes, layout, isolated dependency resolution and semantics."""

    package_root = Path(root).resolve()
    relative_manifest = Path(manifest_relative)
    result = PackageValidation(
        root=package_root,
        manifest_path=(package_root / relative_manifest).resolve())
    try:
        manifest = _load_manifest(package_root, relative_manifest)
        result.manifest = manifest
        entry_text = manifest.get("entry_base")
        if not isinstance(entry_text, str) or not entry_text:
            raise AssetFamilyPackageError("manifest entry_base is missing")
        entry_base = (package_root / Path(entry_text)).resolve()
        try:
            entry_base.relative_to(package_root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                "manifest entry_base escapes package root") from exc

        seen: set[str] = set()
        exported_paths: set[str] = set()
        entry_key = Path(entry_text).as_posix().casefold()
        entry_rows = []
        for index, item in enumerate(manifest["entries"]):
            if not isinstance(item, dict):
                raise AssetFamilyPackageError(
                    f"manifest entry #{index} is not an object")
            relative_text = item.get("exported_path")
            if not isinstance(relative_text, str) or not relative_text:
                raise AssetFamilyPackageError(
                    f"manifest entry #{index} has no exported_path")
            relative = Path(relative_text)
            target = (package_root / relative).resolve()
            try:
                target.relative_to(package_root)
            except ValueError as exc:
                raise AssetFamilyPackageError(
                    f"manifest entry escapes package root: {relative_text}"
                ) from exc
            key = relative.as_posix().casefold()
            if key in seen:
                raise AssetFamilyPackageError(
                    f"duplicate case-insensitive manifest path: {relative_text}")
            seen.add(key)
            if item.get("status") != "exported":
                continue
            exported_paths.add(key)
            if key == entry_key and item.get("asset_class") == "base.class":
                entry_rows.append(item)
            if not target.is_file():
                result.errors.append(f"missing exported file: {relative_text}")
                continue
            expected = item.get("sha256")
            actual = sha256_file(target)
            if not isinstance(expected, str) or actual != expected:
                result.errors.append(
                    f"hash mismatch: {relative_text} "
                    f"(expected {expected}, got {actual})")
        if not entry_base.is_file():
            result.errors.append(f"entry BASE is missing: {entry_text}")
        if len(entry_rows) != 1:
            result.errors.append(
                "manifest must contain exactly one exported base.class row "
                f"for entry BASE {entry_text}")
        if result.errors:
            return result

        DirectoryIndex.clear_cache()
        family = load_asset_family(
            entry_base, isolated_root=package_root)
        result.family = family
        if family.root_object is None:
            result.errors.append("entry BASE failed to parse")
        required_failures = [
            dependency for dependency in family.dependencies
            if dependency.status in ("missing", "ambiguous", "failed_load")]
        for dependency in required_failures:
            result.errors.append(
                f"{dependency.status} {dependency.kind}: "
                f"{dependency.raw_ref}")
        for dependency in family.dependencies:
            if dependency.resolved_path is None:
                continue
            resolved = dependency.resolved_path.resolve()
            try:
                relative = resolved.relative_to(package_root)
            except ValueError:
                result.errors.append(
                    f"dependency resolved outside package: "
                    f"{dependency.raw_ref} -> {resolved}")
                continue
            if relative.as_posix().casefold() not in exported_paths:
                result.errors.append(
                    f"resolved dependency is absent from manifest: "
                    f"{relative.as_posix()}")

        # Imported here on purpose: the asset layer only needs the renderer's adapter for this check.
        from studio.rendering.indexed import IndexedFamilyAdapter

        adapter, reason = IndexedFamilyAdapter.try_create(family)
        if adapter is None:
            result.errors.append(
                "Retail indexed profile is incomplete or inconsistent: "
                + reason)
        else:
            for profile_path in (
                    adapter.palette_path, adapter.shader_path,
                    adapter.tracy_path):
                relative = profile_path.resolve().relative_to(package_root)
                if relative.as_posix().casefold() not in exported_paths:
                    result.errors.append(
                        "indexed profile file is absent from manifest: "
                        + relative.as_posix())

        expected_snapshot = manifest.get("semantic_snapshot")
        expected_hash = manifest.get("semantic_sha256")
        if not isinstance(expected_snapshot, dict) \
                or semantic_sha256(expected_snapshot) != expected_hash:
            result.errors.append(
                "manifest semantic snapshot/hash is inconsistent")
        elif family.root_object is not None:
            actual_snapshot = family_semantic_snapshot(family)
            actual_hash = semantic_sha256(actual_snapshot)
            if actual_hash != expected_hash:
                result.errors.append(
                    "reopened package is not semantically equal to its "
                    "exported source")

        result.warnings.extend(
            warning for warning in family.warnings
            if warning not in result.warnings)
        result.valid = not result.errors
    except (AssetFamilyPackageError, OSError, ValueError) as exc:
        result.errors.append(str(exc))
    return result
