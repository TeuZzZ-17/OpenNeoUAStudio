import json
import os
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageTk

from .constants import *
from .paths import resource_path, writable_path


class AssetMixin:

    def get_fallback_host_ai_presets(self):
        return {
            name: dict(values)
            for name, values in FALLBACK_HOST_AI_PRESETS.items()
        }

    def get_fallback_host_ai_descriptions(self):
        return {
            DEFAULT_HOST_AI_PRESET: "Balanced conquest, defense, recon and attacks.",
            "Tech Hunter": "Prioritizes tech sectors, gems and key objectives.",
            "Adaptive": "Flexible all-round behavior for dynamic maps.",
            "Swarm Mind": "Constant pressure with many cheap attack groups.",
            "Siege Master": "Slow buildup followed by heavy offensive pushes.",
            "Deep Strike": "Focuses on host stations and rear targets.",
            "Air Supremacy": "Radar-heavy air control and fast scouting.",
            "Iron Wall": "Extreme defense with strong flak and territory hold.",
            "Opportunist": "Waits for weak spots before attacking.",
            "Radar Freak": "Obsessive radar coverage and map awareness.",
            "Flak Maniac": "Builds heavy flak defenses almost everywhere.",
            "Paranoid": "Defensive, radar-heavy and hard to surprise.",
            "Doom March": "Slow, relentless pressure across the map.",
            "Custom": "Custom AI values.",
        }

    def clean_host_ai_values(self, values=None):
        values = values if isinstance(values, dict) else {}
        fallback = self.get_fallback_host_ai_presets()[DEFAULT_HOST_AI_PRESET]
        clean = {}
        for field in HOST_AI_FIELDS:
            try:
                raw_value = int(values.get(field, fallback[field]))
            except:
                raw_value = fallback[field]
            if field in HOST_AI_BUDGET_FIELDS:
                raw_value = max(0, min(100, raw_value))
            else:
                raw_value = max(0, raw_value)
            clean[field] = raw_value
        return clean

    def load_host_ai_presets(self):
        path = resource_path(os.path.join("definitions", "host_ai_presets.json"))
        presets = {}
        descriptions = self.get_fallback_host_ai_descriptions()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for name, values in data.items():
                        if not isinstance(values, dict):
                            continue
                        preset_name = str(name).strip()
                        if preset_name:
                            description = str(values.get("description", "")).strip()
                            if description:
                                descriptions[preset_name] = description
                            raw_values = values.get("values") if isinstance(values.get("values"), dict) else values
                            presets[preset_name] = self.clean_host_ai_values(raw_values)
            except Exception as e:
                try:
                    messagebox.showwarning("Host AI Presets", f"Invalid host_ai_presets.json:\n{e}")
                except:
                    pass

        if not presets:
            presets = self.get_fallback_host_ai_presets()
            descriptions.update(self.get_fallback_host_ai_descriptions())
        elif DEFAULT_HOST_AI_PRESET not in presets:
            presets[DEFAULT_HOST_AI_PRESET] = self.get_fallback_host_ai_presets()[DEFAULT_HOST_AI_PRESET]

        self.host_ai_presets = presets
        self.host_ai_preset_descriptions = descriptions
        ordered_names = list(presets.keys())
        if DEFAULT_HOST_AI_PRESET in ordered_names:
            ordered_names.remove(DEFAULT_HOST_AI_PRESET)
            ordered_names.insert(0, DEFAULT_HOST_AI_PRESET)
        self.host_ai_preset_names = ordered_names

    def get_host_ai_preset_description(self, preset_name):
        descriptions = getattr(self, "host_ai_preset_descriptions", None) or self.get_fallback_host_ai_descriptions()
        return descriptions.get(preset_name) or ("Custom AI values." if preset_name == "Custom" else "")

    def make_host_ai(self, preset_name=DEFAULT_HOST_AI_PRESET):
        presets = getattr(self, "host_ai_presets", None) or self.get_fallback_host_ai_presets()
        if preset_name not in presets:
            preset_name = DEFAULT_HOST_AI_PRESET if DEFAULT_HOST_AI_PRESET in presets else next(iter(presets))
        ai = dict(presets[preset_name])
        ai["preset"] = preset_name
        return ai

    def detect_host_ai_preset(self, values):
        clean_values = self.clean_host_ai_values(values)
        presets = getattr(self, "host_ai_presets", None) or self.get_fallback_host_ai_presets()
        for name in getattr(self, "host_ai_preset_names", list(presets.keys())):
            preset_values = presets.get(name)
            if preset_values and all(clean_values[field] == preset_values[field] for field in HOST_AI_FIELDS):
                return name
        return "Custom"

    def normalize_host_ai(self, ai=None):
        if not isinstance(ai, dict):
            return self.make_host_ai(DEFAULT_HOST_AI_PRESET)

        preset_name = str(ai.get("preset", DEFAULT_HOST_AI_PRESET))
        has_explicit_values = any(field in ai for field in HOST_AI_FIELDS)
        if not has_explicit_values and preset_name != "Custom":
            return self.make_host_ai(preset_name)

        values = self.clean_host_ai_values(ai)
        values["preset"] = "Custom" if preset_name == "Custom" else self.detect_host_ai_preset(values)
        return values

    def ensure_host_ai(self, host):
        if not isinstance(host, dict):
            return self.make_host_ai(DEFAULT_HOST_AI_PRESET)
        host["ai"] = self.normalize_host_ai(host.get("ai"))
        return host["ai"]

    def ensure_host_defaults(self, host):
        if not isinstance(host, dict):
            return
        host.setdefault("reload_const", DEFAULT_HOST_RELOAD_CONST)
        host.setdefault("viewangle", DEFAULT_HOST_VIEWANGLE)
        self.ensure_host_ai(host)

    def ensure_squad_defaults(self, squad):
        if not isinstance(squad, dict):
            return
        squad.setdefault("useable", False)

    def ensure_asset_dir(self, relative_dir):
        path = resource_path(relative_dir)
        if os.path.isdir(path):
            return path
        path = writable_path(relative_dir)
        try:
            os.makedirs(path, exist_ok=True)
        except:
            pass
        return path

    def iter_asset_files(self, relative_dir, extensions=IMAGE_EXTENSIONS, create_if_missing=False):
        directory = self.ensure_asset_dir(relative_dir) if create_if_missing else resource_path(relative_dir)
        if not os.path.isdir(directory):
            return []
        return [
            (filename, os.path.join(directory, filename))
            for filename in sorted(os.listdir(directory))
            if filename.lower().endswith(extensions)
        ]

    def make_fallback_icon(self, fallback_col, fallback_txt):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle([4, 4, 60, 60], outline=fallback_col, width=4)
        draw.text((10, 20), fallback_txt, fill=fallback_col)
        return img

    def load_icon(self, filename, key, fallback_col, fallback_txt):
        path = resource_path(os.path.join("icons", filename))
        if os.path.exists(path):
            self.assets[key] = Image.open(path)
        else:
            self.assets[key] = self.make_fallback_icon(fallback_col, fallback_txt)

    def load_standard_icons(self, include_custom_tile_icons=False):
        self.ensure_asset_dir("icons")
        if include_custom_tile_icons:
            self.load_icon("custom_sector.png", "custom_sector", "#FF0000", "SEC\nGHOST")
            self.load_icon("custom_building.png", "custom_building", "#FF0000", "BLG\nGHOST")

        self.load_icon("custom.png", "custom_mod", "#FF00FF", "GHOST")
        self.load_icon("beamgate.png", "gate", "#00FFFF", "GATE")
        self.load_icon("key.png", "key", "#FFFF00", "KEY")
        self.load_icon("superitem.png", "item", "#FF00FF", "ITEM")
        self.load_icon("superitem_key.png", "item_key", "#FF9900", "KEY")
        self.load_icon("gem.png", "gem", "#00FF00", "GEM")

        for i in range(1, 9):
            fac_col = FACTIONS[i][1] if i in FACTIONS and FACTIONS[i][1] else "#FFF"
            self.load_icon(f"host_{i}.png", f"host_{i}", fac_col, f"HOST\n{i}")

    def load_set_tiles(self):
        for filename, path in self.iter_asset_files(self.set_folder, extensions=TILE_IMAGE_EXTENSIONS):
            key = os.path.splitext(filename)[0]
            self.assets[key] = Image.open(path)
            self.lists['type'].append(key)
        if self.lists['type']:
            self.sel['type'] = self.lists['type'][0]

    def load_building_tiles(self):
        for filename, path in self.iter_asset_files("buildings", extensions=TILE_IMAGE_EXTENSIONS, create_if_missing=True):
            key = os.path.splitext(filename)[0]
            self.assets[f"blg_{key}"] = Image.open(path)
            if key not in self.lists['blg']:
                self.lists['blg'].append(key)

    def load_gem_graphics(self):
        for filename, path in self.iter_asset_files("gems", extensions=GEM_IMAGE_EXTENSIONS):
            key = os.path.splitext(filename)[0]
            try:
                self.assets[f"gem_graphic_{key}"] = Image.open(path)
                self.lists['gem_graphics'].append(key)
            except:
                pass

    def load_sky_list(self):
        self.lists['sky'] = [
            filename
            for filename, _ in self.iter_asset_files("skies", create_if_missing=True)
        ]

    def load_generated_owner_overlays(self):
        for i in range(1, 9):
            col_hex = FACTIONS.get(i, ["Unknown", "#888888"])[1]
            if col_hex.startswith("#"):
                col_hex = col_hex[1:]
            if len(col_hex) == 3:
                col_hex = "".join([c * 2 for c in col_hex])
            try:
                r, g, b = tuple(int(col_hex[j:j + 2], 16) for j in (0, 2, 4))
            except:
                r, g, b = (128, 128, 128)
            alpha = 40 if i in [1, 2, 4, 6, 7] else 90
            self.assets[str(i)] = Image.new("RGBA", (64, 64), (r, g, b, alpha))

    def load_asset_catalogs(self, include_generated_overlays=False, include_custom_tile_icons=False):
        if include_generated_overlays:
            self.load_generated_owner_overlays()

        self.load_set_tiles()
        self.load_building_tiles()
        self.load_gem_graphics()
        self.load_sky_list()
        self.load_standard_icons(include_custom_tile_icons=include_custom_tile_icons)
        self.load_building_overlay_icons()
        self.load_building_configs()

    def load_assets(self):
        self.load_asset_catalogs()

    def reload_assets(self):
        self.assets = {}
        self.lists = {'type': [], 'blg': ['00'], 'sky': [], 'gem_graphics': []}
        self.cache = {}

        self.load_asset_catalogs(include_generated_overlays=True, include_custom_tile_icons=True)

        self.draw_palette()
        self.draw_grid()
        self.update_window_title()

    def load_building_overlay_icons(self):
        self.ensure_asset_dir("icons")

        for filename, path in self.iter_asset_files("icons"):
            key = os.path.splitext(filename)[0].lower()
            try:
                self.assets[f"building_overlay_{key}"] = Image.open(path)
            except:
                pass

    def normalize_building_id(self, bid):
        if isinstance(bid, int):
            return f"{max(0, min(255, bid)):02x}"
        text = str(bid).strip().lower()
        try:
            value = int(text, 16)
            if 0 <= value <= 255:
                return f"{value:02x}"
        except:
            pass
        return text

    def normalize_building_config(self, value):
        config = {"icons": [], "hidden": False}

        if isinstance(value, list):
            raw_icons = value
        elif isinstance(value, dict):
            name = str(value.get("name", "")).strip()
            if name:
                config["name"] = name
            config["hidden"] = bool(value.get("hidden", False))
            raw_icons = value.get("icons", [])
        else:
            return config

        if not isinstance(raw_icons, list):
            raw_icons = []

        icons = []
        for icon in raw_icons:
            if not isinstance(icon, str):
                continue
            clean_icon = icon.strip().lower()
            if not clean_icon:
                continue
            if clean_icon == "hidden":
                config["hidden"] = True
                continue
            icons.append(clean_icon)

        config["icons"] = icons
        return config

    def load_building_configs(self):
        candidates = [
            os.path.join("buildings", "building_configs.json"),
            os.path.join("definitions", "building_overlays.json"),
        ]
        self.building_configs = {}

        path = None
        for candidate in candidates:
            candidate_path = resource_path(candidate)
            if os.path.exists(candidate_path):
                path = candidate_path
                break

        if path is None:
            path = writable_path(os.path.join("buildings", "building_configs.json"))
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({}, f, indent=2)
                    f.write("\n")
            except:
                pass
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            try:
                messagebox.showwarning("Building Configs", f"Invalid building config file:\n{e}")
            except:
                pass
            return

        if not isinstance(data, dict):
            try:
                messagebox.showwarning("Building Configs", "building_configs.json must contain an object.")
            except:
                pass
            return

        for bid, value in data.items():
            key = self.normalize_building_id(bid)
            if not key:
                continue
            config = self.normalize_building_config(value)
            if config.get("name") or config.get("icons") or config.get("hidden"):
                self.building_configs[key] = config

    def get_building_config(self, bid):
        return getattr(self, "building_configs", {}).get(self.normalize_building_id(bid), {})

    def get_building_icons(self, bid, palette=False):
        icons = list(self.get_building_config(bid).get("icons", []))
        if not palette:
            icons = [icon for icon in icons if icon not in PALETTE_ONLY_BUILDING_OVERLAYS]
        return icons

    def get_building_display_name(self, bid):
        return self.get_building_config(bid).get("name", "")

    def is_building_hidden(self, bid):
        return bool(self.get_building_config(bid).get("hidden", False))

    def load_definition_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            try:
                messagebox.showwarning("Definitions", f"Could not load {path}:\n{e}")
            except:
                pass
            return {}

        if not isinstance(data, list):
            try:
                messagebox.showwarning("Definitions", f"{path} must contain a JSON array.")
            except:
                pass
            return {}

        loaded = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            try:
                eid = int(entry["id"])
            except:
                continue
            name = str(entry.get("name", "")).strip()
            if name:
                loaded[eid] = name
        return loaded

    def load_definitions(self):
        for kind, base_name in [('veh', 'vehicles'), ('blg', 'buildings'), ('host', 'hoststations'), ('weapon', 'weapons')]:
            path = resource_path(os.path.join("definitions", f"{base_name}.json"))
            if os.path.exists(path):
                self.defs[kind] = self.load_definition_json(path)
                if not self.defs[kind]:
                    try:
                        messagebox.showwarning("Definitions", f"No valid definitions found in:\n{path}")
                    except:
                        pass
            else:
                self.defs[kind] = {}
                try:
                    messagebox.showwarning("Definitions", f"Missing required definition file:\n{path}")
                except:
                    pass
        self.load_host_ai_presets()

    def get_img(self, cat, name, sz, extra=None):
        key = (cat, name, sz, extra)
        if key in self.cache:
            return self.cache[key]
        src = None

        if cat == 'type':
            if name in self.assets:
                src = self.assets[name]

        elif cat == 'blg':
            if f"blg_{name}" in self.assets:
                src = self.assets[f"blg_{name}"]

        elif cat == 'gem_graphic':
            if f"gem_graphic_{name}" in self.assets:
                src = self.assets[f"gem_graphic_{name}"]

        elif cat == 'building_overlay':
            overlay_key = f"building_overlay_{str(name).lower()}"
            if overlay_key in self.assets:
                src = self.assets[overlay_key]

        elif cat == 'special':
            if name in self.assets:
                src = self.assets[name]

        elif cat == 'host':
            h_key = f"host_{name}"
            if h_key not in self.assets:
                path = resource_path(os.path.join("icons", f"{name}.png"))
                if os.path.exists(path):
                    self.assets[h_key] = Image.open(path)
            if h_key in self.assets:
                src = self.assets[h_key]
            else:
                src = self.assets["custom_mod"]

        elif cat == 'overlay':
            fid = int(name)
            if fid in FACTIONS and FACTIONS[fid][1]:
                col = FACTIONS[fid][1]
                rgb = tuple(int(col.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
                if extra == 'pale':
                    alpha = 200
                elif fid == 7:
                    alpha = 171
                elif fid == 5:
                    alpha = 128
                else:
                    alpha = 60
                src = Image.new("RGBA", (sz, sz), rgb + (alpha,))
        elif cat == 'hgt':
            h = int(name)
            diff = abs(h - 127)
            alpha = int(min(200, diff * 6.6))
            color = (255, 255, 255) if h > 127 else (0, 0, 0)
            src = Image.new("RGBA", (sz, sz), color + (alpha,))
        elif cat == 'border':
            src = Image.new("RGBA", (sz, sz), (0, 255, 255, 140))

        if src:
            if cat == 'host' and extra == 'transparent':
                src = src.convert("RGBA").copy()
                alpha = src.getchannel("A")
                alpha = alpha.point(lambda p: int(p * 0.65))
                src.putalpha(alpha)
            tk_img = ImageTk.PhotoImage(src.resize((sz, sz), Image.Resampling.NEAREST))
            self.cache[key] = tk_img
            return tk_img
        return None
