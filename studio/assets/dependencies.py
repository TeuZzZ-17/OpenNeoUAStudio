"""Dependency model of a .base family, saved dependency choices and Assets-tree filtering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path

# ----------------------------------------------------------------------------
# Dependency model for a .base asset family (UI-free).
# (formerly base_dependency_resolver.py)
# ----------------------------------------------------------------------------

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




# ----------------------------------------------------------------------------
# Optional persistence of dependency choices (tool-side, never asset-side).
# (formerly dependency_profile.py)
# ----------------------------------------------------------------------------

PROFILE_DIR = Path.home() / ".openneouastudio"


PROFILE_PATH = PROFILE_DIR / "dependency_choices.json"


LEGACY_OPENUA_PROFILE_PATH = (Path.home() / ".openuastudio"
                              / "dependency_choices.json")


LEGACY_SKLTRON_PROFILE_PATH = (Path.home() / ".skltron"
                                / "skltron_dependency_choices.json")


PROFILE_VERSION = 1


@dataclass
class SavedChoice:
    root_base: str
    owner_node: str
    kind: str
    raw_ref: str
    chosen_path: str
    source: str = ""
    choice_mode: str = "manual"      # manual | trial | setbas
    created_at: str = ""
    last_used_at: str = ""

    def key(self) -> tuple[str, str, str, str]:
        return (self.root_base.lower(), self.owner_node or "root",
                self.kind, self.raw_ref.lower())

    @property
    def stale(self) -> bool:
        if self.chosen_path.lower().startswith("setbas:"):
            return False
        return not Path(self.chosen_path).is_file()


class DependencyProfile:
    """Load/save wrapper around the JSON profile.  All writes go to the
    tool's own config directory; failures degrade to an in-memory profile."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else PROFILE_PATH
        self.auto_apply = False
        self._choices: dict[tuple, SavedChoice] = {}
        self.load_error: str | None = None
        # One-way compatibility bridge for existing users.  Read the new
        # profile first, then the former OpenUAStudio location, then the
        # older SKLtron profile.  The next successful save always writes only
        # the new path; legacy files are never moved, deleted, or overwritten.
        self._load_path = self.path
        if path is None and not self.path.is_file():
            for legacy_path in (LEGACY_OPENUA_PROFILE_PATH,
                                LEGACY_SKLTRON_PROFILE_PATH):
                if legacy_path.is_file():
                    self._load_path = legacy_path
                    break
        self._load()

    # -- persistence -------------------------------------------------------------

    def _load(self) -> None:
        if not self._load_path.is_file():
            return
        try:
            data = json.loads(self._load_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.load_error = f"profile unreadable ({exc}); starting empty"
            return
        self.auto_apply = bool(data.get("auto_apply", False))
        for raw in data.get("choices", []):
            try:
                choice = SavedChoice(
                    root_base=raw["root_base"],
                    owner_node=raw.get("owner_node", "root"),
                    kind=raw.get("kind", "texture"),
                    raw_ref=raw["raw_ref"],
                    chosen_path=raw["chosen_path"],
                    source=raw.get("source", ""),
                    choice_mode=raw.get("choice_mode", "manual"),
                    created_at=raw.get("created_at", ""),
                    last_used_at=raw.get("last_used_at", ""),
                )
            except KeyError:
                continue
            self._choices[choice.key()] = choice

    def save(self) -> str | None:
        """Write the profile; returns an error string instead of raising."""

        payload = {
            "version": PROFILE_VERSION,
            "auto_apply": self.auto_apply,
            "choices": [
                {
                    "root_base": c.root_base,
                    "owner_node": c.owner_node,
                    "kind": c.kind,
                    "raw_ref": c.raw_ref,
                    "chosen_path": c.chosen_path,
                    "source": c.source,
                    "choice_mode": c.choice_mode,
                    "created_at": c.created_at,
                    "last_used_at": c.last_used_at,
                }
                for c in self._choices.values()
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, indent=2),
                                 encoding="utf-8")
        except OSError as exc:
            return f"could not write profile: {exc}"
        return None

    # -- API ---------------------------------------------------------------------

    def _key(self, root_base: str, owner_node: str | None, kind: str,
             raw_ref: str) -> tuple:
        return (Path(root_base).name.lower(), owner_node or "root",
                kind, raw_ref.lower())

    def lookup(self, root_base: str, owner_node: str | None, kind: str,
               raw_ref: str) -> SavedChoice | None:
        return self._choices.get(self._key(root_base, owner_node, kind,
                                           raw_ref))


    def forget(self, root_base: str, owner_node: str | None, kind: str,
               raw_ref: str) -> bool:
        return self._choices.pop(
            self._key(root_base, owner_node, kind, raw_ref), None
        ) is not None

    def touch(self, choice: SavedChoice) -> None:
        choice.last_used_at = datetime.now().isoformat(timespec="seconds")

    def choices_for(self, root_base: str) -> list[SavedChoice]:
        base = Path(root_base).name.lower()
        return [c for c in self._choices.values()
                if c.root_base.lower() == base]

    def __len__(self) -> int:
        return len(self._choices)


# ----------------------------------------------------------------------------
# Signal-safe filtering helpers for the hierarchical Assets tree.
# (formerly asset_tree_filter.py)
# ----------------------------------------------------------------------------

def iter_tree_items(tree):
    """Yield every item depth-first without changing selection/current item."""

    def visit(item):
        yield item
        for index in range(item.childCount()):
            yield from visit(item.child(index))

    for index in range(tree.topLevelItemCount()):
        yield from visit(tree.topLevelItem(index))


def snapshot_expansion(tree) -> dict[int, bool]:
    return {id(item): item.isExpanded() for item in iter_tree_items(tree)}


def filter_tree(
        tree, query: str, extra_text: Callable[[object], str],
        previous_expansion: dict[int, bool] | None = None,
) -> dict[int, bool] | None:
    """Filter recursively and expand only branches containing matches.

    The caller owns ``previous_expansion`` between keystrokes.  Clearing the
    query restores the expansion state captured when filtering began.
    """

    needle = query.strip().casefold()
    if needle and previous_expansion is None:
        previous_expansion = snapshot_expansion(tree)

    signals_were_blocked = tree.blockSignals(True)
    try:
        if not needle:
            for item in iter_tree_items(tree):
                item.setHidden(False)
                if previous_expansion is not None:
                    item.setExpanded(
                        previous_expansion.get(id(item), item.isExpanded()))
            return None

        def visit(item) -> bool:
            child_hit = False
            for index in range(item.childCount()):
                child_hit = visit(item.child(index)) or child_hit
            own_text = " ".join(
                item.text(column) for column in range(tree.columnCount()))
            metadata = extra_text(item)
            own_hit = needle in f"{own_text} {metadata}".casefold()
            visible = own_hit or child_hit
            item.setHidden(not visible)
            if item.childCount():
                item.setExpanded(child_hit)
            return visible

        for index in range(tree.topLevelItemCount()):
            visit(tree.topLevelItem(index))
        return previous_expansion
    finally:
        tree.blockSignals(signals_were_blocked)
