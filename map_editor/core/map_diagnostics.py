"""Read-only checks for authored map references and building functionality."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MapIssue:
    cell: tuple[int, int]
    severity: str
    message: str


def map_issues(doc, lib):
    """Check the active set and prototypes without changing the level."""
    if lib is None:
        return []
    issues = []
    # Special objects construct their own buildings after LoadBlgMap,
    # even at owner 0; they have separate activation rules.
    special_cells = {(value['x'], value['y'])
                     for store in (doc.gates, doc.items, doc.gems) for value in store.values()
                     if doc.cell_is_valid(value)}
    owners = {host['owner'] for host in doc.host_stations
              if host['owner'] > 0 and doc.cell_is_valid(host) and not host.get('_preview')
              and (visual := lib.vehicles.get(host['veh'])) is not None
              and visual.model.casefold() == 'robo'}
    checked = {}

    def check_type(typ):
        if typ not in checked:
            mesh = lib.mesh(typ)
            checked[typ] = list(mesh.resolved.warnings if mesh.resolved else ())
            checked[typ].extend(f'Missing model {name}' for name in mesh.missing)
        return checked[typ]

    for row in range(doc.mh):
        for col in range(doc.mw):
            cell = (col, row)
            typ = int(str(doc.grids['type'][row][col]), 16)
            bid = int(str(doc.grids['blg'][row][col]), 16)
            owner = doc.grids['own'][row][col]
            problems = list(check_type(typ))
            building = lib.buildings.get(bid) if bid else None
            if bid and building is None:
                problems.append(f'Building ID {bid} is not defined in the active scripts')
            if building is not None:
                problems.extend(check_type(building.sec_type))
                if building.has_power or building.has_guns:
                    name = building.name or f'Building {bid}'
                    if owner == 0 and cell not in special_cells:
                        issues.append(MapIssue(cell, 'warning',
                            f'{name}: owner 0; the game does not activate this building'))
                    elif owner not in owners:
                        if building.has_guns:
                            issues.append(MapIssue(cell, 'warning',
                                f'{name}: no active Host for owner {owner}; flak guns do not spawn'))
                        if building.has_power and owner > 0 and cell not in special_cells:
                            issues.append(MapIssue(cell, 'info',
                                f'{name}: no Host for owner {owner}; a neutral power station can be intentional'))
            for problem in dict.fromkeys(problems):
                issues.append(MapIssue(cell, 'error', problem))
    for actor in (*doc.host_stations, *doc.squads):
        if not doc.cell_is_valid(actor) or actor.get('_preview'):
            continue
        mesh = lib.actor_mesh(actor['veh'])
        for missing in dict.fromkeys(mesh.missing):
            issues.append(MapIssue((actor['x'], actor['y']), 'error',
                f'Vehicle {actor["veh"]}: missing {missing}'))
    return issues
