import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk
import os

from .constants import *
from .paths import resource_path

class DialogMixin:

    def center_modal_dialog(self, win, width, height):
        self.root.update_idletasks()
        win.update_idletasks()

        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(1, self.root.winfo_width())
        root_h = max(1, self.root.winfo_height())

        x = root_x + max(0, (root_w - width) // 2)
        y = root_y + max(0, (root_h - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")

    def ask_new_map_settings(self, title="New Map", default_set=None, default_w=DEFAULT_W, default_h=DEFAULT_H):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg="#222")
        win.resizable(False, False)
        win.transient(self.root)

        result = {"value": None}
        default_set_num = 1
        if default_set:
            try:
                default_set_num = max(1, min(6, int(str(default_set).replace("set", ""))))
            except:
                default_set_num = 1

        body = tk.Frame(win, bg="#222", padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)
        content = tk.Frame(body, bg="#222")
        content.pack(expand=True)

        tk.Label(content, text="Create Map", bg="#222", fg="white", font=("Arial", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 14))

        tk.Label(content, text="Env / Set:", bg="#222", fg="#ddd").grid(row=1, column=0, sticky="e", padx=(0, 10), pady=4)
        set_var = tk.StringVar(value=str(default_set_num))
        cb_set = ttk.Combobox(content, values=[str(i) for i in range(1, 7)], textvariable=set_var, state="readonly", width=8)
        cb_set.grid(row=1, column=1, sticky="w", pady=4)

        tk.Label(content, text="W:", bg="#222", fg="#ddd").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=4)
        e_w = tk.Entry(content, width=10, bg="#111", fg="white", insertbackground="white")
        e_w.grid(row=2, column=1, sticky="w", pady=4)
        e_w.insert(0, str(default_w))

        tk.Label(content, text="H:", bg="#222", fg="#ddd").grid(row=3, column=0, sticky="e", padx=(0, 10), pady=4)
        e_h = tk.Entry(content, width=10, bg="#111", fg="white", insertbackground="white")
        e_h.grid(row=3, column=1, sticky="w", pady=4)
        e_h.insert(0, str(default_h))

        buttons = tk.Frame(content, bg="#222")
        buttons.grid(row=4, column=0, columnspan=2, pady=(18, 0))

        def close_dialog():
            try:
                win.grab_release()
            except:
                pass
            win.destroy()

        def close_cancel(event=None):
            result["value"] = None
            close_dialog()

        def confirm(event=None):
            try:
                set_num = int(set_var.get())
                width = int(e_w.get())
                height = int(e_h.get())
            except:
                messagebox.showerror("Invalid Map", "Set, W and H must be valid numbers.", parent=win)
                return

            if not (1 <= set_num <= 6):
                messagebox.showerror("Invalid Set", "Set must be between 1 and 6.", parent=win)
                return
            if width < 3 or height < 3:
                messagebox.showerror("Invalid Size", "W and H must be at least 3.", parent=win)
                return

            set_folder = f"set{set_num}"
            if not os.path.exists(resource_path(set_folder)):
                messagebox.showerror("Missing Set", f"Folder not found: {set_folder}", parent=win)
                return

            result["value"] = {"set_folder": set_folder, "width": width, "height": height}
            close_dialog()

        tk.Button(buttons, text="Cancel", command=close_cancel, bg="#444", fg="white", width=10).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="OK", command=confirm, bg="#006600", fg="white", width=10).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", close_cancel)
        win.bind("<Escape>", close_cancel)
        win.bind("<Return>", confirm)

        self.center_modal_dialog(win, 320, 230)
        win.grab_set()
        e_w.select_range(0, tk.END)
        e_w.focus_set()
        self.root.wait_window(win)
        return result["value"]

    def ask_resize_dimensions(self, default_w, default_h):
        win = tk.Toplevel(self.root)
        win.title("Resize Map")
        win.configure(bg="#222")
        win.resizable(False, False)
        win.transient(self.root)

        result = {"value": None}
        body = tk.Frame(win, bg="#222", padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)
        content = tk.Frame(body, bg="#222")
        content.pack(expand=True)

        tk.Label(content, text="Resize Map", bg="#222", fg="white", font=("Arial", 12, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 14))

        tk.Label(content, text="W:", bg="#222", fg="#ddd").grid(row=1, column=0, sticky="e", padx=(0, 10), pady=4)
        e_w = tk.Entry(content, width=10, bg="#111", fg="white", insertbackground="white")
        e_w.grid(row=1, column=1, sticky="w", pady=4)
        e_w.insert(0, str(default_w))

        tk.Label(content, text="H:", bg="#222", fg="#ddd").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=4)
        e_h = tk.Entry(content, width=10, bg="#111", fg="white", insertbackground="white")
        e_h.grid(row=2, column=1, sticky="w", pady=4)
        e_h.insert(0, str(default_h))

        buttons = tk.Frame(content, bg="#222")
        buttons.grid(row=3, column=0, columnspan=2, pady=(18, 0))

        def close_dialog():
            try:
                win.grab_release()
            except:
                pass
            win.destroy()

        def close_cancel(event=None):
            result["value"] = None
            close_dialog()

        def confirm(event=None):
            try:
                width = int(e_w.get())
                height = int(e_h.get())
            except:
                messagebox.showerror("Invalid Size", "W and H must be valid numbers.", parent=win)
                return

            if width < 3 or height < 3:
                messagebox.showerror("Invalid Size", "W and H must be at least 3.", parent=win)
                return

            result["value"] = (width, height)
            close_dialog()

        tk.Button(buttons, text="Cancel", command=close_cancel, bg="#444", fg="white", width=10).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="OK", command=confirm, bg="#006600", fg="white", width=10).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", close_cancel)
        win.bind("<Escape>", close_cancel)
        win.bind("<Return>", confirm)

        self.center_modal_dialog(win, 320, 200)
        win.grab_set()
        e_w.select_range(0, tk.END)
        e_w.focus_set()
        self.root.wait_window(win)
        return result["value"]

    def count_resize_out_of_bounds(self, width, height):
        counts = {
            'gate_objects': 0,
            'gate_keys': 0,
            'item_objects': 0,
            'item_keys': 0,
            'gems': 0,
            'squads': 0,
            'hosts': 0,
        }

        def in_bounds(x, y):
            return 0 <= x < width and 0 <= y < height

        for gate in self.gates.values():
            if gate['x'] != -1 and not in_bounds(gate['x'], gate['y']):
                counts['gate_objects'] += 1
                counts['gate_keys'] += len(gate['keys'])
            else:
                counts['gate_keys'] += sum(1 for kx, ky in gate['keys'] if not in_bounds(kx, ky))

        for item in self.items.values():
            if item['x'] != -1 and not in_bounds(item['x'], item['y']):
                counts['item_objects'] += 1
                counts['item_keys'] += len(item['keys'])
            else:
                counts['item_keys'] += sum(1 for kx, ky in item['keys'] if not in_bounds(kx, ky))

        for gem in self.gems.values():
            if gem['x'] != -1 and not in_bounds(gem['x'], gem['y']):
                counts['gems'] += 1

        counts['squads'] = sum(1 for squad in self.squads if not in_bounds(squad['x'], squad['y']))
        counts['hosts'] = sum(1 for host in self.host_stations if not in_bounds(host['x'], host['y']))
        return counts

    def has_resize_out_of_bounds(self, counts):
        return any(counts.values())

    def confirm_resize_remove_out_of_bounds(self, counts):
        win = tk.Toplevel(self.root)
        win.title("Resize Map")
        win.configure(bg="#222")
        win.resizable(False, False)
        win.transient(self.root)

        result = {"value": False}
        body = tk.Frame(win, bg="#222", padx=18, pady=16)
        body.pack(fill=tk.BOTH, expand=True)

        tk.Label(
            body,
            text="Some objects will be outside the resized map:",
            bg="#222",
            fg="white",
            font=("Arial", 10, "bold")
        ).pack(anchor=tk.W, pady=(0, 10))

        text = (
            f"Gate objects: {counts['gate_objects']}\n"
            f"Gate keys: {counts['gate_keys']}\n"
            f"Bomb objects: {counts['item_objects']}\n"
            f"Bomb keys: {counts['item_keys']}\n"
            f"Gems: {counts['gems']}\n"
            f"Squads: {counts['squads']}\n"
            f"Hosts: {counts['hosts']}\n\n"
            "Continue and remove out-of-bounds objects?"
        )
        tk.Label(body, text=text, bg="#222", fg="#ddd", justify=tk.LEFT).pack(anchor=tk.W)

        buttons = tk.Frame(body, bg="#222")
        buttons.pack(fill=tk.X, pady=(16, 0))

        def close(value=False):
            result["value"] = value
            try:
                win.grab_release()
            except:
                pass
            win.destroy()

        tk.Button(buttons, text="Cancel", command=lambda: close(False), bg="#444", fg="white", width=12).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="Resize and remove", command=lambda: close(True), bg="#884400", fg="white", width=18).pack(side=tk.RIGHT)

        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.bind("<Escape>", lambda e: close(False))
        self.center_modal_dialog(win, 360, 280)
        win.grab_set()
        self.root.wait_window(win)
        return result["value"]

    def remove_resize_out_of_bounds(self, width, height):
        def in_bounds(x, y):
            return 0 <= x < width and 0 <= y < height

        for gate in self.gates.values():
            if gate['x'] != -1 and not in_bounds(gate['x'], gate['y']):
                gate['x'] = -1
                gate['y'] = -1
                gate['keys'] = []
            else:
                gate['keys'] = [(kx, ky) for kx, ky in gate['keys'] if in_bounds(kx, ky)]

        for item in self.items.values():
            if item['x'] != -1 and not in_bounds(item['x'], item['y']):
                item['x'] = -1
                item['y'] = -1
                item['keys'] = []
            else:
                item['keys'] = [(kx, ky) for kx, ky in item['keys'] if in_bounds(kx, ky)]

        for gem in self.gems.values():
            if gem['x'] != -1 and not in_bounds(gem['x'], gem['y']):
                gem['x'] = -1
                gem['y'] = -1

        self.squads = [squad for squad in self.squads if in_bounds(squad['x'], squad['y'])]
        self.host_stations = [host for host in self.host_stations if in_bounds(host['x'], host['y'])]
        self.current_squad_index = -1
        self.current_host_index = -1

    def ask_dims(self):
        settings = self.ask_new_map_settings(title=APP_NAME, default_set=self.set_folder, default_w=DEFAULT_W, default_h=DEFAULT_H)
        if settings:
            self.create_new_map(settings['set_folder'], settings['width'], settings['height'])
        else:
            self.root.destroy()

    def create_new_map(self, set_folder, width, height):
        if not os.path.exists(resource_path(set_folder)):
            messagebox.showerror("Missing Set", f"Folder not found: {set_folder}")
            return

        set_changed = (set_folder != self.set_folder)
        self.set_folder = set_folder
        if set_changed:
            self.reload_assets()
        else:
            self.update_window_title()

        self.current_filename = None
        self.current_filepath = None
        self.update_window_title()

        self.mw, self.mh = width, height
        self.zoom_m = 64
        self.clear_view = False

        self.lvl_info = {
            'title': "Untitled Map",
            'sky': "objects/x7.bas",
            'mbmap': "",
            'dbmap': "",
            'music': "None",
            'movie': "None"
        }
        self.script_content = DEFAULT_SCRIPT_CONTENT
        self.script_text_widget = None
        self.tech = {i: {'veh': [], 'blg': []} for i in range(1, 8)}
        self.custom_tech_names = {}
        self.curr_tech_faction = 1

        self.current_gate_slot = 0
        self.current_item_slot = 0
        self.current_gem_slot = 0
        self.current_squad_index = -1
        self.current_host_index = -1
        self._drag_squad_idx = -1
        self._drag_host_idx = -1

        self.current_squad_data = {'owner': 1, 'veh': 1, 'num': 1, 'hidden': False, 'useable': False, 'custom_name': None}
        self.current_host_data = {
            'owner': 1,
            'veh': 56,
            'energy': 500000,
            'pos_y': DEFAULT_HOST_POS_Y,
            'reload_const': DEFAULT_HOST_RELOAD_CONST,
            'viewangle': DEFAULT_HOST_VIEWANGLE,
            'custom_name': None,
            'hidden': False,
            'ai': self.make_host_ai()
        }

        self.reset_map(confirm=False, track_history=False)
        self.clear_history()
        self.mark_saved_state()
        self._suppress_script_sync = True
        try:
            self.set_mode("TYPE")
        finally:
            self._suppress_script_sync = False
        self.refresh_palette_layout(force_full=True)
        self.refresh_map_layout(force_full=True)

    def new_map_dialog(self):
        settings = self.ask_new_map_settings(title="New Map", default_set=self.set_folder, default_w=DEFAULT_W, default_h=DEFAULT_H)
        if not settings:
            return
        self.create_new_map(settings['set_folder'], settings['width'], settings['height'])

    def resize_map_dialog(self):
        dims = self.ask_resize_dimensions(self.mw, self.mh)
        if not dims:
            return
        w, h = dims
        
        if w == self.mw and h == self.mh:
            return
        out_counts = self.count_resize_out_of_bounds(w, h)
        remove_out_of_bounds = self.has_resize_out_of_bounds(out_counts)
        if remove_out_of_bounds and not self.confirm_resize_remove_out_of_bounds(out_counts):
            return

        self.push_undo_snapshot()
        old_w, old_h = self.mw, self.mh
        new_grids = {
            'type': [['00'] * w for _ in range(h)],
            'own': [[0] * w for _ in range(h)],
            'hgt': [[DEFAULT_HGT] * w for _ in range(h)],
            'blg': [['00'] * w for _ in range(h)]
        }

        copy_w = min(old_w, w)
        copy_h = min(old_h, h)

        for r in range(copy_h):
            for c in range(copy_w):
                new_grids['type'][r][c] = self.grids['type'][r][c]
                new_grids['own'][r][c] = self.grids['own'][r][c]
                new_grids['hgt'][r][c] = self.grids['hgt'][r][c]
                new_grids['blg'][r][c] = self.grids['blg'][r][c]

        if remove_out_of_bounds:
            self.remove_resize_out_of_bounds(w, h)

        self.mw, self.mh = w, h
        self.grids = new_grids
        self.apply_borders()
        self.normalize_border_heights()
        self.invalidate_render_indexes()
        self.draw_grid()
        self.refresh_tech_panel_if_visible()
        self.dirty = True
        messagebox.showinfo("Resized", f"Map resized to {w}x{h}")

    def edit_info(self):
        win = tk.Toplevel(self.root)
        win.title("Level Info")
        win.geometry("560x780")
        win.configure(bg="#222")
        win.resizable(False, False)
        win.transient(self.root)

        def close_dialog():
            try:
                win.grab_release()
            except:
                pass
            win.destroy()

        # TITLE
        tk.Label(win, text="Level Title:", bg="#222", fg="white").pack(pady=5)
        e_title = tk.Entry(win, width=40); e_title.pack(); e_title.insert(0, self.lvl_info['title'])

        # BRIEFING / DEBRIEFING ART
        def get_art_options(prefix):
            mbpix_dir = resource_path("mbpix")
            options = {}
            if not os.path.isdir(mbpix_dir):
                return [], options
            for filename in os.listdir(mbpix_dir):
                base, ext = os.path.splitext(filename)
                if ext.lower() != ".png" or not base.lower().startswith(f"{prefix.lower()}_"):
                    continue
                options[base.lower()] = (base, os.path.join(mbpix_dir, filename))
            values = [value[0] for key, value in sorted(options.items())]
            return values, options

        mb_values, mb_preview_paths = get_art_options("MB")
        db_values, db_preview_paths = get_art_options("DB")
        mb_values = ["None"] + mb_values
        db_values = ["None"] + db_values
        if not hasattr(self, "mbpix_previews"):
            self.mbpix_previews = {}

        f_maps = tk.Frame(win, bg="#222"); f_maps.pack(pady=(4, 1))
        tk.Label(f_maps, text="Briefing Art (MB):", bg="#222", fg="#AAA").grid(row=0, column=0, padx=5, pady=1, sticky="e")
        cb_mb = ttk.Combobox(f_maps, values=mb_values, width=20)
        cb_mb.grid(row=0, column=1, padx=5, pady=1, sticky="w")
        cb_mb.set(self.get_briefing_art_base_name(self.lvl_info.get('mbmap')) or "MB_01")

        tk.Label(f_maps, text="Debriefing Art (DB):", bg="#222", fg="#AAA").grid(row=1, column=0, padx=5, pady=1, sticky="e")
        cb_db = ttk.Combobox(f_maps, values=db_values, width=20)
        cb_db.grid(row=1, column=1, padx=5, pady=1, sticky="w")
        cb_db.set(self.get_briefing_art_base_name(self.lvl_info.get('dbmap')) or "DB_01")

        tk.Label(
            f_maps,
            text="Original UA artwork, manually assigned. Not generated from current map.",
            bg="#222",
            fg="#777",
            font=("Arial", 8)
        ).grid(row=2, column=0, columnspan=2, pady=(1, 0))

        f_art_preview = tk.Frame(win, bg="#222"); f_art_preview.pack(padx=8, pady=(0, 1))
        mbdb_preview_w, mbdb_preview_h = 188, 172
        mbdb_thumb_w, mbdb_thumb_h = 150, 150
        cv_mb = tk.Canvas(f_art_preview, width=mbdb_preview_w, height=mbdb_preview_h, bg="#111", highlightthickness=1, highlightbackground="#444")
        cv_db = tk.Canvas(f_art_preview, width=mbdb_preview_w, height=mbdb_preview_h, bg="#111", highlightthickness=1, highlightbackground="#444")
        cv_mb.pack(side=tk.LEFT, padx=5)
        cv_db.pack(side=tk.LEFT, padx=5)

        def draw_art_preview(canvas, value, preview_paths, label):
            canvas.delete("all")
            base = self.get_briefing_art_base_name(value)
            path_info = preview_paths.get(base.lower()) if base else None
            if not path_info:
                canvas.create_rectangle(1, 1, mbdb_preview_w - 1, mbdb_preview_h - 1, outline="#333", fill="#181818")
                canvas.create_text(mbdb_preview_w // 2, 68, text=f"{label}: no PNG preview", fill="#777", font=("Arial", 9, "bold"))
                canvas.create_text(mbdb_preview_w // 2, 92, text=base or "manual value", fill="#555", font=("Arial", 9))
                return

            display_base, path = path_info
            cache_key = (path.lower(), mbdb_thumb_w, mbdb_thumb_h)
            if cache_key not in self.mbpix_previews:
                try:
                    img = Image.open(path)
                    img.thumbnail((mbdb_thumb_w, mbdb_thumb_h), Image.Resampling.LANCZOS)
                    img = img.convert("RGB")
                    thumb = Image.new("RGB", (mbdb_thumb_w, mbdb_thumb_h), "#111")
                    thumb.paste(img, ((mbdb_thumb_w - img.width) // 2, (mbdb_thumb_h - img.height) // 2))
                    self.mbpix_previews[cache_key] = ImageTk.PhotoImage(thumb)
                except:
                    self.mbpix_previews[cache_key] = None

            img = self.mbpix_previews.get(cache_key)
            if img:
                canvas.create_image(mbdb_preview_w // 2, 78, image=img)
            else:
                canvas.create_rectangle(19, 4, mbdb_preview_w - 19, 154, outline="#553333", fill="#221111")
                canvas.create_text(mbdb_preview_w // 2, 78, text="Preview load error", fill="#AA7777", font=("Arial", 9, "bold"))
            canvas.create_text(mbdb_preview_w // 2, mbdb_preview_h - 16, text=display_base, fill="#CCCCCC", font=("Arial", 9, "bold"))

        def refresh_art_previews(event=None):
            draw_art_preview(cv_mb, cb_mb.get(), mb_preview_paths, "MB")
            draw_art_preview(cv_db, cb_db.get(), db_preview_paths, "DB")

        cb_mb.bind("<<ComboboxSelected>>", refresh_art_previews)
        cb_mb.bind("<KeyRelease>", refresh_art_previews)
        cb_mb.bind("<FocusOut>", refresh_art_previews)
        cb_db.bind("<<ComboboxSelected>>", refresh_art_previews)
        cb_db.bind("<KeyRelease>", refresh_art_previews)
        cb_db.bind("<FocusOut>", refresh_art_previews)
        win.after(100, refresh_art_previews)

        # SKY SELECTION
        f_sky_lbl = tk.Frame(win, bg="#222"); f_sky_lbl.pack(pady=(2,0))
        tk.Label(f_sky_lbl, text="Select Sky:", bg="#222", fg="white", font=("bold", 11)).pack(side=tk.LEFT, padx=5)
        
        current_sky_name = os.path.basename(self.lvl_info['sky']).replace(".base", "").replace(".bas", "")
        lbl_sky_current = tk.Label(f_sky_lbl, text=f"[{current_sky_name}]", bg="#222", fg="#00FF00", font=("Arial", 10))
        lbl_sky_current.pack(side=tk.LEFT)

        sky_w, sky_h = 140, 90
        sky_cols = 3
        sky_canvas_w = sky_cols * (sky_w + 10) + 10
        fr_sky = tk.Frame(win, bg="#333", bd=2, relief="sunken"); fr_sky.pack(fill=tk.Y, expand=True, padx=10, pady=5)
        cv_s = tk.Canvas(fr_sky, width=sky_canvas_w, bg="#111", highlightthickness=0); sb_s = tk.Scrollbar(
            fr_sky,
            command=cv_s.yview,
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
        cv_s.config(yscrollcommand=sb_s.set); sb_s.pack(side=tk.RIGHT, fill=tk.Y)
        sb_s.config(bg=SCROLL_BG, activebackground=SCROLL_ACTIVE, troughcolor="#444")
        cv_s.pack(side=tk.LEFT, fill=tk.Y, expand=True)

        def draw_skies():
            cv_s.delete("all")
            w = sky_canvas_w
            cols = sky_cols
            r, c = 0, 0
            
            raw_sky = self.lvl_info['sky'].lower().replace("\\", "/")
            base_name = os.path.basename(raw_sky)
            curr_sel = os.path.splitext(base_name)[0]

            for f in self.lists['sky']:
                file_base_name = os.path.splitext(f)[0]
                x, y = c * (sky_w + 10) + 10, r * (sky_h + 10) + 10
                is_selected = (file_base_name.lower() == curr_sel.lower())
                fill_color = "#004400" if is_selected else "#333"
                border_color = "#00FF00" if is_selected else "#555"
                tag = f"sky_{file_base_name}"
                cv_s.create_rectangle(x, y, x + sky_w, y + sky_h, fill=fill_color, outline=border_color, width=2, tags=tag)
                
                if file_base_name not in self.sky_previews:
                    try: 
                        path = resource_path(os.path.join("skies", f))
                        img = Image.open(path).convert("RGB")
                        target_w, target_h = 130, 70
                        scale = max(target_w / img.width, target_h / img.height)
                        resized_w = max(target_w, int(img.width * scale))
                        resized_h = max(target_h, int(img.height * scale))
                        img = img.resize((resized_w, resized_h), Image.Resampling.LANCZOS)
                        left = max(0, (resized_w - target_w) // 2)
                        top = max(0, (resized_h - target_h) // 2)
                        img = img.crop((left, top, left + target_w, top + target_h))
                        self.sky_previews[file_base_name] = ImageTk.PhotoImage(img)
                    except: 
                        img = Image.new("RGB", (130, 70), "#300")
                        self.sky_previews[file_base_name] = ImageTk.PhotoImage(img)
                
                cv_s.create_image(x + sky_w//2, y + 40, image=self.sky_previews[file_base_name], tags=tag)
                cv_s.create_text(x + sky_w//2, y + sky_h - 10, text=file_base_name, fill="white", font=("Arial", 9), tags=tag)
                cv_s.tag_bind(tag, "<Button-1>", lambda e, s=file_base_name: select_sky(s))
                c += 1;
                if c >= cols: c = 0; r += 1
            cv_s.config(scrollregion=(0,0,w, (r+1)*(sky_h+10) + 20))

        def select_sky(name): 
            new_sky = f"objects/{name}.bas"
            if self.lvl_info['sky'] != new_sky:
                self.push_undo_snapshot()
                self.lvl_info['sky'] = new_sky
                lbl_sky_current.config(text=f"[{name}]")
                draw_skies()

        cv_s.bind("<Configure>", lambda e: draw_skies()); self.bind_scroll(cv_s); win.after(100, draw_skies)
        
        f_media = tk.Frame(win, bg="#222"); f_media.pack(fill=tk.X, pady=10)
        tk.Label(f_media, text="Ambience Track:", bg="#222", fg="#AAA").grid(row=0, column=0, padx=5, sticky="e")
        music_options = ["None", "2", "3", "4", "5", "6"]
        cb_music = ttk.Combobox(f_media, values=music_options, width=15) 
        cb_music.grid(row=0, column=1, padx=5, sticky="w")
        cb_music.set(self.lvl_info.get('music', "None"))

        tk.Label(f_media, text="Intro Movie:", bg="#222", fg="#AAA").grid(row=1, column=0, padx=5, sticky="e")
        movie_options = ["None", "intro.mpg", "logo.mpg", "tut1.mpg", "tut2.mpg", "tut3.mpg", "tut4.mpg", "tut5.mpg", "tut6.mpg", "tut7.mpg", "tut8.mpg", "win.mpg", "lose.mpg"]
        cb_movie = ttk.Combobox(f_media, values=movie_options, width=20) 
        cb_movie.grid(row=1, column=1, padx=5, sticky="w")
        
        current_movie = self.get_movie_filename(self.lvl_info.get('movie', "None")) if hasattr(self, "get_movie_filename") else ""
        cb_movie.set(current_movie or "None")

        tk.Label(f_media, text="(You can select from list or type custom values)", bg="#222", fg="#777", font=("Arial", 8)).grid(row=2, column=0, columnspan=2, pady=5)

        def save():
            new_title = e_title.get()
            new_mbmap = self.normalize_briefing_art_for_export(cb_mb.get())
            new_dbmap = self.normalize_briefing_art_for_export(cb_db.get())
            new_music = cb_music.get()
            
            raw_movie = cb_movie.get().strip()
            new_movie = self.get_movie_filename(raw_movie) if hasattr(self, "get_movie_filename") else raw_movie
            if not new_movie:
                new_movie = "None"

            changed = (
                self.lvl_info['title'] != new_title or
                self.lvl_info.get('mbmap') != new_mbmap or
                self.lvl_info.get('dbmap') != new_dbmap or
                self.lvl_info.get('music') != new_music or
                self.lvl_info.get('movie') != new_movie
            )
            if changed:
                self.push_undo_snapshot()
                self.lvl_info['title'] = new_title
                self.lvl_info['mbmap'] = new_mbmap
                self.lvl_info['dbmap'] = new_dbmap
                self.lvl_info['music'] = new_music
                self.lvl_info['movie'] = new_movie

            messagebox.showinfo("Success", "Level Info Updated!", parent=win)
            close_dialog()
        
        tk.Button(win, text="CONFIRM", command=save, bg="#008800", fg="white", width=20).pack(pady=15)
        win.protocol("WM_DELETE_WINDOW", close_dialog)
        win.bind("<Escape>", lambda e: close_dialog())
        self.center_modal_dialog(win, 560, 780)
        win.grab_set()
        win.focus_set()
        self.root.wait_window(win)
