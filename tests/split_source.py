"""Source text of a module whose class is split into mixin modules.

Source-scanning tests read the main module together with its mixin files, so
they keep checking the whole class wherever a method lives.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_MIXIN_FILES = {
    "studio/workbench/window.py": ("studio/workbench/common.py", "studio/workbench/window_*.py"),
    "studio/rendering/asset_viewport.py": ("studio/rendering/asset_viewport_*.py",),
    "entity_designer/editor.py": ("entity_designer/editor_*.py",),
}


def module_source(relative: str) -> str:
    """Text of `relative` followed by the text of its mixin modules."""
    paths = [ROOT / relative]
    for pattern in _MIXIN_FILES.get(relative, ()):
        paths += sorted(ROOT.glob(pattern))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)
