"""Local game resource locations. No game content is copied or stored here."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


FOLDERS = ("briefings", "skies", "music", "movies", "sets", "scripts",
           "levels", "models", "interface")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".iff", ".ilbm", ".lbm", ".vbmp"}
MUSIC_SUFFIXES = {".ogg"}
MOVIE_SUFFIXES = {".mpg", ".mpeg", ".avi", ".mp4", ".mkv", ".webm"}
SCRIPT_SUFFIXES = {".cfg", ".ini", ".lua", ".sdf", ".scr"}


_SET_NAME = re.compile(r"set(\d+)", re.I)


def _has_loose_sets(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_dir() and _SET_NAME.fullmatch(p.name) for p in folder.iterdir())


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
    preview_backgrounds: dict[str, Path] = field(default_factory=dict)
    # SetN folders picked by hand (Change Set > Browse), kept for the session.
    custom_sets: dict[int, Path] = field(default_factory=dict)

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
        # Vanilla keeps every set as a loose SetN folder, with no parent "Sets" folder.
        if not folders["sets"].is_dir() and _has_loose_sets(data):
            folders["sets"] = data
        missing = {key for key, path in folders.items() if not path.is_dir()}
        if missing and data.is_dir():
            aliases = {"briefings": "mbpix", "skies": "objects", "music": "music",
                       "movies": "mov", "sets": "sets", "scripts": "scripts",
                       "levels": "levels", "models": "models", "interface": "interface"}
            candidates = {key: [] for key in missing}
            ignored = {".git", ".codex", "build", "dist", "outputs", "__pycache__", "node_modules"}
            for directory, children, files in os.walk(data):
                children[:] = [name for name in children if name.casefold() not in ignored]
                path = Path(directory)
                names = {name.casefold() for name in files}
                for key in missing:
                    named = path.name.casefold() == aliases[key]
                    marker = key == "scripts" and bool(names & {"startup.cfg", "startup.scr", "vehicles.cfg", "weapons.cfg"})
                    if named or marker:
                        candidates[key].append(path)
            for key, paths in candidates.items():
                if paths:
                    folders[key] = min(paths, key=lambda path: (len(path.relative_to(data).parts), str(path).casefold()))
        return cls(data, folders)

    def folder(self, name: str) -> Path:
        return self.folders.get(name, self.data)

    def valid(self) -> bool:
        return self.data.is_dir() and self.folder("sets").is_dir()

    def set_directories(self) -> dict[int, Path]:
        """Map set number -> folder, for both the modern and the vanilla layout.

        The "sets" folder may hold SetN subfolders (modern), be the game root
        holding loose SetN folders (vanilla), or be a single SetN folder.
        Only sets with both sector data and a VP archive are returned. Sets
        added by hand replace an installed set with the same number.
        """
        found = self._installed_set_directories()
        found.update(self.custom_sets)
        return found

    def register_set(self, folder) -> int:
        """Add a hand-picked ``SetN`` folder and return its number.

        Raises ValueError with a readable reason when it is not a usable set.
        """
        path = Path(folder).expanduser().resolve()
        match = _SET_NAME.fullmatch(path.name)
        if match is None or not 1 <= int(match[1]) <= 255:
            raise ValueError(
                "The folder must be named Set<number>, for example Set5.")
        if not (_child(path, "Objects", "SET.BAS").is_file()
                and _child(path, "Scripts", "set.sdf").is_file()):
            raise ValueError(
                "A set folder needs Objects/SET.BAS and Scripts/set.sdf.")
        number = int(match[1])
        installed = self._installed_set_directories().get(number)
        if installed is not None and installed.resolve() == path:
            self.custom_sets.pop(number, None)
        else:
            self.custom_sets[number] = path
        return number

    def _installed_set_directories(self) -> dict[int, Path]:
        sets = self.folder("sets")
        candidates = []
        for root in (sets, self.data):
            if not root.is_dir():
                continue
            if _SET_NAME.fullmatch(root.name):
                candidates.append(root)
            candidates.extend(p for p in sorted(root.iterdir()) if p.is_dir())
        found: dict[int, Path] = {}
        for path in candidates:
            match = _SET_NAME.fullmatch(path.name)
            if (match and 1 <= int(match[1]) <= 255 and int(match[1]) not in found
                    and _child(path, "Objects", "SET.BAS").is_file()
                    and _child(path, "Scripts", "set.sdf").is_file()):
                found[int(match[1])] = path
        return found

    def available_sets(self) -> list[int]:
        """Return only installed sets with both sector data and a VP archive."""
        return sorted(self.set_directories())

    def inventory(self) -> dict[str, int]:
        def count(folder, predicate):
            root = self.folder(folder)
            return sum(1 for path in root.iterdir() if path.is_file() and predicate(path)) if root.is_dir() else 0

        ready_sets = len(self.available_sets())

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
        return {"data": str(self.data), "folders": {key: str(self.folder(key)) for key in FOLDERS},
                "preview_backgrounds": {key: str(path) for key, path in self.preview_backgrounds.items()}}

    @classmethod
    def from_json(cls, payload: dict) -> "GameInstallation":
        data = Path(payload["data"])
        suggested = cls.suggest(data)
        folders = {key: Path(payload.get("folders", {}).get(key, suggested.folder(key))) for key in FOLDERS}
        return cls(data, folders, {key: Path(value) for key, value in payload.get("preview_backgrounds", {}).items()})


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
