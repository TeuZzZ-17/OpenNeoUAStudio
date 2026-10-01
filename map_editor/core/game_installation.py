"""Local game resource locations. No game content is copied or stored here."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


FOLDERS = ("briefings", "skies", "music", "movies", "sets", "scripts",
           "levels", "models", "interface")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".iff", ".ilbm", ".lbm", ".vbmp"}
MUSIC_SUFFIXES = {".ogg"}
MOVIE_SUFFIXES = {".mpg", ".mpeg", ".avi", ".mp4", ".mkv", ".webm"}
SCRIPT_SUFFIXES = {".cfg", ".ini", ".lua", ".sdf", ".scr"}


def _child(parent: Path, *names: str) -> Path:
    for name in names:
        if parent.is_dir():
            match = next((p for p in parent.iterdir() if p.name.casefold() == name.casefold()), None)
            if match is not None:
                parent = match
                continue
        parent = parent / name
    return parent


@dataclass
class GameInstallation:
    data: Path
    folders: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def suggest(cls, chosen: str | Path) -> "GameInstallation":
        root = Path(chosen).expanduser().resolve()
        data = _child(root, "Data") if _child(root, "Data").is_dir() else root
        folders = {
            "briefings": _child(data, "Levels", "Mbpix"),
            "skies": _child(data, "Objects"),
            "music": _child(data, "Music"),
            "movies": _child(data, "Mov"),
            "sets": _child(data, "Sets"),
            "scripts": _child(data, "Scripts"),
            "levels": _child(data, "Levels"),
            "models": _child(data, "Models"),
            "interface": _child(data, "Interface"),
        }
        return cls(data, folders)

    def folder(self, name: str) -> Path:
        return self.folders.get(name, self.data)

    def valid(self) -> bool:
        return self.data.is_dir() and self.folder("sets").is_dir()

    def inventory(self) -> dict[str, int]:
        def count(folder, predicate):
            root = self.folder(folder)
            return sum(1 for path in root.iterdir() if path.is_file() and predicate(path)) if root.is_dir() else 0

        sets = self.folder("sets")
        ready_sets = 0
        if sets.is_dir():
            for path in sets.iterdir():
                if path.is_dir() and _child(path, "Objects", "SET.BAS").is_file() \
                        and _child(path, "Scripts", "set.sdf").is_file():
                    ready_sets += 1

        def count_recursive(folder, predicate):
            root = self.folder(folder)
            if not root.is_dir():
                return 0
            return sum(1 for path in root.rglob("*")
                       if path.is_file() and predicate(path))

        levels = self.folder("levels")
        level_count = 0
        if levels.is_dir():
            level_count = sum(
                1 for path in levels.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".ldf")
        models = self.folder("models")
        model_count = 0
        if models.is_dir():
            model_count = sum(
                1 for path in models.rglob("*")
                if path.is_file() and path.suffix.casefold() == ".base")
        interface = self.folder("interface")
        icon_count = 0
        status = interface / "StatusIcons" if interface.is_dir() else None
        if status is not None and status.is_dir():
            icon_count = sum(1 for path in status.iterdir()
                             if path.is_file() and path.suffix.casefold() == ".png")
        return {
            "mb": count("briefings", lambda p: p.stem.casefold().startswith("mb")
                        and p.suffix.casefold() in IMAGE_SUFFIXES),
            "db": count("briefings", lambda p: p.stem.casefold().startswith("db")
                        and p.suffix.casefold() in IMAGE_SUFFIXES),
            "skies": count("skies", lambda p: p.suffix.casefold() == ".bas"),
            "music": count("music", lambda p: p.suffix.casefold() in MUSIC_SUFFIXES),
            "movies": count("movies", lambda p: p.suffix.casefold() in MOVIE_SUFFIXES),
            "sets": ready_sets,
            "scripts": count_recursive("scripts", lambda p: p.suffix.casefold() in SCRIPT_SUFFIXES),
            "levels": level_count,
            "models": model_count,
            "icons": icon_count,
        }

    def to_json(self) -> dict:
        return {"data": str(self.data), "folders": {key: str(self.folder(key)) for key in FOLDERS}}

    @classmethod
    def from_json(cls, payload: dict) -> "GameInstallation":
        data = Path(payload["data"])
        suggested = cls.suggest(data)
        folders = {key: Path(payload.get("folders", {}).get(key, suggested.folder(key))) for key in FOLDERS}
        return cls(data, folders)


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return base / "Sektor3" / "installation.json"


def load_remembered(path: Path | None = None) -> GameInstallation | None:
    path = path or settings_path()
    try:
        install = GameInstallation.from_json(json.loads(path.read_text(encoding="utf-8")))
        return install if install.valid() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_remembered(install: GameInstallation, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(install.to_json(), indent=2), encoding="utf-8")


def forget_remembered(path: Path | None = None) -> None:
    path = path or settings_path()
    path.unlink(missing_ok=True)
