"""Shared helpers of the Entity Designer window."""

from __future__ import annotations


from PySide6.QtCore import (
    Qt,
)

from studio.assets.family import AssetFamily
from studio.assets.visproto import (
    normalize_base_name,
    normalize_skeleton_name,
)


# Qt's QWIDGETSIZE_MAX: no maximum height.
_NO_HEIGHT_LIMIT = 16777215
_MODEL_NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 1


_SPHERE_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 2


_MODEL_VP_ROLE = int(Qt.ItemDataRole.UserRole) + 3


_FIRE_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 4


_GUN_POINT_INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 5


# Left-list row carrying the script unit it can load (VehicleModelReference).
_SCRIPT_REFERENCE_ROLE = int(Qt.ItemDataRole.UserRole) + 6


def _fx_item_state(item) -> str | None:
    """The model state a preview item belongs to (None = a plain effect)."""

    if item.kind == "unit":
        state = item.label.split(" · ", 1)[0]
        return "normal" if state == "attached" else state
    return {"plasma": "genesis", "decoration": "normal",
            "tracer": "normal", "cluster": "normal", "laser": "normal"}.get(item.kind)


def _vp_ids_per_model(family: AssetFamily, embedded, table) -> list[list[int]]:
    """VP ids of every model with a skeleton, in family order.

    ``embedded`` / ``table`` are the SET.BAS VP reconstruction and the active
    VP table; without them (a loose BASE) the model order itself is the VP id.
    """

    vp_ids_by_offset: dict[int, list[int]] = {}
    vp_ids_by_skeleton: dict[str, list[int]] = {}
    if embedded is not None and table is not None:
        embedded_by_base: dict[str, list] = {}
        for entry in embedded.entries:
            embedded_by_base.setdefault(
                normalize_base_name(entry.base_name), []).append(entry)
        for active_entry in table.entries:
            candidates = embedded_by_base.get(
                normalize_base_name(active_entry.base_name), [])
            selected_entry = None
            # Repeated dummy/BASE assignments are common.  Preserve the
            # positional match when the active table and embedded table
            # agree at this slot; otherwise accept only a unique match.
            if active_entry.index < len(embedded.entries):
                positional = embedded.entries[active_entry.index]
                if (normalize_base_name(positional.base_name)
                        == normalize_base_name(active_entry.base_name)):
                    selected_entry = positional
            if selected_entry is None and len(candidates) == 1:
                selected_entry = candidates[0]
            if selected_entry is None:
                continue
            if selected_entry.source_objt_offset >= 0:
                vp_ids_by_offset.setdefault(
                    selected_entry.source_objt_offset, []).append(
                        active_entry.index)
            key = normalize_skeleton_name(selected_entry.skeleton_name)
            if key:
                vp_ids_by_skeleton.setdefault(key, []).append(
                    active_entry.index)
    result: list[list[int]] = []
    for obj in family.all_objects():
        if obj.skeleton is None:
            continue
        source_offset = int(getattr(
            obj.base_object, "source_objt_offset", -1))
        vp_ids = list(vp_ids_by_offset.get(source_offset, []))
        if not vp_ids and not vp_ids_by_offset:
            skeleton_key = normalize_skeleton_name(
                obj.base_object.skeleton_name or "")
            candidates = vp_ids_by_skeleton.get(skeleton_key, [])
            if candidates:
                vp_ids = list(dict.fromkeys(candidates))
        if not vp_ids and embedded is None:
            vp_ids = [len(result)]
        result.append(sorted(set(vp_ids)))
    return result
