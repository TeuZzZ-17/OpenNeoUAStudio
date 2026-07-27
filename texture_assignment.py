"""Classification for safe, partial static-material texture replacement."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StaticMaterialGroup:
    block_index: int
    block: object
    selected_polys: frozenset[int]
    affected_polys: frozenset[int]


@dataclass(frozen=True)
class TextureAssignmentSelection:
    groups: tuple[StaticMaterialGroup, ...]
    skipped: tuple[tuple[int, str], ...]

    @property
    def affected_polys(self) -> frozenset[int]:
        return frozenset(
            poly_id for group in self.groups
            for poly_id in group.affected_polys)


def classify_texture_assignment(
        mapping, selected_polys) -> TextureAssignmentSelection:
    """Partition selected polygons without mutating BASE or viewport state."""

    grouped: dict[int, dict] = {}
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
        texture = getattr(ref.block, "texture", None)
        if texture is None:
            skipped[poly_id] = "material has no texture"
            continue
        if str(getattr(texture, "kind", "")).casefold() != "ilbm":
            skipped[poly_id] = "animated/VANM"
            continue
        record = grouped.setdefault(
            ref.block_index,
            {"block": ref.block, "selected": set()})
        record["selected"].add(poly_id)

    groups = []
    for block_index in sorted(grouped):
        record = grouped[block_index]
        block = record["block"]
        affected = {
            entry.poly_id for entry in getattr(block, "atts", ())
            if 0 <= entry.poly_id < mapping.poly_count
        }
        unsafe = any(
            len(mapping.refs.get(poly_id, ())) != 1
            or mapping.refs[poly_id][0].block is not block
            for poly_id in affected)
        if unsafe or not affected:
            reason = ("material overlaps duplicate mapping"
                      if unsafe else "material has no valid polygons")
            for poly_id in record["selected"]:
                skipped[poly_id] = reason
            continue
        groups.append(StaticMaterialGroup(
            block_index=block_index,
            block=block,
            selected_polys=frozenset(record["selected"]),
            affected_polys=frozenset(affected),
        ))

    return TextureAssignmentSelection(
        groups=tuple(groups),
        skipped=tuple(sorted(skipped.items())),
    )
