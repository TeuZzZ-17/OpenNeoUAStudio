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

from PySide6.QtCore import QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from studio.rendering.asset_viewport import VIEW_PRESET_ANGLES, AssetViewport  # noqa: E402
from studio_mcp import core, meshmath  # noqa: E402
from studio_mcp.select import select_points, select_polygons, view_of_object  # noqa: E402

VIEW_ALIASES = {
    "iso": "Isometric Front Right", "iso_left": "Isometric Front Left",
    "iso_back": "Isometric Back Right", "iso_back_left": "Isometric Back Left",
    "front": "Front", "back": "Back", "left": "Left", "right": "Right",
    "top": "Top", "bottom": "Bottom",
}


def _draw_overlays(image, viewport, request: dict, fam_obj) -> None:
    """Draw selections on top of the render: highlighted polygons, wire outlines and index labels.

    Uses the viewport's own camera, so the marks sit exactly on the model. Labels are what lets an
    assistant turn "that corner of the nose" into the point/polygon numbers the edit tools need."""
    view = view_of_object(fam_obj)
    camera = viewport._camera_state()
    rect = QRectF(0, 0, image.width(), image.height())

    def screen(point):
        cam_point = viewport._camera_vertex(point, camera)
        return viewport._project(cam_point, rect, camera), cam_point[2]

    highlighted = select_polygons(request["highlight_polygons"], view) if request.get("highlight_polygons") is not None else []
    labelled_points = select_points(request["label_points"], view) if request.get("label_points") is not None else []
    labelled_polys = select_polygons(request["label_polygons"], view) if request.get("label_polygons") is not None else []
    if not (highlighted or labelled_points or labelled_polys or request.get("show_wire")):
        return
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if request.get("show_wire"):
        painter.setPen(QPen(QColor(120, 200, 255, 130), 1))
        for ring in view.polygons:
            if len(ring) >= 2:
                painter.drawPolygon(QPolygonF([screen(view.points[i])[0] for i in ring]))
    for pid in highlighted:
        painter.setPen(QPen(QColor(255, 80, 200, 255), 2))
        painter.setBrush(QColor(255, 80, 200, 70))
        painter.drawPolygon(QPolygonF([screen(view.points[i])[0] for i in view.polygons[pid]]))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
    for pid in labelled_polys:
        center = screen(meshmath.centroid([view.points[i] for i in view.polygons[pid]]))[0]
        label = QRectF(center.x() - 4, center.y() - 8, 9 * len(f"P{pid}") + 4, 14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(label, 3, 3)
        painter.setPen(QColor(110, 255, 150, 255))
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, f"P{pid}")
    for index in labelled_points:
        position = screen(view.points[index])[0]
        painter.setPen(QPen(QColor(255, 220, 60, 255), 1))
        painter.setBrush(QColor(255, 220, 60, 255))
        painter.drawEllipse(position, 2.5, 2.5)
        label = QRectF(position.x() + 3, position.y() - 15, 8 * len(str(index)) + 4, 13)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(label, 3, 3)
        painter.setPen(QColor(255, 235, 120, 255))
        painter.drawText(label, Qt.AlignmentFlag.AlignCenter, str(index))
    painter.end()


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
            _draw_overlays(image, viewport, request, next(o for o in family.all_objects() if o.skeleton is not None))
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
