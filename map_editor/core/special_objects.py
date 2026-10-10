"""Shared authoring rules for the existing LDF special-object slots."""
from __future__ import annotations

import copy

from .ldf_model import MAX_SPECIAL_SLOTS, new_gate, new_item, new_gem

SPECIAL_KINDS = {
    'gate': ('gates', 'visible_gate_slots', new_gate, 'Beamgate'),
    'item': ('items', 'visible_item_slots', new_item, 'Super item'),
    'gem': ('gems', 'visible_gem_slots', new_gem, 'Gem'),
}
AUTO_VISUALS = {
    'gate': {(5, 6): ('ca', '05'), (25, 26): ('03', '19')},
    'item': {(35, 36, 37): ('f5', '23'), (68, 69, 70): ('eb', '44')},
}


def special_store(doc, kind):
    return getattr(doc, SPECIAL_KINDS[kind][0])


def special_slots(doc, kind):
    return range(1, getattr(doc, SPECIAL_KINDS[kind][1]) + 1)


def special_cell(value):
    x, y = value.get('x', -1), value.get('y', -1)
    return (x, y) if x >= 0 and y >= 0 else None


def add_special(doc, kind):
    count = getattr(doc, SPECIAL_KINDS[kind][1])
    if count >= MAX_SPECIAL_SLOTS:
        raise ValueError(f'Only {MAX_SPECIAL_SLOTS} {SPECIAL_KINDS[kind][3].lower()} slots are available.')
    slot = count + 1
    special_store(doc, kind)[slot] = SPECIAL_KINDS[kind][2]()
    setattr(doc, SPECIAL_KINDS[kind][1], slot)
    return slot


def _visual(kind, value, buildings=None):
    if kind == 'gem':
        definition = (buildings or {}).get(value['blg'])
        typ = f'{definition.sec_type:02x}' if definition is not None else None
        return typ, f"{value['blg']:02x}"
    fields = ('closed_bp', 'opened_bp') if kind == 'gate' else ('inactive_bp', 'active_bp', 'trigger_bp')
    return AUTO_VISUALS[kind].get(tuple(value[f] for f in fields))


def _applied_visual(kind, value, buildings=None):
    if '_visual_applied' in value:
        return value['_visual_applied']
    # An LDF stores the current terrain, not the terrain under a placed gem.
    # Keep that terrain when removing a loaded gem; only its building is known.
    return _visual(kind, value) if kind == 'gem' else _visual(kind, value, buildings)


def _restore_visual(doc, cell, visual, before=None):
    if cell is None or visual is None:
        return
    x, y = cell
    if not (0 <= x < doc.mw and 0 <= y < doc.mh):
        return
    typ, blg = visual
    if str(doc.grids['blg'][y][x]).lower() != blg:
        return
    if typ is not None and str(doc.grids['type'][y][x]).lower() != typ:
        return
    doc.grids['blg'][y][x] = before[1] if before else '00'
    if typ is not None:
        doc.grids['type'][y][x] = before[0] if before else '00'


def _sync_visual(doc, kind, old, value, buildings=None):
    old_cell, cell = special_cell(old), special_cell(value)
    old_visual = _applied_visual(kind, old, buildings)
    visual = _visual(kind, value, buildings)
    if old_cell == cell and (old_visual == visual or kind == 'gem' and old['blg'] == value['blg']):
        return
    _restore_visual(doc, old_cell, old_visual, old.get('_visual_before'))
    value.pop('_visual_before', None)
    value.pop('_visual_applied', None)
    if cell is None or visual is None:
        return
    x, y = cell
    # Respect an authored building for gates/items, as Sektor2 does.
    if kind != 'gem' and str(doc.grids['blg'][y][x]).lower() != '00':
        return
    value['_visual_before'] = (doc.grids['type'][y][x], doc.grids['blg'][y][x])
    value['_visual_applied'] = visual
    typ, blg = visual
    if typ is not None:
        doc.grids['type'][y][x] = typ
    doc.grids['blg'][y][x] = blg


def _validate_cell(doc, cell):
    if not (1 <= cell[0] < doc.mw - 1 and 1 <= cell[1] < doc.mh - 1):
        raise ValueError('Choose a sector inside the map border.')


def _occupied(doc, cell, kind, slot):
    for other_kind in SPECIAL_KINDS:
        for other_slot in special_slots(doc, other_kind):
            if (other_kind, other_slot) != (kind, slot) and special_cell(special_store(doc, other_kind)[other_slot]) == cell:
                return True
    return False


def update_special(doc, kind, slot, changes, buildings=None):
    """Validate first, then update data and its matching map visual together."""
    old = special_store(doc, kind)[slot]
    value = copy.deepcopy(old)
    value.update(copy.deepcopy(changes))
    if old == value:
        return False
    cell = special_cell(value)
    if cell is not None and cell != special_cell(old):
        _validate_cell(doc, cell)
        if _occupied(doc, cell, kind, slot):
            raise ValueError('This sector already contains another object.')
        if kind == 'gem' and not value.get('actions'):
            raise ValueError('Add at least one gem effect before placing it.')
    if kind in ('gate', 'item'):
        seen = set()
        for key in value['keys']:
            key = tuple(key)
            _validate_cell(doc, key)
            if key == cell or key in seen:
                raise ValueError('Key sectors must be unique and separate from objects.')
            for other_kind in ('gate', 'item'):
                for other_slot in special_slots(doc, other_kind):
                    if (other_kind, other_slot) != (kind, slot) and key in special_store(doc, other_kind)[other_slot]['keys']:
                        raise ValueError('This sector already contains another key.')
            seen.add(key)
    _sync_visual(doc, kind, old, value, buildings)
    if kind == 'item':
        old_keys, keys = set(map(tuple, old['keys'])), set(map(tuple, value['keys']))
        saved = copy.deepcopy(old.get('_key_visuals', {}))
        moved = {}
        for index, key in enumerate(value['keys']):
            if index < len(old['keys']) and key not in old_keys and old['keys'][index] not in keys:
                x, y = old['keys'][index]
                if 0 <= x < doc.mw and 0 <= y < doc.mh:
                    typ = str(doc.grids['type'][y][x]).lower()
                    moved[tuple(key)] = typ if typ in ('f3', 'f4') else 'f3'
        for key in old_keys - keys:
            info = saved.pop(key, None)
            x, y = key
            if not (0 <= x < doc.mw and 0 <= y < doc.mh):
                continue
            current = str(doc.grids['type'][y][x]).lower()
            if info is not None and current == info['applied']:
                doc.grids['type'][y][x] = info['before']
            elif info is None and current in ('f3', 'f4'):
                doc.grids['type'][y][x] = '00'
        for x, y in keys - old_keys:
            typ = moved.get((x, y), 'f3')
            saved[(x, y)] = {'before': doc.grids['type'][y][x], 'applied': typ}
            doc.grids['type'][y][x] = typ
        value['_key_visuals'] = saved
    special_store(doc, kind)[slot] = value
    return True


def set_item_key_road(doc, slot, index, road):
    value = doc.items[slot]
    x, y = value['keys'][index]
    _validate_cell(doc, (x, y))
    typ = 'f4' if road else 'f3'
    current = str(doc.grids['type'][y][x]).lower()
    if current == typ:
        return False
    saved = value.setdefault('_key_visuals', {})
    info = saved.get((x, y))
    before = info['before'] if info is not None and current == info['applied'] else doc.grids['type'][y][x]
    saved[(x, y)] = {'before': before, 'applied': typ}
    doc.grids['type'][y][x] = typ
    return True


def remove_special(doc, kind, slot, buildings=None):
    store = special_store(doc, kind)
    old = store[slot]
    if kind == 'item':
        update_special(doc, kind, slot, {'x': -1, 'y': -1, 'keys': []})
    else:
        _restore_visual(doc, special_cell(old), _applied_visual(kind, old, buildings), old.get('_visual_before'))
    count = getattr(doc, SPECIAL_KINDS[kind][1])
    for index in range(slot, count):
        store[index] = store[index + 1]
    store[count] = SPECIAL_KINDS[kind][2]()
    setattr(doc, SPECIAL_KINDS[kind][1], count - 1)
