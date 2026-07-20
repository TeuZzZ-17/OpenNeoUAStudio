import tkinter as tk
import copy


class HistoryMixin:

    def make_document_snapshot(self):
        return {
            'mw': self.mw,
            'mh': self.mh,
            'set_folder': self.set_folder,
            'grids': copy.deepcopy(self.grids),
            'gates': copy.deepcopy(self.gates),
            'items': copy.deepcopy(self.items),
            'gems': copy.deepcopy(self.gems),
            'squads': copy.deepcopy(self.squads),
            'host_stations': copy.deepcopy(self.host_stations),
            'player_owner': self.player_owner,
            'tech': copy.deepcopy(self.tech),
            'custom_tech_names': copy.deepcopy(self.custom_tech_names),
            'lvl_info': copy.deepcopy(self.lvl_info),
            'script_content': self.script_content,
            'visible_gate_slots': self.visible_gate_slots,
            'visible_item_slots': self.visible_item_slots,
            'visible_gem_slots': self.visible_gem_slots,
        }

    def mark_saved_state(self):
        self._saved_document_snapshot = self.make_document_snapshot()
        self.dirty = False

    def has_unsaved_changes(self):
        if not hasattr(self, "_saved_document_snapshot"):
            return False
        if hasattr(self, 'sync_script_widget_to_data'):
            self.sync_script_widget_to_data(push_undo=False)
        self.dirty = self.make_document_snapshot() != self._saved_document_snapshot
        return self.dirty

    def make_snapshot(self):
        return {
            'mw': self.mw,
            'mh': self.mh,
            'set_folder': self.set_folder,
            'current_filename': self.current_filename,
            'current_filepath': getattr(self, 'current_filepath', None),
            'grids': copy.deepcopy(self.grids),
            'gates': copy.deepcopy(self.gates),
            'items': copy.deepcopy(self.items),
            'gems': copy.deepcopy(self.gems),
            'squads': copy.deepcopy(self.squads),
            'host_stations': copy.deepcopy(self.host_stations),
            'player_owner': self.player_owner,
            'tech': copy.deepcopy(self.tech),
            'custom_tech_names': copy.deepcopy(self.custom_tech_names),
            'lvl_info': copy.deepcopy(self.lvl_info),
            'script_content': self.script_content,
            'visible_gate_slots': self.visible_gate_slots,
            'visible_item_slots': self.visible_item_slots,
            'visible_gem_slots': self.visible_gem_slots,
            'mode': self.mode,
            'sel': copy.deepcopy(self.sel),
            'gate_tool': self.gate_tool,
            'item_tool': self.item_tool,
            'gem_tool': self.gem_tool,
            'current_gate_slot': self.current_gate_slot,
            'current_item_slot': self.current_item_slot,
            'current_gem_slot': self.current_gem_slot,
            'curr_tech_faction': self.curr_tech_faction,
            'current_squad_data': copy.deepcopy(self.current_squad_data),
            'current_host_data': copy.deepcopy(self.current_host_data),
            'current_squad_index': self.current_squad_index,
            'current_host_index': self.current_host_index
        }

    def restore_snapshot(self, snapshot):
        old_set_folder = self.set_folder
        self.mw = snapshot['mw']
        self.mh = snapshot['mh']
        self.set_folder = snapshot['set_folder']
        self.current_filename = snapshot.get('current_filename')
        self.current_filepath = snapshot.get('current_filepath')
        self.grids = copy.deepcopy(snapshot['grids'])
        self.gates = copy.deepcopy(snapshot['gates'])
        self.items = copy.deepcopy(snapshot['items'])
        self.gems = copy.deepcopy(snapshot['gems'])
        self.squads = copy.deepcopy(snapshot['squads'])
        self.host_stations = copy.deepcopy(snapshot['host_stations'])
        self.player_owner = snapshot.get('player_owner', 1)
        self.tech = copy.deepcopy(snapshot['tech'])
        self.custom_tech_names = copy.deepcopy(snapshot['custom_tech_names'])
        self.lvl_info = copy.deepcopy(snapshot['lvl_info'])
        self.script_content = snapshot['script_content']
        self.script_text_widget = None
        self.visible_gate_slots = snapshot['visible_gate_slots']
        self.visible_item_slots = snapshot['visible_item_slots']
        self.visible_gem_slots = snapshot['visible_gem_slots']
        self.sel = copy.deepcopy(snapshot['sel'])
        self.gate_tool = snapshot['gate_tool']
        self.item_tool = snapshot['item_tool']
        self.gem_tool = snapshot['gem_tool']
        self.current_gate_slot = snapshot['current_gate_slot']
        self.current_item_slot = snapshot['current_item_slot']
        self.current_gem_slot = snapshot['current_gem_slot']
        self.curr_tech_faction = snapshot['curr_tech_faction']
        self.current_squad_data = copy.deepcopy(snapshot['current_squad_data'])
        self.current_host_data = copy.deepcopy(snapshot['current_host_data'])
        self.current_squad_index = snapshot['current_squad_index']
        self.current_host_index = snapshot['current_host_index']

        if self.set_folder != old_set_folder:
            self.reload_assets()
        self.update_window_title()
        mode_to_restore = snapshot.get('mode', "TYPE")
        self._restoring_snapshot = True
        try:
            self.set_mode(mode_to_restore)
        finally:
            self._restoring_snapshot = False
        self.refresh_palette_layout(force_full=True)
        self.refresh_map_layout(force_full=True)
        self.draw_grid()
        self.update_history_buttons()

    def push_undo_snapshot(self):
        snap = self.make_snapshot()
        if self.undo_stack and self.undo_stack[-1] == snap:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack = self.undo_stack[-self.max_history:]
        self.redo_stack.clear()
        self.update_history_buttons()

    def undo(self, event=None):
        if not self.undo_stack:
            return "break"
        current = self.make_snapshot()
        snap = self.undo_stack.pop()
        self.redo_stack.append(current)
        if len(self.redo_stack) > self.max_history:
            self.redo_stack = self.redo_stack[-self.max_history:]
        self.restore_snapshot(snap)
        self.update_history_buttons()
        return "break"

    def redo(self, event=None):
        if not self.redo_stack:
            return "break"
        current = self.make_snapshot()
        snap = self.redo_stack.pop()
        self.undo_stack.append(current)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack = self.undo_stack[-self.max_history:]
        self.restore_snapshot(snap)
        self.update_history_buttons()
        return "break"

    def clear_history(self):
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.update_history_buttons()

    def update_history_buttons(self):
        if hasattr(self, "btn_undo") and self.btn_undo.winfo_exists():
            self.btn_undo.config(state=(tk.NORMAL if self.undo_stack else tk.DISABLED))
        if hasattr(self, "btn_redo") and self.btn_redo.winfo_exists():
            self.btn_redo.config(state=(tk.NORMAL if self.redo_stack else tk.DISABLED))
