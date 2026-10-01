import io
import sys
from pathlib import Path

SEKTOR2 = Path(__file__).resolve().parents[3] / "Sektor2"


def load_oracle():
    """Carica il MapIOMixin originale di Sektor2 senza GUI, come oracolo."""
    import importlib.util
    package = SEKTOR2 / 'map_editor'
    spec = importlib.util.spec_from_file_location('legacy_sektor2', package / '__init__.py',
                                                submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['legacy_sektor2'] = module
    spec.loader.exec_module(module)
    import tkinter  # noqa: F401
    from tkinter import messagebox
    from legacy_sektor2 import assets, canvas, map_io
    def fail(*a, **k):
        raise RuntimeError(a)

    messagebox.showinfo = messagebox.showwarning = lambda *a, **k: None
    messagebox.showerror = fail
    return map_io, assets, canvas


def sektor2_roundtrip(src_path: str, dst_path: str) -> None:
    map_io, assets, canvas = load_oracle()
    from tkinter import filedialog

    class Oracle(map_io.MapIOMixin, assets.AssetMixin, canvas.CanvasMixin):
        def __init__(self):
            self.defs = {'veh': {}, 'blg': {}, 'host': {}, 'weapon': {}}
            self.set_folder = "set1"
            self.mw = self.mh = 15
            self.current_filename = None
            self.script_content = ""
            self.visible_gate_slots = self.visible_item_slots = 0
            self.visible_gem_slots = 0
            self.mode = "TYPE"
            self.dirty = False
            self.sel = {}
            self.load_host_ai_presets()

        def __getattr__(self, name):
            if name.startswith("__") or name in ("cv_map", "script_text_widget", "btn_undo", "btn_redo", "_saved_document_snapshot", "sync_script_widget_to_data", "_ldf_encoding", "_ldf_newline", "current_filepath"):
                raise AttributeError(name)
            return lambda *a, **k: None

        def reload_assets(self):
            pass

        def has_unsaved_changes(self):
            return False

        def map_cell_is_valid(self, obj):
            return 0 <= obj.get('x', -1) < self.mw and 0 <= obj.get('y', -1) < self.mh

    oracle = Oracle()
    assert oracle.load_map_from_path(src_path, prompt_unsaved=False, show_success=False)
    filedialog.asksaveasfilename = lambda **k: dst_path
    assert oracle.save_map(ask_open_after_save=False)
