"""Headless model operations for the Studio MCP server (no Qt, no GPU).

Everything here reuses Studio's own parsers and writers; nothing re-implements a format.
Rendering lives in render_worker.py because it needs a Qt application.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

STUDIO_ROOT = Path(__file__).resolve().parents[1]
if str(STUDIO_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDIO_ROOT))

from studio.assets.family import load_asset_family  # noqa: E402
from studio.exchange.portable import DirectoryIndex, find_existing_path  # noqa: E402
from studio.assets.mapping import export_base_object_bytes  # noqa: E402
from studio.formats.base_format import parse_base_file  # noqa: E402
from studio.formats.setbas import SetBasArchive, read_setbas  # noqa: E402


class McpToolError(Exception):
    """A user-facing error: the message is returned to the assistant as-is."""


# --- locations ---------------------------------------------------------------


def data_root() -> Path:
    """Game Data folder: ONUA_DATA, else <project>/UA Rising/Data next to the Studio folder."""
    env = os.environ.get("ONUA_DATA")
    root = Path(env) if env else STUDIO_ROOT.parents[1] / "UA Rising" / "Data"
    if not root.is_dir():
        raise McpToolError(f"Game Data folder not found: {root}. Set the ONUA_DATA environment variable.")
    return root.resolve()


def work_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "onua_studio_mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def writable_roots() -> list[Path]:
    """Folders the MCP may write to: game models and wireframes, plus any extra roots from ONUA_MCP_WRITE_ROOTS."""
    roots = [under_data("Models"), under_data("Wireless"), work_dir()]
    for extra in os.environ.get("ONUA_MCP_WRITE_ROOTS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(extra.strip()).resolve())
    return roots


def under_data(relative: str | Path) -> Path:
    """Data/<relative>, matching the folders that exist without case (Linux file systems are case-sensitive)."""
    current = data_root()
    parts = Path(str(relative).replace("\\", "/")).parts
    for index, part in enumerate(parts):
        found = find_existing_path(current, part)
        if found is None:
            return current.joinpath(*parts[index:])
        current = found
    return current


def ensure_writable(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = under_data(target)
    target = target.resolve()
    for root in writable_roots():
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in writable_roots())
    raise McpToolError(f"Refusing to write outside the allowed folders ({allowed}): {target}")


def resolve_existing(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = under_data(target)
    if not target.exists():
        raise McpToolError(f"File not found: {target}")
    return target.resolve()


# --- sets and VISPROTO -------------------------------------------------------


def _find_child(parent: Path, name: str) -> Path | None:
    for child in parent.iterdir():
        if child.name.lower() == name.lower():
            return child
    return None


def list_sets() -> list[dict]:
    sets_dir = under_data("Sets")
    result = []
    if not sets_dir.is_dir():
        return result
    for child in sorted(sets_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or not re.fullmatch(r"set\d+", child.name, re.I):
            continue
        objects = _find_child(child, "Objects")
        setbas = _find_child(objects, "Set.BAS") if objects else None
        scripts = _find_child(child, "Scripts")
        visproto = _find_child(scripts, "VISPROTO.LST") if scripts else None
        result.append({
            "set": child.name,
            "set_bas": str(setbas) if setbas else None,
            "visproto": str(visproto) if visproto else None,
        })
    return result


def _set_info(set_name: str) -> dict:
    wanted = set_name.lower()
    if wanted.isdigit():
        wanted = "set" + wanted
    for entry in list_sets():
        if entry["set"].lower() == wanted:
            if not entry["set_bas"]:
                raise McpToolError(f"{entry['set']} has no Objects/Set.BAS")
            return entry
    raise McpToolError(f"Unknown set '{set_name}'. Available: {', '.join(e['set'] for e in list_sets())}")


_VISPROTO_LINE = re.compile(r"^\s*(?P<name>[^\s;]+?)\s*;\s*(?P<index>\d+)\s*(?P<comment>.*)$")


def list_vp(set_name: str, text_filter: str = "") -> list[dict]:
    """VISPROTO slots of a set: index, BASE file name and the developers' comment."""
    entry = _set_info(set_name)
    if not entry["visproto"]:
        raise McpToolError(f"{entry['set']} has no Scripts/VISPROTO.LST")
    raw = Path(entry["visproto"]).read_bytes().decode("latin-1")
    rows = []
    for line in raw.splitlines():
        match = _VISPROTO_LINE.match(line)
        if not match:
            continue
        row = {"vp": int(match["index"]), "base": match["name"], "comment": match["comment"].strip(" |+-")}
        if text_filter and text_filter.lower() not in (row["base"] + " " + row["comment"]).lower():
            continue
        rows.append(row)
    return rows


# --- sources -----------------------------------------------------------------

_SETBAS_CACHE: dict[str, tuple] = {}


def _setbas(set_name: str):
    """Parsed Set.BAS archive and BASE tree, cached for the life of the process."""
    entry = _set_info(set_name)
    key = entry["set_bas"]
    if key not in _SETBAS_CACHE:
        _SETBAS_CACHE[key] = (read_setbas(key), parse_base_file(key))
    return _SETBAS_CACHE[key]


@dataclass
class Source:
    """A model ready to load: a BASE file plus the optional Set.BAS that supplies its resources."""

    base_path: Path
    setbas_path: Path | None
    label: str
    set_object: object | None = None      # BaseObject when the source is a Set.BAS object
    set_archive: SetBasArchive | None = None
    set_bytes: bytes | None = None


def resolve_source(spec: str, set_hint: str = "") -> Source:
    """Turn 'set1:77', 'set1:VP_GLID2' or a BASE path into a loadable Source.

    A plain path may be absolute or relative to the game Data folder. For loose BASE files the
    Set.BAS used for missing textures comes from set_hint ('set1'), default Set1.
    """
    match = re.fullmatch(r"(set\d+|\d+):(.+)", spec.strip(), re.I)
    if match:
        entry = _set_info(match[1])
        archive, asset = _setbas(match[1])
        token = match[2].strip()
        name = token
        if token.isdigit():
            vp = int(token)
            rows = [r for r in list_vp(match[1]) if r["vp"] == vp]
            if not rows:
                raise McpToolError(f"{entry['set']} has no VP {vp}")
            name = re.sub(r"\.base$", "", rows[0]["base"], flags=re.I)
        wanted = name.lower()
        candidates = [o for o in asset.all_objects() if o.name.lower() == wanted]
        if not candidates:
            raise McpToolError(f"No object named '{name}' in {entry['set']} Set.BAS (try studio_list_vp)")
        obj = candidates[0]
        out_dir = work_dir() / "extract" / entry["set"]
        out_dir.mkdir(parents=True, exist_ok=True)
        base_path = out_dir / f"{obj.name}.BASE"
        base_path.write_bytes(export_base_object_bytes(archive.data, obj))
        return Source(base_path, Path(entry["set_bas"]), f"{entry['set']}:{obj.name}", obj, archive, archive.data)

    base_path = resolve_existing(spec)
    if base_path.suffix.lower() not in (".base", ".bas"):
        raise McpToolError(f"Expected a .BASE model, got {base_path.name}")
    setbas_path = Path(_set_info(set_hint or "set1")["set_bas"])
    return Source(base_path, setbas_path, base_path.name)


def load_family(source: Source):
    # The resolver caches folder listings per process; this server writes files (new .ANM, edited models) and
    # must see them on the next call, so every load starts from a fresh listing.
    DirectoryIndex.clear_cache()
    return load_asset_family(source.base_path, setbas=source.setbas_path)


# --- inspection --------------------------------------------------------------


def _bbox(points) -> dict:
    if not points:
        return {}
    cols = list(zip(*points))
    return {"min": [min(c) for c in cols], "max": [max(c) for c in cols],
            "size": [max(c) - min(c) for c in cols]}


def describe(source: Source) -> dict:
    family = load_family(source)
    if family.root_object is None:
        raise McpToolError(f"Could not load {source.label}: {'; '.join(family.warnings[:3])}")
    objects = []
    for obj in family.all_objects():
        skeleton = obj.skeleton
        entry = {
            "path": obj.owner_path,
            "name": obj.base_object.name,
            "skeleton": obj.base_object.skeleton_name,
            "points": len(skeleton.points) if skeleton else 0,
            "polygons": len(skeleton.polygons) if skeleton else 0,
            "bbox": _bbox(skeleton.points) if skeleton else {},
            "blocks": [],
        }
        for index, block in enumerate(obj.base_object.ades):
            entry["blocks"].append({
                "index": index,
                "class": block.class_id,
                "texture": block.texture.name if block.texture else None,
                "material": block.describe_polflags(),
                "polygons": sorted({a.poly_id for a in block.atts}) or ([block.ade_poly_id] if block.class_id == "area.class" else []),
            })
        objects.append(entry)
    return {"source": source.label, "base_file": str(source.base_path),
            "objects": objects, "warnings": family.warnings, "checks": [list(c) for c in family.checks]}


def geometry(source: Source, owner_path: str = "", points=None, polygons=None, with_uvs: bool = False,
             precision: int = 3) -> dict:
    """Points and polygons of one object (the first one with a skeleton by default).

    ``points`` / ``polygons`` are selectors (see select.py) that limit what is returned; with_uvs adds the
    UV pairs of every returned polygon."""
    from studio_mcp.select import select_points, select_polygons, view_of_object

    family = load_family(source)
    candidates = [o for o in family.all_objects() if o.skeleton is not None]
    if owner_path:
        candidates = [o for o in candidates if o.owner_path == owner_path]
    if not candidates:
        raise McpToolError("No object with a skeleton found")
    obj = candidates[0]
    skeleton = obj.skeleton
    view = view_of_object(obj)
    uv_of = {}
    if with_uvs:
        for block in obj.base_object.ades:
            if block.uses_atts_only_mapping:
                continue
            for index, entry in enumerate(block.atts):
                if index < len(block.olpl):
                    uv_of[entry.poly_id] = [list(uv) for uv in block.olpl[index]]
    point_ids = select_points(points, view)
    polygon_ids = select_polygons(polygons, view)
    rounded = lambda p: [round(v, precision) for v in p]  # noqa: E731
    result = {
        "object": obj.owner_path, "skeleton": obj.base_object.skeleton_name,
        "points": [rounded(skeleton.points[i]) for i in point_ids],
        "polygons": [{"id": i, "points": list(skeleton.polygons[i]), "block": view.poly_block.get(i),
                      **({"uv": uv_of[i]} if i in uv_of else {})} for i in polygon_ids],
        "bbox": _bbox(skeleton.points),
        "axes": "x right, y down (negative = up), z forward",
    }
    if points is not None:
        result["point_indices"] = point_ids
    return result


def list_models(text_filter: str = "", limit: int = 200) -> list[dict]:
    """Loose .BASE models under Data/Models, as paths usable in the game scripts (Data/...)."""
    root = data_root()
    models_dir = _find_child(root, "Models")
    rows = []
    if models_dir is None:
        return rows
    for path in sorted(models_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".base":
            relative = "Data/" + path.relative_to(root).as_posix()
            if text_filter and text_filter.lower() not in relative.lower():
                continue
            rows.append({"model": relative, "skeletons": sorted(p.name for p in path.parent.glob("*.sklt"))})
            if len(rows) >= limit:
                break
    return rows


# --- writing -----------------------------------------------------------------


def export_model(source: Source, out_dir: str, base_name: str, skeleton_stem: str = "") -> dict:
    """Write a loose BASE + skeleton (single-object Set.BAS source) ready for external-model use.

    The BASE text stores the skeleton name inside nested chunk sizes, so the new skeleton stem
    must have exactly the same length as the old one (e.g. VPWGLID2 -> VPKGLID2).
    """
    if source.set_object is None:
        raise McpToolError("studio_export_model needs a Set.BAS source such as 'set1:VPwGLID2'")
    obj = source.set_object
    old_skeleton = obj.skeleton_name.replace("\\", "/")
    old_stem = Path(old_skeleton).stem
    new_stem = skeleton_stem or old_stem
    if len(new_stem) != len(old_stem):
        raise McpToolError(f"skeleton_stem must have {len(old_stem)} characters like '{old_stem}', got '{new_stem}'")
    if new_stem.lower() == old_stem.lower() and skeleton_stem:
        raise McpToolError("skeleton_stem must differ from the set's skeleton name to avoid clashes")
    target_dir = ensure_writable(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    data = source.base_path.read_bytes()
    old_name = obj.name.encode("latin-1")
    new_name = new_stem.encode("latin-1") if len(new_stem) == len(obj.name) else old_name
    old_path = old_skeleton.encode("latin-1")
    new_path = old_skeleton.replace(old_stem, new_stem).encode("latin-1")
    if old_path not in data:
        raise McpToolError("Skeleton name not found in the extracted BASE (unexpected layout)")
    data = data.replace(old_path, new_path).replace(old_name, new_name)
    base_file = target_dir / (base_name if base_name.lower().endswith(".base") else base_name + ".BASE")
    base_file.write_bytes(data)

    archive = source.set_archive
    resources = archive.find(Path(old_skeleton).name, "sklt.class")
    if not resources:
        raise McpToolError(f"Skeleton {old_skeleton} not found in the Set.BAS")
    sklt_file = target_dir / f"{new_stem}.sklt"
    sklt_file.write_bytes(archive.payload_bytes(resources[0]))
    from studio_mcp import history
    history.clear(base_file)   # the files were just re-created: old undo snapshots no longer belong to them
    check = parse_base_file(base_file)
    return {"base_file": str(base_file), "skeleton_file": str(sklt_file),
            "object": check.root.name if check.root else None, "warnings": check.warnings,
            "use_in_cfg": "Data/" + base_file.relative_to(data_root()).as_posix() if _is_under(base_file, data_root()) else str(base_file)}


# Small Set1 vehicles used as an empty starting shell, one per skeleton-name length (the BASE stores that length).
NEW_MODEL_DONORS = {6: "set1:VP_MIG", 7: "set1:VP_JET1", 8: "set1:VP_FLSML"}


def new_model(out_dir: str, base_name: str, skeleton_stem: str) -> dict:
    """An empty loose model (no polygons, plain material blocks kept) to build a new design from scratch."""
    donor = NEW_MODEL_DONORS.get(len(skeleton_stem))
    if donor is None:
        raise McpToolError(f"skeleton_stem must have 6, 7 or 8 characters (e.g. 'VPNBOMB1'), got '{skeleton_stem}'")
    result = export_model(resolve_source(donor), out_dir, base_name, skeleton_stem)
    from studio_mcp import edit, history
    edit.run(result["base_file"], [{"op": "clear"}], label="new empty model")
    history.clear(Path(result["base_file"]))   # the empty shell is the first state: undo never goes back to the donor
    result["note"] = ("Empty model ready. Build with loft / create_primitive / add_polygons / import_geometry "
                      "(swatch='faction.part' paints, see studio_swatches), then auto_shade.")
    return result


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# --- validation ----------------------------------------------------------------


def validate(source: Source) -> dict:
    """Structure checks that do not need rendering (render_worker adds the render check)."""
    problems, notes = [], []
    family = load_family(source)
    if family.root_object is None:
        raise McpToolError(f"Could not load {source.label}")
    for warning in family.warnings:
        if "No CMAP palette" in warning or "external palette" in warning:
            continue  # normal: palette comes from the set
        if "exists both loose" in warning:
            # Studio searches the parent folder too; the engine only looks beside the BASE
            # (and its own subfolders), so this is information, not an error.
            notes.append("Studio resolver note: " + warning)
            continue
        problems.append(warning)
    for obj in family.all_objects():
        if obj.skeleton is None:
            problems.append(f"{obj.owner_path}: skeleton '{obj.base_object.skeleton_name}' not found")
            continue
        covered = set()
        for block in obj.base_object.ades:
            covered.update(a.poly_id for a in block.atts)
            if block.class_id == "area.class" and not block.atts:
                covered.add(block.ade_poly_id)
        missing = [i for i, poly in enumerate(obj.skeleton.polygons) if len(poly) >= 3 and i not in covered]
        if missing:
            problems.append(f"{obj.owner_path}: polygons without material mapping: {missing[:12]}")
        bad = [i for i, poly in enumerate(obj.skeleton.polygons) if any(v >= len(obj.skeleton.points) for v in poly)]
        if bad:
            problems.append(f"{obj.owner_path}: polygons using missing points: {bad[:12]}")
        notes.append(f"{obj.owner_path}: {len(obj.skeleton.points)} points, {len(obj.skeleton.polygons)} polygons")
        if source.set_object is None and obj.skeleton_ref is not None:
            skeleton_dir = Path(str(obj.skeleton_ref.path)).parent if getattr(obj.skeleton_ref, "path", None) else None
            if skeleton_dir and skeleton_dir.resolve() != source.base_path.parent.resolve():
                notes.append(f"{obj.owner_path}: skeleton resolved from {skeleton_dir}, not beside the BASE")
    return {"source": source.label, "ok": not problems, "problems": problems, "notes": notes}


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
