import tkinter as tk
from tkinter import messagebox, ttk
import copy
import re

from .constants import *

class UIMixin:

    GEM_MODEL_LABELS = {
        4: "04 - gem0 (vehicle unlock)",
        7: "07 - gem_with_flak1",
        15: "15 - gem_weapon_power",
        16: "16 - gem_new_building",
        50: "50 - gem_more_shield",
        51: "51 - gem_heavy_weapon",
        60: "60 - gem_more_roboflak",
        61: "61 - gem_power_roboflak",
        65: "65 - gem_more_shield",
    }
    GEM_MODEL_VALUES = [
        "04 - gem0 (vehicle unlock)",
        "07 - gem_with_flak1",
        "15 - gem_weapon_power",
        "16 - gem_new_building",
        "50 - gem_more_shield",
        "51 - gem_heavy_weapon",
        "60 - gem_more_roboflak",
        "61 - gem_power_roboflak",
        "65 - gem_more_shield",
    ]

    FACTION_DISPLAY_NAMES = {
        0: "Neutral",
        1: "Resistance",
        2: "Sulgogar",
        3: "Mykon",
        4: "Taerkasten",
        5: "Black Sect",
        6: "Ghorkov",
        7: "Drones",
    }

    HOST_PLAYER_OWNER_VALUES = [
        "01 - Resistance",
        "02 - Sulgogar",
        "03 - Mykon",
        "04 - Taerkasten",
        "05 - Black Sect",
        "06 - Ghorkov",
        "07 - Drones",
    ]
    HOST_ENERGY_PRESETS = [
        ("Very Low", 100000),
        ("Low", 300000),
        ("Medium", 500000),
        ("High", 700000),
        ("Very High", 1000000),
    ]
    HOST_ENERGY_PRESET_VALUES = [label for label, value in HOST_ENERGY_PRESETS] + ["Custom"]

    HOST_RELOAD_PRESETS = [
        ("Very Low", 153125),
        ("Low", 165625),
        ("Medium", 500000),
        ("Strong", 600000),
        ("Very Strong", 666666),
    ]
    HOST_RELOAD_PRESET_VALUES = [label for label, value in HOST_RELOAD_PRESETS] + ["Custom"]

    HOST_VIEWANGLE_PRESETS = [
        ("Forward", 0),
        ("Left", 90),
        ("Back", 180),
        ("Right", 270),
    ]
    HOST_VIEWANGLE_PRESET_VALUES = [label for label, value in HOST_VIEWANGLE_PRESETS] + ["Custom"]

    def get_faction_display_name(self, owner):
        if owner in self.FACTION_DISPLAY_NAMES:
            return self.FACTION_DISPLAY_NAMES[owner]
        return FACTIONS.get(owner, ("Unknown", None))[0].title()

    def format_player_owner_value(self, owner):
        return f"{owner:02d} - {self.get_faction_display_name(owner)}"

    def parse_player_owner_value(self, value):
        owner = self.parse_leading_int(value)
        if owner is None:
            return 1
        return max(1, min(7, owner))

    def host_energy_preset_for_value(self, energy):
        for label, value in self.HOST_ENERGY_PRESETS:
            if energy == value:
                return label
        return "Custom"

    def parse_host_energy_preset_value(self, preset):
        for label, value in self.HOST_ENERGY_PRESETS:
            if preset == label:
                return value
        return None

    def host_energy_band_label(self, energy):
        if 1 <= energy <= 100000:
            return "Very Low"
        if energy <= 400000:
            return "Low"
        if energy <= 600000:
            return "Medium"
        if energy <= 800000:
            return "High"
        if energy <= 1000000:
            return "Very High"
        return "Custom/Extreme"

    def update_host_energy_controls(self, energy=None):
        if energy is None:
            try:
                energy = int(self.e_host_en.get())
            except:
                energy = None
        preset = "Custom" if energy is None else self.host_energy_preset_for_value(energy)
        if hasattr(self, "cb_host_energy_preset") and self.cb_host_energy_preset.winfo_exists():
            self.cb_host_energy_preset.set(preset)

    def host_reload_preset_for_value(self, reload_const):
        for label, value in self.HOST_RELOAD_PRESETS:
            if reload_const == value:
                return label
        return "Custom"

    def parse_host_reload_preset_value(self, preset):
        for label, value in self.HOST_RELOAD_PRESETS:
            if preset == label:
                return value
        return None

    def update_host_reload_controls(self, reload_const=None):
        if reload_const is None:
            try:
                reload_const = int(self.e_host_reload.get())
            except:
                reload_const = None
        preset = "Custom" if reload_const is None else self.host_reload_preset_for_value(reload_const)
        if hasattr(self, "cb_host_reload_preset") and self.cb_host_reload_preset.winfo_exists():
            self.cb_host_reload_preset.set(preset)

    def host_viewangle_preset_for_value(self, viewangle):
        for label, value in self.HOST_VIEWANGLE_PRESETS:
            if viewangle == value:
                return label
        return "Custom"

    def parse_host_viewangle_preset_value(self, preset):
        for label, value in self.HOST_VIEWANGLE_PRESETS:
            if preset == label:
                return value
        return None

    def update_host_viewangle_controls(self, viewangle=None):
        if viewangle is None:
            try:
                viewangle = int(float(self.e_host_viewangle.get()))
            except:
                viewangle = None
        preset = "Custom" if viewangle is None else self.host_viewangle_preset_for_value(viewangle)
        if hasattr(self, "cb_host_viewangle_preset") and self.cb_host_viewangle_preset.winfo_exists():
            self.cb_host_viewangle_preset.set(preset)

    def format_gem_model_value(self, model_id):
        return self.GEM_MODEL_LABELS.get(model_id, str(model_id))

    def parse_gem_model_value(self, value):
        match = re.match(r'^\s*(\d+)', str(value))
        if not match:
            return None
        return int(match.group(1))

    def format_item_type_value(self, type_id):
        if type_id == 1:
            return "Default"
        return str(type_id)

    def parse_item_type_value(self, value):
        value = str(value).strip()
        if value.lower() == "default":
            return 1
        return self.parse_leading_int(value)

    def format_host_altitude_value(self, value):
        return str(value)

    def parse_leading_int(self, value):
        match = re.match(r'^\s*(\d+)', str(value))
        if not match:
            return None
        return int(match.group(1))

    def definition_combo_values(self, kind):
        return [
            f"{item_id} - {name}"
            for item_id, name in sorted(self.defs.get(kind, {}).items())
        ]

    def format_definition_value(self, kind, item_id):
        name = self.defs.get(kind, {}).get(item_id)
        if name:
            return f"{item_id} - {name}"
        return str(item_id)

    def format_gem_action_for_list(self, action):
        target_type = action.get('target_type', '')
        target_labels = {
            'modify_vehicle': ('Vehicle', 'veh'),
            'modify_weapon': ('Weapon', 'weapon'),
            'modify_building': ('Building', 'blg'),
        }
        label, defs_key = target_labels.get(
            target_type,
            (target_type.replace('modify_', '').replace('_', ' ').title() or "Target", None)
        )
        target_id = action.get('id', 0)
        target_name = self.defs.get(defs_key, {}).get(target_id) if defs_key else None
        target_text = f"{label} {target_id}"
        if target_name:
            target_text += f" - {target_name}"
        return f"{target_text} | {action.get('param', '')} = {action.get('val', '')}"

    def build_gui(self):
        self.f_main = tk.Frame(self.root, bg="#222")
        self.f_main.pack(fill=tk.BOTH, expand=True)

        self.f_left = tk.Frame(self.f_main, bg="#1a1a1a", width=self.left_panel_width)
        self.f_left.pack(side=tk.LEFT, fill=tk.Y, padx=0, pady=0)
        self.f_left.pack_propagate(False)

        self.f_right = tk.Frame(self.f_main, bg="#000")
        self.f_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # --- LEFT PANEL ---
        tb_pal = tk.Frame(self.f_left, bg="#2a2a2a", height=36); tb_pal.pack(fill=tk.X)
        self.lbl_sel = tk.Label(tb_pal, text="00", bg="#2a2a2a", fg="#00FFFF", font=("Courier", 11, "bold"), width=4, anchor="w")
        self.lbl_sel.pack(side=tk.LEFT, padx=(5, 2))

        # CUSTOM ID LABELS & ENTRY
        self.f_custom_inputs = tk.Frame(tb_pal, bg="#2a2a2a")
        
        # ID Input
        self.lbl_custom_title = tk.Label(self.f_custom_inputs, text="Custom Hex:", bg="#2a2a2a", fg="#aaa", font=("Arial",8))
        self.lbl_custom_title.pack(side=tk.LEFT, padx=(2,1))
        
        self.entry_custom = tk.Entry(self.f_custom_inputs, width=4, bg="#111", fg="white", insertbackground="white")
        self.entry_custom.pack(side=tk.LEFT, padx=1)
        self.entry_custom.bind("<KeyRelease>", self.on_custom_input)

        # Name Input
        tk.Label(self.f_custom_inputs, text="Custom Name:", bg="#2a2a2a", fg="#aaa", font=("Arial",8)).pack(side=tk.LEFT, padx=(4,1))
        self.entry_custom_name = tk.Entry(self.f_custom_inputs, width=16, bg="#111", fg="#FFFF00", insertbackground="white")
        self.entry_custom_name.pack(side=tk.LEFT, padx=1)
        self.entry_custom_name.bind("<KeyRelease>", self.on_custom_name_input)

        self.palette_zoom_var = tk.IntVar(value=self.zoom_p)
        self.sld_zoom_p = tk.Scale(
            tb_pal,
            from_=32,
            to=256,
            orient=tk.HORIZONTAL,
            variable=self.palette_zoom_var,
            command=self.set_palette_zoom_from_slider,
            length=88,
            showvalue=False,
            resolution=8,
            bg="#2a2a2a",
            fg="white",
            troughcolor="#444",
            highlightthickness=0
        )
        # Slider applies only to Sector/Building palette thumbnail zoom.
        self.sld_zoom_p.pack(side=tk.RIGHT, padx=(2, 6))

        self.cnt_frame = tk.Frame(self.f_left, bg="#1a1a1a")
        self.cnt_frame.pack(fill=tk.BOTH, expand=True)
        self.cv_pal = tk.Canvas(self.cnt_frame, bg="#1a1a1a", highlightthickness=0)
        self.owner_header = tk.Label(
            self.cnt_frame,
            text="OWNER EDITOR (00 - 07)",
            bg="#CC6600",
            fg="black",
            font=("Arial", 12, "bold")
        )

        self.sb_pal = tk.Scrollbar(
            self.cnt_frame,
            command=self.cv_pal.yview,
            bg="#00B8B8",
            activebackground="#3DF0F0",
            troughcolor="#0f1f1f",
            width=18,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            elementborderwidth=1,
            activerelief=tk.FLAT,
            takefocus=0
        )
        self.cv_pal.config(yscrollcommand=self.sb_pal.set)
        self.cv_pal.bind("<Configure>", self.draw_palette)
        self.bind_palette_scroll_and_zoom()

        self.panels = {
            'GATE': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'ITEM': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'GEM': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'TECH': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'SCRIPT': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'HGT_INPUT': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'SQUAD': tk.Frame(self.cnt_frame, bg="#1a1a1a"),
            'HOST': tk.Frame(self.cnt_frame, bg="#1a1a1a")
        }

        # --- RIGHT PANEL (MAP) ---
        tb_top = tk.Frame(self.f_right, bg="#2a2a2a")
        tb_top.pack(fill=tk.X)
        tb_modes = tk.Frame(tb_top, bg="#2a2a2a", height=28)
        tb_modes.pack(fill=tk.X)
        tb_actions = tk.Frame(tb_top, bg="#242424", height=30)
        tb_actions.pack(fill=tk.X)

        tk.Label(tb_modes, text="MODE:", bg="#2a2a2a", fg="#aaa", font=("Arial", 8, "bold")).pack(side=tk.LEFT, padx=(5,3))
        self.btns = {}

        modes = [("TYPE","SECTOR","#0066CC"),
                 ("BLG","BUILDING","#C71585"),
                 ("OWN","OWNER","#CC6600"),
                 ("HGT","HEIGHT","#660066"),
                 ("GATE","BEAMGATE","#00AAAA"),
                 ("ITEM","BOMB","#FF00FF"),
                 ("GEM","GEM","#00FF00"),
                 ("SQUAD", "SQUAD", "#FF4400"),
                 ("HOST", "HOST", "#FFD700"),
                 ("TECH","TECH","#888800"),
                 ("SCRIPT","SCRIPT","#008080")]

        for m, txt, col in modes:
            b = tk.Button(
                tb_modes,
                text=txt,
                bg="#444",
                fg="white",
                command=lambda x=m: self.set_mode(x),
                font=("Arial", 8),
                padx=3,
                pady=1
            )
            b.pack(side=tk.LEFT, padx=1, pady=2)
            self.btns[m] = (b, col)

        self.btn_undo = tk.Button(tb_actions, text="< UNDO", command=self.undo, bg="#333", fg="white", activebackground="#FFFFFF", activeforeground="black", font=("Arial",8), padx=4, pady=1)
        self.btn_undo.pack(side=tk.LEFT, padx=1, pady=2)
        self.btn_redo = tk.Button(tb_actions, text="REDO >", command=self.redo, bg="#333", fg="white", activebackground="#FFFFFF", activeforeground="black", font=("Arial",8), padx=4, pady=1)
        self.btn_redo.pack(side=tk.LEFT, padx=1, pady=2)

        actions = [("RESET", lambda: self.reset_map(True), "#800000"),
            ("RESIZE", self.resize_map_dialog, "#663300"),
            ("FILL", self.fill_map, "#333")]
        for t, cmd, bg in actions:
            tk.Button(tb_actions, text=t, command=cmd, bg=bg, fg="white", font=("Arial",8), padx=4, pady=1).pack(side=tk.LEFT, padx=1, pady=2)

        self.btn_cl = tk.Button(tb_actions, text="CLEAR VIEW", command=self.toggle_clear, bg="#444", fg="white", font=("Arial",8), padx=4, pady=1)
        self.btn_cl.pack(side=tk.LEFT, padx=(4,2), pady=2)

        io_btns = [("CHANGE LEVEL INFO", self.edit_info, "#4B0082"), ("LOAD MAP", self.load_map, "#CC0000"), ("SAVE MAP", self.save_map, "#006600")]
        for t, cmd, bg in io_btns:
            tk.Button(tb_actions, text=t, command=cmd, bg=bg, fg="white", font=("Arial",8,"bold"), padx=4, pady=1).pack(side=tk.LEFT, padx=2, pady=2)

        self.btn_new_map = tk.Button(
            tb_actions,
            text="NEW MAP",
            command=self.new_map_dialog,
            bg="#2FA8FF",
            fg="white",
            activebackground="#FFFFFF",
            activeforeground="black",
            font=("Arial",8,"bold"),
            padx=4,
            pady=1
        )
        self.btn_new_map.pack(side=tk.LEFT, padx=2, pady=2)

        # MAP CONTAINER (Panning without visible map scrollbars)
        f_map_container = tk.Frame(self.f_right, bg="#000")
        f_map_container.pack(fill=tk.BOTH, expand=True)

        self.cv_map = tk.Canvas(f_map_container, bg="#000", highlightthickness=0)
        self.cv_map.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.cv_map.bind("<Configure>", self.on_map_canvas_configure)

        self.bind_scroll(self.cv_map)
        self.cv_map.bind("<Button-1>", self.on_map_left_press)
        self.cv_map.bind("<B1-Motion>", self.on_map_left_drag)
        self.cv_map.bind("<ButtonRelease-1>", self.on_map_left_release)
        self.cv_map.bind("<Leave>", self.clear_hover_overlay)
        self.cv_map.bind("<Button-3>", self.on_right_click); self.cv_map.bind("<B3-Motion>", self.on_right_click)
        self.cv_map.bind("<ButtonRelease-3>", self.on_map_right_release)
        self.cv_map.bind("<ButtonPress-2>", self.start_map_pan_middle)
        self.cv_map.bind("<B2-Motion>", self.drag_map_pan)
        self.cv_map.bind("<ButtonRelease-2>", self.stop_map_pan)
        self.cv_map.bind("<Control-Button-2>", self.pick)
        self.root.bind_all("<KeyPress-space>", self.on_space_pan_press)
        self.root.bind_all("<KeyRelease-space>", self.on_space_pan_release)
        self.root.bind_all("<Control-z>", self.undo)
        self.root.bind_all("<Control-y>", self.redo)
        self.root.bind_all("<Control-Shift-Z>", self.redo)
        self.root.bind_all("<Delete>", self.delete_selected_object_from_key)
        self.update_history_buttons()


    def set_palette_zoom_from_slider(self, value):
        try:
            new_zoom = int(float(value))
        except:
            return
        new_zoom = max(32, min(256, new_zoom))
        if self.zoom_p == new_zoom:
            return
        self.zoom_p = new_zoom
        self.draw_palette()

    def update_window_title(self):
        title = f"{APP_NAME} - Env: {self.set_folder.upper()}"
        if getattr(self, 'current_filepath', None):
            title += f" - [{self.current_filepath}]"
        elif self.current_filename:
            title += f" - [{self.current_filename}]"
        self.root.title(title)

    def set_mode(self, m):
        old_mode = getattr(self, 'mode', None)
        if (
            old_mode == "SCRIPT" and
            m != "SCRIPT" and
            not getattr(self, '_restoring_snapshot', False) and
            not getattr(self, '_suppress_script_sync', False)
        ):
            self.sync_script_widget_to_data(push_undo=True)

        self.mode = m
        self._drag_squad_idx = -1
        self._drag_host_idx = -1

        for k, (b, c) in self.btns.items(): b.config(bg=c if k==m else "#444")
        self.cv_pal.pack_forget(); self.sb_pal.pack_forget()
        if hasattr(self, "owner_header"):
            self.owner_header.pack_forget()
        for p in self.panels.values(): p.pack_forget()

        if m in ["TYPE", "BLG"]:
            self.lbl_sel.pack(side=tk.LEFT, padx=(5, 2))
            if hasattr(self, "palette_zoom_var"):
                self.palette_zoom_var.set(self.zoom_p)
            self.sld_zoom_p.pack(side=tk.RIGHT, padx=(2, 6))

            self.f_custom_inputs.pack(side=tk.LEFT, padx=(2, 2))
            self.lbl_custom_title.config(text="Custom Hex:")
            
        else:
            self.lbl_sel.pack_forget()
            self.sld_zoom_p.pack_forget()
            self.f_custom_inputs.pack_forget()

        if m == "HGT":
            self.build_height_input_panel()
            self.panels['HGT_INPUT'].pack(fill=tk.BOTH, expand=True)
        elif m in self.panels:
            if m == "GATE": self.build_gate_ui()
            elif m == "ITEM": self.build_item_ui()
            elif m == "GEM": self.build_gem_ui()
            elif m == "TECH": self.build_tech_ui()
            elif m == "SCRIPT": self.build_script_ui()
            elif m == "SQUAD": self.build_squad_ui()
            elif m == "HOST": self.build_host_ui()
            self.panels[m].pack(fill=tk.BOTH, expand=True)
        elif m == "OWN":
            # Owner is a fixed color selector, not a scrollable/zoomable palette.
            # Keep the orange title bar for visual consistency with Height Editor,
            # but do not show the palette scrollbar here.
            if hasattr(self, "owner_header"):
                self.owner_header.pack(fill=tk.X, pady=(10, 6))
            self.cv_pal.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.refresh_palette_layout(force_full=True)
        else:
            self.sb_pal.pack(side=tk.RIGHT, fill=tk.Y); self.cv_pal.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.refresh_palette_layout(force_full=True)

        self.upd_lbl()
        self.draw_grid()

    def sync_script_widget_to_data(self, push_undo=False):
        txt = getattr(self, 'script_text_widget', None)
        if not txt:
            return False
        try:
            if not txt.winfo_exists():
                return False
            new_script = txt.get("1.0", "end-1c")
        except tk.TclError:
            return False

        if new_script == self.script_content:
            return False
        if push_undo:
            self.push_undo_snapshot()
        self.script_content = new_script
        self.dirty = True
        return True

    def upd_lbl(self):
        if self.mode == "GATE":
            slot = getattr(self, "current_gate_slot", 0)
            if 1 <= slot <= getattr(self, "visible_gate_slots", 0):
                tgt = self.gates[slot]['target']
                self.lbl_sel.config(text=f"BEAMGATE [{slot}]: TGT {tgt}")
            else:
                self.lbl_sel.config(text="BEAMGATE EDITOR")
        elif self.mode == "ITEM":
            slot = getattr(self, "current_item_slot", 0)
            if 1 <= slot <= getattr(self, "visible_item_slots", 0):
                mins = self.items[slot]['countdown'] // 60000
                self.lbl_sel.config(text=f"BOMB [{slot}]: {mins}m")
            else:
                self.lbl_sel.config(text="BOMB EDITOR")
        elif self.mode == "GEM":
             slot = getattr(self, "current_gem_slot", 0)
             if 1 <= slot <= getattr(self, "visible_gem_slots", 0):
                 typ = self.gems[slot]['type']
                 t_str = "PWR" if typ==1 else "SHLD" if typ==2 else "TECH"
                 self.lbl_sel.config(text=f"GEM [{slot}]: {t_str}")
             else:
                 self.lbl_sel.config(text="GEM EDITOR")
        elif self.mode == "TECH":
            fid = self.curr_tech_faction
            self.lbl_sel.config(text=f"TECH: {FACTIONS[fid][0]}")
        elif self.mode == "SQUAD":
             self.lbl_sel.config(text="SQUAD EDITOR")
        elif self.mode == "HOST":
             self.lbl_sel.config(text="HOST EDITOR")
        elif self.mode == "SCRIPT":
            self.lbl_sel.config(text="SCRIPT EDITOR")
        elif self.mode == "HGT":
            self.lbl_sel.config(text="")
        else:
            v = self.sel['type'] if self.mode=="TYPE" else self.sel['own'] if self.mode=="OWN" else self.sel['blg']
            t = f"{v:02X}" if isinstance(v,int) else str(v)
            self.lbl_sel.config(text=t)

            if self.mode in ["TYPE", "BLG"] and isinstance(v, str):
                try:
                    v_int = int(v, 16)
                    cat = 'type' if self.mode == "TYPE" else 'blg'
                    self.entry_custom.delete(0, tk.END)
                    self.entry_custom.insert(0, v)
                    
                    self.entry_custom_name.delete(0, tk.END)
                    if v_int in self.custom_definitions[cat]:
                        self.entry_custom_name.insert(0, self.custom_definitions[cat][v_int])
                except: pass

    def schedule_palette_resize_refresh(self):
        if self._palette_resize_refresh_pending:
            return
        self._palette_resize_refresh_pending = True
        self.root.after_idle(self.refresh_palette_layout)

    def refresh_palette_layout(self, force_full=False):
        self._palette_resize_refresh_pending = False
        if not hasattr(self, "cv_pal"):
            return

        y_fraction = 0.0
        try:
            y_fraction = self.cv_pal.yview()[0]
        except:
            pass

        if force_full:
            self.root.update_idletasks()

        container_w = self.cnt_frame.winfo_width()
        sb_w = self.sb_pal.winfo_width() if self.sb_pal.winfo_ismapped() else 0
        target_w = max(120, container_w - sb_w - 2)
        self.cv_pal.config(width=target_w)

        self.draw_palette()
        try:
            self.cv_pal.yview_moveto(max(0.0, min(1.0, y_fraction)))
        except:
            pass

    def on_map_canvas_configure(self, event=None):
        self.schedule_map_layout_refresh()

    def schedule_map_layout_refresh(self):
        if self._map_layout_refresh_pending:
            return
        self._map_layout_refresh_pending = True
        self.root.after_idle(self.refresh_map_layout)

    def refresh_map_layout(self, force_full=False):
        self._map_layout_refresh_pending = False
        if not hasattr(self, "cv_map"):
            return

        x_frac = 0.0
        y_frac = 0.0
        try:
            x_frac = self.cv_map.xview()[0]
            y_frac = self.cv_map.yview()[0]
        except:
            pass

        if force_full:
            self.root.update_idletasks()

        self.draw_grid()

        total_w = max(1, self.mw * self.zoom_m)
        total_h = max(1, self.mh * self.zoom_m)
        view_w = max(1, self.cv_map.winfo_width())
        view_h = max(1, self.cv_map.winfo_height())

        if total_w <= view_w:
            self.cv_map.xview_moveto(0.0)
        else:
            self.cv_map.xview_moveto(max(0.0, min(1.0, x_frac)))

        if total_h <= view_h:
            self.cv_map.yview_moveto(0.0)
        else:
            self.cv_map.yview_moveto(max(0.0, min(1.0, y_frac)))

    def build_height_input_panel(self):
        p = self.panels['HGT_INPUT']
        for w in p.winfo_children(): w.destroy()
        tk.Label(p, text="HEIGHT EDITOR (0 - 60)", bg="#660066", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))

        f = tk.Frame(p, bg="#1a1a1a"); f.pack(pady=10)

        current_user_val = self.sel['hgt'] - HGT_MIN

        def adj_hgt(d):
            new_val = max(HGT_MIN, min(HGT_MAX, self.sel['hgt'] + d))
            self.sel['hgt'] = new_val
            self.update_height_ui_after_value_change()

        tk.Button(f, text="<", command=lambda: adj_hgt(-1), bg="#333", fg="white", font=("bold", 14), width=3).pack(side=tk.LEFT, padx=10)

        self.hgt_entry = tk.Entry(f, width=5, font=("Courier", 24, "bold"), justify='center', bg="#222", fg="#00FFFF", insertbackground="white")
        self.hgt_entry.pack(side=tk.LEFT, padx=10, ipady=4)
        self.hgt_entry.insert(0, str(current_user_val))

        def manual_entry(e):
             self.process_height_input(e)
             self.update_height_ui_after_value_change(update_entry=False)

        self.hgt_entry.bind("<KeyRelease>", manual_entry)

        tk.Button(f, text=">", command=lambda: adj_hgt(1), bg="#333", fg="white", font=("bold", 14), width=3).pack(side=tk.LEFT, padx=10)

        self.lbl_hgt_hex = tk.Label(p, text=f"real hex output: {self.sel['hgt']:02X}", bg="#1a1a1a", fg="#888", font=("Courier", 10, "bold"))
        self.lbl_hgt_hex.pack(pady=(0, 10))

        self.height_slider_canvas = tk.Canvas(p, width=HEIGHT_SLIDER_W, height=HEIGHT_SLIDER_H, bg=HEIGHT_PANEL_BG, highlightthickness=0)
        self.height_slider_canvas.pack(pady=(0, 20))
        self.height_slider_canvas.bind("<Button-1>", self.on_height_slider_drag)
        self.height_slider_canvas.bind("<B1-Motion>", self.on_height_slider_drag)
        self.draw_height_slider()

        self.build_height_legend(p)

    def get_selected_editor_height(self):
        try:
            return max(EDITOR_HEIGHT_MIN, min(EDITOR_HEIGHT_MAX, int(self.sel['hgt']) - HGT_MIN))
        except:
            return EDITOR_HEIGHT_BASE

    def draw_height_slider(self):
        if not hasattr(self, "height_slider_canvas") or not self.height_slider_canvas.winfo_exists():
            return

        cv = self.height_slider_canvas
        cv.delete("all")

        w, h = HEIGHT_SLIDER_W, HEIGHT_SLIDER_H
        top_y, bottom_y = HEIGHT_SLIDER_TOP_Y, HEIGHT_SLIDER_BOTTOM_Y
        center_y = (top_y + bottom_y) // 2
        track_w = HEIGHT_SLIDER_TRACK_W
        x0 = (w - track_w) // 2
        x1 = x0 + track_w
        selected_height = self.get_selected_editor_height()
        delta = selected_height - EDITOR_HEIGHT_BASE

        high_color = HEIGHT_HIGH_COLOR
        low_color = HEIGHT_LOW_COLOR
        base_color = HEIGHT_BASE_COLOR
        bg_color = "#0D1114"
        track_color = "#242B30"

        cv.create_rectangle(x0 - 12, top_y, x1 + 12, bottom_y, fill=bg_color, outline="#4A565D", width=1)
        cv.create_rectangle(x0, top_y + 8, x1, bottom_y - 8, fill=track_color, outline="#111", width=1)

        half_h = max(1, ((bottom_y - top_y) // 2) - 8)
        fill_len = int((abs(delta) / EDITOR_HEIGHT_BASE) * half_h)
        if delta != 0:
            fill_len = max(1, fill_len)

        if delta > 0:
            cv.create_rectangle(x0 + 2, center_y - fill_len, x1 - 2, center_y, fill=high_color, outline="")
        elif delta < 0:
            cv.create_rectangle(x0 + 2, center_y, x1 - 2, center_y + fill_len, fill=low_color, outline="")

        tick_45 = self.height_slider_y_from_value(45)
        tick_15 = self.height_slider_y_from_value(15)
        cv.create_line(x0 - 8, tick_45, x1 + 8, tick_45, fill="#627078", width=1)
        cv.create_line(x0 - 8, tick_15, x1 + 8, tick_15, fill="#627078", width=1)

        marker_y = self.height_slider_y_from_value(selected_height)
        cv.create_line(x0 - 18, center_y, x1 + 18, center_y, fill=base_color, width=3)
        cv.create_rectangle(x0 - 10, marker_y - 5, x1 + 10, marker_y + 5, fill="#F7F7F7", outline="#000", width=1)

        label_x = x1 + 34
        cv.create_text(label_x, top_y, text="60", fill=high_color, font=("Arial", 10, "bold"))
        cv.create_text(label_x, center_y, text="30", fill=base_color, font=("Arial", 10, "bold"))
        cv.create_text(label_x, bottom_y, text="0", fill=low_color, font=("Arial", 10, "bold"))

    def build_height_legend(self, parent):
        legend = tk.Frame(parent, bg=HEIGHT_PANEL_BG)
        legend.pack(pady=(0, 12))

        rows = [
            (HEIGHT_HIGH_COLOR, "HIGHER"),
            (HEIGHT_BASE_COLOR, "BASE 30"),
            (HEIGHT_LOW_COLOR, "LOWER"),
        ]
        for color, text in rows:
            row = tk.Frame(legend, bg=HEIGHT_PANEL_BG)
            row.pack(anchor=tk.W, pady=1)
            swatch = tk.Canvas(row, width=24, height=10, bg=HEIGHT_PANEL_BG, highlightthickness=0)
            swatch.pack(side=tk.LEFT, padx=(0, 6))
            y = 5
            swatch.create_line(2, y, 22, y, fill=color, width=4)
            tk.Label(row, text=text, bg="#1a1a1a", fg="#BBBBBB", font=("Arial", 8, "bold")).pack(side=tk.LEFT)

    def get_height_slider_track_bounds(self):
        w = HEIGHT_SLIDER_W
        top_y, bottom_y = HEIGHT_SLIDER_TOP_Y, HEIGHT_SLIDER_BOTTOM_Y
        track_w = HEIGHT_SLIDER_TRACK_W
        x0 = (w - track_w) // 2
        x1 = x0 + track_w
        return x0 - 12, top_y, x1 + 12, bottom_y

    def height_slider_y_from_value(self, value):
        top_y, bottom_y = HEIGHT_SLIDER_TOP_Y, HEIGHT_SLIDER_BOTTOM_Y
        value = max(EDITOR_HEIGHT_MIN, min(EDITOR_HEIGHT_MAX, int(value)))
        ratio = value / EDITOR_HEIGHT_MAX
        return int(bottom_y - (ratio * (bottom_y - top_y)))

    def height_from_slider_y(self, y):
        top_y, bottom_y = HEIGHT_SLIDER_TOP_Y, HEIGHT_SLIDER_BOTTOM_Y
        y = max(top_y, min(bottom_y, int(y)))
        ratio = (bottom_y - y) / (bottom_y - top_y)
        return max(EDITOR_HEIGHT_MIN, min(EDITOR_HEIGHT_MAX, round(ratio * EDITOR_HEIGHT_MAX)))

    def on_height_slider_drag(self, event):
        track_x0, track_y0, track_x1, track_y1 = self.get_height_slider_track_bounds()
        if event.x < track_x0 or event.x > track_x1 or event.y < track_y0 or event.y > track_y1:
            return

        new_value = self.height_from_slider_y(event.y)
        new_hgt = new_value + HGT_MIN
        if new_hgt != self.sel['hgt']:
            self.sel['hgt'] = new_hgt
            self.update_height_ui_after_value_change()

    def update_height_ui_after_value_change(self, update_entry=True):
        user_val = self.get_selected_editor_height()
        if update_entry and hasattr(self, 'hgt_entry') and self.hgt_entry.winfo_exists():
            self.hgt_entry.delete(0, tk.END)
            self.hgt_entry.insert(0, str(user_val))
        if hasattr(self, 'lbl_hgt_hex') and self.lbl_hgt_hex.winfo_exists():
            self.lbl_hgt_hex.config(text=f"real hex output: {self.sel['hgt']:02X}")
        self.draw_height_slider()

    def process_height_input(self, event=None):
        val_str = self.hgt_entry.get().strip()
        if not val_str.isdigit(): return
        try:
            val = int(val_str)
            val = max(EDITOR_HEIGHT_MIN, min(EDITOR_HEIGHT_MAX, val))
            self.sel['hgt'] = val + HGT_MIN
        except ValueError: pass

    # ------------------------------------------------------------------
    # Special object list workflow: Beamgate / Bomb / Gem
    # ------------------------------------------------------------------
    def get_special_store(self, kind):
        if kind == "GATE": return self.gates
        if kind == "ITEM": return self.items
        if kind == "GEM": return self.gems
        raise ValueError(kind)

    def get_special_visible_count(self, kind):
        if kind == "GATE": return self.visible_gate_slots
        if kind == "ITEM": return self.visible_item_slots
        if kind == "GEM": return self.visible_gem_slots
        return 0

    def set_special_visible_count(self, kind, value):
        value = max(0, min(MAX_SPECIAL_SLOTS, int(value)))
        if kind == "GATE": self.visible_gate_slots = value
        elif kind == "ITEM": self.visible_item_slots = value
        elif kind == "GEM": self.visible_gem_slots = value

    def get_current_special_slot(self, kind):
        if kind == "GATE": return self.current_gate_slot
        if kind == "ITEM": return self.current_item_slot
        if kind == "GEM": return self.current_gem_slot
        return 0

    def set_current_special_slot(self, kind, slot):
        slot = int(slot or 0)
        if kind == "GATE": self.current_gate_slot = slot
        elif kind == "ITEM": self.current_item_slot = slot
        elif kind == "GEM": self.current_gem_slot = slot

    def get_special_tool(self, kind):
        if kind == "GATE": return self.gate_tool
        if kind == "ITEM": return self.item_tool
        if kind == "GEM": return self.gem_tool
        return None

    def set_special_tool(self, kind, tool):
        if kind == "GATE": self.gate_tool = tool
        elif kind == "ITEM": self.item_tool = tool
        elif kind == "GEM": self.gem_tool = tool

    def get_special_default_data(self, kind):
        if kind == "GATE":
            return {'active': True, 'x': -1, 'y': -1, 'keys': [], 'target': 0, 'closed_bp': 25, 'opened_bp': 26, 'hidden': False}
        if kind == "ITEM":
            return {'active': True, 'x': -1, 'y': -1, 'keys': [], 'countdown': 300000,
                    'inactive_bp': 35, 'active_bp': 36, 'trigger_bp': 37, 'type': 1, 'hidden': False}
        if kind == "GEM":
            return {'x': -1, 'y': -1, 'blg': 50, 'type': 3, 'actions': [], 'hidden': False}
        raise ValueError(kind)

    def get_special_color(self, kind):
        return {"GATE": "#00FFFF", "ITEM": "#FF00FF", "GEM": "#00FF00"}.get(kind, "#FFFFFF")

    def get_special_display_title(self, kind):
        return {"GATE": "BEAMGATE", "ITEM": "BOMB", "GEM": "GEM"}.get(kind, kind)

    def get_special_listbox_name(self, kind):
        return {"GATE": "lb_gates", "ITEM": "lb_items", "GEM": "lb_gems"}.get(kind, "")

    def get_special_cells_for_redraw(self, kind, data):
        cells = []
        if not data:
            return cells
        cell = self.get_special_cell(data) if kind in ("GATE", "ITEM") else self.get_gem_cell(data)
        if cell:
            cells.append(cell)
        if kind in ("GATE", "ITEM"):
            cells.extend(list(data.get('keys', [])))
        return cells

    def clear_special_slot_visuals(self, kind, idx):
        store = self.get_special_store(kind)
        if idx not in store:
            return []
        data = store[idx]
        cells = self.get_special_cells_for_redraw(kind, data)
        if kind == "GATE":
            self.clear_special_auto_visual("GATE", data)
        elif kind == "ITEM":
            self.clear_all_item_key_visuals(data)
            self.clear_special_auto_visual("ITEM", data)
        elif kind == "GEM":
            self.clear_gem_building_visual_if_matches(data)
        return cells

    def get_gem_type_display_name(self, type_id):
        return {1: "Weapon", 2: "Shield", 3: "Tech/Unlock"}.get(type_id, "Custom")

    def get_special_list_label(self, kind, idx):
        data = self.get_special_store(kind)[idx]
        prefix = "[UNPLACED] " if not self.map_cell_is_valid(data) else ""
        if kind == "GATE":
            key_count = len(data.get('keys', []))
            road = "Road" if (data.get('closed_bp'), data.get('opened_bp')) == (5, 6) else "No Road" if (data.get('closed_bp'), data.get('opened_bp')) == (25, 26) else "Custom"
            parts = [f"{prefix}#{idx}", f"Target Level: {data.get('target', 0)}", f"Road Preset: {road}", f"Keys: {key_count}"]
        elif kind == "ITEM":
            key_count = len(data.get('keys', []))
            mins = data.get('countdown', 0) / 60000.0
            preset = "Standard" if (data.get('inactive_bp'), data.get('active_bp'), data.get('trigger_bp')) == (35, 36, 37) else "Parasite" if (data.get('inactive_bp'), data.get('active_bp'), data.get('trigger_bp')) == (68, 69, 70) else "Custom"
            parts = [f"{prefix}#{idx}", f"Preset: {preset}", f"Countdown: {mins:.1f}m", f"Keys: {key_count}"]
        else:
            action_count = len(data.get('actions', []))
            gem_type = data.get('type', 3)
            parts = [
                f"{prefix}#{idx}",
                f"Building: {data.get('blg', 50)}",
                f"Type: {gem_type} ({self.get_gem_type_display_name(gem_type)})",
                f"Actions: {action_count}"
            ]
        if data.get('hidden', False):
            parts.append("Hidden")
        return " | ".join(parts)

    def refresh_special_list(self, kind, redraw=True):
        lb_name = self.get_special_listbox_name(kind)
        if not lb_name or not hasattr(self, lb_name):
            return
        lb = getattr(self, lb_name)
        if not lb.winfo_exists():
            return
        sync_name = f"_syncing_{kind.lower()}_selection"
        setattr(self, sync_name, True)
        try:
            lb.delete(0, tk.END)
            count = self.get_special_visible_count(kind)
            current = self.get_current_special_slot(kind)
            for idx in range(1, count + 1):
                lb.insert(tk.END, self.get_special_list_label(kind, idx))
                if idx == current:
                    lb.selection_set(idx - 1)
                    lb.activate(idx - 1)
                    lb.see(idx - 1)
        finally:
            setattr(self, sync_name, False)
        if redraw:
            self.draw_grid()

    def on_special_list_select(self, kind, event=None):
        if getattr(self, f"_syncing_{kind.lower()}_selection", False):
            return
        lb = getattr(self, self.get_special_listbox_name(kind), None)
        if not lb or not lb.winfo_exists():
            return
        sel = lb.curselection()
        if not sel:
            self.deselect_special(kind)
            return
        self.select_special_slot(kind, sel[0] + 1)

    def select_special_slot(self, kind, slot, rebuild=True, redraw=True):
        count = self.get_special_visible_count(kind)
        if slot < 1 or slot > count:
            return
        previous = self.get_special_store(kind).get(self.get_current_special_slot(kind))
        previous_cells = self.get_special_cells_for_redraw(kind, previous)
        self.set_current_special_slot(kind, slot)
        if kind == "GATE": self.gate_tool = "GATE"
        elif kind == "ITEM": self.item_tool = "ITEM"
        elif kind == "GEM": self.gem_tool = "GEM"
        if rebuild:
            self.build_special_ui_by_kind(kind)
        else:
            self.refresh_special_list(kind, redraw=False)
        if redraw:
            selected_cells = self.get_special_cells_for_redraw(kind, self.get_special_store(kind)[slot])
            self.redraw_cells(*(previous_cells + selected_cells))
        self.upd_lbl()

    def deselect_special(self, kind):
        previous = self.get_special_store(kind).get(self.get_current_special_slot(kind))
        previous_cells = self.get_special_cells_for_redraw(kind, previous)
        self.set_current_special_slot(kind, 0)
        self._drag_key_list = None
        self._drag_key_idx = -1
        lb = getattr(self, self.get_special_listbox_name(kind), None)
        if lb and lb.winfo_exists():
            setattr(self, f"_syncing_{kind.lower()}_selection", True)
            try:
                lb.selection_clear(0, tk.END)
            finally:
                setattr(self, f"_syncing_{kind.lower()}_selection", False)
        self.build_special_ui_by_kind(kind)
        self.redraw_cells(*previous_cells)
        self.upd_lbl()

    def add_special_to_list(self, kind):
        count = self.get_special_visible_count(kind)
        if count >= MAX_SPECIAL_SLOTS:
            messagebox.showwarning("Limit reached", f"Maximum {MAX_SPECIAL_SLOTS} {self.get_special_display_title(kind).lower()} slots reached.")
            return
        self.push_undo_snapshot()
        new_slot = count + 1
        self.get_special_store(kind)[new_slot] = self.get_special_default_data(kind)
        self.set_special_visible_count(kind, new_slot)
        self.set_current_special_slot(kind, new_slot)
        if kind == "GATE": self.gate_tool = "GATE"
        elif kind == "ITEM": self.item_tool = "ITEM"
        elif kind == "GEM": self.gem_tool = "GEM"
        self.dirty = True
        self.build_special_ui_by_kind(kind)
        self.draw_grid()

    def delete_special_slot(self, kind, idx):
        count = self.get_special_visible_count(kind)
        if idx < 1 or idx > count:
            return False
        self.push_undo_snapshot()
        affected_cells = []
        affected_cells.extend(self.clear_special_slot_visuals(kind, idx))
        store = self.get_special_store(kind)
        for j in range(idx, count):
            store[j] = copy.deepcopy(store[j + 1])
        store[count] = self.get_special_default_data(kind)
        self.set_special_visible_count(kind, count - 1)
        new_count = self.get_special_visible_count(kind)
        self.set_current_special_slot(kind, min(idx, new_count) if new_count else 0)
        self._drag_key_list = None
        self._drag_key_idx = -1
        self.invalidate_render_indexes()
        self.dirty = True
        self.build_special_ui_by_kind(kind)
        self.draw_grid()
        return True

    def delete_selected_special(self, kind):
        idx = self.get_current_special_slot(kind)
        if idx <= 0:
            lb = getattr(self, self.get_special_listbox_name(kind), None)
            if lb and lb.winfo_exists():
                sel = lb.curselection()
                if sel:
                    idx = sel[0] + 1
        self.delete_special_slot(kind, idx)

    def delete_all_special(self, kind):
        count = self.get_special_visible_count(kind)
        if count <= 0:
            return
        if not messagebox.askyesno(f"Delete All {self.get_special_display_title(kind)}", f"Delete all {self.get_special_display_title(kind).lower()} objects?"):
            return
        self.push_undo_snapshot()
        for idx in range(1, count + 1):
            self.clear_special_slot_visuals(kind, idx)
            self.get_special_store(kind)[idx] = self.get_special_default_data(kind)
        self.set_special_visible_count(kind, 0)
        self.set_current_special_slot(kind, 0)
        self._drag_key_list = None
        self._drag_key_idx = -1
        self.invalidate_render_indexes()
        self.dirty = True
        self.build_special_ui_by_kind(kind)
        self.draw_grid()

    def build_special_actions_and_list(self, parent, kind):
        color = self.get_special_color(kind)
        f_actions = tk.Frame(parent, bg="#1a1a1a")
        f_actions.pack(anchor=tk.CENTER, pady=(6, 4))
        tk.Button(f_actions, text="ADD TO LIST", command=lambda: self.add_special_to_list(kind), bg=color, fg="black", font=("Arial", 9, "bold"), width=14).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DESELECT", command=lambda: self.deselect_special(kind), bg="#333", fg="white", font=("Arial", 9, "bold"), width=11).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DELETE", command=lambda: self.delete_selected_special(kind), bg="#880000", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DELETE ALL", command=lambda: self.delete_all_special(kind), bg="#AA0000", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT)

        f_list = tk.Frame(parent, bg="#1a1a1a")
        f_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 8))
        lb = tk.Listbox(f_list, bg="#222", fg="white", height=8, selectbackground=LISTBOX_SELECTION_BG, selectforeground=LISTBOX_SELECTION_FG, exportselection=False)
        lb.pack(fill=tk.BOTH, expand=True)
        setattr(self, self.get_special_listbox_name(kind), lb)
        lb.bind('<<ListboxSelect>>', lambda e, k=kind: self.on_special_list_select(k, e))
        self.refresh_special_list(kind, redraw=False)

    def build_special_ui_by_kind(self, kind):
        if kind == "GATE": self.build_gate_ui()
        elif kind == "ITEM": self.build_item_ui()
        elif kind == "GEM": self.build_gem_ui()

    def selected_special_data_or_none(self, kind):
        slot = self.get_current_special_slot(kind)
        count = self.get_special_visible_count(kind)
        if slot < 1 or slot > count:
            return None
        return self.get_special_store(kind)[slot]

    def build_no_special_selected_message(self, parent, kind):
        tk.Label(
            parent,
            text=f"No {self.get_special_display_title(kind).lower()} selected.\nUse ADD TO LIST or click an object on the map.",
            fg="#AAAAAA",
            bg="#1a1a1a",
            justify=tk.CENTER,
            font=("Arial", 9, "italic")
        ).pack(anchor=tk.CENTER, pady=16)

    def build_gate_ui(self):
        panel = self.panels['GATE']
        for w in panel.winfo_children(): w.destroy()
        tk.Label(panel, text="BEAMGATE PLACER", bg="#00FFFF", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))
        p = tk.Frame(panel, bg="#1a1a1a")
        p.pack(fill=tk.BOTH, expand=True, anchor=tk.NW, padx=12, pady=6)

        g_data = self.selected_special_data_or_none("GATE")
        if not g_data:
            self.build_no_special_selected_message(p, "GATE")
            self.build_special_actions_and_list(p, "GATE")
            self.upd_lbl(); self.draw_grid()
            return

        # --- PRESET DROPDOWN (GATE) ---
        f_pre = tk.Frame(p, bg="#1a1a1a"); f_pre.pack(anchor=tk.CENTER, pady=(4,2))
        tk.Label(f_pre, text="Road Preset:", bg="#1a1a1a", fg="#aaa").pack(side=tk.LEFT, padx=5)
        gate_preset_values = ["With Roads (5/6)", "No Road (25/26)"]
        cb_gate_preset = ttk.Combobox(f_pre, values=gate_preset_values, state="readonly", width=24)
        cb_gate_preset.pack(side=tk.LEFT, padx=5)
        if g_data['closed_bp'] == 5 and g_data['opened_bp'] == 6:
            cb_gate_preset.set("With Roads (5/6)")
        elif g_data['closed_bp'] == 25 and g_data['opened_bp'] == 26:
            cb_gate_preset.set("No Road (25/26)")
        else:
            cb_gate_preset.set("")

        def on_gate_preset_select(event):
            val = cb_gate_preset.get()
            old_closed = g_data['closed_bp']; old_opened = g_data['opened_bp']
            old_cell = self.get_special_cell(g_data)
            old_visual = self.get_special_auto_visual("GATE", g_data)
            if "With Roads" in val:
                new_closed, new_opened = 5, 6
            elif "No Road" in val:
                new_closed, new_opened = 25, 26
            else:
                return
            if new_closed != old_closed or new_opened != old_opened:
                self.push_undo_snapshot()
                g_data['closed_bp'] = new_closed; g_data['opened_bp'] = new_opened
                self.sync_special_auto_visual("GATE", g_data, old_cell=old_cell, old_visual=old_visual)
                self.dirty = True
            self.build_gate_ui(); self.draw_grid()
        cb_gate_preset.bind("<<ComboboxSelected>>", on_gate_preset_select)
        tk.Label(p, text="(Custom values allowed)", fg="#777", bg="#1a1a1a", font=("Arial", 7)).pack(anchor=tk.CENTER, pady=(0,5))

        f = tk.Frame(p, bg="#1a1a1a"); f.pack(anchor=tk.CENTER, pady=5)
        tk.Label(f, text="Target Level:", fg="white", bg="#1a1a1a", width=12).grid(row=0, column=0, sticky="e", padx=(0, 4), pady=2)
        e_tgt = tk.Entry(f, width=8); e_tgt.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=2); e_tgt.insert(0, str(g_data['target']))
        tk.Label(f, text="Off Model:", fg="white", bg="#1a1a1a", width=10).grid(row=0, column=2, sticky="e", padx=(0, 4), pady=2)
        e_cls = tk.Entry(f, width=8); e_cls.grid(row=0, column=3, sticky="w", pady=2); e_cls.insert(0, str(g_data['closed_bp']))
        tk.Label(f, text="On Model:", fg="white", bg="#1a1a1a", width=12).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=2)
        e_opn = tk.Entry(f, width=8); e_opn.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=2); e_opn.insert(0, str(g_data['opened_bp']))

        def upd_g(ev=None):
            try:
                new_target = int(e_tgt.get()); new_closed = int(e_cls.get()); new_opened = int(e_opn.get())
                if new_target != g_data['target'] or new_closed != g_data['closed_bp'] or new_opened != g_data['opened_bp']:
                    old_cell = self.get_special_cell(g_data)
                    old_visual = self.get_special_auto_visual("GATE", g_data)
                    visual_changed = new_closed != g_data['closed_bp'] or new_opened != g_data['opened_bp']
                    self.push_undo_snapshot()
                    g_data['target'] = new_target; g_data['closed_bp'] = new_closed; g_data['opened_bp'] = new_opened
                    if visual_changed:
                        self.sync_special_auto_visual("GATE", g_data, old_cell=old_cell, old_visual=old_visual)
                    self.dirty = True
                if g_data['closed_bp'] == 5 and g_data['opened_bp'] == 6: cb_gate_preset.set("With Roads (5/6)")
                elif g_data['closed_bp'] == 25 and g_data['opened_bp'] == 26: cb_gate_preset.set("No Road (25/26)")
                else: cb_gate_preset.set("")
                self.refresh_special_list("GATE", redraw=False); self.upd_lbl(); self.draw_grid()
            except: pass
        for e in [e_tgt, e_cls, e_opn]: e.bind("<KeyRelease>", upd_g)

        f_opt = tk.Frame(p, bg="#1a1a1a"); f_opt.pack(anchor=tk.CENTER, pady=(4, 2))
        self.var_gate_hid = tk.BooleanVar(value=g_data.get('hidden', False))
        def tog_gate_hid():
            val = self.var_gate_hid.get()
            if g_data.get('hidden', False) != val:
                self.push_undo_snapshot(); g_data['hidden'] = val; self.dirty = True; self.refresh_special_list("GATE", redraw=False)
        tk.Checkbutton(f_opt, text="Hide in Briefing", variable=self.var_gate_hid, command=tog_gate_hid, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.CENTER, padx=5)

        f_place = tk.Frame(p, bg="#1a1a1a"); f_place.pack(anchor=tk.CENTER, pady=(8, 0))
        tk.Button(f_place, text="[ PLACE BEAMGATE ]", command=lambda: self.set_special_tool("GATE", "GATE"), bg="#00FFFF", fg="black", width=24).pack(anchor=tk.CENTER, pady=(0, 3))
        tk.Button(f_place, text="[ PLACE KEYSECT ]", command=lambda: self.set_special_tool("GATE", "KEY"), bg="#FFFF00", fg="black", width=24).pack(anchor=tk.CENTER)
        self.build_special_actions_and_list(p, "GATE")
        self.upd_lbl(); self.draw_grid()

    def select_gate_slot(self, slot):
        self.select_special_slot("GATE", slot)

    def build_item_ui(self):
        panel = self.panels['ITEM']
        for w in panel.winfo_children(): w.destroy()
        tk.Label(panel, text="BOMB PLACER", bg="#FF00FF", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))
        p = tk.Frame(panel, bg="#1a1a1a")
        p.pack(fill=tk.BOTH, expand=True, anchor=tk.NW, padx=12, pady=6)

        i_data = self.selected_special_data_or_none("ITEM")
        if not i_data:
            self.build_no_special_selected_message(p, "ITEM")
            self.build_special_actions_and_list(p, "ITEM")
            self.upd_lbl(); self.draw_grid()
            return

        f_pre = tk.Frame(p, bg="#1a1a1a"); f_pre.pack(anchor=tk.CENTER, pady=(4,2))
        tk.Label(f_pre, text="Model Preset:", bg="#1a1a1a", fg="#aaa", width=12, anchor="e").pack(side=tk.LEFT, padx=(0, 5))
        preset_values = ["Standard (35/36/37)", "Parasite (68/69/70)"]
        cb_preset = ttk.Combobox(f_pre, values=preset_values, state="readonly", width=22); cb_preset.pack(side=tk.LEFT)
        if i_data['inactive_bp'] == 35 and i_data['active_bp'] == 36 and i_data['trigger_bp'] == 37:
            cb_preset.set("Standard (35/36/37)")
        elif i_data['inactive_bp'] == 68 and i_data['active_bp'] == 69 and i_data['trigger_bp'] == 70:
            cb_preset.set("Parasite (68/69/70)")
        else:
            cb_preset.set("")

        def on_preset_select(event):
            val = cb_preset.get()
            old_vals = (i_data['inactive_bp'], i_data['active_bp'], i_data['trigger_bp'])
            old_cell = self.get_special_cell(i_data); old_visual = self.get_special_auto_visual("ITEM", i_data)
            if "Standard" in val: new_vals = (35, 36, 37)
            elif "Parasite" in val: new_vals = (68, 69, 70)
            else: return
            if new_vals != old_vals:
                self.push_undo_snapshot()
                i_data['inactive_bp'], i_data['active_bp'], i_data['trigger_bp'] = new_vals
                self.sync_special_auto_visual("ITEM", i_data, old_cell=old_cell, old_visual=old_visual)
                self.dirty = True
            self.build_item_ui(); self.draw_grid()
        cb_preset.bind("<<ComboboxSelected>>", on_preset_select)

        f_key_pre = tk.Frame(p, bg="#1a1a1a"); f_key_pre.pack(anchor=tk.CENTER, pady=(8, 2))
        tk.Label(f_key_pre, text="Keysect Preset:", bg="#1a1a1a", fg="#aaa", width=14, anchor="e").pack(side=tk.LEFT, padx=(0, 5))
        key_visual_values = ["No Road (F3)", "With Roads (F4)"]
        cb_key_visual = ttk.Combobox(f_key_pre, values=key_visual_values, state="readonly", width=22); cb_key_visual.pack(side=tk.LEFT)
        cb_key_visual.set("With Roads (F4)" if self.get_item_key_visual_typ(i_data) == "f4" else "No Road (F3)")

        def on_key_visual_select(event=None):
            old_visual = self.get_item_key_visual_typ(i_data)
            new_visual = "f4" if "F4" in cb_key_visual.get() else "f3"
            if old_visual == new_visual: return
            self.push_undo_snapshot(); i_data['_key_visual_typ'] = new_visual
            changed_cells = self.sync_item_key_visual_preset(i_data, old_visual, new_visual)
            if changed_cells:
                self.invalidate_render_indexes(); self.redraw_cells(*changed_cells)
            else:
                self.dirty = True
        cb_key_visual.bind("<<ComboboxSelected>>", on_key_visual_select)
        tk.Label(p, text="(Custom values allowed)", fg="#777", bg="#1a1a1a", font=("Arial", 7)).pack(anchor=tk.CENTER, pady=(0,5))

        f = tk.Frame(p, bg="#1a1a1a"); f.pack(anchor=tk.CENTER, pady=5)
        tk.Label(f, text="Timer:", fg="white", bg="#1a1a1a", width=10).grid(row=0, column=0, sticky="e", padx=(0, 4), pady=2)
        e_cnt = tk.Entry(f, width=8); e_cnt.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=2); e_cnt.insert(0, str(i_data['countdown']))
        lbl_mins = tk.Label(f, text="", bg="#1a1a1a", fg="#00FF00", font=("Arial", 8)); lbl_mins.grid(row=0, column=2, padx=(0, 12), sticky="w")
        tk.Label(f, text="Type:", fg="white", bg="#1a1a1a", width=8).grid(row=0, column=3, sticky="e", padx=(0, 4), pady=2)
        e_typ = ttk.Combobox(f, values=["Default"], width=10); e_typ.grid(row=0, column=4, sticky="w", pady=2); e_typ.insert(0, self.format_item_type_value(i_data['type']))
        tk.Label(f, text="Off Model:", fg="white", bg="#1a1a1a", width=10).grid(row=1, column=0, sticky="e", padx=(0, 4), pady=2)
        e_ina = tk.Entry(f, width=8); e_ina.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=2); e_ina.insert(0, str(i_data['inactive_bp']))
        tk.Label(f, text="On Model:", fg="white", bg="#1a1a1a", width=8).grid(row=1, column=3, sticky="e", padx=(0, 4), pady=2)
        e_act = tk.Entry(f, width=8); e_act.grid(row=1, column=4, sticky="w", pady=2); e_act.insert(0, str(i_data['active_bp']))
        tk.Label(f, text="Trigger:", fg="white", bg="#1a1a1a", width=10).grid(row=2, column=0, sticky="e", padx=(0, 4), pady=2)
        e_trg = tk.Entry(f, width=8); e_trg.grid(row=2, column=1, sticky="w", padx=(0, 8), pady=2); e_trg.insert(0, str(i_data['trigger_bp']))

        def upd(ev=None):
            try:
                new_countdown = int(e_cnt.get()); new_type = self.parse_item_type_value(e_typ.get())
                if new_type is None: raise ValueError
                new_inactive = int(e_ina.get()); new_active = int(e_act.get()); new_trigger = int(e_trg.get())
                if (new_countdown != i_data['countdown'] or new_type != i_data['type'] or new_inactive != i_data['inactive_bp'] or new_active != i_data['active_bp'] or new_trigger != i_data['trigger_bp']):
                    old_cell = self.get_special_cell(i_data); old_visual = self.get_special_auto_visual("ITEM", i_data)
                    visual_changed = (new_inactive != i_data['inactive_bp'] or new_active != i_data['active_bp'] or new_trigger != i_data['trigger_bp'])
                    self.push_undo_snapshot()
                    i_data['countdown'] = new_countdown; i_data['type'] = new_type; i_data['inactive_bp'] = new_inactive; i_data['active_bp'] = new_active; i_data['trigger_bp'] = new_trigger
                    if visual_changed:
                        self.sync_special_auto_visual("ITEM", i_data, old_cell=old_cell, old_visual=old_visual)
                    self.dirty = True
                lbl_mins.config(text=f"{i_data['countdown']/60000:.1f}m")
                if i_data['inactive_bp'] == 35 and i_data['active_bp'] == 36 and i_data['trigger_bp'] == 37: cb_preset.set("Standard (35/36/37)")
                elif i_data['inactive_bp'] == 68 and i_data['active_bp'] == 69 and i_data['trigger_bp'] == 70: cb_preset.set("Parasite (68/69/70)")
                else: cb_preset.set("")
                self.refresh_special_list("ITEM", redraw=False); self.upd_lbl(); self.draw_grid()
            except: lbl_mins.config(text="ERR")
        upd()
        for e in [e_cnt, e_typ, e_ina, e_act, e_trg]: e.bind("<KeyRelease>", upd)
        e_typ.bind("<<ComboboxSelected>>", upd)

        f_opt = tk.Frame(p, bg="#1a1a1a"); f_opt.pack(anchor=tk.CENTER, pady=(4, 2))
        self.var_item_hid = tk.BooleanVar(value=i_data.get('hidden', False))
        def tog_item_hid():
            val = self.var_item_hid.get()
            if i_data.get('hidden', False) != val:
                self.push_undo_snapshot(); i_data['hidden'] = val; self.dirty = True; self.refresh_special_list("ITEM", redraw=False)
        tk.Checkbutton(f_opt, text="Hide in Briefing", variable=self.var_item_hid, command=tog_item_hid, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.CENTER, padx=5)

        f_place = tk.Frame(p, bg="#1a1a1a"); f_place.pack(anchor=tk.CENTER, pady=(8, 0))
        tk.Button(f_place, text="[ PLACE BOMB ]", command=lambda: self.set_special_tool("ITEM", "ITEM"), bg="#FF00FF", fg="black", width=24).pack(anchor=tk.CENTER, pady=(0, 3))
        tk.Button(f_place, text="[ PLACE KEYSECT ]", command=lambda: self.set_special_tool("ITEM", "KEY"), bg="#FF9900", fg="black", width=24).pack(anchor=tk.CENTER)
        self.build_special_actions_and_list(p, "ITEM")
        self.upd_lbl(); self.draw_grid()

    def select_item_slot(self, slot):
        self.select_special_slot("ITEM", slot)

    def build_gem_ui(self):
        panel = self.panels['GEM']
        for w in panel.winfo_children(): w.destroy()
        tk.Label(panel, text="GEM PLACER", bg="#00FF00", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))
        p = tk.Frame(panel, bg="#1a1a1a")
        p.pack(fill=tk.BOTH, expand=True, anchor=tk.NW, padx=12, pady=6)

        data = self.selected_special_data_or_none("GEM")
        if not data:
            self.build_no_special_selected_message(p, "GEM")
            self.build_special_actions_and_list(p, "GEM")
            self.upd_lbl(); self.draw_grid()
            return

        f_prop = tk.Frame(p, bg="#1a1a1a"); f_prop.pack(anchor=tk.CENTER, pady=6)
        tk.Label(f_prop, text="Building ID:", fg="white", bg="#1a1a1a").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        cb_gem_model = ttk.Combobox(f_prop, values=self.GEM_MODEL_VALUES, width=26)
        cb_gem_model.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        cb_gem_model.set(self.format_gem_model_value(data['blg']))

        def apply_gem_model_from_combo(show_warning=False):
            new_blg = self.parse_gem_model_value(cb_gem_model.get())
            if new_blg is None:
                if show_warning:
                    messagebox.showwarning("Invalid Building ID", "Enter a numeric Building ID, for example: 65")
                    cb_gem_model.set(self.format_gem_model_value(data['blg']))
                return
            if new_blg != data['blg']:
                self.push_undo_snapshot(); old_cell = self.get_gem_cell(data); old_blg = data['blg']; data['blg'] = new_blg
                self.sync_gem_building_visual(data, old_cell=old_cell, old_building=old_blg)
                if old_cell: self.redraw_cells(old_cell)
                self.dirty = True
            if cb_gem_model.get().strip().isdigit() or new_blg in self.GEM_MODEL_LABELS:
                cb_gem_model.set(self.format_gem_model_value(new_blg))
            self.refresh_special_list("GEM", redraw=False)
        cb_gem_model.bind("<<ComboboxSelected>>", lambda e: apply_gem_model_from_combo(show_warning=True))
        cb_gem_model.bind("<Return>", lambda e: apply_gem_model_from_combo(show_warning=True))
        cb_gem_model.bind("<FocusOut>", lambda e: apply_gem_model_from_combo(show_warning=True))
        cb_gem_model.bind("<KeyRelease>", lambda e: apply_gem_model_from_combo(show_warning=False))
        tk.Label(f_prop, text="(Custom values allowed)", fg="#777", bg="#1a1a1a", font=("Arial", 7)).grid(row=1, column=1, sticky="w", padx=5, pady=(0,5))

        tk.Label(f_prop, text="Type:", fg="white", bg="#1a1a1a").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        type_map = {1: "1 (Weapon/Pwr)", 2: "2 (Shield)", 3: "3 (Tech/Unlock)"}
        rev_map = {v: k for k, v in type_map.items()}
        current_display = type_map.get(data['type'], "3 (Tech/Unlock)")
        type_var = tk.StringVar(value=current_display)
        def upd_t(val):
            new_type = rev_map.get(val, 3)
            if new_type != data['type']:
                self.push_undo_snapshot(); data['type'] = new_type; self.dirty = True; self.refresh_special_list("GEM", redraw=False)
            self.upd_lbl(); self.draw_grid()
        om = ttk.OptionMenu(f_prop, type_var, current_display, *type_map.values(), command=upd_t)
        om.config(width=20); om.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        f_opt = tk.Frame(p, bg="#1a1a1a"); f_opt.pack(anchor=tk.CENTER, pady=(0, 4))
        self.var_gem_hid = tk.BooleanVar(value=data.get('hidden', False))
        def tog_gem_hid():
            val = self.var_gem_hid.get()
            if data.get('hidden', False) != val:
                self.push_undo_snapshot(); data['hidden'] = val; self.dirty = True; self.refresh_special_list("GEM", redraw=False)
        tk.Checkbutton(f_opt, text="Hide in Briefing", variable=self.var_gem_hid, command=tog_gem_hid, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.CENTER, padx=5)

        tk.Label(p, text="--- ACTIONS ---", fg="#00FF00", bg="#1a1a1a", font=("Arial", 9, "bold")).pack(anchor=tk.CENTER, pady=(8,4))
        f_act = tk.Frame(p, bg="#1a1a1a"); f_act.pack(anchor=tk.CENTER)
        tgt_type_var = tk.StringVar(value="modify_vehicle")
        tk.Label(f_act, text="Modify:", fg="white", bg="#1a1a1a", width=7, anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 5), pady=2)
        om_mod = ttk.OptionMenu(f_act, tgt_type_var, "modify_vehicle", "modify_vehicle", "modify_building", "modify_weapon")
        om_mod.config(width=22); om_mod.grid(row=0, column=1, sticky="w", pady=2)
        tk.Label(f_act, text="ID:", fg="white", bg="#1a1a1a", width=7, anchor="e").grid(row=1, column=0, sticky="e", padx=(0, 5), pady=2)
        cb_target_id = ttk.Combobox(f_act, width=34); cb_target_id.grid(row=1, column=1, sticky="w", pady=2)
        tk.Label(f_act, text="Do:", fg="white", bg="#1a1a1a", width=7, anchor="e").grid(row=2, column=0, sticky="e", padx=(0, 5), pady=2)
        param_combo = ttk.Combobox(f_act, values=GEM_ACTION_PARAMS_BY_TARGET["modify_vehicle"], width=22)
        param_combo.set("enable"); param_combo.grid(row=2, column=1, sticky="w", pady=2)
        tk.Label(f_act, text="Val:", fg="white", bg="#1a1a1a", width=7, anchor="e").grid(row=3, column=0, sticky="e", padx=(0, 5), pady=2)
        e_val = tk.Entry(f_act, width=8); e_val.grid(row=3, column=1, sticky="w", pady=2); e_val.insert(0,"0")
        tk.Label(f_act, text="Custom definition allowed", fg="#777", bg="#1a1a1a", font=("Arial", 7)).grid(row=4, column=1, sticky="w", pady=(0,5))

        action_ui_state = {"selected_index": None, "loading": False, "val_edit_snapshot_taken": False}

        def action_target_defs_key(target_type):
            return {
                "modify_vehicle": "veh",
                "modify_building": "blg",
                "modify_weapon": "weapon",
            }.get(target_type)

        def action_param_values(target_type):
            return GEM_ACTION_PARAMS_BY_TARGET.get(target_type, ())

        def refresh_action_target_controls(reset_values=True):
            target_type = tgt_type_var.get()
            params = action_param_values(target_type)
            defs_key = action_target_defs_key(target_type)
            values = self.definition_combo_values(defs_key) if defs_key else []
            param_combo['values'] = params
            cb_target_id['values'] = values
            if reset_values:
                param_combo.set(params[0] if params else "")
                cb_target_id.set(values[0] if values else "0")

        def current_selected_action_index():
            idx = action_ui_state.get("selected_index")
            if idx is None or idx < 0 or idx >= len(data['actions']):
                return None
            return idx

        def refresh_action_list(select_index=None):
            lb.delete(0, tk.END)
            for a in data['actions']:
                lb.insert(tk.END, self.format_gem_action_for_list(a))
            if select_index is not None and 0 <= select_index < len(data['actions']):
                lb.selection_clear(0, tk.END)
                lb.selection_set(select_index)
                lb.activate(select_index)
                lb.see(select_index)
                action_ui_state["selected_index"] = select_index
            elif current_selected_action_index() is None:
                action_ui_state["selected_index"] = None

        def load_action_into_controls(action):
            action_ui_state["loading"] = True
            action_ui_state["val_edit_snapshot_taken"] = False
            target_type = action.get('target_type', 'modify_vehicle')
            tgt_type_var.set(target_type)
            refresh_action_target_controls(reset_values=False)
            defs_key = action_target_defs_key(target_type)
            cb_target_id.set(self.format_definition_value(defs_key, action.get('id', 0)) if defs_key else str(action.get('id', 0)))
            params = action_param_values(target_type)
            prm = action.get('param', params[0] if params else '')
            param_combo.set(prm)
            e_val.delete(0, tk.END)
            e_val.insert(0, str(action.get('val', '')))
            action_ui_state["loading"] = False

        def refresh_action_list_row(index):
            if index is None or index < 0 or index >= len(data['actions']):
                return
            lb.delete(index)
            lb.insert(index, self.format_gem_action_for_list(data['actions'][index]))
            lb.selection_clear(0, tk.END)
            lb.selection_set(index)
            lb.activate(index)
            lb.see(index)
            action_ui_state["selected_index"] = index

        def update_selected_action_from_controls(event=None):
            if action_ui_state.get("loading"):
                return
            idx = current_selected_action_index()
            if idx is None:
                return
            try:
                tid = self.parse_leading_int(cb_target_id.get())
                if tid is None:
                    return
                prm = param_combo.get().strip()
                if not prm:
                    return
                new_action = {
                    'target_type': tgt_type_var.get(),
                    'id': tid,
                    'param': prm,
                    'val': e_val.get(),
                }
                old_action = data['actions'][idx]
                if old_action == new_action:
                    return
                self.push_undo_snapshot()
                action_ui_state["val_edit_snapshot_taken"] = True
                old_action.clear()
                old_action.update(new_action)
                self.dirty = True
                refresh_action_list(select_index=idx)
                self.refresh_special_list("GEM", redraw=False)
            except Exception:
                return

        def update_selected_action_value_from_entry(event=None):
            if action_ui_state.get("loading"):
                return
            idx = current_selected_action_index()
            if idx is None:
                return
            try:
                old_action = data['actions'][idx]
                new_val = e_val.get()
                if str(old_action.get('val', '')) == str(new_val):
                    return
                if not action_ui_state.get("val_edit_snapshot_taken", False):
                    self.push_undo_snapshot()
                    action_ui_state["val_edit_snapshot_taken"] = True
                old_action['val'] = new_val
                self.dirty = True
                refresh_action_list_row(idx)
                self.refresh_special_list("GEM", redraw=False)
            except Exception:
                return

        def end_action_value_edit(event=None):
            update_selected_action_value_from_entry(event)
            action_ui_state["val_edit_snapshot_taken"] = False

        def on_modify_change(*args):
            if action_ui_state.get("loading"):
                return
            refresh_action_target_controls(reset_values=True)
            update_selected_action_from_controls()

        tgt_type_var.trace("w", on_modify_change); refresh_action_target_controls(reset_values=True)

        lb_frame = tk.Frame(p); lb_frame.pack(anchor=tk.CENTER, padx=0, pady=4)
        lb = tk.Listbox(lb_frame, height=5, width=56, bg="#222", fg="white", selectbackground=LISTBOX_SELECTION_BG, selectforeground=LISTBOX_SELECTION_FG, font=("Arial", 9))
        lb.pack(side=tk.LEFT)
        sb = tk.Scrollbar(lb_frame, command=lb.yview, bg="#00B8B8", activebackground="#3DF0F0", troughcolor="#0f1f1f", width=16, relief=tk.FLAT, borderwidth=0, highlightthickness=0, elementborderwidth=1, activerelief=tk.FLAT)
        sb.pack(side=tk.RIGHT, fill=tk.Y); lb.config(yscrollcommand=sb.set)

        def on_action_selected(event=None):
            sel = lb.curselection()
            if not sel:
                action_ui_state["selected_index"] = None
                return
            idx = sel[0]
            if 0 <= idx < len(data['actions']):
                action_ui_state["selected_index"] = idx
                load_action_into_controls(data['actions'][idx])

        lb.bind("<<ListboxSelect>>", on_action_selected)
        cb_target_id.bind("<<ComboboxSelected>>", update_selected_action_from_controls)
        cb_target_id.bind("<Return>", update_selected_action_from_controls)
        cb_target_id.bind("<FocusOut>", update_selected_action_from_controls)
        param_combo.bind("<<ComboboxSelected>>", update_selected_action_from_controls)
        param_combo.bind("<Return>", update_selected_action_from_controls)
        param_combo.bind("<FocusOut>", update_selected_action_from_controls)
        e_val.bind("<KeyRelease>", update_selected_action_value_from_entry)
        e_val.bind("<Return>", end_action_value_edit)
        e_val.bind("<FocusOut>", end_action_value_edit)

        def add_action():
            try:
                tid = self.parse_leading_int(cb_target_id.get())
                if tid is None: raise ValueError
                val = e_val.get(); prm = param_combo.get().strip()
                if not prm: return
                self.push_undo_snapshot(); action = {'target_type': tgt_type_var.get(), 'id': tid, 'param': prm, 'val': val}
                data['actions'].append(action); self.dirty = True
                new_idx = len(data['actions']) - 1
                refresh_action_list(select_index=new_idx)
                load_action_into_controls(action)
                self.refresh_special_list("GEM", redraw=False)
            except: messagebox.showerror("Error", "Invalid ID")

        def del_action():
            sel = lb.curselection()
            if sel:
                idx = sel[0]
                self.push_undo_snapshot(); data['actions'].pop(idx); self.dirty = True
                next_idx = min(idx, len(data['actions']) - 1) if data['actions'] else None
                refresh_action_list(select_index=next_idx)
                if next_idx is not None:
                    load_action_into_controls(data['actions'][next_idx])
                else:
                    action_ui_state["selected_index"] = None
                self.refresh_special_list("GEM", redraw=False)
        refresh_action_list()
        f_btns = tk.Frame(p, bg="#1a1a1a"); f_btns.pack(anchor=tk.CENTER, pady=2)
        tk.Button(f_btns, text="ADD", command=add_action, bg="#005500", fg="white", width=8).pack(side=tk.LEFT, padx=10)
        tk.Button(f_btns, text="DEL", command=del_action, bg="#550000", fg="white", width=8).pack(side=tk.RIGHT, padx=10)

        f_place = tk.Frame(p, bg="#1a1a1a"); f_place.pack(anchor=tk.CENTER, pady=(6, 2))
        tk.Button(f_place, text="[ PLACE GEM ON MAP ]", command=lambda: self.set_gem_tool(), bg="#00FF00", fg="black", width=24).pack(anchor=tk.CENTER)
        self.build_special_actions_and_list(p, "GEM")
        self.upd_lbl(); self.draw_grid()

    def select_gem_slot(self, slot):
        self.select_special_slot("GEM", slot)

    def set_gem_tool(self):
        data = self.selected_special_data_or_none("GEM")
        if not data:
            messagebox.showinfo("Error", "Add or select a Gem first.")
            return
        if not data.get('actions'):
            messagebox.showinfo("Error", "Define actions in the Upgrade panel first.")
            return
        self.gem_tool = "GEM"

    def parse_definition_entry(self, kind, value, default_id, custom_name):
        val = str(value).strip()
        match = re.match(r'^(\d+)\s*-\s*(.*)$', val)
        if match:
            item_id = int(match.group(1))
            name = match.group(2).strip() or None
            return item_id, name
        try:
            item_id = int(val)
            name = custom_name if item_id not in self.defs.get(kind, {}) else None
            return item_id, name
        except:
            return default_id, None

    def map_cell_is_valid(self, obj):
        if not obj:
            return False
        x = obj.get('x', -1)
        y = obj.get('y', -1)
        return 0 <= x < self.mw and 0 <= y < self.mh

    def delete_selected_object_from_key(self, event=None):
        if event is not None:
            widget_class = event.widget.winfo_class()
            if widget_class in ("Entry", "TEntry", "Text", "TCombobox", "Spinbox"):
                return None
        if self.mode == "SQUAD":
            self.delete_selected_squad()
            return "break"
        if self.mode == "HOST":
            self.delete_selected_host()
            return "break"
        if self.mode in ["GATE", "ITEM", "GEM"]:
            self.delete_selected_special(self.mode)
            return "break"
        return None

    def build_squad_ui(self):
        p = self.panels['SQUAD']
        for w in p.winfo_children(): w.destroy()
        source_squad = self.current_squad_data
        if 0 <= self.current_squad_index < len(self.squads):
            source_squad = self.squads[self.current_squad_index]
        self.ensure_squad_defaults(source_squad)

        tk.Label(p, text="SQUAD PLACER", bg="#FF4400", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))

        editor = tk.Frame(p, bg="#1a1a1a")
        editor.pack(fill=tk.X, padx=12, pady=(2, 6))

        f_fac = tk.Frame(editor, bg="#1a1a1a"); f_fac.pack(fill=tk.X, pady=3)
        tk.Label(f_fac, text="Owner:", fg="white", bg="#1a1a1a", width=8, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        self.btn_squad_fac = tk.Menubutton(f_fac, text="1", bg="#333", fg="white", relief="raised", width=18)
        self.btn_squad_fac.pack(side=tk.LEFT)
        self._squad_control_owner = source_squad.get('owner', 1)

        def set_fac(f):
            self._squad_control_owner = f
            if self.current_squad_index != -1 and self.current_squad_index < len(self.squads):
                cell = self.get_squad_cell(self.current_squad_index)
                if self.squads[self.current_squad_index]['owner'] != f:
                    self.push_undo_snapshot()
                    self.squads[self.current_squad_index]['owner'] = f
                    self.refresh_squad_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_squad_data['owner'] = f
            self.btn_squad_fac.config(text=f"{f} ({FACTIONS[f][0]})", fg=FACTIONS[f][1] if FACTIONS[f][1] else "white")

        menu = tk.Menu(self.btn_squad_fac, tearoff=0)
        self.btn_squad_fac.configure(menu=menu)
        for i in range(1, 8):
            menu.add_command(label=f"{i} - {FACTIONS[i][0]}", command=lambda x=i: set_fac(x))
        set_fac(self._squad_control_owner)

        f_veh = tk.Frame(editor, bg="#1a1a1a"); f_veh.pack(fill=tk.X, pady=3)
        tk.Label(f_veh, text="Vehicle:", fg="white", bg="#1a1a1a", width=8, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        veh_list = self.definition_combo_values('veh')
        self.cb_veh = ttk.Combobox(f_veh, values=veh_list, width=32)
        self.cb_veh.pack(side=tk.LEFT)
        curr_v = source_squad['veh']
        curr_name = source_squad.get('custom_name') or self.defs['veh'].get(curr_v, "Unknown")
        self.cb_veh.set(f"{curr_v} - {curr_name}")

        def on_veh_change(ev=None):
            new_veh, new_name = self.parse_definition_entry('veh', self.cb_veh.get(), 1, "Custom_Unit")
            if self.current_squad_index != -1 and self.current_squad_index < len(self.squads):
                cell = self.get_squad_cell(self.current_squad_index)
                curr = self.squads[self.current_squad_index]
                if curr['veh'] != new_veh or curr.get('custom_name') != new_name:
                    self.push_undo_snapshot()
                    curr['veh'] = new_veh
                    curr['custom_name'] = new_name
                    self.refresh_squad_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_squad_data['veh'] = new_veh
                self.current_squad_data['custom_name'] = new_name

        self.cb_veh.bind("<<ComboboxSelected>>", on_veh_change)
        self.cb_veh.bind("<Return>", on_veh_change)
        tk.Label(editor, text="Custom format: ID - Name", fg="#aaa", bg="#1a1a1a", font=("Arial", 8, "italic")).pack(anchor=tk.W, padx=76, pady=(0, 5))

        f_cnt = tk.Frame(editor, bg="#1a1a1a"); f_cnt.pack(fill=tk.X, pady=3)
        tk.Label(f_cnt, text="Count:", fg="white", bg="#1a1a1a", width=8, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        self.e_squad_cnt = tk.Entry(f_cnt, width=6)
        self.e_squad_cnt.pack(side=tk.LEFT)
        self.e_squad_cnt.insert(0, str(source_squad['num']))

        def upd_cnt(ev=None):
            try:
                val = max(1, int(self.e_squad_cnt.get()))
            except:
                return
            if self.current_squad_index != -1 and self.current_squad_index < len(self.squads):
                cell = self.get_squad_cell(self.current_squad_index)
                if self.squads[self.current_squad_index]['num'] != val:
                    self.push_undo_snapshot()
                    self.squads[self.current_squad_index]['num'] = val
                    self.refresh_squad_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_squad_data['num'] = val
        self.e_squad_cnt.bind("<KeyRelease>", upd_cnt)

        f_opt = tk.Frame(editor, bg="#1a1a1a"); f_opt.pack(fill=tk.X, pady=(4, 2))
        self.var_squad_hid = tk.BooleanVar(value=source_squad.get('hidden', False))
        self.var_squad_useable = tk.BooleanVar(value=source_squad.get('useable', False))
        def tog_hid():
            val = self.var_squad_hid.get()
            if self.current_squad_index != -1 and self.current_squad_index < len(self.squads):
                cell = self.get_squad_cell(self.current_squad_index)
                if self.squads[self.current_squad_index]['hidden'] != val:
                    self.push_undo_snapshot()
                    self.squads[self.current_squad_index]['hidden'] = val
                    self.refresh_squad_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_squad_data['hidden'] = val
        tk.Checkbutton(f_opt, text="Hide in Briefing", variable=self.var_squad_hid, command=tog_hid, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.W, padx=76)

        def tog_useable():
            val = self.var_squad_useable.get()
            if self.current_squad_index != -1 and self.current_squad_index < len(self.squads):
                cell = self.get_squad_cell(self.current_squad_index)
                if self.squads[self.current_squad_index].get('useable', False) != val:
                    self.push_undo_snapshot()
                    self.squads[self.current_squad_index]['useable'] = val
                    self.refresh_squad_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_squad_data['useable'] = val
        tk.Checkbutton(f_opt, text="Movable by AI", variable=self.var_squad_useable, command=tog_useable, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.W, padx=76)

        def add_squad_to_list():
            veh, custom_name = self.parse_definition_entry('veh', self.cb_veh.get(), 1, "Custom_Unit")
            try:
                num = max(1, int(self.e_squad_cnt.get()))
            except:
                messagebox.showwarning("Invalid Squad", "Count must be a number.")
                return
            previous_cell = self.get_squad_cell(self.current_squad_index)
            self._drag_squad_idx = -1
            self.push_undo_snapshot()
            self.squads.append({
                'owner': self._squad_control_owner,
                'veh': veh,
                'num': num,
                'hidden': self.var_squad_hid.get(),
                'useable': self.var_squad_useable.get(),
                'custom_name': custom_name,
                'x': -1,
                'y': -1
            })
            new_index = len(self.squads) - 1
            self.current_squad_index = -1
            self.invalidate_render_indexes()
            self.refresh_squad_list(redraw=False)
            self.select_squad_index(new_index, redraw=False)
            self.redraw_cells(previous_cell)
            self.dirty = True

        f_actions = tk.Frame(editor, bg="#1a1a1a")
        f_actions.pack(anchor=tk.CENTER, pady=(6, 4))
        tk.Button(f_actions, text="ADD TO LIST", command=add_squad_to_list, bg="#FF4400", fg="black", font=("Arial", 9, "bold"), width=14).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DESELECT", command=self.deselect_squad, bg="#333", fg="white", font=("Arial", 9, "bold"), width=11).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DELETE", command=self.delete_selected_squad, bg="#880000", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(f_actions, text="DELETE ALL", command=self.delete_all_squads, bg="#AA0000", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT)

        f_list = tk.Frame(p, bg="#1a1a1a")
        f_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 8))

        self.lb_squads = tk.Listbox(f_list, bg="#222", fg="white", height=10, selectbackground=LISTBOX_SELECTION_BG, selectforeground=LISTBOX_SELECTION_FG, exportselection=False)
        self.lb_squads.pack(fill=tk.BOTH, expand=True)
        self.lb_squads.bind('<<ListboxSelect>>', self.on_squad_select)

        self.refresh_squad_list()

    def capture_squad_controls_as_new_config(self):
        if hasattr(self, 'cb_veh') and self.cb_veh.winfo_exists():
            veh, custom_name = self.parse_definition_entry('veh', self.cb_veh.get(), 1, "Custom_Unit")
            self.current_squad_data['veh'] = veh
            self.current_squad_data['custom_name'] = custom_name
        if hasattr(self, 'e_squad_cnt') and self.e_squad_cnt.winfo_exists():
            try:
                self.current_squad_data['num'] = max(1, int(self.e_squad_cnt.get()))
            except:
                pass
        self.current_squad_data['owner'] = getattr(self, '_squad_control_owner', self.current_squad_data.get('owner', 1))
        if hasattr(self, 'var_squad_hid'):
            self.current_squad_data['hidden'] = self.var_squad_hid.get()
        if hasattr(self, 'var_squad_useable'):
            self.current_squad_data['useable'] = self.var_squad_useable.get()

    def deselect_squad(self):
        previous_cell = self.get_squad_cell(self.current_squad_index)
        self.capture_squad_controls_as_new_config()
        self.current_squad_index = -1
        self._drag_squad_idx = -1
        if hasattr(self, 'lb_squads') and self.lb_squads.winfo_exists():
            self._syncing_squad_selection = True
            try:
                self.lb_squads.selection_clear(0, tk.END)
            finally:
                self._syncing_squad_selection = False
        self.redraw_cells(previous_cell)

    def select_squad_index(self, idx, redraw=True):
        if idx < 0 or idx >= len(self.squads):
            return
        previous_cell = self.get_squad_cell(self.current_squad_index)
        self.current_squad_index = idx
        s = self.squads[idx]
        selected_cell = self.get_squad_cell(idx)
        self.current_squad_data = {
            'owner': s['owner'],
            'veh': s['veh'],
            'num': s['num'],
            'hidden': s.get('hidden', False),
            'useable': s.get('useable', False),
            'custom_name': s.get('custom_name')
        }
        if hasattr(self, 'lb_squads') and self.lb_squads.winfo_exists():
            self._syncing_squad_selection = True
            try:
                self.lb_squads.selection_clear(0, tk.END)
                self.lb_squads.selection_set(idx)
                self.lb_squads.activate(idx)
                self.lb_squads.see(idx)
            finally:
                self._syncing_squad_selection = False
        if hasattr(self, 'var_squad_hid'):
            self.var_squad_hid.set(s.get('hidden', False))
        if hasattr(self, 'var_squad_useable'):
            self.var_squad_useable.set(s.get('useable', False))
        if hasattr(self, 'e_squad_cnt') and self.e_squad_cnt.winfo_exists():
            self.e_squad_cnt.delete(0, tk.END)
            self.e_squad_cnt.insert(0, str(s['num']))
        if hasattr(self, 'cb_veh') and self.cb_veh.winfo_exists():
            vname = s.get('custom_name') if s.get('custom_name') else self.defs['veh'].get(s['veh'], "Unknown")
            self.cb_veh.set(f"{s['veh']} - {vname}")
        self._squad_control_owner = s['owner']
        if hasattr(self, 'btn_squad_fac') and self.btn_squad_fac.winfo_exists():
            f = s['owner']
            self.btn_squad_fac.config(text=f"{f} ({FACTIONS[f][0]})", fg=FACTIONS[f][1] if FACTIONS[f][1] else "white")
        if redraw:
            self.redraw_cells(previous_cell, selected_cell)

    def on_squad_select(self, evt):
        if getattr(self, '_syncing_squad_selection', False):
            return
        sel = self.lb_squads.curselection()
        if sel:
            self.select_squad_index(sel[0])
        else:
            self.deselect_squad()

    def delete_squad_index(self, idx):
        if idx < 0 or idx >= len(self.squads):
            return False
        cell = self.get_squad_cell(idx)
        self.push_undo_snapshot()
        self.squads.pop(idx)
        self.current_squad_index = -1
        self._drag_squad_idx = -1
        self.invalidate_render_indexes()
        self.refresh_squad_list(redraw=False)
        self.redraw_cells(cell)
        self.dirty = True
        return True

    def delete_selected_squad(self):
        idx = self.current_squad_index
        if idx == -1 and hasattr(self, 'lb_squads') and self.lb_squads.winfo_exists():
            sel = self.lb_squads.curselection()
            if sel:
                idx = sel[0]
        self.delete_squad_index(idx)

    def delete_all_squads(self):
        if not self.squads:
            return
        if not messagebox.askyesno("Delete All Squads", "Delete all squads?"):
            return
        self.push_undo_snapshot()
        self.squads.clear()
        self.current_squad_index = -1
        self._drag_squad_idx = -1
        self.invalidate_render_indexes()
        self.refresh_squad_list(redraw=False)
        self.draw_grid()
        self.dirty = True

    def refresh_squad_list(self, redraw=True):
        if not hasattr(self, 'lb_squads') or not self.lb_squads.winfo_exists(): return
        self._syncing_squad_selection = True
        try:
            self.lb_squads.delete(0, tk.END)
            for i, s in enumerate(self.squads):
                vname = s.get('custom_name') if s.get('custom_name') else self.defs['veh'].get(s['veh'], "Unknown")
                faction_name = self.get_faction_display_name(s['owner'])
                tags = []
                if s.get('hidden', False):
                    tags.append("Hidden")
                if s.get('useable', False):
                    tags.append("Movable by AI")
                prefix = "[UNPLACED] " if not self.map_cell_is_valid(s) else ""
                txt_parts = [f"{prefix}{faction_name}", vname, f"x{s['num']}"] + tags
                txt = " | ".join(txt_parts)
                self.lb_squads.insert(tk.END, txt)
                color = FACTION_TEXT_COLORS.get(s['owner'], "#FFFFFF")
                self.lb_squads.itemconfig(i, {'fg': color})
                if i == self.current_squad_index:
                    self.lb_squads.selection_set(i)
                    self.lb_squads.activate(i)
                    self.lb_squads.see(i)
        finally:
            self._syncing_squad_selection = False
        if redraw:
            self.draw_grid()

    def get_active_host_data_for_ui(self):
        if 0 <= self.current_host_index < len(self.host_stations):
            return self.host_stations[self.current_host_index]
        return self.current_host_data

    def update_host_ai_controls_state(self):
        if not hasattr(self, "cb_host_ai_preset") or not self.cb_host_ai_preset.winfo_exists():
            return
        host = self.get_active_host_data_for_ui()
        ai = self.ensure_host_ai(host)
        preset_name = ai.get("preset", DEFAULT_HOST_AI_PRESET)
        self.cb_host_ai_preset.set(preset_name)
        if hasattr(self, "lbl_host_ai_description") and self.lbl_host_ai_description.winfo_exists():
            self.lbl_host_ai_description.config(text=self.get_host_ai_preset_description(preset_name))

        is_existing_host = 0 <= self.current_host_index < len(self.host_stations)
        is_player_host = is_existing_host and self.is_host_player_export_slot(host)
        state = "disabled" if is_player_host else "readonly"
        self.cb_host_ai_preset.config(state=state)
        if hasattr(self, "btn_host_ai_advanced") and self.btn_host_ai_advanced.winfo_exists():
            self.btn_host_ai_advanced.config(state=tk.DISABLED if is_player_host else tk.NORMAL)

    def apply_host_ai_preset(self, preset_name):
        if preset_name == "Custom":
            self.update_host_ai_controls_state()
            return
        host = self.get_active_host_data_for_ui()
        old_ai = self.ensure_host_ai(host)
        new_ai = self.make_host_ai(preset_name)
        if old_ai == new_ai:
            return
        if 0 <= self.current_host_index < len(self.host_stations):
            self.push_undo_snapshot()
            host['ai'] = new_ai
            self.current_host_data['ai'] = copy.deepcopy(new_ai)
            self.dirty = True
            self.refresh_host_list(redraw=False)
        else:
            self.current_host_data['ai'] = new_ai
        self.update_host_ai_controls_state()

    def open_host_ai_settings(self):
        host = self.get_active_host_data_for_ui()
        if 0 <= self.current_host_index < len(self.host_stations) and self.is_host_player_export_slot(host):
            return
        ai = copy.deepcopy(self.ensure_host_ai(host))

        win = tk.Toplevel(self.root)
        win.title("Advanced AI Settings")
        win.configure(bg="#222")
        win.resizable(False, False)
        win.transient(self.root)

        body = tk.Frame(win, bg="#222", padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, text="Delay values are milliseconds", bg="#222", fg="#AAAAAA", font=("Arial", 8)).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        tk.Label(body, text="Task", bg="#222", fg="white", font=("Arial", 9, "bold")).grid(row=1, column=0, sticky="w")
        tk.Label(body, text="Budget", bg="#222", fg="white", font=("Arial", 9, "bold")).grid(row=1, column=1, padx=8)
        tk.Label(body, text="Delay", bg="#222", fg="white", font=("Arial", 9, "bold")).grid(row=1, column=2, padx=8)

        rows = [
            ("con", "Construction"),
            ("def", "Defense"),
            ("rec", "Recon"),
            ("rob", "Robots"),
            ("pow", "Power"),
            ("rad", "Radar"),
            ("saf", "Safety"),
            ("cpl", "Capture"),
        ]
        budget_entries = {}
        delay_entries = {}
        for row_idx, (prefix, label) in enumerate(rows, start=2):
            budget_key = f"{prefix}_budget"
            delay_key = f"{prefix}_delay"
            tk.Label(body, text=label, bg="#222", fg="#DDDDDD").grid(row=row_idx, column=0, sticky="e", padx=(0, 8), pady=2)
            budget_entry = tk.Spinbox(body, from_=0, to=100, width=6)
            budget_entry.delete(0, tk.END)
            budget_entry.insert(0, str(ai[budget_key]))
            budget_entry.grid(row=row_idx, column=1, padx=8, pady=2)
            delay_entry = tk.Entry(body, width=10)
            delay_entry.insert(0, str(ai[delay_key]))
            delay_entry.grid(row=row_idx, column=2, padx=8, pady=2)
            budget_entries[budget_key] = budget_entry
            delay_entries[delay_key] = delay_entry

        buttons = tk.Frame(body, bg="#222")
        buttons.grid(row=11, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def close_dialog():
            try:
                win.grab_release()
            except:
                pass
            win.destroy()

        def confirm():
            new_ai = {}
            try:
                for key, entry in budget_entries.items():
                    value = int(entry.get())
                    if not 0 <= value <= 100:
                        raise ValueError
                    new_ai[key] = value
                for key, entry in delay_entries.items():
                    value = int(entry.get())
                    if value < 0:
                        raise ValueError
                    new_ai[key] = value
            except:
                messagebox.showwarning("Invalid AI Settings", "Budgets must be 0-100. Delays must be integer milliseconds >= 0.", parent=win)
                return

            old_ai = self.ensure_host_ai(host)
            if all(old_ai[field] == new_ai[field] for field in HOST_AI_FIELDS):
                close_dialog()
                return
            new_ai["preset"] = "Custom"
            if old_ai != new_ai:
                if 0 <= self.current_host_index < len(self.host_stations):
                    self.push_undo_snapshot()
                    host['ai'] = new_ai
                    self.current_host_data['ai'] = copy.deepcopy(new_ai)
                    self.dirty = True
                    self.refresh_host_list(redraw=False)
                else:
                    self.current_host_data['ai'] = new_ai
                self.update_host_ai_controls_state()
            close_dialog()

        tk.Button(buttons, text="Cancel", command=close_dialog, bg="#444", fg="white", width=10).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="OK", command=confirm, bg="#008800", fg="white", width=10).pack(side=tk.RIGHT)
        win.protocol("WM_DELETE_WINDOW", close_dialog)
        self.center_modal_dialog(win, 360, 360)
        win.grab_set()
        win.focus_set()

    def build_host_ui(self):
        p = self.panels['HOST']
        for w in p.winfo_children(): w.destroy()
        source_host = self.current_host_data
        if 0 <= self.current_host_index < len(self.host_stations):
            source_host = self.host_stations[self.current_host_index]
        self.ensure_host_defaults(source_host)

        tk.Label(p, text="HOST STATION PLACER", bg="#FFD700", fg="black", font=("Arial", 12, "bold")).pack(fill=tk.X, pady=(10, 6))

        editor = tk.Frame(p, bg="#1a1a1a")
        editor.pack(fill=tk.X, padx=12, pady=(2, 6))

        f_fac = tk.Frame(editor, bg="#1a1a1a"); f_fac.pack(fill=tk.X, pady=3)
        tk.Label(f_fac, text="Owner:", fg="white", bg="#1a1a1a", width=11, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        self.btn_host_fac = tk.Menubutton(f_fac, text="", bg="#333", fg="white", relief="raised", width=18)
        self.btn_host_fac.pack(side=tk.LEFT)
        self._host_control_owner = source_host.get('owner', 1)

        def set_host_fac(f):
            self._host_control_owner = f
            if self.current_host_index != -1 and self.current_host_index < len(self.host_stations):
                cell = self.get_host_cell(self.current_host_index)
                if self.host_stations[self.current_host_index]['owner'] != f:
                    self.push_undo_snapshot()
                    self.host_stations[self.current_host_index]['owner'] = f
                    self.refresh_host_list(redraw=False)
                    self.update_host_ai_controls_state()
                    self.redraw_cells(cell)
                    self.refresh_tech_panel_if_visible()
            else:
                self.current_host_data['owner'] = f
            self.btn_host_fac.config(text=self.format_player_owner_value(f), fg=FACTIONS[f][1] if FACTIONS[f][1] else "white")

        menu = tk.Menu(self.btn_host_fac, tearoff=0)
        self.btn_host_fac.configure(menu=menu)
        for i in range(1, 8):
            menu.add_command(label=self.format_player_owner_value(i), command=lambda x=i: set_host_fac(x))
        set_host_fac(self._host_control_owner)

        def set_player_owner(new_owner):
            old_owner = getattr(self, 'player_owner', 1)
            if new_owner != old_owner:
                self.push_undo_snapshot()
                self.player_owner = new_owner
                self.dirty = True
                self.refresh_host_list(redraw=False)
                self.update_host_ai_controls_state()
            else:
                self.player_owner = new_owner
            if hasattr(self, "btn_player_owner") and self.btn_player_owner.winfo_exists():
                self.btn_player_owner.config(
                    text=self.format_player_owner_value(self.player_owner),
                    fg=FACTIONS[self.player_owner][1] if FACTIONS[self.player_owner][1] else "white"
                )

        f_veh = tk.Frame(editor, bg="#1a1a1a"); f_veh.pack(fill=tk.X, pady=3)
        tk.Label(f_veh, text="Station:", fg="white", bg="#1a1a1a", width=11, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        self.cb_host = ttk.Combobox(f_veh, values=self.definition_combo_values('host'), width=29)
        self.cb_host.pack(side=tk.LEFT)
        curr_v = source_host['veh']
        curr_name = source_host.get('custom_name') or self.defs['host'].get(curr_v, "Unknown")
        self.cb_host.set(f"{curr_v} - {curr_name}")

        def on_host_change(ev=None):
            new_veh, new_name = self.parse_definition_entry('host', self.cb_host.get(), 56, "Custom_Host")
            if self.current_host_index != -1 and self.current_host_index < len(self.host_stations):
                cell = self.get_host_cell(self.current_host_index)
                curr = self.host_stations[self.current_host_index]
                if curr['veh'] != new_veh or curr.get('custom_name') != new_name:
                    self.push_undo_snapshot()
                    curr['veh'] = new_veh
                    curr['custom_name'] = new_name
                    self.refresh_host_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_host_data['veh'] = new_veh
                self.current_host_data['custom_name'] = new_name

        self.cb_host.bind("<<ComboboxSelected>>", on_host_change)
        self.cb_host.bind("<Return>", on_host_change)
        tk.Label(editor, text="Custom format: ID - Name", fg="#aaa", bg="#1a1a1a", font=("Arial", 8, "italic")).pack(anchor=tk.W, padx=93, pady=(0, 5))

        f_stats = tk.Frame(editor, bg="#1a1a1a"); f_stats.pack(fill=tk.X, pady=3)
        tk.Label(f_stats, text="Energy:", fg="white", bg="#1a1a1a", width=11, anchor="e").grid(row=0, column=0, padx=(0, 6), sticky="e")
        self.e_host_en = tk.Entry(f_stats, width=10)
        self.e_host_en.grid(row=0, column=1, sticky="w")
        self.e_host_en.insert(0, str(source_host['energy']))
        self.cb_host_energy_preset = ttk.Combobox(f_stats, values=self.HOST_ENERGY_PRESET_VALUES, state="readonly", width=12)
        self.cb_host_energy_preset.grid(row=0, column=2, padx=(8, 0), sticky="w")

        tk.Label(f_stats, text="Altitude:", fg="white", bg="#1a1a1a", width=11, anchor="e").grid(row=1, column=0, padx=(0, 6), pady=(4, 0), sticky="e")
        self.e_host_y = tk.Entry(f_stats, width=10)
        self.e_host_y.grid(row=1, column=1, pady=(4, 0), sticky="w")
        self.e_host_y.insert(0, self.format_host_altitude_value(source_host.get('pos_y', DEFAULT_HOST_POS_Y)))

        tk.Label(f_stats, text="Energy Reload Rate:", fg="white", bg="#1a1a1a", width=17, anchor="e").grid(row=2, column=0, padx=(0, 6), pady=(4, 0), sticky="e")
        self.e_host_reload = tk.Entry(f_stats, width=10)
        self.e_host_reload.grid(row=2, column=1, pady=(4, 0), sticky="w")
        self.e_host_reload.insert(0, str(source_host.get('reload_const', DEFAULT_HOST_RELOAD_CONST)))
        self.cb_host_reload_preset = ttk.Combobox(f_stats, values=self.HOST_RELOAD_PRESET_VALUES, state="readonly", width=12)
        self.cb_host_reload_preset.grid(row=2, column=2, padx=(8, 0), pady=(4, 0), sticky="w")

        tk.Label(f_stats, text="View Angle:", fg="white", bg="#1a1a1a", width=17, anchor="e").grid(row=3, column=0, padx=(0, 6), pady=(4, 0), sticky="e")
        self.e_host_viewangle = tk.Entry(f_stats, width=10)
        self.e_host_viewangle.grid(row=3, column=1, pady=(4, 0), sticky="w")
        self.e_host_viewangle.insert(0, str(source_host.get('viewangle', DEFAULT_HOST_VIEWANGLE)))
        self.cb_host_viewangle_preset = ttk.Combobox(f_stats, values=self.HOST_VIEWANGLE_PRESET_VALUES, state="readonly", width=12)
        self.cb_host_viewangle_preset.grid(row=3, column=2, padx=(8, 0), pady=(4, 0), sticky="w")

        def upd_host_stats(ev=None):
            try:
                new_energy = int(self.e_host_en.get())
                new_pos_y = int(self.e_host_y.get())
                new_reload_const = int(self.e_host_reload.get())
                new_viewangle = int(float(self.e_host_viewangle.get()))
                if new_reload_const < 0:
                    return
            except:
                return
            if self.current_host_index != -1 and self.current_host_index < len(self.host_stations):
                cell = self.get_host_cell(self.current_host_index)
                curr = self.host_stations[self.current_host_index]
                if (
                    curr['energy'] != new_energy or
                    curr['pos_y'] != new_pos_y or
                    curr.get('reload_const', DEFAULT_HOST_RELOAD_CONST) != new_reload_const or
                    curr.get('viewangle', DEFAULT_HOST_VIEWANGLE) != new_viewangle
                ):
                    self.push_undo_snapshot()
                    curr['energy'] = new_energy
                    curr['pos_y'] = new_pos_y
                    curr['reload_const'] = new_reload_const
                    curr['viewangle'] = new_viewangle
                    self.refresh_host_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_host_data['energy'] = new_energy
                self.current_host_data['pos_y'] = new_pos_y
                self.current_host_data['reload_const'] = new_reload_const
                self.current_host_data['viewangle'] = new_viewangle
            self.update_host_energy_controls(new_energy)
            self.update_host_reload_controls(new_reload_const)
            self.update_host_viewangle_controls(new_viewangle)
        self.e_host_en.bind("<KeyRelease>", upd_host_stats)
        self.e_host_y.bind("<KeyRelease>", upd_host_stats)
        self.e_host_reload.bind("<KeyRelease>", upd_host_stats)
        self.e_host_viewangle.bind("<KeyRelease>", upd_host_stats)

        def on_energy_preset_select(event=None):
            preset_energy = self.parse_host_energy_preset_value(self.cb_host_energy_preset.get())
            if preset_energy is None:
                self.update_host_energy_controls()
                return
            self.e_host_en.delete(0, tk.END)
            self.e_host_en.insert(0, str(preset_energy))
            upd_host_stats()
        self.cb_host_energy_preset.bind("<<ComboboxSelected>>", on_energy_preset_select)

        def on_reload_preset_select(event=None):
            preset_reload = self.parse_host_reload_preset_value(self.cb_host_reload_preset.get())
            if preset_reload is None:
                self.update_host_reload_controls()
                return
            self.e_host_reload.delete(0, tk.END)
            self.e_host_reload.insert(0, str(preset_reload))
            upd_host_stats()
        self.cb_host_reload_preset.bind("<<ComboboxSelected>>", on_reload_preset_select)

        def on_viewangle_preset_select(event=None):
            preset_viewangle = self.parse_host_viewangle_preset_value(self.cb_host_viewangle_preset.get())
            if preset_viewangle is None:
                self.update_host_viewangle_controls()
                return
            self.e_host_viewangle.delete(0, tk.END)
            self.e_host_viewangle.insert(0, str(preset_viewangle))
            upd_host_stats()
        self.cb_host_viewangle_preset.bind("<<ComboboxSelected>>", on_viewangle_preset_select)

        self.update_host_energy_controls(source_host.get('energy'))
        self.update_host_reload_controls(source_host.get('reload_const', DEFAULT_HOST_RELOAD_CONST))
        self.update_host_viewangle_controls(source_host.get('viewangle', DEFAULT_HOST_VIEWANGLE))

        f_ai = tk.Frame(editor, bg="#1a1a1a"); f_ai.pack(fill=tk.X, pady=(3, 0))
        tk.Label(f_ai, text="AI Preset:", fg="white", bg="#1a1a1a", width=11, anchor="e").grid(row=0, column=0, padx=(0, 6), sticky="e")
        ai_values = list(getattr(self, "host_ai_preset_names", [DEFAULT_HOST_AI_PRESET]))
        if "Custom" not in ai_values:
            ai_values.append("Custom")
        self.cb_host_ai_preset = ttk.Combobox(f_ai, values=ai_values, state="readonly", width=18)
        self.cb_host_ai_preset.grid(row=0, column=1, sticky="w")
        self.cb_host_ai_preset.bind("<<ComboboxSelected>>", lambda e: self.apply_host_ai_preset(self.cb_host_ai_preset.get()))
        self.btn_host_ai_advanced = tk.Button(
            f_ai,
            text="Advanced AI Settings",
            command=self.open_host_ai_settings,
            bg="#333",
            fg="white",
            width=20
        )
        self.btn_host_ai_advanced.grid(row=0, column=2, padx=(8, 0), sticky="w")
        self.lbl_host_ai_description = tk.Label(
            f_ai,
            text="",
            fg="#A7DDE8",
            bg="#1a1a1a",
            font=("Arial", 8, "italic"),
            wraplength=390,
            justify=tk.LEFT
        )
        self.lbl_host_ai_description.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))
        self.update_host_ai_controls_state()

        f_opt = tk.Frame(editor, bg="#1a1a1a"); f_opt.pack(fill=tk.X, pady=(0, 1))
        self.var_host_hid = tk.BooleanVar(value=source_host.get('hidden', False))
        def tog_host_hid():
            val = self.var_host_hid.get()
            if self.current_host_index != -1 and self.current_host_index < len(self.host_stations):
                cell = self.get_host_cell(self.current_host_index)
                if self.host_stations[self.current_host_index]['hidden'] != val:
                    self.push_undo_snapshot()
                    self.host_stations[self.current_host_index]['hidden'] = val
                    self.refresh_host_list(redraw=False)
                    self.redraw_cells(cell)
            else:
                self.current_host_data['hidden'] = val
        tk.Checkbutton(f_opt, text="Hide in Briefing", variable=self.var_host_hid, command=tog_host_hid, bg="#1a1a1a", fg="white", selectcolor="#444").pack(anchor=tk.W, padx=93)

        def add_host_to_list():
            veh, custom_name = self.parse_definition_entry('host', self.cb_host.get(), 56, "Custom_Host")
            try:
                energy = int(self.e_host_en.get())
                pos_y = int(self.e_host_y.get())
                reload_const = int(self.e_host_reload.get())
                viewangle = int(float(self.e_host_viewangle.get()))
                if reload_const < 0:
                    raise ValueError
            except:
                messagebox.showwarning("Invalid Host", "Energy, Altitude, Energy Reload Rate and View Angle must be numbers.")
                return
            previous_cell = self.get_host_cell(self.current_host_index)
            source_ai = copy.deepcopy(self.ensure_host_ai(self.get_active_host_data_for_ui()))
            self._drag_host_idx = -1
            self.push_undo_snapshot()
            self.host_stations.append({
                'owner': self._host_control_owner,
                'veh': veh,
                'energy': energy,
                'pos_y': pos_y,
                'reload_const': reload_const,
                'viewangle': viewangle,
                'custom_name': custom_name,
                'hidden': self.var_host_hid.get(),
                'ai': source_ai,
                'x': -1,
                'y': -1
            })
            new_index = len(self.host_stations) - 1
            self.current_host_index = -1
            self.invalidate_render_indexes()
            self.refresh_host_list(redraw=False)
            self.select_host_index(new_index, redraw=False)
            self.redraw_cells(previous_cell)
            self.refresh_tech_panel_if_visible()
            self.dirty = True

        f_actions = tk.Frame(editor, bg="#1a1a1a")
        f_actions.pack(anchor=tk.CENTER, pady=(3, 8))
        tk.Button(f_actions, text="ADD TO LIST", command=add_host_to_list, bg="#FFD700", fg="black", font=("Arial", 9, "bold"), width=13).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(f_actions, text="DESELECT", command=self.deselect_host, bg="#333", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(f_actions, text="DELETE", command=self.delete_selected_host, bg="#880000", fg="white", font=("Arial", 9, "bold"), width=8).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(f_actions, text="DELETE ALL", command=self.delete_all_hosts, bg="#AA0000", fg="white", font=("Arial", 9, "bold"), width=10).pack(side=tk.LEFT)

        f_player = tk.Frame(editor, bg="#1a1a1a"); f_player.pack(fill=tk.X, pady=(2, 5))
        tk.Label(f_player, text="Player faction:", fg="white", bg="#1a1a1a", width=11, anchor="e").pack(side=tk.LEFT, padx=(0, 6))
        self.btn_player_owner = tk.Menubutton(f_player, text="", bg="#333", fg="white", relief="raised", width=18)
        self.btn_player_owner.pack(side=tk.LEFT)
        player_menu = tk.Menu(self.btn_player_owner, tearoff=0)
        self.btn_player_owner.configure(menu=player_menu)
        for i in range(1, 8):
            player_menu.add_command(label=self.format_player_owner_value(i), command=lambda x=i: set_player_owner(x))
        set_player_owner(getattr(self, 'player_owner', 1))

        f_list = tk.Frame(p, bg="#1a1a1a")
        f_list.pack(fill=tk.BOTH, expand=True, padx=12, pady=(6, 8))

        self.lb_hosts = tk.Listbox(f_list, bg="#222", fg="white", height=8, selectbackground=LISTBOX_SELECTION_BG, selectforeground=LISTBOX_SELECTION_FG, exportselection=False)
        self.lb_hosts.pack(fill=tk.BOTH, expand=True)
        self.lb_hosts.bind('<<ListboxSelect>>', self.on_host_select)
        self.refresh_host_list()

    def capture_host_controls_as_new_config(self):
        if hasattr(self, 'cb_host') and self.cb_host.winfo_exists():
            veh, custom_name = self.parse_definition_entry('host', self.cb_host.get(), 56, "Custom_Host")
            self.current_host_data['veh'] = veh
            self.current_host_data['custom_name'] = custom_name
        if hasattr(self, 'e_host_en') and self.e_host_en.winfo_exists():
            try:
                self.current_host_data['energy'] = int(self.e_host_en.get())
            except:
                pass
        if hasattr(self, 'e_host_y') and self.e_host_y.winfo_exists():
            try:
                self.current_host_data['pos_y'] = int(self.e_host_y.get())
            except:
                pass
        if hasattr(self, 'e_host_reload') and self.e_host_reload.winfo_exists():
            try:
                self.current_host_data['reload_const'] = max(0, int(self.e_host_reload.get()))
            except:
                pass
        if hasattr(self, 'e_host_viewangle') and self.e_host_viewangle.winfo_exists():
            try:
                self.current_host_data['viewangle'] = int(float(self.e_host_viewangle.get()))
            except:
                pass
        self.current_host_data['owner'] = getattr(self, '_host_control_owner', self.current_host_data.get('owner', 1))
        if hasattr(self, 'var_host_hid'):
            self.current_host_data['hidden'] = self.var_host_hid.get()
        self.current_host_data['ai'] = copy.deepcopy(self.ensure_host_ai(self.current_host_data))

    def deselect_host(self):
        previous_cell = self.get_host_cell(self.current_host_index)
        self.capture_host_controls_as_new_config()
        self.current_host_index = -1
        self._drag_host_idx = -1
        if hasattr(self, 'lb_hosts') and self.lb_hosts.winfo_exists():
            self._syncing_host_selection = True
            try:
                self.lb_hosts.selection_clear(0, tk.END)
            finally:
                self._syncing_host_selection = False
        self.update_host_ai_controls_state()
        self.redraw_cells(previous_cell)

    def select_host_index(self, idx, redraw=True):
        if idx < 0 or idx >= len(self.host_stations):
            return
        previous_cell = self.get_host_cell(self.current_host_index)
        self.current_host_index = idx
        h = self.host_stations[idx]
        selected_cell = self.get_host_cell(idx)
        self.current_host_data = {
            'owner': h['owner'],
            'veh': h['veh'],
            'energy': h['energy'],
            'pos_y': h.get('pos_y', DEFAULT_HOST_POS_Y),
            'reload_const': h.get('reload_const', DEFAULT_HOST_RELOAD_CONST),
            'viewangle': h.get('viewangle', DEFAULT_HOST_VIEWANGLE),
            'custom_name': h.get('custom_name'),
            'hidden': h.get('hidden', False),
            'ai': copy.deepcopy(self.ensure_host_ai(h))
        }
        if hasattr(self, 'lb_hosts') and self.lb_hosts.winfo_exists():
            self._syncing_host_selection = True
            try:
                self.lb_hosts.selection_clear(0, tk.END)
                self.lb_hosts.selection_set(idx)
                self.lb_hosts.activate(idx)
                self.lb_hosts.see(idx)
            finally:
                self._syncing_host_selection = False
        if hasattr(self, 'var_host_hid'):
            self.var_host_hid.set(h.get('hidden', False))
        if hasattr(self, 'e_host_en') and self.e_host_en.winfo_exists():
            self.e_host_en.delete(0, tk.END); self.e_host_en.insert(0, str(h['energy']))
        if hasattr(self, 'e_host_y') and self.e_host_y.winfo_exists():
            self.e_host_y.delete(0, tk.END); self.e_host_y.insert(0, self.format_host_altitude_value(h.get('pos_y', DEFAULT_HOST_POS_Y)))
        if hasattr(self, 'e_host_reload') and self.e_host_reload.winfo_exists():
            self.e_host_reload.delete(0, tk.END); self.e_host_reload.insert(0, str(h.get('reload_const', DEFAULT_HOST_RELOAD_CONST)))
        if hasattr(self, 'e_host_viewangle') and self.e_host_viewangle.winfo_exists():
            self.e_host_viewangle.delete(0, tk.END); self.e_host_viewangle.insert(0, str(h.get('viewangle', DEFAULT_HOST_VIEWANGLE)))
        if hasattr(self, 'cb_host') and self.cb_host.winfo_exists():
            vname = h.get('custom_name') if h.get('custom_name') else self.defs['host'].get(h['veh'], "Unknown")
            self.cb_host.set(f"{h['veh']} - {vname}")
        self._host_control_owner = h['owner']
        if hasattr(self, 'btn_host_fac') and self.btn_host_fac.winfo_exists():
            f = h['owner']
            self.btn_host_fac.config(text=self.format_player_owner_value(f), fg=FACTIONS[f][1] if FACTIONS[f][1] else "white")
        self.update_host_energy_controls(h['energy'])
        self.update_host_reload_controls(h.get('reload_const', DEFAULT_HOST_RELOAD_CONST))
        self.update_host_viewangle_controls(h.get('viewangle', DEFAULT_HOST_VIEWANGLE))
        self.update_host_ai_controls_state()
        if redraw:
            self.redraw_cells(previous_cell, selected_cell)

    def on_host_select(self, evt):
        if getattr(self, '_syncing_host_selection', False):
            return
        sel = self.lb_hosts.curselection()
        if sel:
            self.select_host_index(sel[0])
        else:
            self.deselect_host()

    def delete_host_index(self, idx):
        if idx < 0 or idx >= len(self.host_stations):
            return False
        cell = self.get_host_cell(idx)
        self.push_undo_snapshot()
        self.host_stations.pop(idx)
        self.current_host_index = -1
        self._drag_host_idx = -1
        self.invalidate_render_indexes()
        self.refresh_host_list(redraw=False)
        self.redraw_cells(cell)
        self.refresh_tech_panel_if_visible()
        self.dirty = True
        return True

    def delete_selected_host(self):
        idx = self.current_host_index
        if idx == -1 and hasattr(self, 'lb_hosts') and self.lb_hosts.winfo_exists():
            sel = self.lb_hosts.curselection()
            if sel:
                idx = sel[0]
        self.delete_host_index(idx)

    def delete_all_hosts(self):
        if not self.host_stations:
            return
        if not messagebox.askyesno("Delete All Host Stations", "Delete all host stations?"):
            return
        self.push_undo_snapshot()
        self.host_stations.clear()
        self.current_host_index = -1
        self._drag_host_idx = -1
        self.invalidate_render_indexes()
        self.refresh_host_list(redraw=False)
        self.update_host_ai_controls_state()
        self.draw_grid()
        self.refresh_tech_panel_if_visible()
        self.dirty = True

    def refresh_host_list(self, redraw=True):
        if not hasattr(self, 'lb_hosts') or not self.lb_hosts.winfo_exists(): return
        self._syncing_host_selection = True
        try:
            self.lb_hosts.delete(0, tk.END)
            for i, h in enumerate(self.host_stations):
                self.ensure_host_defaults(h)
                vname = h.get('custom_name') or self.defs['host'].get(h['veh'], "Unknown")
                faction_name = self.get_faction_display_name(h['owner'])
                export_index = self.get_host_export_index(h, placed_only=True)
                tags = []
                if export_index == 0:
                    tags.append("[PLAYER]")
                if not self.map_cell_is_valid(h):
                    tags.append("[UNPLACED]")
                prefix = " ".join(tags)
                if prefix:
                    prefix += " "

                txt_parts = [f"{prefix}{faction_name}", vname, f"{h['energy']} HP"]
                if h.get('hidden', False):
                    txt_parts.append("Hidden")
                if export_index != 0:
                    preset_name = h['ai'].get('preset', DEFAULT_HOST_AI_PRESET)
                    txt_parts.append("Custom AI" if preset_name == "Custom" else preset_name)
                txt = " | ".join(txt_parts)
                self.lb_hosts.insert(tk.END, txt)
                color = FACTION_TEXT_COLORS.get(h['owner'], "#FFFFFF")
                self.lb_hosts.itemconfig(i, {'fg': color})
                if i == self.current_host_index:
                    self.lb_hosts.selection_set(i)
                    self.lb_hosts.activate(i)
                    self.lb_hosts.see(i)
        finally:
            self._syncing_host_selection = False
        if redraw:
            self.draw_grid()
        self.update_host_ai_controls_state()

    def faction_has_placed_host(self, faction_id):
        for host in self.host_stations:
            if host.get('owner') == faction_id and self.map_cell_is_valid(host):
                return True
        return False

    def can_edit_current_tech_faction(self):
        return self.faction_has_placed_host(self.curr_tech_faction)

    def set_tech_edit_controls_enabled(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in getattr(self, "tech_edit_widgets", []):
            try:
                if widget.winfo_exists():
                    widget.config(state=state)
            except:
                pass

    def refresh_tech_panel_if_visible(self):
        if getattr(self, "mode", None) != "TECH":
            return
        if hasattr(self, "cv_tech") and self.cv_tech.winfo_exists():
            self.draw_tech_list()

    def build_tech_ui(self):
        p = self.panels['TECH']
        for w in p.winfo_children(): w.destroy()
        self.tech_edit_widgets = []
        h = tk.Frame(p, bg="#1a1a1a", height=40); h.pack(fill=tk.X)
        h_inner = tk.Frame(h, bg="#1a1a1a")
        h_inner.pack(anchor=tk.W)
        
        # MOD: Nav Logic Range to 7 (Drones)
        def nav_tech(d):
            self.curr_tech_faction = 1 if self.curr_tech_faction+d > 7 else 7 if self.curr_tech_faction+d < 1 else self.curr_tech_faction+d
            self.update_tech_header(lbl_faction, lbl_num)
            self.draw_tech_list()
            self.upd_lbl()
            
        tk.Button(h_inner, text="<", command=lambda: nav_tech(-1), bg="#333", fg="white", font=("bold",12)).pack(side=tk.LEFT, padx=(10, 2))
        tk.Button(h_inner, text=">", command=lambda: nav_tech(1), bg="#333", fg="white", font=("bold",12)).pack(side=tk.LEFT, padx=(2, 8))
        lbl_num = tk.Label(h_inner, text="01", bg="#333", font=("Arial", 14, "bold"), width=3); lbl_num.pack(side=tk.LEFT, padx=5)

        lbl_faction = tk.Label(h_inner, text="", bg="#1a1a1a", font=("Arial", 12, "bold"), anchor=tk.W, width=14)
        lbl_faction.pack(side=tk.LEFT, padx=5)
        self.update_tech_header(lbl_faction, lbl_num)

        f_cust = tk.Frame(p, bg="#1a1a1a"); f_cust.pack(fill=tk.X, pady=2)
        tk.Label(f_cust, text="Custom ID:", bg="#1a1a1a", fg="#aaa").pack(side=tk.LEFT, padx=5)
        e_cust = tk.Entry(f_cust, width=5); e_cust.pack(side=tk.LEFT)

        tk.Label(f_cust, text="Custom Name:", fg="#aaa", bg="#1a1a1a").pack(side=tk.LEFT, padx=5)
        e_name = tk.Entry(f_cust, width=8); e_name.pack(side=tk.LEFT)
        self.tech_edit_widgets.extend([e_cust, e_name])

        def add_cust_tech(kind):
            if not self.can_edit_current_tech_faction():
                self.draw_tech_list()
                return
            try:
                vid = int(e_cust.get())
                cname = e_name.get().strip()
                lst = self.tech[self.curr_tech_faction][kind]
                changed = False
                if cname and self.custom_tech_names.get(vid) != cname:
                    changed = True
                if vid not in lst:
                    changed = True
                if not changed:
                    return
                self.push_undo_snapshot()
                if cname:
                    self.custom_tech_names[vid] = cname
                if vid not in lst:
                    lst.append(vid)
                self.draw_tech_list()
            except: pass
        f_cust_btns = tk.Frame(p, bg="#1a1a1a"); f_cust_btns.pack(fill=tk.X, pady=(0, 4))
        btn_add_custom_vehicle = tk.Button(f_cust_btns, text="Add Custom Vehicle", command=lambda: add_cust_tech('veh'), bg="#444", fg="white", font=("Arial", 8), width=18)
        btn_add_custom_vehicle.pack(side=tk.LEFT, padx=(5, 4))
        btn_add_custom_building = tk.Button(f_cust_btns, text="Add Custom Building", command=lambda: add_cust_tech('blg'), bg="#444", fg="white", font=("Arial", 8), width=19)
        btn_add_custom_building.pack(side=tk.LEFT, padx=4)
        self.tech_edit_widgets.extend([btn_add_custom_vehicle, btn_add_custom_building])

        self.cv_tech = tk.Canvas(p, bg="#222", highlightthickness=0)
        self.sb_tech = tk.Scrollbar(
            p,
            command=self.cv_tech.yview,
            bg="#00B8B8",
            activebackground="#3DF0F0",
            troughcolor="#0f1f1f",
            width=16,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            elementborderwidth=1,
            activerelief=tk.FLAT
        )
        self.cv_tech.configure(yscrollcommand=self.sb_tech.set)
        self.sb_tech.pack(side=tk.RIGHT, fill=tk.Y); self.cv_tech.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.cv_tech.bind("<Configure>", lambda e: self.draw_tech_list())
        self.bind_scroll(self.cv_tech)
        self.cv_tech.bind("<Button-1>", self.on_tech_click)
        self.cv_tech.bind("<B1-Motion>", self.on_tech_drag)
        self.cv_tech.bind("<ButtonRelease-1>", self.on_tech_release)
        self.cv_tech.bind("<Button-3>", self.on_tech_right_click)
        self.cv_tech.bind("<B3-Motion>", self.on_tech_drag)
        self.cv_tech.bind("<ButtonRelease-3>", self.on_tech_release)

        self.root.after(50, self.draw_tech_list)

    def update_tech_header(self, lbl_name, lbl_n):
        fid = self.curr_tech_faction
        fname, fcolor = FACTIONS[fid]
        if fid == 3:
            fname = "MYKONIAN"
        lbl_name.config(text=fname, fg=fcolor)
        lbl_n.config(text=f"{fid:02d}", fg=fcolor)

    def get_tech_list_items(self):
        items = []
        for uid, name in self.defs['veh'].items():
             if uid in self.custom_tech_names: name = self.custom_tech_names[uid]
             items.append(('veh', uid, name))
        for uid, name in self.defs['blg'].items():
             if uid in self.custom_tech_names: name = self.custom_tech_names[uid]
             items.append(('blg', uid, name))

        fid = self.curr_tech_faction
        active_veh = self.tech[fid]['veh']; active_blg = self.tech[fid]['blg']
        for vid in active_veh:
             if not any(i[0]=='veh' and i[1]==vid for i in items):
                 name = self.custom_tech_names.get(vid, f"Custom_{vid}")
                 items.append(('veh', vid, name))
        for bid in active_blg:
             if not any(i[0]=='blg' and i[1]==bid for i in items):
                 name = self.custom_tech_names.get(bid, f"Custom_{bid}")
                 items.append(('blg', bid, name))

        items.sort(key=lambda x: (0 if x[0]=='veh' else 1, x[1]))
        return items

    def draw_tech_list(self,):
        if not hasattr(self, 'cv_tech') or not self.cv_tech.winfo_exists(): return
        self.cv_tech.delete("all")
        width = self.cv_tech.winfo_width()
        fid = self.curr_tech_faction

        if not self.faction_has_placed_host(fid):
            self.set_tech_edit_controls_enabled(False)
            message = "No Host Station placed for this faction.\nPlace a Host Station first to edit its Tech list."
            text_width = max(180, width - 30)
            self.cv_tech.create_text(
                max(width // 2, text_width // 2),
                52,
                text=message,
                fill="#CCCCCC",
                width=text_width,
                justify=tk.CENTER,
                font=("Arial", 9, "bold")
            )
            self.cv_tech.config(scrollregion=(0, 0, max(width, TECH_ITEM_W + 20), 120))
            return

        self.set_tech_edit_controls_enabled(True)

        faction_color = FACTIONS[fid][1] or "#FFFFFF"
        active_bg = faction_color
        active_outline = SELECTION_COLOR
        active_text = "black" if fid in [2, 3, 4] else "white"

        items = self.get_tech_list_items()
        active_veh = self.tech[fid]['veh']; active_blg = self.tech[fid]['blg']

        for i, (cat, uid, name) in enumerate(items):
            x, y = 10, i * TECH_ITEM_H + 5
            is_active = uid in (active_veh if cat=='veh' else active_blg)

            if is_active:
                bg_color = active_bg
                border_color = active_outline
                text_color = active_text
            else:
                bg_color = "#333333"
                border_color = "#555555"
                text_color = "#BBBBBB"

            line_w = 3 if is_active else 1
            item_w = max(TECH_ITEM_W, width - 20)
            tag = f"item_{cat}_{uid}"
            self.cv_tech.create_rectangle(x, y, x + item_w, y + TECH_ITEM_H - 4, fill=bg_color, outline=border_color, width=line_w, tags=tag)
            prefix = "vehicle" if cat == "veh" else "building"
            self.cv_tech.create_text(x + 10, y + (TECH_ITEM_H/2) - 3, text=f"{prefix} = {uid} ; {name}", fill=text_color, anchor=tk.W, font=("Courier", 9, "bold" if is_active else "normal"), tags=tag)
        self.cv_tech.config(scrollregion=(0, 0, max(width, TECH_ITEM_W + 20), len(items) * TECH_ITEM_H + 20))

    def on_tech_click(self, e):
        self.start_tech_drag(e, "enable")

    def on_tech_right_click(self, e):
        self.start_tech_drag(e, "disable")
        return "break"

    def start_tech_drag(self, e, action):
        if not self.can_edit_current_tech_faction():
            self.draw_tech_list()
            self._tech_drag_active = False
            return
        self._tech_drag_active = True
        self._tech_dragging = False
        self._tech_drag_snapshot_taken = False
        self._tech_drag_start = (e.x, e.y)
        self._tech_drag_action = action
        self._last_dragged_id = None

    def on_tech_drag(self, e):
        if not getattr(self, "_tech_drag_active", False):
            return
        start_x, start_y = getattr(self, "_tech_drag_start", (e.x, e.y))
        if abs(e.x - start_x) + abs(e.y - start_y) >= 4:
            self._tech_dragging = True
        if self._tech_dragging:
            self.apply_tech_drag_hit(e.x, e.y)

    def on_tech_release(self, e):
        if getattr(self, "_tech_dragging", False):
            self.apply_tech_drag_hit(e.x, e.y)
        elif getattr(self, "_tech_drag_action", "enable") == "disable":
            self.deactivate_tech_hit(e.x, e.y)
        else:
            self.process_tech_hit(e.x, e.y)
        self._tech_drag_active = False
        self._tech_dragging = False
        self._tech_drag_snapshot_taken = False
        self._tech_drag_action = "enable"
        self._last_dragged_id = None
        if getattr(e, "num", None) == 3:
            return "break"

    def get_tech_hit_item(self, x, y):
        if not self.can_edit_current_tech_faction():
            return None
        cy = int(self.cv_tech.canvasy(y)); idx = cy // TECH_ITEM_H
        items = self.get_tech_list_items()
        if 0 <= idx < len(items):
            cat, uid, name = items[idx]
            return cat, uid
        return None

    def push_tech_drag_undo_once(self):
        if not getattr(self, "_tech_drag_snapshot_taken", False):
            self.push_undo_snapshot()
            self._tech_drag_snapshot_taken = True

    def apply_tech_drag_hit(self, x, y):
        action = getattr(self, "_tech_drag_action", "enable")
        if action == "disable":
            self.deactivate_tech_hit(x, y)
        else:
            self.activate_tech_hit(x, y)

    def activate_tech_hit(self, x, y):
        hit = self.get_tech_hit_item(x, y)
        if not hit:
            return
        cat, uid = hit
        unique_id = f"{cat}_{uid}"
        if unique_id == self._last_dragged_id:
            return
        self._last_dragged_id = unique_id

        lst = self.tech[self.curr_tech_faction][cat]
        if uid not in lst:
            self.push_tech_drag_undo_once()
            lst.append(uid)
            self.draw_tech_list()

    def deactivate_tech_hit(self, x, y):
        hit = self.get_tech_hit_item(x, y)
        if not hit:
            return
        cat, uid = hit
        unique_id = f"{cat}_{uid}"
        if unique_id == self._last_dragged_id:
            return
        self._last_dragged_id = unique_id

        lst = self.tech[self.curr_tech_faction][cat]
        if uid in lst:
            self.push_tech_drag_undo_once()
            lst.remove(uid)
            self.draw_tech_list()

    def process_tech_hit(self, x, y):
        hit = self.get_tech_hit_item(x, y)
        if hit:
            cat, uid = hit
            lst = self.tech[self.curr_tech_faction][cat]
            self.push_undo_snapshot()
            if uid in lst: lst.remove(uid)
            else: lst.append(uid)
            self.draw_tech_list()

    def build_script_ui(self):
        p = self.panels['SCRIPT']
        for w in p.winfo_children(): w.destroy()

        tk.Label(p, text="LEVEL SCRIPT (LDF)", fg="#008080", bg="#1a1a1a", font=("Arial", 12, "bold")).pack(pady=5)
        tk.Label(p, text="Write custom LDF commands here", fg="#aaa", bg="#1a1a1a", font=("Arial", 9)).pack(pady=2)

        txt = tk.Text(p, bg="#111", fg="#00FF00", font=("Courier", 10), insertbackground="white", height=20)
        txt.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        txt.insert("1.0", self.script_content)
        self.script_text_widget = txt

        def save_script():
            self.sync_script_widget_to_data(push_undo=True)
            messagebox.showinfo("Script", "Script saved successfully!")

        tk.Button(p, text="SAVE SCRIPT", command=save_script, bg="#005555", fg="white", font=("bold")).pack(fill=tk.X, padx=5, pady=5)
