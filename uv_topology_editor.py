"""Atomic POL2/POO2/OLPL vertex insertion for the model UV editor."""

from __future__ import annotations

from dataclasses import dataclass
import math


class UVTopologyError(ValueError):
    pass


@dataclass(frozen=True)
class UVVertexInsertion:
    poly_id: int
    atts_index: int
    edge_start: int
    insert_at: int
    new_point_index: int
    edge_point_indices: tuple[int, int]
    edge_uvs: tuple[tuple[int, int], tuple[int, int]]
    point: tuple[float, float, float]
    uv: tuple[int, int]


def plan_uv_vertex_insertion(
        model, block, poly_id: int, atts_index: int,
        edge_start: int) -> UVVertexInsertion:
    """Plan a midpoint inserted after one selected UV/winding vertex."""

    if (block.class_id or "").lower() != "amesh.class":
        raise UVTopologyError("only amesh.class OLPL mappings are editable")
    if not (0 <= poly_id < len(model.polygons)):
        raise UVTopologyError(f"polygon #{poly_id} is out of range")
    if not (0 <= atts_index < len(block.atts)):
        raise UVTopologyError("the ATTS mapping entry is unavailable")
    if block.atts[atts_index].poly_id != poly_id:
        raise UVTopologyError("the selected ATTS entry maps another polygon")
    if not (0 <= atts_index < len(block.olpl)):
        raise UVTopologyError("the matching OLPL group is unavailable")

    polygon = model.polygons[poly_id]
    uvs = block.olpl[atts_index]
    if len(polygon) < 3:
        raise UVTopologyError("the selected POL2 record is not a face")
    if len(uvs) != len(polygon):
        raise UVTopologyError(
            "POL2 winding and OLPL coordinate counts do not match")
    if not (0 <= edge_start < len(polygon)):
        raise UVTopologyError("select exactly one UV handle first")
    if len(model.points) >= 0x10000:
        raise UVTopologyError("POO2 already uses every unsigned 16-bit index")
    if len(polygon) >= 0x7FFF:
        raise UVTopologyError("the OLPL signed 16-bit vertex limit was reached")

    edge_end = (edge_start + 1) % len(polygon)
    point_indices = (polygon[edge_start], polygon[edge_end])
    if any(index < 0 or index >= len(model.points)
           for index in point_indices):
        raise UVTopologyError("the selected POL2 edge references invalid POO2")
    points = (model.points[point_indices[0]],
              model.points[point_indices[1]])
    if not all(math.isfinite(value) for point in points for value in point):
        raise UVTopologyError("the selected POO2 edge is not finite")
    edge_uvs = (tuple(uvs[edge_start]), tuple(uvs[edge_end]))
    if any(
            len(uv) != 2
            or any(value < 0 or value > 255 for value in uv)
            for uv in edge_uvs):
        raise UVTopologyError("the selected OLPL edge is outside byte range")

    point = tuple(
        (points[0][axis] + points[1][axis]) / 2.0
        for axis in range(3))
    uv = tuple(
        round((edge_uvs[0][axis] + edge_uvs[1][axis]) / 2.0)
        for axis in range(2))
    return UVVertexInsertion(
        poly_id=poly_id,
        atts_index=atts_index,
        edge_start=edge_start,
        insert_at=edge_start + 1,
        new_point_index=len(model.points),
        edge_point_indices=point_indices,
        edge_uvs=edge_uvs,
        point=point,
        uv=uv,
    )


def apply_uv_vertex_insertion(model, block,
                              plan: UVVertexInsertion) -> None:
    """Apply a still-current plan; callers provide transaction rollback."""

    if len(model.points) != plan.new_point_index:
        raise UVTopologyError("POO2 changed after the insertion was planned")
    polygon = model.polygons[plan.poly_id]
    uvs = block.olpl[plan.atts_index]
    edge_end = plan.insert_at % len(polygon)
    current_points = (
        polygon[plan.edge_start], polygon[edge_end])
    current_uvs = (
        tuple(uvs[plan.edge_start]), tuple(uvs[edge_end]))
    if current_points != plan.edge_point_indices \
            or current_uvs != plan.edge_uvs:
        raise UVTopologyError(
            "the selected POL2/OLPL edge changed before insertion")
    model.points.append(plan.point)
    polygon.insert(plan.insert_at, plan.new_point_index)
    uvs.insert(plan.insert_at, plan.uv)
