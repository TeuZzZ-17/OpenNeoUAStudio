"""Game object / texture catalogs and visual-state resolution shared by all workspaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from map_editor.core.script_catalog import script_texts
from studio.assets.visproto import normalize_base_name, reconstruct_embedded_vps

# ----------------------------------------------------------------------------
# Game prototypes in the same manifest and override order as the engine.
# (formerly game_object_catalog.py)
# ----------------------------------------------------------------------------

_HEADER = re.compile(r"(new|modify)_(vehicle|weapon|building)\s+(\d+)\b", re.I)


@dataclass
class GameObject:
    kind: str
    object_id: int
    source: Path
    values: dict[str, str] = field(default_factory=dict)
    bodies: list[str] = field(default_factory=list)
    body_sources: list[tuple[Path, int, int]] = field(default_factory=list)

    @property
    def name(self):
        return self.values.get("name", f"{self.kind.title()} {self.object_id}")

    @property
    def effective_text(self):
        return f"new_{self.kind} {self.object_id}\n" + "\n".join(self.bodies) + "\nend\n"

    def number(self, key, default=0):
        try:
            return float(self.values.get(key, str(default)))
        except (ValueError, OverflowError):
            return default


def load_game_objects(installation):
    scripts = installation.folder("scripts")
    objects = {}
    for text, source in script_texts(scripts, with_source=True):
        active = None
        depth = 0
        body = []
        for line_number, raw in enumerate(text.splitlines()):
            code = re.split(r";|//|#", raw, maxsplit=1)[0].strip()
            header = _HEADER.fullmatch(code)
            if header and active is None:
                mode, kind, number = header.groups()
                kind = kind.casefold()
                key = kind, int(number)
                if mode.casefold() == "new" or key not in objects:
                    objects[key] = GameObject(kind, key[1], scripts / source)
                active = objects[key]
                depth, body = 0, []
                body_start = line_number
                continue
            if active is None:
                continue
            token = code.split(None, 1)[0].casefold() if code else ""
            if token == "end" and depth == 0:
                active.bodies.append("\n".join(body))
                active.body_sources.append((scripts / source, body_start, line_number))
                active = None
                continue
            body.append(raw)
            if token == "begin" or token.startswith("begin_"):
                depth += 1
            elif token == "end":
                depth = max(0, depth - 1)
            elif depth == 0 and "=" in code:
                name, value = code.split("=", 1)
                active.values[name.strip().casefold()] = value.strip().strip('"')
    return sorted(objects.values(), key=lambda obj: (obj.name.casefold(), obj.kind, obj.object_id))


# ----------------------------------------------------------------------------
# Resolve named prototype states through the existing Studio asset loaders.
# (formerly game_visuals.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class VisualChoice:
    label: str
    path: Path
    base_name: str = ""


@lru_cache(maxsize=8)
def _assets(data, folders, number):
    return SetAssets(number).load()


def clear_visual_cache():
    _assets.cache_clear()


def set_assets(number):
    install = bootstrap.installation()
    if install is None or number not in install.available_sets():
        raise ValueError("Choose an installed set in Resources.")
    return _assets(str(install.data), tuple(sorted((k, str(v)) for k, v in install.folders.items())), number)


def _base_choice(assets, label, base_name):
    path = assets._index_loose().get(normalize_base_name(base_name))
    if path is not None:
        return VisualChoice(label, path)
    if assets.base_object(base_name) is not None and assets.setbas_path is not None:
        return VisualChoice(label, assets.setbas_path, base_name)
    return None


def visual_choices(obj, number):
    assets = set_assets(number)
    choices = []
    if obj.kind == "building":
        sector = assets.sdf.sectors.get(int(obj.number("sec_type")))
        if sector is None:
            return []
        for slot, building_id in enumerate(sector.subsects):
            if not 0 <= building_id < len(assets.sdf.buildings):
                continue
            building = assets.sdf.buildings[building_id]
            for state, label in enumerate(("Intact", "Damaged", "Heavy", "Flat")):
                model_id = building.state(state)
                if 0 <= model_id < len(assets.sdf.models):
                    choice = _base_choice(assets, f"Sector slot {slot + 1} — {label}", assets.sdf.models[model_id].base)
                    if choice is not None:
                        choices.append(choice)
        return choices

    # Keep the engine's external 3DS -> BASE -> positional VP precedence.
    from entity_designer.script_model import _resolve_data_asset_path
    vp_table = None
    for state in ("normal", "fire", "wait", "dead", "megadeth", "genesis"):
        choice = None
        for prefix in ("3ds", "base"):
            value = obj.values.get(f"{prefix}_{state}", "")
            if value and value != "0":
                path = _resolve_data_asset_path(value, obj.source, assets.setbas_path, assets.setbas_path)
                if path is not None:
                    choice = VisualChoice(f"{state.title()} — {prefix.upper()}", path)
                    break
        if choice is None and f"vp_{state}" in obj.values:
            if vp_table is None:
                vp_table = getattr(assets, '_studio_vp_table', None)
                if vp_table is None:
                    vp_table = reconstruct_embedded_vps(assets.setbas_path).as_table()
                    assets._studio_vp_table = vp_table
            try:
                vp_id = int(obj.values[f"vp_{state}"], 0)
            except ValueError:
                continue
            if 0 <= vp_id < len(vp_table.entries):
                choice = _base_choice(assets, f"{state.title()} — VP {vp_id}", vp_table.entries[vp_id].base_name)
        if choice is not None:
            choices.append(choice)
    return choices


# ----------------------------------------------------------------------------
# Lazy, case-insensitive texture catalogue for the Visuals panel.
# (formerly texture_catalog.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class TextureCatalogEntry:
    name: str
    referenced: bool = False
    reference: object | None = None
    archive_resource: object | None = None

    @property
    def decodable(self) -> bool:
        resource = self.archive_resource
        if resource is None:
            return True
        return bool(getattr(resource, "decodable", False))

    @property
    def status(self) -> str:
        if self.reference is not None:
            return str(getattr(self.reference, "status", "found"))
        resource = self.archive_resource
        if resource is None:
            return "loaded"
        if getattr(resource, "error", ""):
            return "decode failed"
        return "setbas" if self.decodable else "unsupported"


def build_texture_catalog(family, archive=None) -> list[TextureCatalogEntry]:
    """Return family + archive ILBMs without case-only duplicates.

    This function only indexes metadata; it deliberately never decodes an
    archive payload.
    """

    entries: dict[str, TextureCatalogEntry] = {}
    order: list[str] = []

    def merge(name: str, *, referenced=False, reference=None,
              archive_resource=None) -> None:
        if not name:
            return
        key = name.casefold()
        current = entries.get(key)
        if current is None:
            entries[key] = TextureCatalogEntry(
                name=name, referenced=referenced, reference=reference,
                archive_resource=archive_resource)
            order.append(key)
            return
        entries[key] = TextureCatalogEntry(
            name=current.name if current.referenced else name,
            referenced=current.referenced or referenced,
            reference=current.reference or reference,
            archive_resource=current.archive_resource or archive_resource,
        )

    if family is not None:
        for name, reference in getattr(family, "texture_refs", {}).items():
            merge(name, referenced=True, reference=reference)
        for name in getattr(family, "textures", {}):
            merge(name)

    if archive is not None:
        for resource in getattr(archive, "resources", ()):
            if str(getattr(resource, "class_id", "")).casefold() \
                    != "ilbm.class":
                continue
            merge(str(getattr(resource, "resource_name", "")),
                  archive_resource=resource)

    return sorted((entries[key] for key in order),
                  key=lambda entry: entry.name.casefold())


# ----------------------------------------------------------------------------
# Classification for selection-scoped material-binding replacement.
# (formerly texture_assignment.py)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class TextureBinding:
    block_index: int
    block: object
    binding_slot: str
    selected_polys: frozenset[int]


@dataclass(frozen=True)
class TextureBindingGroup:
    bindings: tuple[TextureBinding, ...]
    selected_polys: frozenset[int]
    affected_polys: frozenset[int]
    binding_kind: str
    binding_name: str

    @property
    def block_index(self) -> int:
        return self.bindings[0].block_index

    @property
    def block(self):
        return self.bindings[0].block

    @property
    def binding_slot(self) -> str:
        return self.bindings[0].binding_slot


@dataclass(frozen=True)
class TextureAssignmentSelection:
    groups: tuple[TextureBindingGroup, ...]
    skipped: tuple[tuple[int, str], ...]

    @property
    def affected_polys(self) -> frozenset[int]:
        return frozenset(
            poly_id for group in self.groups
            for poly_id in group.affected_polys)


def classify_texture_assignment(
        mapping, selected_polys) -> TextureAssignmentSelection:
    """Partition only selected polygons without expanding to sibling blocks."""

    def binding_key(binding_slot: str, texture):
        kind = str(getattr(texture, "kind", "")).casefold()
        return binding_slot, kind

    grouped: dict[tuple[str, str], dict] = {}
    skipped: dict[int, str] = {}
    for poly_id in sorted({int(value) for value in selected_polys}):
        status = mapping.status(poly_id)
        if status != "mapped":
            skipped[poly_id] = {
                "unmapped": "unmapped",
                "duplicate": "duplicate-mapped",
                "invalid": "invalid",
            }.get(status, status)
            continue
        ref = mapping.refs[poly_id][0]
        represented = False
        unsupported = []
        for binding_slot in ("texture", "tracy_texture"):
            texture = getattr(ref.block, binding_slot, None)
            if texture is None:
                continue
            kind = str(getattr(texture, "kind", "")).casefold()
            if kind not in ("ilbm", "bmpanim"):
                unsupported.append(kind or "unknown")
                continue
            represented = True
            key = binding_key(binding_slot, texture)
            record = grouped.setdefault(
                key,
                {
                    "selected": set(),
                    "bindings": {},
                    "kind": kind,
                })
            record["selected"].add(poly_id)
            record["bindings"][
                (ref.block_index, id(ref.block))] = (
                    ref.block_index, ref.block)
        if not represented:
            skipped[poly_id] = (
                "unsupported texture binding " + ", ".join(unsupported)
                if unsupported else "material has no texture")

    groups = []
    for key in sorted(grouped):
        record = grouped[key]
        binding_slot = key[0]
        bindings = []
        affected = set()
        represented_selected = set()
        binding_names = set()
        for block_index, block in sorted(record["bindings"].values()):
            texture = getattr(block, binding_slot, None)
            if texture is None or binding_key(binding_slot, texture) != key:
                continue
            member_polys = {
                entry.poly_id for entry in getattr(block, "atts", ())
                if 0 <= entry.poly_id < mapping.poly_count
            }
            selected_members = member_polys & record["selected"]
            if not member_polys:
                for poly_id in selected_members:
                    skipped[poly_id] = "material has no valid polygons"
                continue
            if any(
                    len(mapping.refs.get(poly_id, ())) != 1
                    or mapping.refs[poly_id][0].block is not block
                    for poly_id in member_polys):
                for poly_id in selected_members:
                    skipped[poly_id] = "material overlaps duplicate mapping"
                continue
            bindings.append(TextureBinding(
                block_index, block, binding_slot,
                frozenset(selected_members)))
            affected.update(selected_members)
            represented_selected.update(selected_members)
            binding_names.add(str(getattr(texture, "name", "")))
        if not bindings or not affected:
            continue
        names = sorted(binding_names, key=str.casefold)
        groups.append(TextureBindingGroup(
            bindings=tuple(bindings),
            selected_polys=frozenset(represented_selected),
            affected_polys=frozenset(affected),
            binding_kind=record["kind"],
            binding_name=(names[0] if len(names) == 1 else "<multiple>"),
        ))

    groups.sort(key=lambda group: (
        group.block_index, group.binding_slot))
    return TextureAssignmentSelection(
        groups=tuple(groups),
        skipped=tuple(sorted(skipped.items())),
    )
