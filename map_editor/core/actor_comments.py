"""Default comments of squads and host stations.

A record keeps its own ``custom_name`` only when the user wrote one. Otherwise the comment is
built from the record, so it always matches the current number, vehicle, count and energy.
"""


def vehicle_name(vehicles, veh):
    visual = vehicles.get(veh)
    return (visual.name if visual else '') or f'Vehicle {veh}'


def is_custom(record, vehicles):
    # LDF files label every block with the plain vehicle name; that is not a user comment.
    name = (record.get('custom_name') or '').strip()
    return bool(name) and name.casefold() != vehicle_name(vehicles, record.get('veh')).casefold()


def default_squad_comment(squads, index, vehicles):
    squad = squads[index]
    return f"Squad {index + 1} · {vehicle_name(vehicles, squad['veh'])} ×{squad['num']}"


def player_host(doc):
    placed = [host for host in doc.host_stations if doc.cell_is_valid(host) and not host.get('_preview')]
    ordered = doc.host_stations_for_save(placed)
    return ordered[0] if ordered else None


def default_host_comment(doc, index, vehicles):
    host = doc.host_stations[index]
    role = 'Player' if host is player_host(doc) else f"AI {host.get('ai', {}).get('preset', 'Custom')}"
    return f"{vehicle_name(vehicles, host['veh'])} · Energy {host['energy']:,} · {role}"


def squad_comment(doc, index, vehicles):
    squad = doc.squads[index]
    return squad['custom_name'] if is_custom(squad, vehicles) else default_squad_comment(doc.squads, index, vehicles)


def host_comment(doc, index, vehicles):
    host = doc.host_stations[index]
    return host['custom_name'] if is_custom(host, vehicles) else default_host_comment(doc, index, vehicles)
