"""Pictures of sectors, set models and small sector grids, drawn with the Sector Editor's software rasteriser.

Runs inside render_worker (it needs Qt). The tables come from SetDocument, so override.lst replacements look
exactly as the Sector Editor and the game load them. Sectors of one request share the same framing (same scale),
so variants can be compared side by side.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from map_editor.core.asset_bridge import SetAssets
from map_editor.core.sector_resolver import SECTOR_SIZE, SUB_SIZE
from map_editor.render.sector_mesh import SectorMesh, SectorMeshLibrary
from map_editor.render.sector_sprite import build_pieces, fitted_preview_camera, rasterize_sprite
from studio_mcp import core, sets

ISO_PITCH = 35.264
# iso = the Map/Sector Editor home view; top = the Sector Editor cell grid (cells 0-2 / grid row 0 at the top,
# column 0 at the left).
VIEWS = {"iso": (-45.0, ISO_PITCH), "iso_left": (45.0, ISO_PITCH), "iso_back": (135.0, ISO_PITCH),
         "iso_back_left": (-135.0, ISO_PITCH), "top": (180.0, 90.0)}
FLAT_FILLER = (0.0,) * 10


def _model_mesh(lib: SectorMeshLibrary, base: str) -> SectorMesh:
    faces = lib._base_faces(base)
    mesh = SectorMesh(-1, faces=[(face, 0, 0) for face in faces or []])
    if faces is None:
        mesh.missing.append(base)
    return mesh


def _grid_mesh(lib: SectorMeshLibrary, doc, grid, state: int) -> SectorMesh:
    """Sectors side by side as on a map (row 0 far, column 0 left) with the set's transition strips."""
    mesh = SectorMesh(-1)

    def ground(col, row):
        number = grid[row][col] if 0 <= row < len(grid) and 0 <= col < len(grid[row]) else None
        return doc.sdf.sectors[number].ground if number in doc.sdf.sectors else None

    for row, line in enumerate(grid):
        for col, number in enumerate(line):
            if number is None:
                continue
            if number not in doc.sdf.sectors:
                raise core.McpToolError(f"grid: sector {number} is not defined")
            x, z = col * SECTOR_SIZE, -row * SECTOR_SIZE
            part = lib.mesh(int(number), state)
            mesh.missing.extend(part.missing)
            mesh.faces.extend((face, ox + x, oz + z) for face, ox, oz in part.faces)
            for vertical, (c, r) in ((True, (col - 1, row)), (False, (col, row - 1))):
                if ground(c, r) is None:
                    continue
                filler = lib.filler_mesh(ground(c, r), ground(col, row), vertical, FLAT_FILLER)
                mesh.faces.extend((face, x, z) for face, _, _ in filler.faces)
    return mesh


def _frame(meshes: list[SectorMesh], footprint: float):
    """Points that every picture of the request must show: the ground footprint and the tallest model."""
    top = min([0.0] + [face_y for mesh in meshes for face, _, _ in mesh.faces for face_y in
                       (v[1] for v in face.vertices)])
    return [(x, y, z) for x in (-footprint, footprint) for z in (-footprint, footprint) for y in (0.0, top)]


def _picture(lib, mesh: SectorMesh, frame, view: str, size, background: QColor, labels):
    yaw, pitch = VIEWS[view]
    extra = [(v[0] + ox, v[1], v[2] + oz) for face, ox, oz in mesh.faces for v in face.vertices]
    cam = fitted_preview_camera(extra + frame, yaw, pitch, size)
    width, height = cam.width, cam.height
    origin = (width / 2 + cam.pan[0], height / 2 + cam.pan[1])
    pieces = build_pieces(lib, mesh, cam, origin)
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(background)
    painter = QPainter(image)
    sprite = rasterize_sprite((width, height, origin, pieces), lib.tables, 1.0) if pieces else None
    if sprite is not None:
        painter.drawImage(0, 0, sprite.image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
    for text, point in labels:
        x, y = cam.to_screen(cam.to_camera(point, True), origin)
        box = QRectF(x - 9, y - 9, 18, 18)
        painter.setPen(QPen(QColor(0, 0, 0, 200), 1))
        painter.setBrush(QColor(255, 220, 60, 220))
        painter.drawEllipse(box)
        painter.setPen(QColor(0, 0, 0))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
    painter.end()
    return image


def _cell_labels(doc, number: int, offset=(0.0, 0.0)):
    sector = doc.sdf.sectors[number]
    if sector.single:
        return [("4", (offset[0], 0.0, offset[1]))]
    return [(str(cell), (offset[0] + (cell % 3 - 1) * SUB_SIZE, 0.0, offset[1] + (1 - cell // 3) * SUB_SIZE))
            for cell in range(9)]


def render(request: dict) -> dict:
    sets._prepare()
    doc = sets.open_document(request["set"])
    assets = SetAssets(doc.number, set_dir=doc.folder).load()
    assets.sdf = doc.sdf
    library = SectorMeshLibrary(assets, set_only=True)
    state = int(request.get("state", 0))
    width, height = request.get("size", [360, 270])
    background = QColor(request.get("background", "#1f2228"))
    views = request.get("views") or ["iso"]
    for view in views:
        if view not in VIEWS:
            raise core.McpToolError(f"Unknown view {view!r}. Use: {', '.join(VIEWS)}")
    label_cells = bool(request.get("label_cells"))
    items, warnings = [], []
    for number in request.get("sectors") or []:
        number = int(number)
        if number not in doc.sdf.sectors:
            raise core.McpToolError(f"sector {number} is not defined")
        labels = _cell_labels(doc, number) if label_cells else []
        items.append((f"S{number:03d} [{number:02X}]", library.mesh(number, state), labels, "sector"))
    for model in request.get("models") or []:
        row = doc.sdf.models[sets.SetContext(doc).model(model)]
        items.append((f"M{row.id} {Path(row.base).stem}", _model_mesh(library, row.base), [], "model"))
    for building in request.get("buildings") or []:
        row = doc.sdf.buildings[sets.SetContext(doc).building(building)]
        base = doc.sdf.models[row.state(state)].base
        items.append((f"B{row.id} {Path(base).stem}", _model_mesh(library, base), [], "model"))
    grid = request.get("grid")
    if grid:
        labels = []
        if label_cells:
            for r, line in enumerate(grid):
                for c, number in enumerate(line):
                    if number in doc.sdf.sectors:
                        labels.append((str(number), (c * SECTOR_SIZE, 0.0, -r * SECTOR_SIZE)))
        items.append(("grid", _grid_mesh(library, doc, grid, state), labels, "grid"))
    if not items:
        raise core.McpToolError("give sectors, models, buildings or grid")
    sector_frame = _frame([m for _, m, _, kind in items if kind == "sector"], SECTOR_SIZE / 2)
    out_dir = Path(request["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for index, (label, mesh, labels, kind) in enumerate(items):
        warnings.extend(f"{label}: missing {name}" for name in mesh.missing)
        frame = sector_frame if kind == "sector" else _frame([mesh], SUB_SIZE / 2 if kind == "model" else 0)
        for view in views:
            image = _picture(library, mesh, frame, view, (width, height), background, labels)
            path = out_dir / f"{index:03d}_{view}.png"
            image.save(str(path))
            images.append({"view": f"{label} {view}" if len(views) > 1 or kind != "grid" else label,
                           "path": str(path)})
    return {"images": images, "warnings": warnings, "state": state}
