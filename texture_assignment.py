"""Classification for safe logical material-binding replacement."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextureBinding:
    block_index: int
    block: object
    binding_slot: str


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
    """Partition selected polygons without mutating BASE or viewport state."""

    def binding_key(block_index: int, binding_slot: str, texture):
        kind = str(getattr(texture, "kind", "")).casefold()
        name = str(getattr(texture, "name", ""))
        logical_name = (
            name.casefold() if name
            else f"__unnamed_block_{block_index}")
        return binding_slot, kind, logical_name

    blocks = {}
    for refs in mapping.refs.values():
        for ref in refs:
            blocks[(ref.block_index, id(ref.block))] = (
                ref.block_index, ref.block)

    grouped: dict[tuple[str, str, str], dict] = {}
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
            key = binding_key(ref.block_index, binding_slot, texture)
            record = grouped.setdefault(
                key,
                {
                    "selected": set(),
                    "kind": kind,
                    "name": str(getattr(texture, "name", "")),
                })
            record["selected"].add(poly_id)
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
        unsafe = False
        for block_index, block in sorted(blocks.values()):
            texture = getattr(block, binding_slot, None)
            if texture is None or binding_key(
                    block_index, binding_slot, texture) != key:
                continue
            member_polys = {
                entry.poly_id for entry in getattr(block, "atts", ())
                if 0 <= entry.poly_id < mapping.poly_count
            }
            if not member_polys or any(
                    len(mapping.refs.get(poly_id, ())) != 1
                    or mapping.refs[poly_id][0].block is not block
                    for poly_id in member_polys):
                unsafe = True
                break
            bindings.append(TextureBinding(
                block_index, block, binding_slot))
            affected.update(member_polys)
        if unsafe or not bindings or not affected:
            reason = ("material overlaps duplicate mapping"
                      if unsafe else "material has no valid polygons")
            for poly_id in record["selected"]:
                skipped[poly_id] = reason
            continue
        groups.append(TextureBindingGroup(
            bindings=tuple(bindings),
            selected_polys=frozenset(record["selected"]),
            affected_polys=frozenset(affected),
            binding_kind=record["kind"],
            binding_name=record["name"],
        ))

    groups.sort(key=lambda group: (
        group.block_index, group.binding_slot))
    return TextureAssignmentSelection(
        groups=tuple(groups),
        skipped=tuple(sorted(skipped.items())),
    )
