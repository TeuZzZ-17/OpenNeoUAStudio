"""Typed POL2 reference graph and conservative structural remappers.

The graph is built exclusively from structures decoded by ``base_parser``.
It deliberately has no byte-scanning fallback: a BASE class without a
registered handler blocks topology renumbering until its real format is known.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Iterable

from base_parser import AmeshBlock, AttsEntry


class PolygonReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class PolygonReference:
    owner_path: str
    block_index: int
    class_id: str
    location: str
    poly_id: int
    writable: bool
    material_name: str = ""
    animation_name: str = ""
    fx_memberships: tuple[tuple, ...] = ()

    @property
    def block_path(self) -> str:
        return f"{self.owner_path}/ADES[{self.block_index}]"


@dataclass
class PolygonReferenceGraph:
    owner_path: str
    polygon_count: int
    blocks: tuple[AmeshBlock, ...]
    references: dict[int, list[PolygonReference]] = field(default_factory=dict)
    unknown_blocks: list[tuple[int, str, str]] = field(default_factory=list)

    def references_for_polygon(self, poly_id: int) -> tuple[PolygonReference, ...]:
        return tuple(self.references.get(poly_id, ()))

    def plan_polygon_renumber(
            self, old_to_new: dict[int, int],
            removed_ids: Iterable[int] = ()) -> "PolygonReferenceRewritePlan":
        return plan_polygon_renumber(self, old_to_new, removed_ids)


@dataclass(frozen=True)
class PolygonReferenceRewritePlan:
    owner_path: str
    old_to_new: tuple[tuple[int, int], ...]
    removed_ids: tuple[int, ...]
    blocks: tuple[AmeshBlock, ...]
    removed_block_indices: tuple[int, ...]
    notes: tuple[str, ...]


class PolygonReferenceHandler:
    class_ids: tuple[str, ...] = ()
    writable = False

    def supports(self, block: AmeshBlock) -> bool:
        return (block.class_id or "").lower() in self.class_ids

    def parse_references(
            self, owner_path: str, block_index: int, block: AmeshBlock,
            memberships: dict[int, tuple[tuple, ...]],
            ) -> list[PolygonReference]:
        return []

    def rewrite(
            self, block_index: int, block: AmeshBlock,
            old_to_new: dict[int, int], removed_ids: set[int],
            polygon_count: int) -> AmeshBlock | None:
        return copy.deepcopy(block)


class AmeshPolygonReferenceHandler(PolygonReferenceHandler):
    class_ids = ("amesh.class",)
    writable = True

    def parse_references(
            self, owner_path: str, block_index: int, block: AmeshBlock,
            memberships: dict[int, tuple[tuple, ...]],
            ) -> list[PolygonReference]:
        material = block.texture.name if block.texture else ""
        animation = material if block.texture and block.texture.kind == "bmpanim" else ""
        return [
            PolygonReference(
                owner_path=owner_path,
                block_index=block_index,
                class_id=block.class_id,
                location=f"ATTS[{entry_index}].polyID",
                poly_id=entry.poly_id,
                writable=True,
                material_name=material,
                animation_name=animation,
                fx_memberships=memberships.get(entry.poly_id, ()),
            )
            for entry_index, entry in enumerate(block.atts)
        ]

    def rewrite(
            self, block_index: int, block: AmeshBlock,
            old_to_new: dict[int, int], removed_ids: set[int],
            polygon_count: int) -> AmeshBlock:
        atts_only = bool(
            block.texture is not None
            and block.texture.kind == "bmpanim"
            and not block.olpl)
        if not atts_only and len(block.atts) != len(block.olpl):
            raise PolygonReferenceError(
                f"amesh.class at ADES[{block_index}] has ambiguous "
                f"ATTS/OLPL counts ({len(block.atts)}/{len(block.olpl)})")
        rewritten = copy.deepcopy(block)
        rewritten.atts = []
        rewritten.olpl = []
        for entry_index, entry in enumerate(block.atts):
            if not (0 <= entry.poly_id < polygon_count):
                raise PolygonReferenceError(
                    f"amesh.class at ADES[{block_index}]/ATTS[{entry_index}] "
                    f"references invalid polyID {entry.poly_id}")
            if entry.poly_id in removed_ids:
                continue
            new_poly_id = old_to_new.get(entry.poly_id)
            if new_poly_id is None:
                raise PolygonReferenceError(
                    f"amesh.class at ADES[{block_index}]/ATTS[{entry_index}] "
                    f"has no renumber target for polyID {entry.poly_id}")
            rewritten.atts.append(AttsEntry(
                new_poly_id, entry.color_val, entry.shade_val,
                entry.tracy_val, entry.pad))
            if not atts_only:
                rewritten.olpl.append(copy.deepcopy(block.olpl[entry_index]))
        return rewritten


class AreaPolygonReferenceHandler(PolygonReferenceHandler):
    class_ids = ("area.class",)
    writable = True

    def parse_references(
            self, owner_path: str, block_index: int, block: AmeshBlock,
            memberships: dict[int, tuple[tuple, ...]],
            ) -> list[PolygonReference]:
        material = block.texture.name if block.texture else ""
        animation = material if block.texture and block.texture.kind == "bmpanim" else ""
        return [PolygonReference(
            owner_path=owner_path,
            block_index=block_index,
            class_id=block.class_id,
            location="FORM AREA/FORM ADE/STRC.polyID",
            poly_id=block.ade_poly_id,
            writable=True,
            material_name=material,
            animation_name=animation,
            fx_memberships=memberships.get(block.ade_poly_id, ()),
        )]

    def rewrite(
            self, block_index: int, block: AmeshBlock,
            old_to_new: dict[int, int], removed_ids: set[int],
            polygon_count: int) -> AmeshBlock | None:
        poly_id = block.ade_poly_id
        if not (0 <= poly_id < polygon_count):
            raise PolygonReferenceError(
                f"area.class at ADES[{block_index}]/FORM ADE/STRC "
                f"references invalid polyID {poly_id}")
        if len(block.atts) != 1 or block.atts[0].poly_id != poly_id:
            raise PolygonReferenceError(
                f"area.class at ADES[{block_index}] has an inconsistent "
                "derived polygon mapping")
        if poly_id in removed_ids:
            return None
        new_poly_id = old_to_new.get(poly_id)
        if new_poly_id is None:
            raise PolygonReferenceError(
                f"area.class at ADES[{block_index}] has no renumber target "
                f"for polyID {poly_id}")
        rewritten = copy.deepcopy(block)
        rewritten.ade_poly_id = new_poly_id
        entry = block.atts[0]
        rewritten.atts = [AttsEntry(
            new_poly_id, entry.color_val, entry.shade_val,
            entry.tracy_val, entry.pad)]
        return rewritten


class ParticlePolygonReferenceHandler(PolygonReferenceHandler):
    """particle.class PTCL/ATTS contains emitter data, not POL2 references."""

    class_ids = ("particle.class",)
    writable = True


_HANDLERS: tuple[PolygonReferenceHandler, ...] = (
    AmeshPolygonReferenceHandler(),
    AreaPolygonReferenceHandler(),
    ParticlePolygonReferenceHandler(),
)


def polygon_reference_handler(block: AmeshBlock) -> PolygonReferenceHandler | None:
    return next((handler for handler in _HANDLERS if handler.supports(block)), None)


def _fx_memberships(fx_elements) -> dict[int, tuple[tuple, ...]]:
    result: dict[int, list[tuple]] = {}
    for element in fx_elements or ():
        identity = tuple(getattr(element, "identity", ()))
        for poly_id in getattr(element, "poly_ids", ()):
            result.setdefault(poly_id, []).append(identity)
    return {poly_id: tuple(values) for poly_id, values in result.items()}


def build_polygon_reference_graph(
        fam_obj, fx_elements=()) -> PolygonReferenceGraph:
    model = getattr(fam_obj, "skeleton", None)
    blocks = tuple(
        getattr(getattr(fam_obj, "base_object", None), "ades", ()))
    graph = PolygonReferenceGraph(
        owner_path=getattr(fam_obj, "owner_path", "root"),
        polygon_count=len(model.polygons) if model is not None else 0,
        blocks=blocks,
    )
    memberships = _fx_memberships(fx_elements)
    for block_index, block in enumerate(blocks):
        handler = polygon_reference_handler(block)
        if handler is None:
            class_id = block.class_id or "<missing class>"
            payload = block.payload_form_type or "<unknown FORM>"
            graph.unknown_blocks.append((
                block_index, class_id,
                f"{graph.owner_path}/ADES[{block_index}]/{payload}"))
            continue
        for reference in handler.parse_references(
                graph.owner_path, block_index, block, memberships):
            graph.references.setdefault(reference.poly_id, []).append(reference)
    return graph


def references_for_polygon(
        graph: PolygonReferenceGraph, poly_id: int
        ) -> tuple[PolygonReference, ...]:
    return graph.references_for_polygon(poly_id)


def plan_polygon_renumber(
        graph: PolygonReferenceGraph, old_to_new: dict[int, int],
        removed_ids: Iterable[int] = ()) -> PolygonReferenceRewritePlan:
    removed = set(removed_ids)
    valid_ids = set(range(graph.polygon_count))
    if not removed.issubset(valid_ids):
        raise PolygonReferenceError(
            f"removed POL2 IDs are outside 0..{graph.polygon_count - 1}")
    expected_survivors = valid_ids - removed
    if set(old_to_new) != expected_survivors:
        missing = sorted(expected_survivors - set(old_to_new))
        extra = sorted(set(old_to_new) - expected_survivors)
        raise PolygonReferenceError(
            f"POL2 renumber map is incomplete (missing={missing}, "
            f"extra={extra})")
    expected_targets = set(range(len(expected_survivors)))
    if set(old_to_new.values()) != expected_targets:
        raise PolygonReferenceError(
            "POL2 renumber targets are not a contiguous unique range")
    changed_ids = sorted(
        removed | {
            old_id for old_id, new_id in old_to_new.items()
            if old_id != new_id
        })
    if graph.unknown_blocks and changed_ids:
        block_index, class_id, path = graph.unknown_blocks[0]
        raise PolygonReferenceError(
            f"class {class_id!r} at {path} has no typed POL2 handler; "
            f"affected polyIDs {changed_ids}")

    rewritten: list[AmeshBlock] = []
    removed_blocks: list[int] = []
    notes: list[str] = []
    for block_index, block in enumerate(graph.blocks):
        handler = polygon_reference_handler(block)
        if handler is None:
            rewritten.append(copy.deepcopy(block))
            continue
        updated = handler.rewrite(
            block_index, block, old_to_new, removed, graph.polygon_count)
        if updated is None:
            removed_blocks.append(block_index)
            notes.append(
                f"removed exclusive {block.class_id} at "
                f"{graph.owner_path}/ADES[{block_index}]")
        else:
            rewritten.append(updated)
    plan = PolygonReferenceRewritePlan(
        owner_path=graph.owner_path,
        old_to_new=tuple(sorted(old_to_new.items())),
        removed_ids=tuple(sorted(removed)),
        blocks=tuple(rewritten),
        removed_block_indices=tuple(removed_blocks),
        notes=tuple(notes),
    )
    validate_reference_rewrite(graph, plan)
    return plan


def validate_reference_rewrite(
        before: PolygonReferenceGraph,
        plan: PolygonReferenceRewritePlan) -> None:
    expected_count = before.polygon_count - len(plan.removed_ids)
    for block_index, block in enumerate(plan.blocks):
        handler = polygon_reference_handler(block)
        if handler is None:
            continue
        refs = handler.parse_references(
            plan.owner_path, block_index, block, {})
        for reference in refs:
            if not (0 <= reference.poly_id < expected_count):
                raise PolygonReferenceError(
                    f"{reference.class_id} at {reference.block_path}/"
                    f"{reference.location} rewrites to invalid polyID "
                    f"{reference.poly_id}")


def apply_reference_rewrite(
        blocks: list[AmeshBlock], plan: PolygonReferenceRewritePlan) -> None:
    blocks[:] = [copy.deepcopy(block) for block in plan.blocks]


def diagnose_polygon_references(fam_obj, fx_elements=()) -> list[str]:
    """Return dangling POL2/POO2/mapping/handler diagnostics."""

    issues: list[str] = []
    model = getattr(fam_obj, "skeleton", None)
    if model is None:
        return ["owner has no decoded skeleton"]
    for poly_id, polygon in enumerate(model.polygons):
        for point_index in polygon:
            if not (0 <= point_index < len(model.points)):
                issues.append(
                    f"POL2[{poly_id}] references invalid POO2 {point_index}")
    graph = build_polygon_reference_graph(fam_obj, fx_elements)
    for poly_id, refs in graph.references.items():
        if not (0 <= poly_id < graph.polygon_count):
            for ref in refs:
                issues.append(
                    f"{ref.block_path}/{ref.location} references invalid "
                    f"polyID {poly_id}")
    for block_index, block in enumerate(graph.blocks):
        class_id = (block.class_id or "").lower()
        if class_id == "amesh.class" and block.olpl \
                and len(block.atts) != len(block.olpl):
            issues.append(
                f"{graph.owner_path}/ADES[{block_index}] has ATTS/OLPL "
                f"count mismatch {len(block.atts)}/{len(block.olpl)}")
        if class_id == "area.class" and (
                len(block.atts) != 1
                or block.atts[0].poly_id != block.ade_poly_id):
            issues.append(
                f"{graph.owner_path}/ADES[{block_index}] area.class has an "
                "incomplete derived mapping")
    for _index, class_id, path in graph.unknown_blocks:
        issues.append(f"{path}: missing typed handler for {class_id!r}")
    for element in fx_elements or ():
        count = len(getattr(element, "poly_ids", ()))
        if count not in (1, 2) or (
                getattr(element, "bilateral", False) and count != 2):
            issues.append(
                f"{getattr(element, 'fx_name', 'FX')} element is incomplete")
    return issues
