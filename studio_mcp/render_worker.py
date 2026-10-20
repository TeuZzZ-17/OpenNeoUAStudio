"""Render a model with Studio's own Retail indexed renderer, without a window.

Runs as a separate process (python -m studio_mcp.render_worker request.json) so Qt always owns
its main thread and a render crash never takes the MCP server down.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

STUDIO_ROOT = Path(__file__).resolve().parents[1]
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from studio.rendering.asset_viewport import VIEW_PRESET_ANGLES, AssetViewport  # noqa: E402
from studio_mcp import core  # noqa: E402

VIEW_ALIASES = {
    "iso": "Isometric Front Right", "iso_left": "Isometric Front Left",
    "iso_back": "Isometric Back Right", "iso_back_left": "Isometric Back Left",
    "front": "Front", "back": "Back", "left": "Left", "right": "Right",
    "top": "Top", "bottom": "Bottom",
}


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_dir = Path(request["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    width, height = request.get("size", [480, 360])
    background = QColor(request.get("background", "#1f2228"))
    result = {"ok": False, "images": [], "warnings": []}
    try:
        source = core.resolve_source(request["spec"], request.get("set_hint", ""))
        family = core.load_family(source)
        if family.root_object is None:
            raise core.McpToolError("Model could not be loaded: " + "; ".join(family.warnings[:3]))
        app = QApplication.instance() or QApplication([])  # noqa: F841 - must stay alive
        viewport = AssetViewport()
        viewport.resize(width, height)
        viewport.load_family(family)
        viewport.begin_snapshot_mode(background)
        for name in request.get("views", ["iso"]):
            preset = VIEW_ALIASES.get(name.lower(), name)
            if preset not in VIEW_PRESET_ANGLES:
                raise core.McpToolError(f"Unknown view '{name}'. Use: {', '.join(VIEW_ALIASES)}")
            viewport.apply_view_preset(preset, QSize(width, height), int(request.get("zoom", 100)))
            image = viewport.render_snapshot(QSize(width, height), background)
            if image.isNull():
                raise core.McpToolError("Renderer produced no image (empty model?)")
            path = out_dir / f"{name}.png"
            image.save(str(path))
            result["images"].append({"view": name, "path": str(path)})
        viewport.end_snapshot_mode()
        result["warnings"] = [w for w in family.warnings if "palette" not in w.lower()]
        result["renderer"] = getattr(viewport, "_last_effective_renderer", "")
        result["ok"] = True
    except Exception as exc:  # report to the server instead of crashing silently
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("RESULT_JSON " + json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
