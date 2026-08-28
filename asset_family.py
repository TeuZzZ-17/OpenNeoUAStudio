"""Assembles a complete Urban Assault asset family from a .base file.

Given BP_FLAK1.base this module resolves and loads:
  - the referenced skeleton (.sklt/.skl) through the existing sklt_parser;
  - every referenced texture (.ilbm/.ilb, embedded VBMP not required);
  - every referenced texture animation (.anm/.vanm);
  - an external palette (.PAL) when a texture has no CMAP;

then cross-checks the pieces (ATTS/OLPL counts vs POL2, polyID ranges, UV
counts vs polygon vertex counts) and produces per-polygon material groups
ready for 3D preview.

Everything is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from anm_parser import VanmData, parse_anm_file
from asset_resolver import AssetResolver, ResolvedFile, normalize_logical_name
from base_parser import AmeshBlock, BaseAsset, BaseObject, parse_base_file
from ilbm_parser import IlbmImage, Palette, parse_ilbm_file, parse_pal_file
from setbas_reader import (
    SetBasArchive,
    SetBasError,
    decode_animation,
    decode_skeleton,
    decode_texture,
    read_setbas,
)
from sklt_parser import SkltModel, parse_sklt_file

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

    if ref.path is not None:
        ref.source = "manual" if ref.status == "manual" else "loose"
        if embedded is not None and ref.status != "manual":
            family.warnings.append(
                f"{name}: exists both loose ({ref.path}) and embedded in "
                f"{family.setbas_archive.path.name}; loose wins (engine "
                "precedence). Use the Resolve panel to compare sources."
            )
    elif ref.status == "ambiguous":
        # A plausible loose override must not be bypassed by silently using
        # SET.BAS; the user first has to disambiguate the higher-priority
        # loose source.
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
            "wrong). Trial-load one in the Dependencies panel."
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
            "not auto-loaded. Trial-load one in the Dependencies panel."
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
                "auto-loaded. Trial-load one in the Dependencies panel."
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
            override = root / "HI" / "ALPHA" / f"{stem}.PNG"
            if override.is_file():
                family.effect_override_paths[logical_name.lower()] = override
                break


def _collect_textured_diagnostics(family: AssetFamily) -> None:
    """Explain, in user terms, everything that degrades the textured preview."""

    for name, ref in family.texture_refs.items():
        if ref.status == "ambiguous" and ref.path is None:
            family.diag(f"{name}: ambiguous ({len(ref.candidates)} candidates) "
                        "- not auto-loaded; trial-load one in the "
                        "Dependencies panel.")
            continue
        if not ref.found:
            family.diag(f"{name}: texture missing - assign it manually in the "
                        "Dependencies panel.")
            continue
        if ref.status == "ambiguous" and ref.path is not None:
            family.diag(f"{name}: ambiguous ({len(ref.candidates)} candidates) "
                        f"- using {ref.path.name} from "
                        f"{ref.path.parent.name}; pick the right one in the "
                        "Dependencies panel.")
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
    resolver = AssetResolver(roots, file_overrides)
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

    from base_dependency_resolver import collect_dependencies

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

    from base_dependency_resolver import collect_dependencies

    family.dependencies = collect_dependencies(family)
    return family


if __name__ == "__main__":
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
        from base_dependency_resolver import print_report

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
