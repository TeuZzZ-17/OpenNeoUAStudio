"""Blender-style geometry Edit Mode session for skeleton vertices.

Holds the editing state for one FamilyObject's skeleton: vertex selection,
modal transform previews (grab / rotate / scale, with optional axis
constraint in model space), an undo/redo history, and commit of the edited
coordinates back into the in-memory SkltModel.

The session works entirely in *model space* (raw POO2 coordinates).  The
viewport converts screen input into model-space deltas and calls the
preview_* methods; committing writes ``model.points`` in place so every
other panel (stats, save, reload of the same family) sees the edit.

Only vertex *positions* change: the point count and the POL2 topology stay
untouched, which keeps the on-disk save path (`save_sklt_with_poo2_points`)
byte-safe.
"""

from __future__ import annotations

import math

from asset_family import FamilyObject
from sklt_parser import Point3D, SkltModel

Matrix3 = list[list[float]]

MAX_UNDO = 64


def invert_3x3(matrix: Matrix3) -> Matrix3 | None:
    """Inverse of a 3x3 matrix, or None when singular."""

    (a, b, c), (d, e, f), (g, h, i) = matrix
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    inv_det = 1.0 / det
    return [
        [(e * i - f * h) * inv_det, (c * h - b * i) * inv_det,
         (b * f - c * e) * inv_det],
        [(f * g - d * i) * inv_det, (a * i - c * g) * inv_det,
         (c * d - a * f) * inv_det],
        [(d * h - e * g) * inv_det, (b * g - a * h) * inv_det,
         (a * e - b * d) * inv_det],
    ]


def mat_apply(matrix: Matrix3, vector) -> tuple[float, float, float]:
    x, y, z = vector
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z,
    )


def normalize(vector) -> tuple[float, float, float] | None:
    x, y, z = vector
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return None
    return (x / length, y / length, z / length)


def rotate_around_axis(point, axis, angle: float, pivot):
    """Rodrigues rotation of ``point`` around ``axis`` through ``pivot``."""

    px = point[0] - pivot[0]
    py = point[1] - pivot[1]
    pz = point[2] - pivot[2]
    ux, uy, uz = axis
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    dot = ux * px + uy * py + uz * pz
    cx = uy * pz - uz * py
    cy = uz * px - ux * pz
    cz = ux * py - uy * px
    return (
        pivot[0] + px * cos_a + cx * sin_a + ux * dot * (1.0 - cos_a),
        pivot[1] + py * cos_a + cy * sin_a + uy * dot * (1.0 - cos_a),
        pivot[2] + pz * cos_a + cz * sin_a + uz * dot * (1.0 - cos_a),
    )


AXIS_VECTORS = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
}


class GeometryEditSession:
    """Vertex editing state for one skeleton-bearing family object."""

    def __init__(self, fam_obj: FamilyObject, matrix: Matrix3,
                 position: tuple[float, float, float]) -> None:
        if fam_obj.skeleton is None:
            raise ValueError("object has no skeleton to edit")
        self.fam_obj = fam_obj
        self.model: SkltModel = fam_obj.skeleton
        self.matrix = matrix
        self.position = position
        inverse = invert_3x3(matrix)
        self.inv_matrix = inverse if inverse is not None else [
            [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
        ]
        self.degenerate_transform = inverse is None
        self.selection: set[int] = set()
        self.dirty = False
        self._undo: list[list[Point3D]] = []
        self._redo: list[list[Point3D]] = []
        # Modal preview state
        self._pending: list[Point3D] | None = None
        self._modal_origin: list[Point3D] | None = None
        self._modal_pivot: tuple[float, float, float] | None = None

    # -- coordinate helpers ---------------------------------------------------

    def points(self) -> list[Point3D]:
        """Current coordinates: modal preview if active, else committed."""

        return self._pending if self._pending is not None else self.model.points

    def world_points(self) -> list[tuple[float, float, float]]:
        m, p = self.matrix, self.position
        return [
            (
                m[0][0] * x + m[0][1] * y + m[0][2] * z + p[0],
                m[1][0] * x + m[1][1] * y + m[1][2] * z + p[1],
                m[2][0] * x + m[2][1] * y + m[2][2] * z + p[2],
            )
            for x, y, z in self.points()
        ]

    def world_delta_to_model(self, delta) -> tuple[float, float, float]:
        return mat_apply(self.inv_matrix, delta)

    def world_dir_to_model(self, direction) -> tuple[float, float, float] | None:
        return normalize(mat_apply(self.inv_matrix, direction))

    def model_axis_world(self, axis: str) -> tuple[float, float, float] | None:
        """Model axis expressed in world space (for the constraint guide)."""

        return normalize(mat_apply(self.matrix, AXIS_VECTORS[axis]))

    # -- selection --------------------------------------------------------------

    def select_all(self) -> None:
        self.selection = set(range(len(self.model.points)))

    def select_none(self) -> None:
        self.selection = set()

    def set_selection(self, indices) -> None:
        """Replace the selection with validated POO2 vertex indices."""

        selected = set(indices)
        if any(not isinstance(index, int) for index in selected):
            raise ValueError("vertex selection indices must be integers")
        invalid = sorted(index for index in selected
                         if index < 0 or index >= len(self.model.points))
        if invalid:
            raise ValueError(
                f"vertex selection contains invalid POO2 index {invalid[0]}"
            )
        self.selection = selected

    def toggle(self, index: int) -> None:
        if index in self.selection:
            self.selection.discard(index)
        else:
            self.selection.add(index)

    def selection_pivot(self) -> tuple[float, float, float] | None:
        """Median-point pivot of the current selection, in model space."""

        if not self.selection:
            return None
        pts = self.model.points
        sel = [pts[i] for i in sorted(self.selection) if i < len(pts)]
        if not sel:
            return None
        n = float(len(sel))
        return (sum(p[0] for p in sel) / n,
                sum(p[1] for p in sel) / n,
                sum(p[2] for p in sel) / n)

    # -- modal transforms ---------------------------------------------------------

    @property
    def modal_active(self) -> bool:
        return self._modal_origin is not None

    def modal_origin_points(self) -> list[Point3D] | None:
        return (list(self._modal_origin)
                if self._modal_origin is not None else None)

    def begin_modal(self) -> bool:
        if not self.selection or self.modal_active:
            return False
        self._modal_origin = list(self.model.points)
        self._pending = list(self.model.points)
        self._modal_pivot = self.selection_pivot()
        return True

    def preview_grab(self, delta_model, axis: str | None = None) -> None:
        if self._modal_origin is None:
            return
        dx, dy, dz = delta_model
        if axis == "X":
            dy = dz = 0.0
        elif axis == "Y":
            dx = dz = 0.0
        elif axis == "Z":
            dx = dy = 0.0
        pending = list(self._modal_origin)
        for i in self.selection:
            if i < len(pending):
                x, y, z = self._modal_origin[i]
                pending[i] = (x + dx, y + dy, z + dz)
        self._pending = pending

    def preview_rotate(self, axis_vector, angle: float) -> None:
        if self._modal_origin is None or self._modal_pivot is None:
            return
        axis = normalize(axis_vector)
        if axis is None:
            return
        pending = list(self._modal_origin)
        for i in self.selection:
            if i < len(pending):
                pending[i] = rotate_around_axis(
                    self._modal_origin[i], axis, angle, self._modal_pivot
                )
        self._pending = pending

    def preview_scale(self, factor: float, axis: str | None = None) -> None:
        if self._modal_origin is None or self._modal_pivot is None:
            return
        fx = fy = fz = factor
        if axis == "X":
            fy = fz = 1.0
        elif axis == "Y":
            fx = fz = 1.0
        elif axis == "Z":
            fx = fy = 1.0
        cx, cy, cz = self._modal_pivot
        pending = list(self._modal_origin)
        for i in self.selection:
            if i < len(pending):
                x, y, z = self._modal_origin[i]
                pending[i] = (
                    cx + (x - cx) * fx,
                    cy + (y - cy) * fy,
                    cz + (z - cz) * fz,
                )
        self._pending = pending

    def preview_replace_selected(self, points) -> None:
        """Replace selected coordinates during a normal undoable modal edit."""

        if self._modal_origin is None:
            return
        indices = sorted(self.selection)
        replacements = list(points)
        if len(indices) != len(replacements):
            raise ValueError(
                "replacement point count must match the vertex selection")
        pending = list(self._modal_origin)
        for index, point in zip(indices, replacements):
            if index < len(pending):
                pending[index] = tuple(float(value) for value in point)
        self._pending = pending

    def cancel_modal(self) -> None:
        self._pending = None
        self._modal_origin = None
        self._modal_pivot = None

    def commit_modal(self) -> bool:
        """Apply the preview; returns True when geometry actually changed."""

        if self._modal_origin is None or self._pending is None:
            self.cancel_modal()
            return False
        changed = self._pending != self._modal_origin
        if changed:
            self._undo.append(list(self._modal_origin))
            del self._undo[:-MAX_UNDO]
            self._redo.clear()
            self.model.points[:] = self._pending
            self.dirty = True
        self.cancel_modal()
        return changed

    # -- history ---------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if self.modal_active or not self._undo:
            return False
        self._redo.append(list(self.model.points))
        self.model.points[:] = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if self.modal_active or not self._redo:
            return False
        self._undo.append(list(self.model.points))
        self.model.points[:] = self._redo.pop()
        self.dirty = True
        return True
