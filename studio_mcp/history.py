"""Undo / redo for MCP edits: a copy of the model files is taken before every write.

Snapshots live in the MCP work folder (never inside the game Data folder), one stack per BASE file.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from studio_mcp import core

MAX_ENTRIES = 40


def _stack_root(base_path: Path) -> Path:
    key = hashlib.sha1(str(base_path.resolve()).lower().encode("utf-8")).hexdigest()[:12]
    return core.work_dir() / "history" / key


def _entries(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")[:40] or "edit"


def _next_name(root: Path, label: str) -> Path:
    existing = _entries(root)
    number = int(existing[-1].name[:4]) + 1 if existing else 1
    return root / f"{number:04d}_{_slug(label)}"


def _store(root: Path, files: list[Path], label: str) -> Path:
    target = _next_name(root, label)
    target.mkdir(parents=True)
    manifest = {"label": label, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
    for index, source in enumerate(files):
        stored = f"{index}_{source.name}"
        shutil.copy2(source, target / stored)
        manifest["files"].append({"stored": stored, "target": str(source)})
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    for old in _entries(root)[:-MAX_ENTRIES]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def _restore(entry: Path) -> list[str]:
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    restored = []
    for item in manifest["files"]:
        target = core.ensure_writable(item["target"])
        shutil.copy2(entry / item["stored"], target)
        restored.append(str(target))
    return restored


def clear(base_path: Path) -> None:
    """Forget the undo/redo stacks of a model (used when the model files are re-created from scratch)."""
    shutil.rmtree(_stack_root(base_path), ignore_errors=True)


def snapshot(base_path: Path, files: list[Path], label: str) -> str:
    """Save the current files before an edit; a new edit clears the redo stack."""
    root = _stack_root(base_path)
    shutil.rmtree(root / "redo", ignore_errors=True)
    _store(root / "undo", files, label)
    return label


def _paths_of(entry: Path) -> list[Path]:
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    return [Path(item["target"]) for item in manifest["files"]]


def undo(base_path: Path, steps: int = 1) -> dict:
    root = _stack_root(base_path)
    done = []
    for _ in range(max(1, steps)):
        entries = _entries(root / "undo")
        if not entries:
            break
        latest = entries[-1]
        manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        _store(root / "redo", _paths_of(latest), manifest["label"])
        _restore(latest)
        shutil.rmtree(latest, ignore_errors=True)
        done.append(manifest["label"])
    if not done:
        raise core.McpToolError("Nothing to undo for this model")
    return {"undone": done, "undo_left": len(_entries(root / "undo")), "redo_available": len(_entries(root / "redo"))}


def redo(base_path: Path, steps: int = 1) -> dict:
    root = _stack_root(base_path)
    done = []
    for _ in range(max(1, steps)):
        entries = _entries(root / "redo")
        if not entries:
            break
        latest = entries[-1]
        manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
        _store(root / "undo", _paths_of(latest), manifest["label"])
        _restore(latest)
        shutil.rmtree(latest, ignore_errors=True)
        done.append(manifest["label"])
    if not done:
        raise core.McpToolError("Nothing to redo for this model")
    return {"redone": done, "undo_left": len(_entries(root / "undo")), "redo_available": len(_entries(root / "redo"))}


def listing(base_path: Path) -> dict:
    root = _stack_root(base_path)

    def describe(entries):
        rows = []
        for entry in entries:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            rows.append({"label": manifest["label"], "time": manifest["time"]})
        return rows

    return {"undo": describe(_entries(root / "undo")), "redo": describe(_entries(root / "redo")),
            "note": "Newest entry is last. studio_undo restores the state before the newest undo entry."}
