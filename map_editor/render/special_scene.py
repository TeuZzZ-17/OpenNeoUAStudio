"""Special sectors share geometry, picking IDs and styles with map actors."""
from dataclasses import dataclass

from ..core.special_objects import SPECIAL_KINDS, special_store, special_slots, special_cell, AUTO_VISUALS

SPECIAL_COLORS = {'gate': (95, 200, 240), 'item': (240, 125, 160), 'gem': (120, 220, 125)}


@dataclass(frozen=True)
class SpecialMember:
    kind: str
    slot: int
    key: int
    cell: tuple
    typ: int
    building: int
    preview: bool = False


def special_members(doc, lib=None):
    for kind in SPECIAL_KINDS:
        for slot in special_slots(doc, kind):
            value = special_store(doc, kind)[slot]
            cell = special_cell(value)
            if cell is None or not (1 <= cell[0] < doc.mw - 1 and 1 <= cell[1] < doc.mh - 1):
                continue
            x, y = cell
            building = value['closed_bp'] if kind == 'gate' else value['inactive_bp'] if kind == 'item' else value['blg']
            definition = lib.buildings.get(building) if lib is not None else None
            typ = definition.sec_type if definition is not None else int(str(doc.grids['type'][y][x]), 16)
            if definition is None and kind in AUTO_VISUALS:
                fields = ('closed_bp', 'opened_bp') if kind == 'gate' else ('inactive_bp', 'active_bp', 'trigger_bp')
                visual = AUTO_VISUALS[kind].get(tuple(value[f] for f in fields))
                if visual:
                    typ = int(visual[0], 16)
            yield SpecialMember(kind, slot, -1, cell, typ, building, value.get('_preview', False))
            for index, cell in enumerate(value.get('keys', [])):
                x, y = cell
                if not (1 <= x < doc.mw - 1 and 1 <= y < doc.mh - 1):
                    continue
                typ = ('f4' if value.get('_key_roads', {}).get(index) else 'f3') if kind == 'item' and value.get('_preview') else str(doc.grids['type'][y][x])
                yield SpecialMember(kind, slot, index, (x, y), int(typ, 16),
                                    int(str(doc.grids['blg'][y][x]), 16), value.get('_preview', False))


def special_cells(doc, lib=None):
    # Main objects own the visible geometry where a key shares their sector.
    result = {}
    for index, member in enumerate(special_members(doc, lib)):
        if member.cell not in result or member.key < 0:
            result[member.cell] = (index, member)
    return result


def scene_object_styles(view):
    if view.doc is None:
        return []
    doc = view.doc
    selected = getattr(getattr(view, 'squad_overlay', None), 'selected', set())
    dragged = getattr(view, 'dragged_codes', set())
    styles = []
    for index, value in enumerate(doc.squads + doc.host_stations):
        code = -(doc.mw * doc.mh + index + 1)
        preview = value.get('_preview') or code in dragged
        is_selected = index in selected if index < len(doc.squads) else index - len(doc.squads) == getattr(view, 'selected_host', -1)
        color = (160, 160, 160) if preview else view.owner_colors.get(value['owner'], (150, 150, 150))
        styles.append((*color, 3 if preview else 2 if is_selected else 1))
    selection = getattr(view, 'selected_special', None)
    for index, member in enumerate(special_members(doc, view.lib)):
        code = -(doc.mw * doc.mh + len(doc.squads) + len(doc.host_stations) + index + 1)
        preview = member.preview or code in dragged
        is_selected = selection == (member.kind, member.slot, member.key)
        color = (160, 160, 160) if preview else SPECIAL_COLORS[member.kind]
        styles.append((*color, 3 if preview else 2 if is_selected else 1))
    return styles


def actor_code(doc, special_index):
    return -(doc.mw * doc.mh + len(doc.squads) + len(doc.host_stations) + special_index + 1)
