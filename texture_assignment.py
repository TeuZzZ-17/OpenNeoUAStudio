"""Classification for selection-scoped material-binding replacement."""

from __future__ import annotations

from dataclasses import dataclass


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
