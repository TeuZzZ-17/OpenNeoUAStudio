"""Optional OpenNeoUA level locations; polygon rules shared by the editor and preview."""
import math
import re

DEFAULT_LOCATION_COLOR = 'white'

# Names are stored in the LDF, so the engine and both guides use this same list.
LOCATION_COLORS = {
    'white': (240, 244, 233),
    'yellow': (255, 221, 87),
    'orange': (255, 155, 74),
    'red': (255, 105, 105),
    'pink': (255, 140, 200),
    'purple': (190, 130, 255),
    'blue': (110, 170, 255),
    'cyan': (100, 225, 235),
    'green': (110, 225, 130),
    'lime': (195, 235, 100),
    'gray': (185, 195, 205),
}


def location_color(value):
    color = str(value.get('color', DEFAULT_LOCATION_COLOR)).strip().lower()
    return color if color in LOCATION_COLORS else DEFAULT_LOCATION_COLOR


def new_location(identifier):
    return dict(id=identifier, name='New location', color=DEFAULT_LOCATION_COLOR, sound='', points=[],
                fade_in_ms=450, hold_ms=2800, fade_out_ms=750)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (abs(_cross(a, b, p)) <= 1e-7
            and min(a[0], b[0]) - 1e-7 <= p[0] <= max(a[0], b[0]) + 1e-7
            and min(a[1], b[1]) - 1e-7 <= p[1] <= max(a[1], b[1]) + 1e-7)


def contains_point(points, x, z):
    inside = False
    for a, b in zip(points, points[1:] + points[:1]):
        if _on_segment(a, b, (x, z)):
            return True
        if (a[1] > z) != (b[1] > z) and x < (b[0] - a[0]) * (z - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def validate_location(value):
    if type(value['id']) is not int or not 1 <= value['id'] <= 2147483647:
        raise ValueError('Location ID must be a positive integer.')
    if not value['name'].strip() or len(value['name']) > 96 or any(c in value['name'] for c in '\r\n;'):
        raise ValueError('Location name must contain 1–96 characters, without newlines or semicolons.')
    if value.get('color', DEFAULT_LOCATION_COLOR) not in LOCATION_COLORS:
        raise ValueError('Choose a valid location color: ' + ', '.join(LOCATION_COLORS) + '.')
    sound = value['sound']
    if sound and (not sound.startswith('Data/') or '\\' in sound or
                  any(c in sound for c in '\r\n;:') or any(part in ('', '.', '..') for part in sound.split('/'))):
        raise ValueError('Choose a sound inside Data using a Data/... path.')
    for field, maximum in (('fade_in_ms', 10000), ('hold_ms', 60000), ('fade_out_ms', 10000)):
        if type(value[field]) is not int or not 0 <= value[field] <= maximum:
            raise ValueError(f'{field} must be between 0 and {maximum} ms.')
    points = value['points']
    if not 3 <= len(points) <= 256 or len(set(map(tuple, points))) != len(points):
        raise ValueError('Close the perimeter with 3–256 distinct vertices.')
    if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError('Location coordinates must be finite X/Z pairs.')
    edges = list(zip(points, points[1:] + points[:1]))
    if abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) <= 1e-7:
        raise ValueError('The location perimeter must enclose an area.')
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            c, d = edges[j]
            crosses = _cross(a, b, c) * _cross(a, b, d) < 0 and _cross(c, d, a) * _cross(c, d, b) < 0
            if crosses or any((_on_segment(a, b, c), _on_segment(a, b, d),
                               _on_segment(c, d, a), _on_segment(c, d, b))):
                raise ValueError('Location perimeter edges must not intersect.')


def notice_opacity(value, elapsed):
    fade, hold, out = (value[k] for k in ('fade_in_ms', 'hold_ms', 'fade_out_ms'))
    if elapsed < 0 or elapsed >= fade + hold + out:
        return 0.0
    if fade and elapsed < fade:
        return elapsed / fade
    if elapsed < fade + hold:
        return 1.0
    return (fade + hold + out - elapsed) / out if out else 0.0


def parse_location_blocks(text):
    values = []
    pattern = re.compile(r'^\s*begin_location\s+(\d+)[ \t]*(?:;[^\r\n]*)?\r?\n(.*?)^\s*end[ \t]*(?:;[^\r\n]*)?\r?$', re.M | re.S | re.I)
    def read(match):
        value = new_location(int(match[1]))
        for raw in match[2].splitlines():
            line = raw.split(';', 1)[0].strip()
            if not line:
                continue
            key, separator, data = line.partition('=')
            key, data = key.strip().lower(), data.strip()
            if not separator:
                raise ValueError('Invalid location field: ' + line)
            if key in ('name', 'sound'):
                value[key] = data
            elif key == 'color':
                value[key] = data.lower()
            elif key in ('fade_in_ms', 'hold_ms', 'fade_out_ms'):
                value[key] = int(data)
            elif key == 'point':
                x, z = data.split('_')
                value['points'].append((float(x), float(z)))
            else:
                raise ValueError('Unknown location field: ' + key)
        validate_location(value)
        values.append(value)
        return ''
    rest = pattern.sub(read, text)
    if len(values) > 256 or len({v['id'] for v in values}) != len(values):
        raise ValueError('Use at most 256 locations with distinct IDs.')
    if re.search(r'^\s*begin_location\b', rest, re.M | re.I):
        raise ValueError('Invalid or unclosed location block.')
    return values, rest


def location_lines(values, format_number):
    if len(values) > 256 or len({v['id'] for v in values}) != len(values):
        raise ValueError('Use at most 256 locations with distinct IDs.')
    for value in values:
        validate_location(value)
        yield f'begin_location {value["id"]}'
        yield f'   name = {value["name"]}'
        color = value.get('color', DEFAULT_LOCATION_COLOR)
        if color != DEFAULT_LOCATION_COLOR:
            yield f'   color = {color}'
        if value['sound']:
            yield f'   sound = {value["sound"]}'
        for field in ('fade_in_ms', 'hold_ms', 'fade_out_ms'):
            yield f'   {field} = {value[field]}'
        for x, z in value['points']:
            yield f'   point = {format_number(x)}_{format_number(z)}'
        yield 'end'
        yield ';------------------------------------------------------------'
