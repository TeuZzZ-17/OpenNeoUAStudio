"""Sector occupancy rules for editable squads and Host Stations."""

import copy


def actors_fit(doc, candidates, *, kind=None, exclude_squads=(), exclude_hosts=()):
    """Allow one squad and one Host Station per interior sector.

    Existing overlaps in loaded levels are not modified automatically.
    """
    excluded_squads = set(exclude_squads)
    excluded_hosts = set(exclude_hosts)
    proposed_squads, proposed_hosts = set(), set()
    for actor in candidates:
        cell = (actor['x'], actor['y'])
        if not (1 <= cell[0] < doc.mw - 1 and 1 <= cell[1] < doc.mh - 1):
            return False
        is_host = kind == 'host' if kind is not None else 'num' not in actor
        group = proposed_hosts if is_host else proposed_squads
        if cell in group:
            return False
        group.add(cell)
    for index, actor in enumerate(doc.squads):
        if index not in excluded_squads and (actor['x'], actor['y']) in proposed_squads:
            return False
    for index, actor in enumerate(doc.host_stations):
        if index not in excluded_hosts and (actor['x'], actor['y']) in proposed_hosts:
            return False
    return True


def host_copy_preview(original, cell):
    """Prepare a copy for placement without modifying the original host."""
    preview = copy.deepcopy(original)
    preview.update(x=cell[0], y=cell[1], _preview=True)
    # Explicit world coordinates belong to the old sector, not the new one.
    preview.pop('pos_x', None)
    preview.pop('pos_z', None)
    return preview
