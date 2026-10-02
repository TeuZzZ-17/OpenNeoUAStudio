"""Run two UI regressions with selected modules imported from git HEAD in memory."""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "gpu_widget": "gpu_widget.py",
    "assembly_viewer": "assembly_viewer.py",
    "assembly_window": "assembly_window.py",
    "wireframe_editor.window": "wireframe_editor/window.py",
    "collision_editor.editor": "collision_editor/editor.py",
}
TESTS = [
    "tests/test_wireframe_editor.py::WireframeEditorUiTests::test_toolbar_ends_before_vertex_details",
    "tests/test_window_contract_v5.py::WindowContractV5Tests::test_child_metadata_and_texture_owner_selection_are_preserved",
]
loaded: dict[str, tuple[str, str]] = {}


class GitHeadLoader(importlib.abc.Loader):
    def __init__(self, module_name: str, relative_path: str) -> None:
        self.module_name = module_name
        self.relative_path = relative_path

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        source = subprocess.check_output(
            ["git", "show", f"HEAD:{self.relative_path}"], cwd=ROOT)
        digest = hashlib.sha256(source).hexdigest()
        source_text = source.decode("utf-8-sig")
        source_path = ROOT / self.relative_path
        module.__file__ = str(source_path)
        module.__cached__ = None
        code = compile(source_text, str(source_path), "exec")
        loaded[self.module_name] = (self.relative_path, digest)
        exec(code, module.__dict__)


class GitHeadFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        relative_path = MODULES.get(fullname)
        if relative_path is None:
            return None
        loader = GitHeadLoader(fullname, relative_path)
        return importlib.util.spec_from_loader(
            fullname, loader, origin=str(ROOT / relative_path))


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    sys.meta_path.insert(0, GitHeadFinder())

    # Preload every requested baseline module before pytest imports any tests.
    for name in MODULES:
        importlib.import_module(name)

    for name, (relative_path, digest) in loaded.items():
        blob = subprocess.check_output(
            ["git", "rev-parse", f"HEAD:{relative_path}"], cwd=ROOT,
            text=True).strip()
        print(f"BASELINE {name} HEAD:{relative_path} blob={blob} sha256={digest}")

    import pytest
    result = pytest.main(["-q", "--tb=short", *TESTS])

    missing = sorted(set(MODULES) - set(loaded))
    if missing:
        print(f"ERROR: baseline modules not loaded: {', '.join(missing)}")
        return 2
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
