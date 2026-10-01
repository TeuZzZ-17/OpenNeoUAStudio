from __future__ import annotations

import copy
import io
import os
import re
import tempfile
from dataclasses import dataclass, field

SECTOR_STEP = 1200
SECTOR_OFFSET = 700
SECTOR_SIZE = SECTOR_STEP
HALF_SECTOR = SECTOR_SIZE // 2

DEFAULT_W, DEFAULT_H = 15, 15
DEFAULT_HGT = 0x7F
HGT_MIN = 0x61
HGT_MAX = 0x9D
EDITOR_HEIGHT_BASE = 30
EDITOR_HEIGHT_MIN = 0
EDITOR_HEIGHT_MAX = 60
MAX_SPECIAL_SLOTS = 10
MAX_HISTORY = 100
DEFAULT_HOST_POS_Y = -700
DEFAULT_HOST_RELOAD_CONST = 500000
DEFAULT_HOST_VIEWANGLE = 0
DEFAULT_SCRIPT_CONTENT = ";include data:scripts/startup2.scr"
DEFAULT_LDF_ENCODING = "cp1252"
TECH_FACTIONS = range(1, 8)

FACTIONS = {
    0: "NEUTRAL", 1: "RESISTANCE", 2: "SULGOGAR", 3: "MYKON",
    4: "TAERKASTEN", 5: "BLACK SECT", 6: "GHORKOV", 7: "DRONES",
}

GEM_ACTION_PARAMS_BY_TARGET = {
    "modify_vehicle": ("enable", "add_energy", "add_shield", "add_radar",
                       "add_unhide_radar", "num_weapons"),
    "modify_building": ("enable",),
    "modify_weapon": ("add_energy", "add_energy_heli", "add_energy_tank",
                      "add_energy_flyer", "add_energy_Robo", "add_shot_time",
                      "add_shot_time_user"),
}
GEM_ACTION_PARAMS_BY_TARGET_LOWER = {
    t: {p.lower(): p for p in params}
    for t, params in GEM_ACTION_PARAMS_BY_TARGET.items()
}

HOST_AI_BUDGET_FIELDS = ("con_budget", "def_budget", "rec_budget", "rob_budget",
                         "pow_budget", "rad_budget", "saf_budget", "cpl_budget")
HOST_AI_FIELDS = (
    "con_budget", "con_delay", "def_budget", "def_delay",
    "rec_budget", "rec_delay", "rob_budget", "rob_delay",
    "pow_budget", "pow_delay", "rad_budget", "rad_delay",
    "saf_budget", "saf_delay", "cpl_budget", "cpl_delay",
)
DEFAULT_HOST_AI_PRESET = "Balanced"
_BALANCED = {
    "con_budget": 70, "con_delay": 0, "def_budget": 70, "def_delay": 0,
    "rec_budget": 70, "rec_delay": 0, "rob_budget": 70, "rob_delay": 0,
    "pow_budget": 40, "pow_delay": 0, "rad_budget": 10, "rad_delay": 0,
    "saf_budget": 40, "saf_delay": 0, "cpl_budget": 20, "cpl_delay": 0,
}


def grid_to_world(col: int, row: int) -> tuple[int, int]:
    return col * SECTOR_STEP + SECTOR_OFFSET, -(row * SECTOR_STEP + SECTOR_OFFSET)


def world_to_grid(wx: float, wz: float) -> tuple[int, int]:
    return (int(round((wx - SECTOR_OFFSET) / SECTOR_STEP)),
            int(round((abs(wz) - SECTOR_OFFSET) / SECTOR_STEP)))


def make_host_ai(preset: str = DEFAULT_HOST_AI_PRESET) -> dict:
    ai = dict(_BALANCED)
    ai["preset"] = DEFAULT_HOST_AI_PRESET
    return ai


def clean_host_ai_values(values: dict) -> dict:
    clean = {}
    for name in HOST_AI_FIELDS:
        try:
            raw = int(values.get(name, _BALANCED[name]))
        except (TypeError, ValueError):
            raw = _BALANCED[name]
        raw = max(0, min(100, raw)) if name in HOST_AI_BUDGET_FIELDS else max(0, raw)
        clean[name] = raw
    return clean


def normalize_host_ai(ai=None) -> dict:
    if not isinstance(ai, dict):
        return make_host_ai()
    preset = str(ai.get("preset", DEFAULT_HOST_AI_PRESET))
    if not any(f in ai for f in HOST_AI_FIELDS) and preset != "Custom":
        return make_host_ai(preset)
    values = clean_host_ai_values(ai)
    if preset == "Custom" or any(values[f] != _BALANCED[f] for f in HOST_AI_FIELDS):
        values["preset"] = "Custom"
    else:
        values["preset"] = DEFAULT_HOST_AI_PRESET
    return values


def ensure_host_defaults(host: dict) -> None:
    host.setdefault("reload_const", DEFAULT_HOST_RELOAD_CONST)
    host.setdefault("viewangle", DEFAULT_HOST_VIEWANGLE)
    host["ai"] = normalize_host_ai(host.get("ai"))


def new_gate() -> dict:
    return {'active': False, 'x': -1, 'y': -1, 'keys': [], 'target': 0,
            'closed_bp': 25, 'opened_bp': 26, 'hidden': False}


def new_item() -> dict:
    return {'active': False, 'x': -1, 'y': -1, 'keys': [], 'countdown': 300000,
            'inactive_bp': 35, 'active_bp': 36, 'trigger_bp': 37, 'type': 1,
            'hidden': False}


def new_gem() -> dict:
    return {'x': -1, 'y': -1, 'blg': 50, 'type': 3, 'actions': [], 'hidden': False}


def new_lvl_info() -> dict:
    return {'title': "Untitled Map", 'sky': "objects/x7.bas", 'mbmap': "",
            'dbmap': "", 'music': "None", 'movie': "None"}


def new_tech() -> dict:
    return {i: {'veh': [], 'blg': []} for i in TECH_FACTIONS}


def decode_ldf_bytes(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="strict"), "utf-8-sig"
    try:
        text = data.decode("utf-8", errors="strict")
        enc = "utf-8" if any(ord(c) > 127 for c in text) else DEFAULT_LDF_ENCODING
        return text, enc
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252", errors="strict"), "cp1252"
        except UnicodeDecodeError:
            return data.decode("latin-1", errors="strict"), "latin-1"


def atomic_write_bytes(path: str, payload: bytes) -> None:
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or os.curdir
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def movie_filename(value) -> str:
    value = str(value or "").strip().strip("\"'")
    if not value or value.lower() == "none":
        return ""
    normalized = value.replace("\\", "/")
    while ":" in normalized:
        prefix, suffix = normalized.split(":", 1)
        if prefix.replace("\\", "/").lower().endswith("mov"):
            normalized = suffix
            continue
        break
    parts = [p for p in normalized.strip("/").split("/") if p]
    if not parts:
        return ""
    name = parts[-1].strip().strip("\"'")
    return "" if name.lower() == "none" else name


def movie_for_export(value) -> str:
    name = movie_filename(value)
    return f"mov:{name}" if name else ""


def briefing_base_name(value) -> str:
    value = str(value or "").strip().strip("\"'")
    if not value:
        return ""
    parts = [p for p in value.replace("\\", "/").strip("/").split("/") if p]
    return os.path.splitext(parts[-1].strip())[0] if parts else ""


def briefing_for_export(value, default_name="") -> str:
    raw = str(value or "").strip().strip("\"'")
    if not raw or raw.lower() in {"none", "no", "null", "-", "--"}:
        return ""
    name = raw.replace("\\", "/").strip("/").split("/")[-1]
    if not name:
        name = briefing_base_name(default_name)
    if not name:
        return ""
    stem, suffix = os.path.splitext(name)
    if suffix.casefold() == ".iff":
        return f"{stem}.IFF"
    return name if suffix else f"{name}.IFF"


def split_user_script_block(raw_text: str) -> tuple[str, str]:
    marker = re.search(r'(?im)^[ \t]*;[ \t]*---[ \t]*USER SCRIPT[ \t]*---[ \t\r]*$', raw_text)
    if not marker:
        return "", raw_text
    rest = raw_text[marker.end():]
    maps_match = re.search(r'(?im)^[ \t]*begin_maps\b', rest)
    block_end = marker.end() + maps_match.start() if maps_match else len(raw_text)
    script = raw_text[marker.end():block_end]
    if script.startswith("\r\n"):
        script = script[2:]
    elif script.startswith("\n") or script.startswith("\r"):
        script = script[1:]
    script = re.sub(r'(?m)(?:\r?\n)?^[ \t]*;-{5,}[ \t]*(?:\r?\n)?\s*$', '', script)
    return script, raw_text[:marker.start()] + raw_text[block_end:]


def _level_line_value(raw_text: str, key: str):
    in_level = False
    pattern = re.compile(rf'^\s*{re.escape(key)}\s*=\s*(.*)$', re.IGNORECASE)
    for line in raw_text.splitlines():
        lower = line.strip().lower()
        if lower == 'begin_level':
            in_level = True
            continue
        if in_level and lower == 'end':
            break
        if in_level:
            m = pattern.match(line)
            if m:
                return m.group(1).strip()
    return None


def _block_vehicle_comment_names(raw_text: str, block_name: str) -> list:
    names, in_block, current = [], False, None
    begin = re.compile(rf'^\s*{re.escape(block_name)}\b', re.IGNORECASE)
    veh = re.compile(r'^\s*vehicle\s*(?:=\s*)?\d+\b.*?;\s*(.*?)\s*$', re.IGNORECASE)
    for line in raw_text.splitlines():
        s = line.strip()
        if begin.match(s):
            in_block, current = True, None
            continue
        if in_block and s.lower() == 'end':
            names.append(current)
            in_block = False
            continue
        if in_block:
            m = veh.match(line)
            if m:
                current = m.group(1).strip() or None
    if in_block:
        names.append(current)
    return names


def _tech_comment_names(raw_text: str) -> dict:
    names, in_enable = {}, False
    entry = re.compile(r'^\s*(?:vehicle|building)\s*(?:=\s*)?(\d+)\b.*?;\s*(.*?)\s*$', re.IGNORECASE)
    for line in raw_text.splitlines():
        s = line.strip()
        if re.match(r'^\s*begin_enable\b', s, re.IGNORECASE):
            in_enable = True
            continue
        if in_enable and s.lower() == 'end':
            in_enable = False
            continue
        if in_enable:
            m = entry.match(line)
            if m and m.group(2).strip():
                names[int(m.group(1))] = m.group(2).strip()
    return names


@dataclass
class LdfDocument:
    mw: int = DEFAULT_W
    mh: int = DEFAULT_H
    set_number: int = 1
    grids: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    gems: dict = field(default_factory=dict)
    squads: list = field(default_factory=list)
    host_stations: list = field(default_factory=list)
    player_owner: int = 1
    tech: dict = field(default_factory=new_tech)
    custom_tech_names: dict = field(default_factory=dict)
    lvl_info: dict = field(default_factory=new_lvl_info)
    script_content: str = DEFAULT_SCRIPT_CONTENT
    visible_gate_slots: int = 0
    visible_item_slots: int = 0
    visible_gem_slots: int = 0
    encoding: str = DEFAULT_LDF_ENCODING
    newline: str = "\r\n"
    skipped_special: dict = field(default_factory=lambda: {'gate': 0, 'item': 0, 'gem': 0})

    def __post_init__(self):
        if not self.grids:
            self.reset_map()

    def reset_map(self, w: int | None = None, h: int | None = None) -> None:
        if w:
            self.mw = w
        if h:
            self.mh = h
        w, h = self.mw, self.mh
        self.grids = {
            'type': [['00'] * w for _ in range(h)],
            'own': [[0] * w for _ in range(h)],
            'hgt': [[DEFAULT_HGT] * w for _ in range(h)],
            'blg': [['00'] * w for _ in range(h)],
        }
        self.apply_borders()
        self.normalize_border_heights()
        self.gates = {i: new_gate() for i in range(1, MAX_SPECIAL_SLOTS + 1)}
        self.items = {i: new_item() for i in range(1, MAX_SPECIAL_SLOTS + 1)}
        self.gems = {i: new_gem() for i in range(1, MAX_SPECIAL_SLOTS + 1)}
        self.script_content = DEFAULT_SCRIPT_CONTENT
        self.visible_gate_slots = self.visible_item_slots = self.visible_gem_slots = 0
        self.squads, self.host_stations, self.player_owner = [], [], 1

    def snapshot(self) -> dict:
        return self._copy_state(self.__dict__)

    @staticmethod
    def _copy_state(state: dict) -> dict:
        # Grid cells are immutable int/string values. Copy rows in C instead of
        # walking hundreds of thousands of scalar values in Python on each click.
        result = copy.deepcopy({k: v for k, v in state.items() if k != 'grids'})
        result['grids'] = {key: [row[:] for row in grid] for key, grid in state['grids'].items()}
        return result

    def restore(self, snap: dict) -> None:
        self.__dict__.update(self._copy_state(snap))

    def cell_is_valid(self, obj) -> bool:
        if not obj:
            return False
        return 0 <= obj.get('x', -1) < self.mw and 0 <= obj.get('y', -1) < self.mh

    def apply_borders(self) -> None:
        w, h = self.mw, self.mh
        for r in range(h):
            for c in range(w):
                if r == 0:
                    t = 'f8' if c == 0 else 'f9' if c == w - 1 else 'fc'
                elif r == h - 1:
                    t = 'fb' if c == 0 else 'fa' if c == w - 1 else 'fe'
                elif c == 0:
                    t = 'ff'
                elif c == w - 1:
                    t = 'fd'
                else:
                    continue
                self.grids['type'][r][c] = t

    def normalize_border_heights(self) -> list:
        hgt = self.grids.get('hgt')
        if not hgt or self.mw < 3 or self.mh < 3:
            return []
        changed = []

        def put(r, c, v):
            if hgt[r][c] != v:
                hgt[r][c] = v
                changed.append((c, r))

        lc, lr = self.mw - 1, self.mh - 1
        for c in range(1, lc):
            put(0, c, hgt[1][c])
            put(lr, c, hgt[lr - 1][c])
        for r in range(1, lr):
            put(r, 0, hgt[r][1])
            put(r, lc, hgt[r][lc - 1])
        put(0, 0, hgt[1][1])
        put(0, lc, hgt[1][lc - 1])
        put(lr, 0, hgt[lr - 1][1])
        put(lr, lc, hgt[lr - 1][lc - 1])
        return changed

    def border_heights_need_normalize(self) -> bool:
        probe = copy.deepcopy(self.grids['hgt'])
        saved, self.grids['hgt'] = self.grids['hgt'], probe
        try:
            return bool(self.normalize_border_heights())
        finally:
            self.grids['hgt'] = saved

    def count_resize_out_of_bounds(self, width: int, height: int) -> dict:
        counts = dict.fromkeys(('gate_objects', 'gate_keys', 'item_objects',
                                'item_keys', 'gems', 'squads', 'hosts'), 0)
        inb = lambda x, y: 0 <= x < width and 0 <= y < height
        for key, objs in (('gate', self.gates), ('item', self.items)):
            for o in objs.values():
                if o['x'] != -1 and not inb(o['x'], o['y']):
                    counts[f'{key}_objects'] += 1
                    counts[f'{key}_keys'] += len(o['keys'])
                else:
                    counts[f'{key}_keys'] += sum(1 for kx, ky in o['keys'] if not inb(kx, ky))
        counts['gems'] = sum(1 for g in self.gems.values()
                             if g['x'] != -1 and not inb(g['x'], g['y']))
        counts['squads'] = sum(1 for s in self.squads if not inb(s['x'], s['y']))
        counts['hosts'] = sum(1 for h in self.host_stations if not inb(h['x'], h['y']))
        return counts

    def remove_out_of_bounds(self, width: int, height: int) -> None:
        inb = lambda x, y: 0 <= x < width and 0 <= y < height
        for objs in (self.gates, self.items):
            for o in objs.values():
                if o['x'] != -1 and not inb(o['x'], o['y']):
                    o['x'] = o['y'] = -1
                    o['keys'] = []
                else:
                    o['keys'] = [(kx, ky) for kx, ky in o['keys'] if inb(kx, ky)]
        for g in self.gems.values():
            if g['x'] != -1 and not inb(g['x'], g['y']):
                g['x'] = g['y'] = -1
        self.squads = [s for s in self.squads if inb(s['x'], s['y'])]
        self.host_stations = [h for h in self.host_stations if inb(h['x'], h['y'])]

    def resize(self, w: int, h: int, remove_out_of_bounds: bool = False) -> dict:
        counts = self.count_resize_out_of_bounds(w, h)
        if any(counts.values()) and not remove_out_of_bounds:
            raise ValueError(f"objects outside map after resize: {counts}")
        new = {
            'type': [['00'] * w for _ in range(h)],
            'own': [[0] * w for _ in range(h)],
            'hgt': [[DEFAULT_HGT] * w for _ in range(h)],
            'blg': [['00'] * w for _ in range(h)],
        }
        for r in range(min(self.mh, h)):
            for c in range(min(self.mw, w)):
                for k in new:
                    new[k][r][c] = self.grids[k][r][c]
        if any(counts.values()):
            self.remove_out_of_bounds(w, h)
        self.mw, self.mh, self.grids = w, h, new
        self.apply_borders()
        self.normalize_border_heights()
        return counts

    def sync_gem_buildings(self) -> bool:
        changed = False
        for i in range(1, self.visible_gem_slots + 1):
            g = self.gems[i]
            x, y = g.get('x', -1), g.get('y', -1)
            if not (0 <= x < self.mw and 0 <= y < self.mh):
                continue
            try:
                value = int(g.get('blg'))
            except (TypeError, ValueError):
                continue
            if 0 <= value <= 255 and str(self.grids['blg'][y][x]).lower() != f"{value:02x}":
                self.grids['blg'][y][x] = f"{value:02x}"
                changed = True
        return changed

    def host_stations_for_save(self, hosts=None) -> list:
        hosts = self.host_stations if hosts is None else hosts
        mine = [h for h in hosts if h.get('owner') == self.player_owner]
        if not hosts or not mine:
            return hosts
        return mine + [h for h in hosts if h.get('owner') != self.player_owner]


def _parse_tokens(text: str) -> list[str]:
    return re.sub(r';.*', '', text).replace('=', ' = ').split()


def loads_ldf(raw_text: str, encoding: str = DEFAULT_LDF_ENCODING) -> LdfDocument:
    newline = "\r\n" if "\r\n" in raw_text else "\n"
    script, parse_text = split_user_script_block(raw_text)
    raw_title = _level_line_value(parse_text, 'title_default')
    squad_names = _block_vehicle_comment_names(parse_text, 'begin_squad')
    host_names = _block_vehicle_comment_names(parse_text, 'begin_robo')
    tech_names = _tech_comment_names(parse_text)
    tokens = _parse_tokens(parse_text)

    w = h = 0
    it = iter(tokens)
    for tok in it:
        if tok.lower() == 'typ_map':
            nxt = next(it)
            if nxt == '=':
                w = int(next(it))
            else:
                w = int(nxt)
            h = int(next(it))
            break
    if w <= 0 or h <= 0:
        raise ValueError("unable to determine map dimensions")

    doc = LdfDocument(mw=w, mh=h)
    doc.script_content = script
    doc.lvl_info = new_lvl_info()
    doc.tech = new_tech()
    doc.encoding, doc.newline = encoding, newline
    set_match = re.search(r'set\s*=\s*(\d+)', parse_text, re.IGNORECASE)
    if set_match:
        doc.set_number = int(set_match.group(1))

    iterator = iter(tokens)
    pushed: list[str] = []
    skipped = doc.skipped_special
    squad_idx = host_idx = 0
    loaded = {}
    cur_gate = cur_item = cur_gem = cur_squad = cur_host = None
    cur_enable = None
    action_ctx = None
    reading_level = reading_mb = reading_db = False

    def next_token():
        return pushed.pop() if pushed else next(iterator)

    def get_val():
        v = next_token()
        return next_token() if v == '=' else v

    def read_key_pair():
        try:
            kx_raw = get_val()
        except StopIteration:
            return None
        try:
            kx = int(kx_raw)
        except ValueError:
            pushed.append(kx_raw)
            return None
        try:
            key_token = next_token()
        except StopIteration:
            return None
        if key_token.lower() != 'keysec_y':
            pushed.append(key_token)
            return None
        try:
            ky_raw = get_val()
        except StopIteration:
            return None
        try:
            ky = int(ky_raw)
        except ValueError:
            pushed.append(ky_raw)
            return None
        return (kx, ky)

    def free_slot(objs):
        for i in range(1, MAX_SPECIAL_SLOTS + 1):
            if objs[i]['x'] == -1:
                return i
        return -1

    def world_x(v):
        try:
            return max(0, min(w - 1, world_to_grid(v, 0)[0]))
        except Exception:
            return 0

    def world_y(v):
        try:
            return max(0, min(h - 1, world_to_grid(0, v)[1]))
        except Exception:
            return 0

    while True:
        try:
            token = next_token()
            t = token.lower()
            if t == 'begin_squad':
                name = squad_names[squad_idx] if squad_idx < len(squad_names) else None
                squad_idx += 1
                cur_squad = {'owner': 1, 'veh': 1, 'num': 1, 'hidden': False,
                             'useable': False, 'custom_name': name, 'x': -1, 'y': -1}
            elif t == 'begin_robo':
                name = host_names[host_idx] if host_idx < len(host_names) else None
                host_idx += 1
                cur_host = {'owner': 1, 'veh': 56, 'energy': 500000,
                            'pos_y': DEFAULT_HOST_POS_Y,
                            'reload_const': DEFAULT_HOST_RELOAD_CONST,
                            'viewangle': DEFAULT_HOST_VIEWANGLE,
                            'custom_name': name, 'x': -1, 'y': -1,
                            'hidden': False, 'ai': make_host_ai()}
            elif t == 'begin_level':
                reading_level = True
            elif t == 'begin_mbmap':
                reading_mb = True
            elif t == 'begin_dbmap':
                reading_db = True
            elif t == 'begin_gate':
                idx = free_slot(doc.gates)
                if idx != -1:
                    cur_gate = doc.gates[idx]
                    doc.visible_gate_slots = max(doc.visible_gate_slots, idx)
                else:
                    skipped['gate'] += 1
            elif t == 'begin_item':
                idx = free_slot(doc.items)
                if idx != -1:
                    cur_item = doc.items[idx]
                    doc.visible_item_slots = max(doc.visible_item_slots, idx)
                else:
                    skipped['item'] += 1
            elif t == 'begin_gem':
                idx = free_slot(doc.gems)
                if idx != -1:
                    cur_gem = doc.gems[idx]
                    doc.visible_gem_slots = max(doc.visible_gem_slots, idx)
                    cur_gem['actions'] = []
                    action_ctx = None
                else:
                    skipped['gem'] += 1
            elif t == 'begin_enable':
                try:
                    fid = int(next_token())
                    cur_enable = fid if fid in doc.tech else None
                except Exception:
                    cur_enable = None
            elif cur_enable is not None:
                if t == 'end':
                    cur_enable = None
                elif t == 'vehicle':
                    v = int(get_val())
                    if v not in doc.tech[cur_enable]['veh']:
                        doc.tech[cur_enable]['veh'].append(v)
                elif t == 'building':
                    b = int(get_val())
                    if b not in doc.tech[cur_enable]['blg']:
                        doc.tech[cur_enable]['blg'].append(b)
            elif cur_gem:
                if t == 'end':
                    if action_ctx:
                        action_ctx = None
                    else:
                        cur_gem = None
                elif t == 'sec_x':
                    cur_gem['x'] = int(get_val())
                elif t == 'sec_y':
                    cur_gem['y'] = int(get_val())
                elif t == 'building':
                    cur_gem['blg'] = int(get_val())
                elif t == 'type':
                    cur_gem['type'] = int(get_val())
                elif t.startswith('modify_'):
                    action_ctx = (t, int(get_val()))
                elif action_ctx:
                    param = GEM_ACTION_PARAMS_BY_TARGET_LOWER.get(action_ctx[0], {}).get(t)
                    if param:
                        cur_gem['actions'].append({
                            'target_type': action_ctx[0], 'id': action_ctx[1],
                            'param': param, 'val': get_val()})
                elif t == 'mb_status':
                    if get_val().lower() == 'unknown':
                        cur_gem['hidden'] = True
            elif t == 'end':
                if cur_squad:
                    if cur_squad['x'] != -1:
                        doc.squads.append(cur_squad)
                    cur_squad = None
                elif cur_host:
                    if cur_host['x'] != -1:
                        ensure_host_defaults(cur_host)
                        doc.host_stations.append(cur_host)
                    cur_host = None
                elif cur_gate:
                    cur_gate = None
                elif cur_item:
                    cur_item = None
                reading_level = reading_mb = reading_db = False
            elif reading_level:
                if t == 'sky':
                    doc.lvl_info['sky'] = get_val()
                elif t == 'title_default':
                    doc.lvl_info['title'] = get_val()
                elif t == 'ambiencetrack':
                    raw = get_val()
                    doc.lvl_info['music'] = raw.split('_')[0] if '_' in raw else raw
                elif t == 'movie':
                    doc.lvl_info['movie'] = movie_filename(get_val()) or "None"
            elif reading_mb:
                if t == 'name':
                    doc.lvl_info['mbmap'] = briefing_for_export(get_val())
            elif reading_db:
                if t == 'name':
                    doc.lvl_info['dbmap'] = briefing_for_export(get_val())
            elif cur_squad:
                if t == 'owner':
                    cur_squad['owner'] = int(get_val())
                elif t == 'vehicle':
                    cur_squad['veh'] = int(get_val())
                elif t == 'num':
                    cur_squad['num'] = int(get_val())
                elif t == 'useable':
                    cur_squad['useable'] = True
                elif t == 'mb_status':
                    if get_val().lower() == 'unknown':
                        cur_squad['hidden'] = True
                elif t == 'pos_x':
                    cur_squad['x'] = world_x(int(get_val()))
                elif t == 'pos_z':
                    cur_squad['y'] = world_y(int(get_val()))
            elif cur_host:
                if t == 'owner':
                    cur_host['owner'] = int(get_val())
                elif t == 'vehicle':
                    cur_host['veh'] = int(get_val())
                elif t == 'energy':
                    cur_host['energy'] = int(get_val())
                elif t == 'pos_y':
                    cur_host['pos_y'] = int(get_val())
                elif t == 'reload_const':
                    cur_host['reload_const'] = int(get_val())
                elif t == 'viewangle':
                    cur_host['viewangle'] = int(float(get_val()))
                elif t in HOST_AI_FIELDS:
                    cur_host.setdefault('ai', make_host_ai())[t] = int(get_val())
                elif t == 'mb_status':
                    if get_val().lower() == 'unknown':
                        cur_host['hidden'] = True
                elif t == 'pos_x':
                    cur_host['x'] = world_x(int(get_val()))
                elif t == 'pos_z':
                    cur_host['y'] = world_y(int(get_val()))
            elif cur_gate:
                if t == 'sec_x':
                    cur_gate['x'] = int(get_val())
                elif t == 'sec_y':
                    cur_gate['y'] = int(get_val())
                elif t == 'target_level':
                    cur_gate['target'] = int(get_val())
                elif t == 'closed_bp':
                    cur_gate['closed_bp'] = int(get_val())
                elif t == 'opened_bp':
                    cur_gate['opened_bp'] = int(get_val())
                elif t == 'mb_status':
                    if get_val().lower() == 'unknown':
                        cur_gate['hidden'] = True
                elif t == 'keysec_x':
                    pair = read_key_pair()
                    if pair:
                        cur_gate['keys'].append(pair)
            elif cur_item:
                if t == 'sec_x':
                    cur_item['x'] = int(get_val())
                elif t == 'sec_y':
                    cur_item['y'] = int(get_val())
                elif t in ('type', 'inactive_bp', 'active_bp', 'trigger_bp', 'countdown'):
                    cur_item[t] = int(get_val())
                elif t == 'mb_status':
                    if get_val().lower() == 'unknown':
                        cur_item['hidden'] = True
                elif t == 'keysec_x':
                    pair = read_key_pair()
                    if pair:
                        cur_item['keys'].append(pair)
            elif t in ('typ_map', 'own_map', 'hgt_map', 'blg_map'):
                v1 = next_token()
                if v1 == '=':
                    v1 = next_token()
                next_token()
                data = [next_token() for _ in range(w * h)]
                rows = []
                for r in range(h):
                    row_tokens = data[r * w:(r + 1) * w]
                    if t == 'own_map':
                        row = [int(x) for x in row_tokens]
                    elif t == 'hgt_map':
                        row = []
                        for x in row_tokens:
                            try:
                                row.append(int(x, 16))
                            except ValueError:
                                row.append(0)
                    else:
                        row = row_tokens
                    rows.append(row)
                loaded[t] = rows
        except StopIteration:
            break

    doc.grids['type'] = loaded.get('typ_map', [['00'] * w for _ in range(h)])
    doc.grids['own'] = loaded.get('own_map', [[0] * w for _ in range(h)])
    doc.grids['hgt'] = loaded.get('hgt_map', [[DEFAULT_HGT] * w for _ in range(h)])
    doc.grids['blg'] = loaded.get('blg_map', [['00'] * w for _ in range(h)])
    doc.normalize_border_heights()
    if raw_title is not None:
        doc.lvl_info['title'] = raw_title
    doc.custom_tech_names.update(tech_names)
    doc.player_owner = doc.host_stations[0]['owner'] if doc.host_stations else 1
    return doc


def load_ldf(path) -> LdfDocument:
    with open(path, "rb") as fh:
        text, encoding = decode_ldf_bytes(fh.read())
    return loads_ldf(text, encoding)


def dumps_ldf(doc: LdfDocument, defs: dict | None = None) -> str:
    defs = defs or {}
    veh_names, blg_names = defs.get('veh', {}), defs.get('blg', {})
    host_names = defs.get('host', {})
    le = doc.newline
    doc.normalize_border_heights()
    doc.sync_gem_buildings()
    f = io.StringIO(newline="")

    def w(txt):
        norm = str(txt).replace("\r\n", "\n").replace("\r", "\n")
        f.write(norm.replace("\n", le) + le)

    sep = ";------------------------------------------------------------"
    info = doc.lvl_info
    w("; --- Generated by Map Editor ---")
    w(sep)
    w("begin_level")
    w(f"   set = {doc.set_number}")
    w(f"   sky = {info['sky']}")
    for i, pal in enumerate(("standard", "red", "blau", "gruen", "inverse",
                             "invdark", "sw", "invtuerk")):
        w(f"   slot{i}        = palette/{pal}.pal")
    w(f"   title_default        = {info['title']}")
    w(f"   title_deutsch        = {info['title']}")
    w(f"   title_english        = {info['title']}")
    movie = movie_for_export(info.get('movie'))
    if movie:
        w(f"   movie                = {movie}")
    if info.get('music') and info['music'] != "None":
        w(f"   ambiencetrack        = {info['music']}")
    w("end")
    w(sep)
    for key, tag in (('mbmap', 'begin_mbmap'), ('dbmap', 'begin_dbmap')):
        name = briefing_for_export(info.get(key))
        if name:
            w(tag)
            w(f"   name          =  {name}")
            w("   size_x        = 480")
            w("   size_y        = 480")
            w("end")
            w(sep)
    for i in range(1, doc.visible_gate_slots + 1):
        g = doc.gates[i]
        if g['x'] != -1:
            w("begin_gate")
            w(f"   sec_x = {g['x']}\n   sec_y = {g['y']}")
            w(f"   target_level = {g['target']}")
            w(f"   closed_bp = {g['closed_bp']}")
            w(f"   opened_bp = {g['opened_bp']}")
            if g.get('hidden'):
                w("   mb_status = unknown")
            for kx, ky in g['keys']:
                w(f"   keysec_x = {kx}\n   keysec_y = {ky}")
            w("end")
            w(sep)
    for i in range(1, doc.visible_item_slots + 1):
        it = doc.items[i]
        if it['x'] != -1:
            w("begin_item")
            w(f"   sec_x = {it['x']}\n   sec_y = {it['y']}")
            w(f"   inactive_bp = {it['inactive_bp']}")
            w(f"   active_bp = {it['active_bp']}")
            w(f"   trigger_bp = {it['trigger_bp']}")
            w(f"   type = {it['type']}")
            w(f"   countdown = {it['countdown']}")
            if it.get('hidden'):
                w("   mb_status = unknown")
            for kx, ky in it['keys']:
                w(f"   keysec_x = {kx}\n   keysec_y = {ky}")
            w("end")
            w(sep)
    for i in range(1, doc.visible_gem_slots + 1):
        gm = doc.gems[i]
        if gm['x'] != -1:
            w("begin_gem")
            w(f"   sec_x = {gm['x']}\n   sec_y = {gm['y']}")
            w(f"   building = {gm['blg']}")
            w(f"   type = {gm['type']}")
            grouped: dict = {}
            for a in gm['actions']:
                grouped.setdefault((a['target_type'], a['id']), []).append(
                    f"      {a['param']} = {a['val']}")
            if grouped:
                w("   begin_action")
                for (tt, tid), lines in grouped.items():
                    w(f"      {tt} {tid}")
                    for ln in lines:
                        w(ln)
                    w("      end")
                w("   end_action")
            if gm.get('hidden'):
                w("   mb_status = unknown")
            w("end")
            w(sep)

    placed_hosts = [h for h in doc.host_stations if doc.cell_is_valid(h)]
    placed_squads = [s for s in doc.squads if doc.cell_is_valid(s)]
    for idx, h in enumerate(doc.host_stations_for_save(placed_hosts)):
        ensure_host_defaults(h)
        w("begin_robo")
        w(f"   owner = {h['owner']}")
        label = h['custom_name'] or host_names.get(h['veh'], '')
        w(f"   vehicle = {h['veh']} ; {label}")
        fx, fz = grid_to_world(h['x'], h['y'])
        w(f"   pos_x = {fx}")
        w(f"   pos_y = {h['pos_y']}")
        w(f"   pos_z = {fz}")
        w(f"   energy = {h['energy']}")
        if h.get('hidden'):
            w("   mb_status = unknown")
        w(f"   reload_const = {h.get('reload_const', DEFAULT_HOST_RELOAD_CONST)}")
        w(f"   viewangle = {h.get('viewangle', DEFAULT_HOST_VIEWANGLE)}")
        if idx > 0:
            ai = h['ai']
            for name in HOST_AI_FIELDS:
                w(f"   {name} = {ai[name]}")
        w("end")
        w(sep)
    for s in placed_squads:
        s.setdefault("useable", False)
        w("begin_squad")
        w(f"   owner = {s['owner']}")
        label = s['custom_name'] or veh_names.get(s['veh'], '')
        w(f"   vehicle = {s['veh']} ; {label}")
        w(f"   num = {s['num']}")
        if s.get('useable'):
            w("   useable")
        if s['hidden']:
            w("   mb_status = unknown")
        fx, fz = grid_to_world(s['x'], s['y'])
        w(f"   pos_x = {fx}")
        w(f"   pos_z = {fz}")
        w("end")
    w(sep)
    for i in TECH_FACTIONS:
        d = doc.tech[i]
        if d['veh'] or d['blg']:
            w(f"begin_enable {i}")
            for v in d['veh']:
                w(f"   vehicle = {v:<4} ; {doc.custom_tech_names.get(v, veh_names.get(v, ''))}")
            for b in d['blg']:
                w(f"   building = {b:<4} ; {doc.custom_tech_names.get(b, blg_names.get(b, ''))}")
            w("end")
    w(sep)
    w("; --- USER SCRIPT ---")
    script = doc.script_content or ""
    if script:
        norm = script.replace("\r\n", "\n").replace("\r", "\n")
        f.write(norm.replace("\n", le))
        if not norm.endswith("\n"):
            f.write(le)
    w(sep)
    w("begin_maps")
    for key, grid in (('typ_map', doc.grids['type']), ('own_map', doc.grids['own']),
                      ('hgt_map', doc.grids['hgt']), ('blg_map', doc.grids['blg'])):
        w(f"   {key} =")
        w(f"      {doc.mw} {doc.mh}")
        for row in grid:
            if 'own' in key:
                cells = [f"{x:02d}" for x in row]
            elif 'hgt' in key:
                cells = [f"{x:02x}" for x in row]
            else:
                cells = [str(x) for x in row]
            w("      " + " ".join(cells))
    w("end")
    return f.getvalue()


def save_ldf(doc: LdfDocument, path, defs: dict | None = None) -> None:
    payload = dumps_ldf(doc, defs).encode(doc.encoding, errors="strict")
    atomic_write_bytes(str(path), payload)
