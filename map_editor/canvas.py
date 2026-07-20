import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox
import os
import time

from .constants import *
from .paths import resource_path

class CanvasMixin:
    MAP_BORDER_TAG = "map_outer_border"
    MAP_BORDER_COLOR = "#00FFFF"
    HOVER_TAG = "map_hover_overlay"
    HOVER_COLOR = "#B8F4FF"

    OWNER_PANEL_LABELS = {
        0: "Neutral",
        1: "Resistance",
        2: "Sulgogar",
        3: "Mykonian",
        4: "Taerkasten",
        5: "Black Sect",
        6: "Ghorkov",
        7: "Drones",
    }

    def has_main_object(self, cx, cy, exclude_mode=None, exclude_slot=None):
        for i in range(1, MAX_SPECIAL_SLOTS + 1):
            if exclude_mode == "GATE" and exclude_slot == i: continue
            if self.gates[i]['x'] == cx and self.gates[i]['y'] == cy: return True

        for i in range(1, MAX_SPECIAL_SLOTS + 1):
            if exclude_mode == "ITEM" and exclude_slot == i: continue
            if self.items[i]['x'] == cx and self.items[i]['y'] == cy: return True

        for i in range(1, MAX_SPECIAL_SLOTS + 1):
            if exclude_mode == "GEM" and exclude_slot == i: continue
            if self.gems[i]['x'] == cx and self.gems[i]['y'] == cy: return True
        return False

    def has_key(self, cx, cy):
        for i in range(1, MAX_SPECIAL_SLOTS + 1):
            if (cx, cy) in self.gates[i]['keys']: return True
            if (cx, cy) in self.items[i]['keys']: return True
        return False

    def can_place_special_key(self, cx, cy, current_data=None, dragging_key_list=None, dragging_key_idx=-1):
        if current_data and current_data.get('x') == cx and current_data.get('y') == cy:
            return False, "parent"

        for gate in self.gates.values():
            for idx, key_cell in enumerate(gate['keys']):
                if key_cell != (cx, cy):
                    continue
                if gate['keys'] is dragging_key_list and idx == dragging_key_idx:
                    continue
                return False, "key"

        for item in self.items.values():
            for idx, key_cell in enumerate(item['keys']):
                if key_cell != (cx, cy):
                    continue
                if item['keys'] is dragging_key_list and idx == dragging_key_idx:
                    continue
                return False, "key"

        return True, None

    GATE_AUTO_VISUALS = {
        (5, 6): ("ca", "05"),
        (25, 26): ("03", "19"),
    }
    ITEM_AUTO_VISUALS = {
        (35, 36, 37): ("f5", "23"),
        (68, 69, 70): ("eb", "44"),
    }
    ITEM_KEY_AUTO_VISUALS = {
        "NO_ROAD": "f3",
        "WITH_ROADS": "f4",
    }
    DEFAULT_ITEM_KEY_AUTO_VISUAL = "f3"

    def get_special_cell(self, data):
        if not data or data.get('x', -1) == -1 or data.get('y', -1) == -1:
            return None
        return (data['x'], data['y'])

    def get_special_auto_visual(self, mode, data):
        if mode == "GATE":
            key = (data.get('closed_bp'), data.get('opened_bp'))
            return self.GATE_AUTO_VISUALS.get(key)
        if mode == "ITEM":
            key = (data.get('inactive_bp'), data.get('active_bp'), data.get('trigger_bp'))
            return self.ITEM_AUTO_VISUALS.get(key)
        return None

    def format_gem_building_hex(self, building_id):
        try:
            value = int(building_id)
        except (TypeError, ValueError):
            return None
        if not (0 <= value <= 255):
            return None
        return f"{value:02x}"

    def get_gem_cell(self, data):
        if not data or data.get('x', -1) == -1 or data.get('y', -1) == -1:
            return None
        x, y = data['x'], data['y']
        if not (0 <= x < self.mw and 0 <= y < self.mh):
            return None
        return (x, y)

    def apply_gem_building_visual(self, data, cell=None, building_id=None, mark_dirty=True):
        cell = cell or self.get_gem_cell(data)
        building_hex = self.format_gem_building_hex(data.get('blg') if building_id is None else building_id)
        if not cell or building_hex is None:
            return False
        x, y = cell
        if str(self.grids['blg'][y][x]).lower() == building_hex:
            return False
        self.grids['blg'][y][x] = building_hex
        if mark_dirty:
            self.dirty = True
        return True

    def clear_gem_building_visual_if_matches(self, data, cell=None, building_id=None, mark_dirty=True):
        cell = cell or self.get_gem_cell(data)
        building_hex = self.format_gem_building_hex(data.get('blg') if building_id is None else building_id)
        if not cell or building_hex is None:
            return False
        x, y = cell
        if str(self.grids['blg'][y][x]).lower() != building_hex:
            return False
        self.grids['blg'][y][x] = "00"
        if mark_dirty:
            self.dirty = True
        return True

    def sync_gem_building_visual(self, data, old_cell=None, old_building=None, mark_dirty=True):
        changed = False
        new_cell = self.get_gem_cell(data)
        if old_cell and old_cell != new_cell:
            changed = self.clear_gem_building_visual_if_matches(
                data,
                cell=old_cell,
                building_id=old_building if old_building is not None else data.get('blg'),
                mark_dirty=mark_dirty
            ) or changed
        if new_cell:
            changed = self.apply_gem_building_visual(data, cell=new_cell, mark_dirty=mark_dirty) or changed
        return changed

    def sync_all_gem_building_visuals(self, mark_dirty=True):
        changed = False
        for i in range(1, getattr(self, "visible_gem_slots", 1) + 1):
            changed = self.sync_gem_building_visual(self.gems[i], mark_dirty=mark_dirty) or changed
        return changed

    def cell_matches_auto_visual(self, cell, visual):
        if not cell or not visual:
            return False
        x, y = cell
        if not (0 <= x < self.mw and 0 <= y < self.mh):
            return False
        typ, blg = visual
        return (
            str(self.grids['type'][y][x]).lower() == typ.lower() and
            str(self.grids['blg'][y][x]).lower() == blg.lower()
        )

    def get_cell_visual(self, cell):
        if not cell:
            return None
        x, y = cell
        if not (0 <= x < self.mw and 0 <= y < self.mh):
            return None
        return (str(self.grids['type'][y][x]), str(self.grids['blg'][y][x]))

    def same_visual(self, visual_a, visual_b):
        if not visual_a or not visual_b:
            return not visual_a and not visual_b
        return (
            str(visual_a[0]).lower() == str(visual_b[0]).lower() and
            str(visual_a[1]).lower() == str(visual_b[1]).lower()
        )

    def cell_is_current_object_auto_visual(self, cell, data):
        if not cell or not data:
            return False
        applied_cell = data.get('_auto_visual_cell')
        applied_visual = data.get('_auto_visual_applied')
        return applied_cell == cell and self.cell_matches_auto_visual(cell, applied_visual)

    def cell_has_manual_building_for_auto_visual(self, cell, data=None):
        visual = self.get_cell_visual(cell)
        if not visual:
            return True
        _typ, blg = visual
        if blg.lower() == "00":
            return False
        return not self.cell_is_current_object_auto_visual(cell, data)

    def forget_auto_visual_metadata(self, data):
        if not data:
            return
        data.pop('_auto_visual_cell', None)
        data.pop('_auto_visual_before', None)
        data.pop('_auto_visual_applied', None)

    def restore_auto_visual_if_matches(self, data, fallback_cell=None, fallback_visual=None):
        cell = data.get('_auto_visual_cell') if data else None
        visual = data.get('_auto_visual_applied') if data else None
        before = data.get('_auto_visual_before') if data else None

        if not cell:
            cell = fallback_cell
        if not visual:
            visual = fallback_visual

        changed = False
        if self.cell_matches_auto_visual(cell, visual):
            x, y = cell
            restore_typ, restore_blg = before if before else ("00", "00")
            self.grids['type'][y][x] = restore_typ
            self.grids['blg'][y][x] = restore_blg
            changed = True

        self.forget_auto_visual_metadata(data)
        return changed

    def clear_auto_visual_if_matches(self, cell, visual, before=None):
        if not self.cell_matches_auto_visual(cell, visual):
            return False
        x, y = cell
        restore_typ, restore_blg = before if before else ("00", "00")
        self.grids['type'][y][x] = restore_typ
        self.grids['blg'][y][x] = restore_blg
        return True

    def set_auto_visual_if_safe(self, cell, new_visual, old_visual=None, data=None):
        if not cell or not new_visual:
            return False
        if self.cell_has_manual_building_for_auto_visual(cell, data):
            return False

        if self.cell_matches_auto_visual(cell, new_visual):
            return False

        before = self.get_cell_visual(cell)
        x, y = cell
        typ, blg = new_visual
        self.grids['type'][y][x] = typ
        self.grids['blg'][y][x] = blg
        if data is not None:
            data['_auto_visual_cell'] = cell
            data['_auto_visual_before'] = before
            data['_auto_visual_applied'] = new_visual
        return True

    def sync_special_auto_visual(self, mode, data, old_cell=None, old_visual=None):
        new_cell = self.get_special_cell(data)
        new_visual = self.get_special_auto_visual(mode, data)
        changed = False

        visual_changed = not self.same_visual(old_visual, new_visual)
        if old_cell and (old_cell != new_cell or visual_changed):
            changed = self.restore_auto_visual_if_matches(
                data,
                fallback_cell=old_cell,
                fallback_visual=old_visual
            ) or changed

        if new_cell:
            changed = self.set_auto_visual_if_safe(new_cell, new_visual, old_visual, data) or changed

        if changed:
            self.dirty = True
        return changed

    def clear_special_auto_visual(self, mode, data):
        cell = self.get_special_cell(data)
        visual = self.get_special_auto_visual(mode, data)
        changed = self.restore_auto_visual_if_matches(
            data,
            fallback_cell=cell,
            fallback_visual=visual
        )
        if changed:
            self.dirty = True
        return changed

    def get_item_key_visual_typ(self, data):
        if not data:
            return self.DEFAULT_ITEM_KEY_AUTO_VISUAL
        stored_visual = data.get('_key_visual_typ')
        if stored_visual is None:
            for cell in data.get('keys', []):
                cell_typ = self.get_cell_typ(cell)
                if cell_typ and str(cell_typ).lower() == self.ITEM_KEY_AUTO_VISUALS["WITH_ROADS"]:
                    return self.ITEM_KEY_AUTO_VISUALS["WITH_ROADS"]
            return self.DEFAULT_ITEM_KEY_AUTO_VISUAL
        visual = str(stored_visual).lower()
        if visual not in self.ITEM_KEY_AUTO_VISUALS.values():
            return self.DEFAULT_ITEM_KEY_AUTO_VISUAL
        return visual

    def get_cell_typ(self, cell):
        if not cell:
            return None
        x, y = cell
        if not (0 <= x < self.mw and 0 <= y < self.mh):
            return None
        return str(self.grids['type'][y][x])

    def get_item_key_visual_meta(self, data, cell):
        visuals = data.get('_key_visuals') if data else None
        if not visuals:
            return None
        return visuals.get(cell)

    def apply_item_key_visual(self, data, cell, visual_typ=None):
        if not data or not cell:
            return False
        visual_typ = str(visual_typ or self.get_item_key_visual_typ(data)).lower()
        if visual_typ not in self.ITEM_KEY_AUTO_VISUALS.values():
            return False
        old_typ = self.get_cell_typ(cell)
        if old_typ is None:
            return False

        meta = self.get_item_key_visual_meta(data, cell)
        if str(old_typ).lower() == visual_typ:
            return False

        x, y = cell
        self.grids['type'][y][x] = visual_typ
        visuals = data.setdefault('_key_visuals', {})
        if meta:
            meta['applied'] = visual_typ
        else:
            visuals[cell] = {'before': old_typ, 'applied': visual_typ}
        return True

    def restore_item_key_visual_if_matches(self, data, cell):
        if not data or not cell:
            return False
        visuals = data.get('_key_visuals')
        if not visuals:
            return False
        meta = visuals.pop(cell, None)
        if not meta:
            return False
        current_typ = self.get_cell_typ(cell)
        if current_typ is None:
            return False
        if str(current_typ).lower() != str(meta.get('applied', '')).lower():
            return False
        x, y = cell
        self.grids['type'][y][x] = meta.get('before', "00")
        return True

    def clear_all_item_key_visuals(self, data):
        changed = False
        for cell in list((data.get('_key_visuals') or {}).keys()):
            changed = self.restore_item_key_visual_if_matches(data, cell) or changed
        return changed

    def sync_item_key_visual_preset(self, data, old_visual_typ, new_visual_typ):
        if not data:
            return []
        changed_cells = []
        old_visual_typ = str(old_visual_typ or self.DEFAULT_ITEM_KEY_AUTO_VISUAL).lower()
        new_visual_typ = str(new_visual_typ or self.DEFAULT_ITEM_KEY_AUTO_VISUAL).lower()
        if old_visual_typ == new_visual_typ:
            return changed_cells

        for cell in list(data.get('keys', [])):
            current_typ = self.get_cell_typ(cell)
            if current_typ is None:
                continue
            current_typ_lower = str(current_typ).lower()
            meta = self.get_item_key_visual_meta(data, cell)
            if meta:
                can_update = current_typ_lower == str(meta.get('applied', '')).lower()
            else:
                can_update = (
                    current_typ_lower == old_visual_typ or
                    current_typ_lower in self.ITEM_KEY_AUTO_VISUALS.values()
                )
            if not can_update:
                continue

            x, y = cell
            self.grids['type'][y][x] = new_visual_typ
            visuals = data.setdefault('_key_visuals', {})
            if meta:
                meta['applied'] = new_visual_typ
            else:
                visuals[cell] = {'before': current_typ, 'applied': new_visual_typ}
            changed_cells.append(cell)
        if changed_cells:
            self.dirty = True
        return changed_cells


    def find_gate_at(self, cx, cy, exclude_slot=None):
        for i in reversed(range(1, getattr(self, "visible_gate_slots", 0) + 1)):
            if exclude_slot is not None and i == exclude_slot:
                continue
            gate = self.gates[i]
            if gate.get('x') == cx and gate.get('y') == cy:
                return i
        return -1

    def find_gate_key_at(self, cx, cy):
        for i in reversed(range(1, getattr(self, "visible_gate_slots", 0) + 1)):
            gate = self.gates[i]
            for idx, cell in enumerate(gate.get('keys', [])):
                if cell == (cx, cy):
                    return i, idx
        return -1, -1

    def find_item_at(self, cx, cy, exclude_slot=None):
        for i in reversed(range(1, getattr(self, "visible_item_slots", 0) + 1)):
            if exclude_slot is not None and i == exclude_slot:
                continue
            item = self.items[i]
            if item.get('x') == cx and item.get('y') == cy:
                return i
        return -1

    def find_item_key_at(self, cx, cy):
        for i in reversed(range(1, getattr(self, "visible_item_slots", 0) + 1)):
            item = self.items[i]
            for idx, cell in enumerate(item.get('keys', [])):
                if cell == (cx, cy):
                    return i, idx
        return -1, -1

    def find_gem_at(self, cx, cy, exclude_slot=None):
        for i in reversed(range(1, getattr(self, "visible_gem_slots", 0) + 1)):
            if exclude_slot is not None and i == exclude_slot:
                continue
            gem = self.gems[i]
            if gem.get('x') == cx and gem.get('y') == cy:
                return i
        return -1

    def find_squad_at(self, cx, cy, exclude_index=None):
        for i in reversed(range(len(self.squads))):
            if exclude_index is not None and i == exclude_index:
                continue
            s = self.squads[i]
            if s['x'] == cx and s['y'] == cy:
                return i
        return -1

    def has_squad(self, cx, cy, exclude_index=None):
        return self.find_squad_at(cx, cy, exclude_index=exclude_index) != -1

    def find_host_at(self, cx, cy, exclude_index=None):
        for i in reversed(range(len(self.host_stations))):
            if exclude_index is not None and i == exclude_index:
                continue
            h = self.host_stations[i]
            if h['x'] == cx and h['y'] == cy:
                return i
        return -1

    def has_host(self, cx, cy, exclude_index=None):
        return self.find_host_at(cx, cy, exclude_index=exclude_index) != -1

    def get_cell_label_font(self, size):
        if not hasattr(self, "_cell_label_fonts"):
            self._cell_label_fonts = {}
        size = int(size)
        if size not in self._cell_label_fonts:
            self._cell_label_fonts[size] = tkfont.Font(family="Arial", size=size, weight="bold")
        return self._cell_label_fonts[size]

    def fit_label_text(self, text, sz, min_font=7, max_font=None, padding=12):
        text = str(text)
        min_font = int(min_font)
        max_font = int(max_font if max_font is not None else max(7, sz // 8))
        max_font = max(min_font, max_font)
        max_width = max(1, int(sz - padding))

        for font_size in range(max_font, min_font - 1, -1):
            label_font = self.get_cell_label_font(font_size)
            text_width = label_font.measure(text)
            if text_width <= max_width:
                return text, font_size, text_width

        label_font = self.get_cell_label_font(min_font)
        ellipsis = "..."
        if label_font.measure(ellipsis) > max_width:
            final_text = ellipsis
            while final_text and label_font.measure(final_text) > max_width:
                final_text = final_text[:-1]
            return final_text, min_font, label_font.measure(final_text)

        base = text
        while base and label_font.measure(base.rstrip() + ellipsis) > max_width:
            base = base[:-1]
        final_text = (base.rstrip() + ellipsis) if base.strip() else ellipsis
        while final_text and label_font.measure(final_text) > max_width:
            final_text = final_text[:-1]
        return final_text, min_font, label_font.measure(final_text)

    def fit_cell_multiline_text(self, text, sz, min_font=4, max_font=None, padding=8):
        lines = str(text).splitlines() or [str(text)]
        min_font = int(min_font)
        max_font = int(max_font if max_font is not None else max(8, sz // 4))
        max_font = max(min_font, max_font)
        max_width = max(1, int(sz - padding))
        max_height = max(1, int(sz - padding))

        for font_size in range(max_font, min_font - 1, -1):
            label_font = self.get_cell_label_font(font_size)
            line_height = label_font.metrics("linespace")
            text_height = line_height * len(lines)
            widest_line = max(label_font.measure(line) for line in lines)
            if widest_line <= max_width and text_height <= max_height:
                return "\n".join(lines), label_font

        label_font = self.get_cell_label_font(min_font)
        if label_font.metrics("linespace") * len(lines) > max_height:
            return "", label_font

        fitted_lines = []
        for line in lines:
            fitted_line, _, _ = self.fit_label_text(line, sz, min_font=min_font, max_font=min_font, padding=padding)
            fitted_lines.append(fitted_line)
        return "\n".join(fitted_lines), label_font

    def draw_cell_label(self, x, y, sz, text, fill_color, text_color, tag, position="bottom"):
        label_text, font_size, text_width = self.fit_label_text(text, sz)
        label_font = self.get_cell_label_font(font_size)

        cell_margin = 2
        pad_x = 4
        max_plate_w = max(1, sz - (cell_margin * 2))
        plate_w = min(max_plate_w, max(1, text_width + (pad_x * 2)))
        cx = x + sz // 2
        x1 = max(x + cell_margin, cx - (plate_w // 2))
        x2 = min(x + sz - cell_margin, x1 + plate_w)
        x1 = max(x + cell_margin, x2 - plate_w)

        plate_h = min(max(1, sz - (cell_margin * 2)), label_font.metrics("linespace") + 4)
        if position == "top":
            y1 = y + cell_margin
            y2 = min(y + sz - cell_margin, y1 + plate_h)
        else:
            y2 = y + sz - cell_margin
            y1 = max(y + cell_margin, y2 - plate_h)
        self.cv_map.create_rectangle(x1, y1, x2, y2, fill=fill_color, outline="black", tags=tag)
        self.cv_map.create_text(cx, (y1 + y2) // 2, text=label_text, fill=text_color, font=label_font, tags=tag)

    def draw_building_overlay_icons(self, x, y, sz, bid, tag, canvas=None, palette=False):
        if canvas is None:
            canvas = self.cv_map

        icons = self.get_building_icons(bid, palette=palette)[:4]
        if not icons:
            return
        count = len(icons)
        if count <= 2:
            icon_sz = max(8, min(sz - 8, int(sz * 0.34)))
        else:
            icon_sz = max(7, min((sz - 10) // 2, int(sz * 0.30)))
        if icon_sz <= 0:
            return

        gap = max(2, sz // 18)
        cx = x + sz // 2
        cy = y + sz // 2
        positions = []

        if count == 1:
            positions = [(cx - icon_sz // 2, cy - icon_sz // 2)]
        elif count == 2:
            group_w = (icon_sz * 2) + gap
            left = cx - group_w // 2
            top = cy - icon_sz // 2
            positions = [(left, top), (left + icon_sz + gap, top)]
        else:
            group_w = (icon_sz * 2) + gap
            group_h = (icon_sz * 2) + gap
            left = cx - group_w // 2
            top = cy - group_h // 2
            positions = [
                (left, top),
                (left + icon_sz + gap, top),
                (left + (group_w - icon_sz) // 2, top + icon_sz + gap),
            ]
            if count >= 4:
                positions[2] = (left, top + icon_sz + gap)
                positions.append((left + icon_sz + gap, top + icon_sz + gap))

        for icon_name, (ix, iy) in zip(icons, positions):
            img = self.get_img('building_overlay', icon_name, icon_sz)
            if img:
                ix = max(x + 2, min(x + sz - icon_sz - 2, ix))
                iy = max(y + 2, min(y + sz - icon_sz - 2, iy))
                canvas.create_image(ix, iy, image=img, anchor=tk.NW, tags=tag)

    def draw_building_markers(self, x, y, sz, tag, color=MARKER_COLOR):
        inset = max(3, sz // 16)
        max_segment_len = max(2, sz - (inset * 2))
        segment_len = min(max(8, sz // 3), max_segment_len)
        half_len = segment_len // 2
        width = 3 if sz >= 48 else 2
        cx = x + sz // 2
        cy = y + sz // 2

        left = max(x + inset, cx - half_len)
        right = min(x + sz - inset, cx + half_len)
        top = max(y + inset, cy - half_len)
        bottom = min(y + sz - inset, cy + half_len)

        self.cv_map.create_line(left, y + inset, right, y + inset, fill=color, width=width, tags=tag)
        self.cv_map.create_line(left, y + sz - inset, right, y + sz - inset, fill=color, width=width, tags=tag)
        self.cv_map.create_line(x + inset, top, x + inset, bottom, fill=color, width=width, tags=tag)
        self.cv_map.create_line(x + sz - inset, top, x + sz - inset, bottom, fill=color, width=width, tags=tag)

    def get_editor_height_value(self, r, c):
        try:
            return int(self.grids['hgt'][r][c]) - HGT_MIN
        except Exception:
            return 30

    def draw_text_with_outline(self, canvas, x, y, text, fill, font, tag, anchor=tk.NW, outline="#000000", width=1):
        offsets = []
        for dx in range(-width, width + 1):
            for dy in range(-width, width + 1):
                if dx == 0 and dy == 0:
                    continue
                if abs(dx) == width or abs(dy) == width:
                    offsets.append((dx, dy))

        for dx, dy in offsets:
            canvas.create_text(x + dx, y + dy, text=text, fill=outline, font=font, anchor=anchor, tags=tag)
        canvas.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor, tags=tag)

    def draw_cell_outline(self, x, y, sz, tag, color=SELECTION_COLOR, width=SELECTION_OUTLINE_WIDTH, inset=2):
        outline_width = max(width, SELECTION_OUTLINE_WIDTH)
        shadow_inset = max(0, inset - 2)
        self.cv_map.create_rectangle(
            x + shadow_inset,
            y + shadow_inset,
            x + sz - shadow_inset,
            y + sz - shadow_inset,
            outline=SELECTION_SHADOW_COLOR,
            width=outline_width + 2,
            tags=tag
        )
        self.cv_map.create_rectangle(
            x + inset,
            y + inset,
            x + sz - inset,
            y + sz - inset,
            outline=color,
            width=outline_width,
            tags=tag
        )

    def draw_owner_indicator(self, x, y, sz, fid, tag):
        base_color = FACTION_MAP_COLORS.get(fid, FACTIONS.get(fid, (None, None))[1])
        if not base_color:
            return

        if fid in FACTION_MAP_OUTLINES:
            outline_color = FACTION_MAP_OUTLINES[fid]
            self.cv_map.create_rectangle(
                x + 2, y + 2,
                x + sz - 2, y + sz - 2,
                outline=outline_color, width=1, tags=tag
            )
            self.cv_map.create_rectangle(
                x + 4, y + 4,
                x + sz - 4, y + sz - 4,
                outline=base_color, width=2, tags=tag
            )
            return

        self.cv_map.create_rectangle(
            x + 1, y + 1,
            x + sz - 1, y + sz - 1,
            outline=base_color, width=1, tags=tag
        )

    def add_render_index_entry(self, index, cell, value):
        x, y = cell
        if x == -1 or y == -1:
            return
        index.setdefault((x, y), []).append(value)

    def cell_has_host_underlay_object(self, c, r, bid=None):
        """Return True when a Host Station shares its cell with a visible main object.

        This is used only for rendering: if a building, gem, beamgate, bomb
        or related keysector is under a Host Station, the host icon is drawn
        semi-transparent so the object below remains readable.
        """
        if bid is None:
            bid = self.grids['blg'][r][c]
        if str(bid).lower() != '00':
            return True

        cell = (c, r)
        return bool(
            self.render_gate_objects_by_cell.get(cell) or
            self.render_gate_keys_by_cell.get(cell) or
            self.render_item_objects_by_cell.get(cell) or
            self.render_item_keys_by_cell.get(cell) or
            self.render_gems_by_cell.get(cell)
        )

    def rebuild_render_indexes(self):
        self.render_gate_objects_by_cell = {}
        self.render_gate_keys_by_cell = {}
        self.render_item_objects_by_cell = {}
        self.render_item_keys_by_cell = {}
        self.render_gems_by_cell = {}
        self.render_squads_by_cell = {}
        self.render_hosts_by_cell = {}

        for i in range(1, self.visible_gate_slots + 1):
            g = self.gates[i]
            self.add_render_index_entry(self.render_gate_objects_by_cell, (g['x'], g['y']), i)
            for key_cell in g['keys']:
                self.add_render_index_entry(self.render_gate_keys_by_cell, key_cell, i)

        for i in range(1, self.visible_item_slots + 1):
            it = self.items[i]
            self.add_render_index_entry(self.render_item_objects_by_cell, (it['x'], it['y']), i)
            for key_cell in it['keys']:
                self.add_render_index_entry(self.render_item_keys_by_cell, key_cell, i)

        for i in range(1, self.visible_gem_slots + 1):
            gm = self.gems[i]
            self.add_render_index_entry(self.render_gems_by_cell, (gm['x'], gm['y']), i)

        for i, squad in enumerate(self.squads):
            self.add_render_index_entry(self.render_squads_by_cell, (squad['x'], squad['y']), i)

        for i, host in enumerate(self.host_stations):
            self.add_render_index_entry(self.render_hosts_by_cell, (host['x'], host['y']), i)

        self._render_indexes_valid = True

    def ensure_render_indexes(self):
        if not getattr(self, "_render_indexes_valid", False):
            self.rebuild_render_indexes()

    def invalidate_render_indexes(self):
        self._render_indexes_valid = False

    def get_squad_cell(self, index):
        if index is None or index < 0 or index >= len(self.squads):
            return None
        squad = self.squads[index]
        if squad['x'] == -1 or squad['y'] == -1:
            return None
        return (squad['x'], squad['y'])

    def get_host_cell(self, index):
        if index is None or index < 0 or index >= len(self.host_stations):
            return None
        host = self.host_stations[index]
        if host['x'] == -1 or host['y'] == -1:
            return None
        return (host['x'], host['y'])

    def redraw_cells(self, *cells):
        if not hasattr(self, 'cv_map'):
            return

        unique_cells = []
        seen = set()
        for cell in cells:
            if not cell:
                continue
            c, r = cell
            if not (0 <= c < self.mw and 0 <= r < self.mh):
                continue
            if (c, r) in seen:
                continue
            seen.add((c, r))
            unique_cells.append((c, r))

        if not unique_cells:
            return

        self.rebuild_render_indexes()
        self.cv_map.delete(self.MAP_BORDER_TAG)
        for c, r in unique_cells:
            self.draw_cell(c, r, refresh_border=False, refresh_indexes=False)
        self.draw_map_outer_border()

    def ensure_hover_overlay_item(self):
        if not hasattr(self, 'cv_map'):
            return None

        item_id = getattr(self, "_hover_item_id", None)
        if item_id is not None:
            try:
                self.cv_map.itemconfigure(item_id, state="hidden")
                return item_id
            except tk.TclError:
                pass

        self._hover_item_id = self.cv_map.create_rectangle(
            0, 0, 0, 0,
            outline=self.HOVER_COLOR,
            width=1,
            dash=(3, 2),
            state="hidden",
            tags=self.HOVER_TAG
        )
        return self._hover_item_id

    def clear_hover_overlay(self, event=None):
        item_id = self.ensure_hover_overlay_item()
        if item_id is not None:
            self.cv_map.itemconfigure(item_id, state="hidden")
        self.hover_cell = None

    def draw_hover_overlay(self, col, row):
        if not hasattr(self, 'cv_map'):
            return

        sz = self.zoom_m
        x = col * sz
        y = row * sz
        inset = max(3, min(6, sz // 8))
        item_id = self.ensure_hover_overlay_item()
        if item_id is None:
            return

        self.cv_map.coords(
            item_id,
            x + inset,
            y + inset,
            x + sz - inset,
            y + sz - inset
        )
        self.cv_map.itemconfigure(item_id, state="normal")
        self.cv_map.tag_raise(self.HOVER_TAG)
        self.cv_map.tag_raise(self.MAP_BORDER_TAG)

    def update_hover_from_event(self, event):
        if self._map_panning:
            self.clear_hover_overlay()
            return

        col = int(self.cv_map.canvasx(event.x) // self.zoom_m)
        row = int(self.cv_map.canvasy(event.y) // self.zoom_m)
        if not (0 <= col < self.mw and 0 <= row < self.mh):
            self.clear_hover_overlay()
            return

        hover_cell = (col, row)
        if hover_cell == getattr(self, "hover_cell", None):
            return

        self.hover_cell = hover_cell
        self.draw_hover_overlay(col, row)

    def draw_map_outer_border(self):
        if not hasattr(self, 'cv_map'):
            return

        self.cv_map.delete(self.MAP_BORDER_TAG)

        sz = getattr(self, 'zoom_m', 0)
        rows = getattr(self, 'mh', 0)
        cols = getattr(self, 'mw', 0)
        if sz <= 0 or rows <= 0 or cols <= 0:
            return

        width = 2 if sz >= 32 else 1
        inset = max(1, width // 2)
        self.cv_map.create_rectangle(
            inset,
            inset,
            (cols * sz) - inset,
            (rows * sz) - inset,
            outline=self.MAP_BORDER_COLOR,
            width=width,
            tags=self.MAP_BORDER_TAG
        )
        self.cv_map.tag_raise(self.HOVER_TAG)
        self.cv_map.tag_raise(self.MAP_BORDER_TAG)

    def draw_special_icon(self, x, y, sz, tag, icon_key):
        img = self.get_img('special', icon_key, sz)
        if img:
            self.cv_map.create_image(x, y, image=img, anchor=tk.NW, tags=tag)

    def get_unknown_id_label(self, cat, hex_id):
        try:
            val_int = int(hex_id, 16)
            return self.custom_definitions[cat].get(val_int, "GHOST")
        except:
            return "GHOST"

    def get_height_tint_color(self, editor_height, base_color):
        if editor_height > EDITOR_HEIGHT_BASE:
            return HEIGHT_HIGH_COLOR
        if editor_height < EDITOR_HEIGHT_BASE:
            return HEIGHT_LOW_COLOR
        return base_color

    def draw_height_overlay(self, x, y, sz, editor_height, tag):
        delta = editor_height - EDITOR_HEIGHT_BASE
        visual_delta = max(-15, min(15, delta))
        center_y = y + (sz // 2)
        fill_h = int((abs(visual_delta) / 15.0) * (sz / 2))

        self.cv_map.create_rectangle(x, y, x + sz, y + sz, fill="#202020", outline="", stipple="gray12", tags=tag)

        if visual_delta > 0 and fill_h > 0:
            self.cv_map.create_rectangle(
                x, center_y - fill_h, x + sz, center_y,
                fill=HEIGHT_HIGH_COLOR, outline="", stipple="gray50", tags=tag
            )
        elif visual_delta < 0 and fill_h > 0:
            self.cv_map.create_rectangle(
                x, center_y, x + sz, center_y + fill_h,
                fill=HEIGHT_LOW_COLOR, outline="", stipple="gray50", tags=tag
            )

    def draw_height_number(self, x, y, sz, editor_height, tag):
        if self.mode != "HGT" and not SHOW_HEIGHT_NUMBERS_OUTSIDE_HGT:
            return
        if self.mode == "HGT" and sz < HGT_NUMBER_MIN_ZOOM:
            return

        text = str(editor_height)
        if self.mode == "HGT":
            font = ("Arial", max(6, sz // 8), "bold")
            text_color = self.get_height_tint_color(editor_height, "#FFFFFF")
            self.cv_map.create_text(
                x + 3,
                y + 2,
                text=text,
                fill=text_color,
                font=font,
                anchor=tk.NW,
                tags=tag
            )
        elif sz >= 32:
            font = ("Arial", max(6, sz // 8), "bold")
            self.cv_map.create_text(
                x + 3,
                y + 2,
                text=text,
                fill="#68C7CC",
                font=font,
                anchor=tk.NW,
                tags=tag
            )

    def draw_cell(self, c, r, refresh_border=True, refresh_indexes=True):
        if refresh_indexes:
            self.rebuild_render_indexes()
        else:
            self.ensure_render_indexes()

        sz = self.zoom_m; x, y = c*sz, r*sz; tag = f"c_{c}_{r}"
        self.cv_map.delete(tag)
        if refresh_border:
            self.cv_map.delete(self.MAP_BORDER_TAG)

        # RULER LOGIC
        if r == 0 or r == self.mh - 1 or c == 0 or c == self.mw - 1:
            self.cv_map.create_rectangle(x, y, x + sz, y + sz, fill="#151515", outline="#404040", tags=tag)
            if self.mode == "HGT" or SHOW_HEIGHT_NUMBERS_OUTSIDE_HGT:
                self.draw_height_number(x, y, sz, self.get_editor_height_value(r, c), tag)
            
            txt = ""
            if (r == 0 or r == self.mh - 1) and (0 < c < self.mw - 1): txt = str(c)
            elif (c == 0 or c == self.mw - 1) and (0 < r < self.mh - 1): txt = str(r)
            if txt: self.cv_map.create_text(x + sz//2, y + sz//2, text=txt, fill="#00FFFF", font=("Arial", max(8, sz//3), "bold"), tags=tag)
            if refresh_border:
                self.draw_map_outer_border()
            return

        # --- STANDARD MAP RENDERING (BASE LAYER) ---
        tid = self.grids['type'][r][c]
        
        # 1. Background Fill (Always standard dark grid to allow transparency)
        self.cv_map.create_rectangle(x,y,x+sz,y+sz, fill="#202020", outline="", tags=tag)

        # 2. Sector Image
        img = self.get_img('type', tid, sz)
        if img: self.cv_map.create_image(x,y,image=img, anchor=tk.NW, tags=tag)

        if self.clear_view and self.mode == "HGT":
            self.cv_map.create_rectangle(x,y,x+sz,y+sz, outline=GRID_COLOR, tags=tag)
            if refresh_border:
                self.draw_map_outer_border()
            return

        if self.mode == "HGT":
            editor_height = self.get_editor_height_value(r, c)
            bid = self.grids['blg'][r][c]
            if bid != '00':
                img = self.get_img('blg', bid, sz)
                if img:
                    self.cv_map.create_image(x, y, image=img, anchor=tk.NW, tags=tag)
            self.draw_height_overlay(x, y, sz, editor_height, tag)
            self.cv_map.create_rectangle(x, y, x+sz, y+sz, outline="#3A3A3A", width=1, tags=tag)
            self.draw_height_number(x, y, sz, editor_height, tag)
            if refresh_border:
                self.draw_map_outer_border()
            return

        # 3. Custom Sector Outline & Logic (Transparent center)
        is_custom_sector = tid not in self.lists['type']
        if is_custom_sector:
            # Only Red Border, no fill (Transparent)
            self.cv_map.create_rectangle(x,y,x+sz,y+sz, outline=CUSTOM_BORDER_COLOR, width=2, tags=tag)

        if self.clear_view:
            self.cv_map.create_rectangle(x,y,x+sz,y+sz, outline=GRID_COLOR, tags=tag)
            if refresh_border:
                self.draw_map_outer_border()
            return

        # 4. Building Layer
        bid = self.grids['blg'][r][c]
        is_custom_building = (bid != '00' and bid not in self.lists['blg'])

        if bid!='00':
            img = self.get_img('blg', bid, sz)
            if img: self.cv_map.create_image(x,y,image=img, anchor=tk.NW, tags=tag)
            
            self.draw_building_overlay_icons(x, y, sz, bid, tag)

        # --- TEXT RENDERING PRIORITY & COLOR UNIFICATION ---
        # Rule: Show Building Text if present. Else Show Sector Text if present.
        # Color: Yellow (#FFFF00) for both.
        # Format: ID \n Name
        
        text_to_draw = None
        
        if is_custom_building:
            name = self.get_unknown_id_label('blg', bid)
            text_to_draw = f"{bid}\n{name}"
            
        elif is_custom_sector:
            # Only draw sector text if building text is NOT drawn
            name = self.get_unknown_id_label('type', tid)
            text_to_draw = f"{tid}\n{name}"

        if text_to_draw:
             label_text, label_font = self.fit_cell_multiline_text(text_to_draw, sz)
             if label_text:
                 self.cv_map.create_text(x+sz//2, y+sz//2, text=label_text, fill="#FFFF00", font=label_font, justify=tk.CENTER, tags=tag)


        # --- GLOW LOGIC ---
        glow_width = 3

        # Main special objects first; keysector overlays are drawn after them.
        for i in self.render_gate_objects_by_cell.get((c, r), []):
            self.draw_special_icon(x, y, sz, tag, 'gate')
            if self.mode == "GATE" and i == self.current_gate_slot:
                 self.draw_cell_outline(x, y, sz, tag, width=glow_width, inset=2)

        for i in self.render_item_objects_by_cell.get((c, r), []):
            self.draw_special_icon(x, y, sz, tag, 'item')
            if self.mode == "ITEM" and i == self.current_item_slot:
                 self.draw_cell_outline(x, y, sz, tag, width=glow_width, inset=2)

        for i in self.render_gems_by_cell.get((c, r), []):
            self.draw_special_icon(x, y, sz, tag, 'gem')
            if self.mode == "GEM" and i == self.current_gem_slot:
                 self.draw_cell_outline(x, y, sz, tag, width=glow_width, inset=2)

        gate_key_slots_here = self.render_gate_keys_by_cell.get((c, r), [])
        for i in self.render_item_keys_by_cell.get((c, r), []):
            if not gate_key_slots_here:
                self.draw_special_icon(x, y, sz, tag, 'item_key')
            if self.mode == "ITEM" and i == self.current_item_slot:
                 self.draw_cell_outline(x, y, sz, tag, width=glow_width, inset=2)

        for i in gate_key_slots_here:
            self.draw_special_icon(x, y, sz, tag, 'key')
            if self.mode == "GATE" and i == self.current_gate_slot:
                 self.draw_cell_outline(x, y, sz, tag, width=glow_width, inset=2)

        fid = self.grids['own'][r][c]
        if fid != 0:
            self.draw_owner_indicator(x, y, sz, fid, tag)

        if bid != '00':
            marker_color = CUSTOM_BORDER_COLOR if is_custom_building else MARKER_COLOR
            self.draw_building_markers(x, y, sz, tag, color=marker_color)

        # HOST STATIONS
        host_indexes_here = self.render_hosts_by_cell.get((c, r), [])
        hosts_here = [(i, self.host_stations[i]) for i in host_indexes_here]
        if hosts_here:
            i, h = hosts_here[-1]
            f_col = FACTIONS[h['owner']][1] if FACTIONS[h['owner']][1] else "#FFF"
            self.cv_map.create_rectangle(x+2, y+2, x+sz-2, y+sz-2, outline=f_col, width=3, tags=tag)
            vname = h['custom_name']
            if not vname: vname = self.defs['host'].get(h['veh'], "Unknown")
            has_known_host_name = bool(h.get('custom_name')) or h['veh'] in self.defs['host']
            host_icon_key = f"host_{h['veh']}"
            has_host_icon = host_icon_key in self.assets or os.path.exists(resource_path(os.path.join("icons", f"{h['veh']}.png")))
            if has_host_icon or not has_known_host_name:
                host_icon_extra = 'transparent' if self.cell_has_host_underlay_object(c, r, bid) else None
                img = self.get_img('host', h['veh'], sz, extra=host_icon_extra)
                if img: self.cv_map.create_image(x,y,image=img, anchor=tk.NW, tags=tag)
            txt_col = "black" if h['owner'] in [2,3,4] else "white"
            squad_indexes_here = self.render_squads_by_cell.get((c, r), [])
            squads_here = [(i, self.squads[i]) for i in squad_indexes_here]
            host_label_position = "top" if squads_here else "bottom"
            self.draw_cell_label(x, y, sz, vname, f_col, txt_col, tag, position=host_label_position)
            if self.mode == "HOST" and i == self.current_host_index:
                 self.draw_cell_outline(x, y, sz, tag, width=2, inset=1)

        # SQUADS
        squad_indexes_here = self.render_squads_by_cell.get((c, r), [])
        squads_here = [(i, self.squads[i]) for i in squad_indexes_here]
        if squads_here:
            i, s = squads_here[-1]
            vname = s['custom_name']
            if not vname: vname = self.defs['veh'].get(s['veh'], "Unknown")
            vname = f"{s.get('num', 1)} {vname}"
            col = FACTIONS[s['owner']][1] if FACTIONS[s['owner']][1] else "#FFF"
            txt_col = "black" if s['owner'] in [2,3,4] else "white"
            self.draw_cell_label(x, y, sz, vname, col, txt_col, tag)
            if self.mode == "SQUAD" and i == self.current_squad_index:
                 self.draw_cell_outline(x, y, sz, tag, width=3, inset=1)

        self.cv_map.create_rectangle(x,y,x+sz,y+sz, outline=GRID_COLOR, tags=tag)
        if SHOW_HEIGHT_NUMBERS_OUTSIDE_HGT:
            self.draw_height_number(x, y, sz, self.get_editor_height_value(r, c), tag)
        if refresh_border:
            self.draw_map_outer_border()

    def draw_grid(self):
        if not hasattr(self, 'cv_map'): return
        start_time = time.perf_counter() if DEBUG_RENDER_PERF else None
        self.cv_map.delete("all")
        self.hover_cell = None
        self._hover_item_id = None
        self.rebuild_render_indexes()
        rows = getattr(self, 'mh', DEFAULT_H)
        cols = getattr(self, 'mw', DEFAULT_W)
        for r in range(rows):
            for c in range(cols): self.draw_cell(c, r, refresh_border=False, refresh_indexes=False)
        self.ensure_hover_overlay_item()
        self.draw_map_outer_border()
        self.cv_map.config(scrollregion=(0,0,self.mw*self.zoom_m, self.mh*self.zoom_m))
        if DEBUG_RENDER_PERF:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            item_count = len(self.cv_map.find_all())
            print(
                f"draw_grid: mode={self.mode} size={cols}x{rows} "
                f"zoom={self.zoom_m} items={item_count} time={elapsed_ms:.1f} ms"
            )

    def get_palette_label_font(self):
        if not hasattr(self, "_palette_label_font"):
            self._palette_label_font = tkfont.Font(family="Arial", size=8, weight="bold")
        return self._palette_label_font

    def fit_text_to_width(self, text, max_width, font):
        text = str(text)
        if font.measure(text) <= max_width:
            return text

        ellipsis = "..."
        if font.measure(ellipsis) > max_width:
            return ""

        base = text
        while base and font.measure(base.rstrip() + ellipsis) > max_width:
            base = base[:-1]
        return (base.rstrip() + ellipsis) if base.strip() else ellipsis

    def get_building_palette_label(self, bid):
        name = self.get_building_display_name(bid)
        if name:
            return f"{self.normalize_building_id(bid)} - {name}"
        return str(bid)

    def draw_palette(self, e=None):
        if self.mode in ["HGT", "GATE", "ITEM", "TECH", "GEM", "SCRIPT", "SQUAD", "HOST"]: return

        previous_y = 0.0
        try:
            previous_y = self.cv_pal.yview()[0]
        except:
            pass

        self.cv_pal.delete("all")
        w = self.cv_pal.winfo_width() or 300
        # Sector/Building palettes are zoomable. Owner palette must stay fixed-size
        # and must not be affected by the Sector/Building thumbnail zoom slider.
        if self.mode == "OWN":
            cols = 3
            cw = max(1, w // cols)
            # Owner cards stay fixed-size and independent from the thumbnail
            # zoom slider, but must still fit cleanly inside three columns.
            sz = max(90, min(170, cw - 18))
            if sz + 12 > cw:
                sz = max(32, cw - 12)
            ch = sz + 35
        else:
            sz = self.zoom_p
            # Building uses the same dynamic grid behavior as Sector: zooming out
            # increases the number of visible columns instead of keeping a fixed
            # two-column layout.
            cw, ch = sz + 5, sz + 25
            cols = max(1, w // (cw+5))
        items = self.get_current_items()

        if not items: return

        sel_val = self.sel['type'] if self.mode=="TYPE" else self.sel['own'] if self.mode=="OWN" else self.sel['hgt'] if self.mode=="HGT" else self.sel['blg']
        r, c = 0, 0
        label_font = self.get_palette_label_font()
        for it in items:
            if self.mode == "OWN":
                cell_x, y = c * cw, r * ch + 10
                x = cell_x + max(6, (cw - sz) // 2)
            else:
                cell_x, y = c * cw + 10, r * ch + 10
                x = cell_x
            label_x = x + sz//2
            if it==sel_val:
                self.cv_pal.create_rectangle(x-6, y-6, x+sz+6, y+sz+23, fill=SELECTION_SHADOW_COLOR, outline="")
                self.cv_pal.create_rectangle(x-3, y-3, x+sz+3, y+sz+20, fill="#004A56", outline=SELECTION_COLOR, width=3)

            img = None
            lbl = str(it)

            if self.mode=="TYPE": img = self.get_img('type', it, sz)
            elif self.mode=="OWN":
                 lbl = f"{it:02d} - {self.OWNER_PANEL_LABELS.get(it, FACTIONS[it][0].title())}"
                 if it!=0: img = self.get_img('overlay', it, sz, 'pale')
            elif self.mode=="BLG":
                 lbl = self.get_building_palette_label(it)
                 if it!='00': img = self.get_img('blg', it, sz)
            elif self.mode=="HGT":
                 lbl = str(it - HGT_MIN)
                 img = self.get_img('hgt', it, sz)

            if img:
                self.cv_pal.create_image(x,y,image=img, anchor=tk.NW)
                if self.mode=="BLG":
                    self.draw_building_overlay_icons(x, y, sz, it, None, canvas=self.cv_pal, palette=True)
            else: self.cv_pal.create_rectangle(x,y,x+sz,y+sz, fill="#222", outline="#555")
            lbl = self.fit_text_to_width(lbl, cw - 8, label_font)
            self.cv_pal.create_text(label_x,y+sz+10, text=lbl, fill="white", font=label_font)
            tag = self.cv_pal.create_rectangle(x-3,y-3,x+sz+3,y+sz+20, fill="", outline="")
            self.cv_pal.tag_bind(tag, "<Button-1>", lambda e, i=it: self.set_sel(i))
            c+=1
            if c>=cols: c=0; r+=1
        content_h = (r+1)*ch+20
        canvas_h = self.cv_pal.winfo_height()
        if content_h <= canvas_h: self.cv_pal.config(scrollregion=(0, 0, w, canvas_h))
        else: self.cv_pal.config(scrollregion=(0, 0, w, content_h))
        try:
            self.cv_pal.yview_moveto(max(0.0, min(1.0, previous_y)))
        except:
            pass

    def get_current_items(self):
        if self.mode=="TYPE": return self.lists['type']
        elif self.mode=="OWN": return list(FACTIONS.keys())
        elif self.mode=="HGT":
            return list(range(HGT_MIN, HGT_MAX + 1))
        elif self.mode=="BLG":
            return [
                bid for bid in self.lists['blg']
                if not self.is_building_hidden(bid)
            ]
        return []

    def set_sel(self, val):
        if self.mode=="TYPE": self.sel['type']=val
        elif self.mode=="OWN": self.sel['own']=val
        elif self.mode=="HGT": self.sel['hgt']=val
        elif self.mode=="BLG": self.sel['blg']=val
        self.upd_lbl(); self.draw_palette()

    def cycle_sel(self, d):
        items = self.get_current_items();
        if not items: return
        curr = self.sel['type'] if self.mode=="TYPE" else self.sel['own'] if self.mode=="OWN" else self.sel['hgt'] if self.mode=="HGT" else self.sel['blg']
        try: idx = items.index(curr); new_idx = (idx + d) % len(items); new_val = items[new_idx]; self.set_sel(new_val)
        except ValueError: self.set_sel(items[0])

    def zoom_pal(self, d):
        self.zoom_p = max(32, min(256, self.zoom_p + d))
        if hasattr(self, "palette_zoom_var"):
            try:
                self.palette_zoom_var.set(self.zoom_p)
            except:
                pass
        self.draw_palette()

    def zoom_map(self, d):
        self.zoom_m = max(self.zoom_m_min, min(self.zoom_m_max, self.zoom_m+d))
        self.draw_grid(); self.cv_map.config(scrollregion=(0,0,self.mw*self.zoom_m, self.mh*self.zoom_m))

    def zoom_map_at_cursor(self, event, direction):
        if direction == 0:
            return "break"

        old_zoom = self.zoom_m
        new_zoom = max(
            self.zoom_m_min,
            min(self.zoom_m_max, old_zoom + (self.zoom_m_step if direction > 0 else -self.zoom_m_step))
        )
        if new_zoom == old_zoom:
            return "break"

        view_w = max(1, self.cv_map.winfo_width())
        view_h = max(1, self.cv_map.winfo_height())
        mouse_x = max(0, min(view_w - 1, int(event.x)))
        mouse_y = max(0, min(view_h - 1, int(event.y)))

        old_total_w = max(1, self.mw * old_zoom)
        old_total_h = max(1, self.mh * old_zoom)

        old_offset_x = 0.0
        old_offset_y = 0.0
        try:
            x0, _ = self.cv_map.xview()
            y0, _ = self.cv_map.yview()
            if old_total_w > view_w:
                old_offset_x = x0 * (old_total_w - view_w)
            if old_total_h > view_h:
                old_offset_y = y0 * (old_total_h - view_h)
        except:
            pass

        # Punto logico sotto il mouse prima dello zoom.
        map_x = (old_offset_x + mouse_x) / max(1, old_zoom)
        map_y = (old_offset_y + mouse_y) / max(1, old_zoom)

        self.zoom_m = new_zoom
        self.draw_grid()

        new_total_w = max(1, self.mw * new_zoom)
        new_total_h = max(1, self.mh * new_zoom)
        self.root.update_idletasks()

        # Nuovi offset che mantengono lo stesso punto logico sotto il cursore.
        new_offset_x = (map_x * new_zoom) - mouse_x
        new_offset_y = (map_y * new_zoom) - mouse_y

        max_off_x = max(0.0, new_total_w - view_w)
        max_off_y = max(0.0, new_total_h - view_h)
        new_offset_x = max(0.0, min(max_off_x, new_offset_x))
        new_offset_y = max(0.0, min(max_off_y, new_offset_y))

        if new_total_w > view_w:
            x_frac = new_offset_x / max(1, (new_total_w - view_w))
            self.cv_map.xview_moveto(max(0.0, min(1.0, x_frac)))
        else:
            self.cv_map.xview_moveto(0.0)

        if new_total_h > view_h:
            y_frac = new_offset_y / max(1, (new_total_h - view_h))
            self.cv_map.yview_moveto(max(0.0, min(1.0, y_frac)))
        else:
            self.cv_map.yview_moveto(0.0)

        self.update_hover_from_event(event)
        return "break"

    def bind_scroll(self, w):
        if w == self.cv_map:
            w.bind("<Button-4>", self.on_map_wheel_linux_up)
            w.bind("<Button-5>", self.on_map_wheel_linux_down)
            w.bind("<MouseWheel>", self.on_map_mousewheel)
            w.bind("<Motion>", self.on_mouse_move)
        else:
            w.bind("<Button-4>", lambda e: w.yview_scroll(-1,"units"))
            w.bind("<Button-5>", lambda e: w.yview_scroll(1,"units"))
            w.bind("<MouseWheel>", lambda e: w.yview_scroll(int(-1*(e.delta/120)),"units"))

    def bind_palette_scroll_and_zoom(self):
        self.cv_pal.bind("<Button-4>", lambda e: self.on_palette_wheel(e, 1))
        self.cv_pal.bind("<Button-5>", lambda e: self.on_palette_wheel(e, -1))
        self.cv_pal.bind("<MouseWheel>", self.on_palette_mousewheel)

    def on_palette_mousewheel(self, event):
        delta = 1 if event.delta > 0 else -1
        return self.on_palette_wheel(event, delta)

    def on_palette_wheel(self, event, delta_sign):
        ctrl_pressed = (event.state & 0x4) != 0
        if ctrl_pressed:
            self.zoom_pal(16 if delta_sign > 0 else -16)
        else:
            self.cv_pal.yview_scroll(-1 if delta_sign > 0 else 1, "units")
        return "break"

    def on_map_wheel_linux_up(self, event):
        return self.zoom_map_at_cursor(event, 1)

    def on_map_wheel_linux_down(self, event):
        return self.zoom_map_at_cursor(event, -1)

    def on_map_mousewheel(self, event):
        if event.delta > 0:
            return self.zoom_map_at_cursor(event, 1)
        if event.delta < 0:
            return self.zoom_map_at_cursor(event, -1)
        return "break"

    def on_space_pan_press(self, event):
        self.space_pan_active = True

    def on_space_pan_release(self, event):
        self.space_pan_active = False
        if self._map_panning and self._map_pan_source == "space_left":
            self.stop_map_pan(event)

    def on_map_left_press(self, event):
        if self.space_pan_active:
            return self.start_map_pan(event, source="space_left")
        self.update_hover_from_event(event)
        if self.mode in ["TYPE", "OWN", "BLG", "HGT", "GATE", "ITEM", "GEM"] and not self._map_edit_snapshot_taken:
            self.push_undo_snapshot()
            self._map_edit_snapshot_taken = True
        return self.on_click(event)

    def on_map_left_drag(self, event):
        if self._map_panning and self._map_pan_source == "space_left":
            return self.drag_map_pan(event)
        self.update_hover_from_event(event)
        return self.on_click(event)

    def on_map_left_release(self, event):
        if self._map_panning and self._map_pan_source == "space_left":
            self.stop_map_pan(event)
            return "break"
        self._map_edit_snapshot_taken = False
        self.on_release(event)

    def on_map_right_release(self, event):
        self._map_right_edit_snapshot_taken = False
        self.on_release(event)
        return "break"

    def start_map_pan_middle(self, event):
        return self.start_map_pan(event, source="middle")

    def start_map_pan(self, event, source="middle"):
        self._map_panning = True
        self._map_pan_source = source
        self.clear_hover_overlay()
        self.cv_map.focus_set()
        self.cv_map.scan_mark(event.x, event.y)
        self.cv_map.config(cursor="fleur")
        return "break"

    def drag_map_pan(self, event):
        if not self._map_panning:
            return
        self.cv_map.scan_dragto(event.x, event.y, gain=1)
        return "break"

    def stop_map_pan(self, event=None):
        self._map_panning = False
        self._map_pan_source = None
        self.cv_map.config(cursor="")
        if event is not None:
            self.update_hover_from_event(event)
        return "break"

    def on_click(self, e):
        cx, cy = int(self.cv_map.canvasx(e.x)//self.zoom_m), int(self.cv_map.canvasy(e.y)//self.zoom_m)
        if not (0<=cx<self.mw and 0<=cy<self.mh): return

        # --- 1. GLOBAL BORDER PROTECTION (Except HGT) ---
        if self.mode != "HGT":
            if cx == 0 or cx == self.mw - 1 or cy == 0 or cy == self.mh - 1:
                if str(e.type) == '4':
                    messagebox.showwarning("Restricted Area", "Cannot modify map borders (Reserved for Logic)!")
                return

        # --- 2. GATES / ITEMS / GEMS HANDLING ---
        if self.mode in ["GATE", "ITEM", "GEM"]:
            tool = self.gate_tool if self.mode == "GATE" else self.item_tool if self.mode == "ITEM" else "GEM"
            data_list = self.gates if self.mode == "GATE" else self.items if self.mode == "ITEM" else self.gems
            current_slot = self.current_gate_slot if self.mode == "GATE" else self.current_item_slot if self.mode == "ITEM" else self.current_gem_slot
            visible_count = self.visible_gate_slots if self.mode == "GATE" else self.visible_item_slots if self.mode == "ITEM" else self.visible_gem_slots
            current_data = data_list.get(current_slot) if 1 <= current_slot <= visible_count else None

            # A. MOTION (DRAGGING)
            if str(e.type) == '6':
                if current_data is None:
                    return
                # 1. Key Dragging Logic
                if self.mode in ["GATE", "ITEM"] and self._drag_key_list is not None and self._drag_key_idx != -1:
                    if self._drag_key_idx < len(self._drag_key_list):
                        old_x, old_y = self._drag_key_list[self._drag_key_idx]
                        if (old_x != cx or old_y != cy):
                            can_place, _ = self.can_place_special_key(
                                cx,
                                cy,
                                current_data=current_data,
                                dragging_key_list=self._drag_key_list,
                                dragging_key_idx=self._drag_key_idx
                            )
                            if not can_place:
                                return
                            if self.mode == "ITEM":
                                self.restore_item_key_visual_if_matches(current_data, (old_x, old_y))
                            self._drag_key_list[self._drag_key_idx] = (cx, cy)
                            if self.mode == "ITEM":
                                self.apply_item_key_visual(current_data, (cx, cy))
                            self.dirty = True
                            self.invalidate_render_indexes()
                            self.redraw_cells((old_x, old_y), (cx, cy))
                            if hasattr(self, 'refresh_special_list'):
                                self.refresh_special_list(self.mode, redraw=False)
                    return

                # 2. Main Object Dragging Logic
                if tool != "KEY" and current_data.get('x', -1) != -1:
                    if current_data['x'] != cx or current_data['y'] != cy:
                        if (
                            not self.has_main_object(cx, cy, self.mode, current_slot) and
                            (self.mode not in ["GATE", "ITEM"] or (cx, cy) not in current_data.get('keys', []))
                        ):
                            ox, oy = current_data['x'], current_data['y']
                            old_cell = (ox, oy)
                            old_gem_building = current_data.get('blg') if self.mode == "GEM" else None
                            old_visual = self.get_special_auto_visual(self.mode, current_data)
                            current_data['x'], current_data['y'] = cx, cy
                            if self.mode in ["GATE", "ITEM"]:
                                self.sync_special_auto_visual(self.mode, current_data, old_cell=old_cell, old_visual=old_visual)
                            elif self.mode == "GEM":
                                self.sync_gem_building_visual(current_data, old_cell=old_cell, old_building=old_gem_building)
                            self.dirty = True
                            self.invalidate_render_indexes()
                            self.redraw_cells((ox, oy), (cx, cy))
                            if hasattr(self, 'refresh_special_list'):
                                self.refresh_special_list(self.mode, redraw=False)
                return

            # B. CLICK (SELECTION / PLACEMENT)
            if str(e.type) == '4':
                if self.mode == "GATE":
                    key_slot, key_idx = self.find_gate_key_at(cx, cy)
                    obj_slot = self.find_gate_at(cx, cy)
                    if key_slot != -1 and (current_slot != key_slot or tool != "KEY"):
                        self.select_special_slot("GATE", key_slot)
                        self.gate_tool = "KEY"
                        self._drag_key_list = self.gates[key_slot]['keys']
                        self._drag_key_idx = key_idx
                        return
                    if obj_slot != -1 and obj_slot != current_slot:
                        self.select_special_slot("GATE", obj_slot)
                        self.gate_tool = "GATE"
                        return
                elif self.mode == "ITEM":
                    key_slot, key_idx = self.find_item_key_at(cx, cy)
                    obj_slot = self.find_item_at(cx, cy)
                    if key_slot != -1 and (current_slot != key_slot or tool != "KEY"):
                        self.select_special_slot("ITEM", key_slot)
                        self.item_tool = "KEY"
                        self._drag_key_list = self.items[key_slot]['keys']
                        self._drag_key_idx = key_idx
                        return
                    if obj_slot != -1 and obj_slot != current_slot:
                        self.select_special_slot("ITEM", obj_slot)
                        self.item_tool = "ITEM"
                        return
                elif self.mode == "GEM":
                    obj_slot = self.find_gem_at(cx, cy)
                    if obj_slot != -1 and obj_slot != current_slot:
                        self.select_special_slot("GEM", obj_slot)
                        self.gem_tool = "GEM"
                        return

                if current_data is None:
                    messagebox.showwarning("No Object Selected", f"Add a {self.mode.lower()} to the list first.")
                    return

                if (self.mode == "GATE" and tool == "KEY") or (self.mode == "ITEM" and tool == "KEY"):
                    if (cx, cy) in current_data.get('keys', []):
                        self._drag_key_list = current_data['keys']
                        self._drag_key_idx = current_data['keys'].index((cx, cy))
                    else:
                        can_place, reason = self.can_place_special_key(cx, cy, current_data=current_data)
                        if not can_place:
                            if reason == "parent":
                                messagebox.showwarning("Error", "Cannot place Key on Parent Object!")
                            elif reason == "key":
                                messagebox.showwarning("Occupied", "Tile has a Key!")
                            else:
                                messagebox.showwarning("Occupied", "Cannot place Key here!")
                            return
                        self.push_undo_snapshot()
                        current_data['keys'].append((cx, cy))
                        if self.mode == "ITEM":
                            self.apply_item_key_visual(current_data, (cx, cy))
                        self.dirty = True
                        self.invalidate_render_indexes()
                        self.redraw_cells((cx, cy))
                        if hasattr(self, 'refresh_special_list'):
                            self.refresh_special_list(self.mode, redraw=False)
                    return
                else:
                    target_mode = self.mode
                    if target_mode == "GEM" and not current_data.get('actions'):
                        messagebox.showwarning("Error", "Cannot place Gem without actions!\nAdd actions in the panel first.")
                        return
                    if self.has_main_object(cx, cy, target_mode, current_slot):
                        messagebox.showwarning("Occupied", "Tile occupied!"); return
                    if target_mode in ["GATE", "ITEM"] and (cx, cy) in current_data.get('keys', []):
                        messagebox.showwarning("Error", "Cannot place Object on its own Key!"); return

                    old_x, old_y = current_data.get('x', -1), current_data.get('y', -1)
                    old_cell = (old_x, old_y) if old_x != -1 and old_y != -1 else None
                    old_gem_building = current_data.get('blg') if target_mode == "GEM" else None
                    old_visual = self.get_special_auto_visual(target_mode, current_data)
                    self.push_undo_snapshot()
                    current_data['x'] = cx; current_data['y'] = cy
                    if target_mode in ["GATE", "ITEM"]:
                        self.sync_special_auto_visual(target_mode, current_data, old_cell=old_cell, old_visual=old_visual)
                    elif target_mode == "GEM":
                        self.sync_gem_building_visual(current_data, old_cell=old_cell, old_building=old_gem_building)
                    self.dirty = True
                    self.invalidate_render_indexes()
                    self.redraw_cells((old_x, old_y), (cx, cy))
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list(self.mode, redraw=False)
            return

        # --- 3. SQUAD HANDLING ---
        if self.mode == "SQUAD":
            if str(e.type) == '6': # Drag
                if hasattr(self, '_drag_squad_idx') and self._drag_squad_idx != -1:
                    idx = self._drag_squad_idx
                    if idx < len(self.squads):
                         if self.has_squad(cx, cy, exclude_index=idx): return
                         sq = self.squads[idx]; old_x, old_y = sq['x'], sq['y']
                         if old_x != cx or old_y != cy:
                            if not self._map_edit_snapshot_taken:
                                self.push_undo_snapshot()
                                self._map_edit_snapshot_taken = True
                            sq['x'], sq['y'] = cx, cy
                            self.dirty = True
                            self.invalidate_render_indexes()
                            self.redraw_cells((old_x, old_y), (cx, cy))
                            self.refresh_squad_list(redraw=False)
                return

            if str(e.type) == '4': # Click
                hovered_idx = self.find_squad_at(cx, cy)
                if hovered_idx != -1 and hovered_idx != self.current_squad_index:
                    self.select_squad_index(hovered_idx)
                    self._drag_squad_idx = hovered_idx
                    return

                idx = self.current_squad_index
                if idx == -1 or idx >= len(self.squads):
                    messagebox.showwarning("No Squad Selected", "Add a squad to the list, select it, then click the map to place it.")
                    return

                if self.has_squad(cx, cy, exclude_index=idx):
                    messagebox.showwarning("Occupied", "Tile occupied!"); return

                sq = self.squads[idx]
                old_x, old_y = sq['x'], sq['y']
                if old_x != cx or old_y != cy:
                    if not self._map_edit_snapshot_taken:
                        self.push_undo_snapshot()
                        self._map_edit_snapshot_taken = True
                    sq['x'], sq['y'] = cx, cy
                    self.dirty = True
                self.invalidate_render_indexes()
                self._drag_squad_idx = idx
                self.redraw_cells((old_x, old_y), (cx, cy))
                self.refresh_squad_list(redraw=False)
            return

        # --- 4. HOST STATION HANDLING ---
        if self.mode == "HOST":
            if str(e.type) == '6': # Drag
                if hasattr(self, '_drag_host_idx') and self._drag_host_idx != -1:
                    idx = self._drag_host_idx
                    if idx < len(self.host_stations):
                         if self.has_host(cx, cy, exclude_index=idx): return
                         hst = self.host_stations[idx]; old_x, old_y = hst['x'], hst['y']
                         if old_x != cx or old_y != cy:
                            if not self._map_edit_snapshot_taken:
                                self.push_undo_snapshot()
                                self._map_edit_snapshot_taken = True
                            hst['x'], hst['y'] = cx, cy
                            self.dirty = True
                            self.invalidate_render_indexes()
                            self.redraw_cells((old_x, old_y), (cx, cy))
                            self.refresh_host_list(redraw=False)
                            self.refresh_tech_panel_if_visible()
                return

            if str(e.type) == '4': # Click
                hovered_idx = self.find_host_at(cx, cy)
                if hovered_idx != -1 and hovered_idx != self.current_host_index:
                    self.select_host_index(hovered_idx)
                    self._drag_host_idx = hovered_idx
                    return

                idx = self.current_host_index
                if idx == -1 or idx >= len(self.host_stations):
                    messagebox.showwarning("No Host Selected", "Add a Host Station to the list, select it, then click the map to place it.")
                    return

                if self.has_host(cx, cy, exclude_index=idx):
                    messagebox.showwarning("Occupied", "Tile occupied!"); return

                hst = self.host_stations[idx]
                old_x, old_y = hst['x'], hst['y']
                if old_x != cx or old_y != cy:
                    if not self._map_edit_snapshot_taken:
                        self.push_undo_snapshot()
                        self._map_edit_snapshot_taken = True
                    hst['x'], hst['y'] = cx, cy
                    self.dirty = True
                self.invalidate_render_indexes()
                self._drag_host_idx = idx
                self.redraw_cells((old_x, old_y), (cx, cy))
                self.refresh_host_list(redraw=False)
                self.refresh_tech_panel_if_visible()
            return

        # --- 5. TILE PAINTING ---
        if self.mode in ["TECH", "SCRIPT"]: return

        if self.mode=="TYPE": self.grids['type'][cy][cx]=self.sel['type']
        elif self.mode=="OWN": self.grids['own'][cy][cx]=self.sel['own']
        elif self.mode=="HGT":
            self.grids['hgt'][cy][cx] = max(HGT_MIN, min(HGT_MAX, self.sel['hgt']))
            changed_border_cells = self.normalize_border_heights()
            self.redraw_cells((cx, cy), *changed_border_cells)
            return
        elif self.mode=="BLG": self.grids['blg'][cy][cx]=self.sel['blg']
        self.redraw_cells((cx, cy))

    def on_right_click(self, e):
        cx, cy = int(self.cv_map.canvasx(e.x)//self.zoom_m), int(self.cv_map.canvasy(e.y)//self.zoom_m)
        if not (0<=cx<self.mw and 0<=cy<self.mh): return

        if self.mode != "HGT":
            if cx == 0 or cx == self.mw - 1 or cy == 0 or cy == self.mh - 1:
                return

        def push_right_undo_once():
            if not getattr(self, "_map_right_edit_snapshot_taken", False):
                self.push_undo_snapshot()
                self._map_right_edit_snapshot_taken = True
            self.dirty = True

        # SQUAD SELECTION / DELETION
        if self.mode == "SQUAD":
            idx = self.find_squad_at(cx, cy)
            if idx == -1:
                return
            if idx == self.current_squad_index:
                self.deselect_squad()
            elif self.current_squad_index == -1:
                self.delete_squad_index(idx)
            else:
                self.select_squad_index(idx)
            return

        # HOST SELECTION / DELETION
        if self.mode == "HOST":
            idx = self.find_host_at(cx, cy)
            if idx == -1:
                return
            if idx == self.current_host_index:
                self.deselect_host()
            elif self.current_host_index == -1:
                self.delete_host_index(idx)
            else:
                self.select_host_index(idx)
            return

        if self.mode == "TYPE":
            if self.grids['type'][cy][cx] != '00':
                push_right_undo_once()
                self.grids['type'][cy][cx] = '00'
                self.redraw_cells((cx, cy))
            return
        if self.mode == "OWN":
            if self.grids['own'][cy][cx] != 0:
                push_right_undo_once()
                self.grids['own'][cy][cx] = 0
                self.redraw_cells((cx, cy))
            return
        if self.mode == "BLG":
            if self.grids['blg'][cy][cx] != '00':
                push_right_undo_once()
                self.grids['blg'][cy][cx] = '00'
                self.redraw_cells((cx, cy))
            return
        if self.mode == "HGT":
            if self.grids['hgt'][cy][cx] != DEFAULT_HGT:
                push_right_undo_once()
                self.grids['hgt'][cy][cx] = DEFAULT_HGT
                changed_border_cells = self.normalize_border_heights()
                self.redraw_cells((cx, cy), *changed_border_cells)
            return

        if self.mode == "GATE":
            for i, g in self.gates.items():
                if g['x'] == cx and g['y'] == cy:
                    push_right_undo_once()
                    self.clear_special_auto_visual("GATE", g)
                    g['x'] = -1; g['y'] = -1
                    self.invalidate_render_indexes()
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list("GATE", redraw=False)
                    self.redraw_cells((cx, cy)); return
                if (cx, cy) in g['keys']:
                    push_right_undo_once()
                    g['keys'].remove((cx, cy))
                    self.invalidate_render_indexes()
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list("GATE", redraw=False)
                    self.redraw_cells((cx, cy)); return
        elif self.mode == "ITEM":
            for i, it in self.items.items():
                if it['x'] == cx and it['y'] == cy:
                    push_right_undo_once()
                    self.clear_special_auto_visual("ITEM", it)
                    it['x'] = -1; it['y'] = -1
                    self.invalidate_render_indexes()
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list("ITEM", redraw=False)
                    self.redraw_cells((cx, cy)); return
                if (cx, cy) in it['keys']:
                    push_right_undo_once()
                    self.restore_item_key_visual_if_matches(it, (cx, cy))
                    it['keys'].remove((cx, cy))
                    self.invalidate_render_indexes()
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list("ITEM", redraw=False)
                    self.redraw_cells((cx, cy)); return
        elif self.mode == "GEM":
            for i, gm in self.gems.items():
                if gm['x'] == cx and gm['y'] == cy:
                    push_right_undo_once()
                    self.clear_gem_building_visual_if_matches(gm)
                    gm['x'] = -1; gm['y'] = -1
                    self.invalidate_render_indexes()
                    if hasattr(self, 'refresh_special_list'):
                        self.refresh_special_list("GEM", redraw=False)
                    self.redraw_cells((cx, cy)); return

    def handle_special_click(self, cx, cy, data_dict, tool_type):
        old_cell = None
        if tool_type.endswith("GATE") or tool_type.endswith("ITEM"):
            if data_dict['x'] != -1:
                old_cell = (data_dict['x'], data_dict['y'])
                data_dict['x'] = -1
            data_dict['x'], data_dict['y'] = cx, cy
        elif "KEY" in tool_type:
            if (cx,cy) in data_dict['keys']: data_dict['keys'].remove((cx,cy))
            else: data_dict['keys'].append((cx,cy))
        self.invalidate_render_indexes()
        self.redraw_cells(old_cell, (cx, cy))

    def pick(self, e):
        cx, cy = int(self.cv_map.canvasx(e.x)//self.zoom_m), int(self.cv_map.canvasy(e.y)//self.zoom_m)
        if not (0 <= cx < self.mw and 0 <= cy < self.mh):
            return
        if self.mode=="TYPE": self.sel['type']=self.grids['type'][cy][cx]
        elif self.mode=="OWN": self.sel['own']=self.grids['own'][cy][cx]
        elif self.mode=="HGT":
            new_hgt = self.grids['hgt'][cy][cx]; self.sel['hgt']=new_hgt;
            if hasattr(self, 'update_height_ui_after_value_change'):
                self.update_height_ui_after_value_change()
            elif hasattr(self, 'hgt_entry'):
                user_val = new_hgt - HGT_MIN
                self.hgt_entry.delete(0, tk.END); self.hgt_entry.insert(0, str(user_val))
        elif self.mode=="BLG": self.sel['blg']=self.grids['blg'][cy][cx]
        self.upd_lbl(); self.draw_palette()

    def on_mouse_move(self, e):
        self.update_hover_from_event(e)
        if self._map_panning:
            return
        if self.mode in ["TYPE", "OWN", "BLG", "HGT"]: return

        cx = int(self.cv_map.canvasx(e.x)//self.zoom_m)
        cy = int(self.cv_map.canvasy(e.y)//self.zoom_m)
        if not (0 <= cx < self.mw and 0 <= cy < self.mh):
            return
        world_x, world_z = self.grid_to_world(cx, cy)

        if hasattr(self, 'lbl_sel'):
            base_txt = self.lbl_sel.cget("text").split(" | ")[0]
            self.lbl_sel.config(text=f"{base_txt} | POS_X: {world_x}  POS_Z: {world_z}")

        # Auto-trigger move logic for Dragging
        if (self.mode == "SQUAD" or self.mode == "HOST") and str(e.type) == '6': self.on_click(e)

    def on_release(self, e):
        # Reset Key Dragging
        self._drag_key_list = None
        self._drag_key_idx = -1
        # Reset Tech Dragging
        self._last_dragged_id = None
        # Reset Squad Dragging
        self._drag_squad_idx = -1
        # Reset Host Dragging
        self._drag_host_idx = -1

    def on_custom_input(self, event):
        val_str = self.entry_custom.get().strip()
        if not val_str: return
        try:
            val_int = int(val_str, 16)
            if self.mode == "TYPE":
                 self.set_sel(f"{val_int:02X}")
            elif self.mode == "BLG":
                 self.set_sel(f"{val_int:02X}")
        except: pass

    def on_custom_name_input(self, event):
        name_str = self.entry_custom_name.get().strip()
        val_str = self.entry_custom.get().strip()
        if not val_str: return
        
        try:
            val_int = int(val_str, 16)
            cat = 'type' if self.mode == "TYPE" else 'blg' if self.mode == "BLG" else None
            
            if cat:
                if name_str:
                    self.custom_definitions[cat][val_int] = name_str
                elif val_int in self.custom_definitions[cat]:
                    del self.custom_definitions[cat][val_int]
        except: pass

    def grid_to_world(self, col, row):
        STEP = 1200
        OFFSET = 700
        wx = (col * STEP) + OFFSET
        wz = -1 * ((row * STEP) + OFFSET)
        return wx, wz

    def world_to_grid(self, wx, wz):
        STEP = 1200
        OFFSET = 700
        col = int(round((wx - OFFSET) / STEP))
        row = int(round((abs(wz) - OFFSET) / STEP))
        return col, row

    def toggle_clear(self):
        self.clear_view = not self.clear_view
        self.btn_cl.config(bg="#00FF00" if self.clear_view else "#444"); self.draw_grid()
