"""Resolve named prototype states through the existing Studio asset loaders."""

from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache

from map_editor import bootstrap
from map_editor.core.asset_bridge import SetAssets
from vp_manager import reconstruct_embedded_vps, normalize_base_name


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
    from collision_editor.editor import _resolve_data_asset_path
    vp_table = None
    for state in ("normal", "fire", "wait", "dead", "megadeth", "genesis", "launch"):
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
