"""Local game resource locations. No game content is copied or stored here.

The person only says where the game is installed. From there Studio finds the
data folder, recognises which game it is (OpenNeoUA, the original Urban Assault
or its expansion) and catalogues every resource folder, whatever the letter case
and the file formats of that edition. ``report`` says what was really found.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from studio.exchange.portable import find_existing_path


FOLDERS = ("briefings", "skies", "music", "movies", "sets", "scripts",
           "levels", "models", "graphics", "collisions")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".iff", ".ilbm", ".ilb", ".lbm", ".vbmp"}
MUSIC_SUFFIXES = {".ogg"}
MOVIE_SUFFIXES = {".mpg", ".mpeg", ".avi", ".mp4", ".mkv", ".webm"}
SCRIPT_SUFFIXES = {".cfg", ".ini", ".lua", ".sdf", ".scr"}

OPENNEOUA, VANILLA, EXPANSION = "openneoua", "vanilla", "expansion"
EDITION_TITLES = {OPENNEOUA: "OpenNeoUA",
                  VANILLA: "Urban Assault (original)",
                  EXPANSION: "Urban Assault: Metropolis Dawn (expansion)"}
FOLDER_TITLES = {"briefings": "Briefings", "skies": "Skies", "music": "Music",
                 "movies": "Movies", "sets": "Sets", "scripts": "Scripts",
                 "levels": "Levels", "models": "Models", "graphics": "Graphics",
                 "collisions": "Collisions"}
# The original game has no such folders: they are not missing there.
OPENNEOUA_ONLY = frozenset({"models", "graphics", "collisions"})
# Studio cannot work without these; everything else only disables one feature.
REQUIRED_FOLDERS = frozenset({"sets", "scripts"})

READY, MISSING, REQUIRED_MISSING, NOT_IN_EDITION = "ready", "missing", "required", "not_in_edition"
# Folders that never hold a game: looking inside them only finds false matches.
_IGNORED_FOLDERS = {".git", ".codex", "build", "dist", "outputs", "__pycache__", "node_modules"}


_SET_NAME = re.compile(r"set(\d+)", re.I)


def _has_loose_sets(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_dir() and _SET_NAME.fullmatch(p.name) for p in folder.iterdir())


def _child(parent: Path, *names: str) -> Path:
    """Return the existing path ignoring case, or the plain join when it is missing."""
    return find_existing_path(parent, *names) or Path(parent).joinpath(*names)


def _first_dir(bases, *names: str) -> Path:
    """Return the first existing ``base/names`` folder, or the join on the first base."""
    for base in bases:
        found = find_existing_path(base, *names)
        if found is not None and found.is_dir():
            return found
    return Path(bases[0]).joinpath(*names)


def _game_root(data: Path) -> Path:
    """Folder that holds ``Data``; the vanilla layout keeps Levels and Music there."""
    return data.parent if data.name.casefold() in ("data", "dataxp") else data


def _holds_sets(folder: Path) -> bool:
    """True for a game data folder: SetN folders inside it or inside its Sets folder."""
    return _has_loose_sets(folder) or _has_loose_sets(_child(folder, "Sets"))


def _data_child(folder: Path) -> Path | None:
    """The data folder directly inside ``folder``, if it really holds sets."""
    for name in ("Data", "Dataxp"):
        found = find_existing_path(folder, name)
        if found is not None and found.is_dir() and _holds_sets(found):
            return found
    return None


def locate_data(chosen: str | Path) -> Path:
    """Find the data folder from any folder the person points at.

    The game folder, the data folder itself, a folder inside it, or a folder
    around the game (the expansion sits in ``<name>/<name>/Game``) all work.
    """
    chosen = Path(chosen).expanduser().resolve()
    if chosen.name.casefold() == "sets" or _SET_NAME.fullmatch(chosen.name):
        chosen = chosen.parent.parent if chosen.parent.name.casefold() == "sets" else chosen.parent
    found = _data_child(chosen)
    if found is not None:
        return found
    if _holds_sets(chosen):
        return chosen
    for parent in chosen.parents[:2]:
        if parent.name.casefold() != "sets" and _holds_sets(parent):
            return parent

    level = [chosen]
    for _ in range(3):
        below = []
        for folder in level:
            try:
                below += [path for path in sorted(folder.iterdir())
                          if path.is_dir() and path.name.casefold() not in _IGNORED_FOLDERS]
            except OSError:
                continue
        for folder in below:
            found = _data_child(folder)
            if found is not None:
                return found
        level = below

    # Nothing usable yet: keep the plain answer, so a new or empty game folder still opens.
    for name in ("Data", "Dataxp"):
        found = find_existing_path(chosen, name)
        if found is not None and found.is_dir():
            return found
    return chosen


def detect_edition(data: Path) -> str:
    """Which game lives here. The file names decide, never the name of the folder."""
    root = _game_root(data)
    if data.name.casefold() == "dataxp" or find_existing_path(root, "Ua_xp.exe") is not None:
        return EXPANSION
    if (find_existing_path(root, "OpenNeoUA.exe") is not None
            or any(_child(data, name).is_dir() for name in ("Sets", "Models", "Graphics"))):
        return OPENNEOUA
    return VANILLA


@dataclass(frozen=True)
class ResourceStatus:
    """What Studio really found for one kind of resource."""

    key: str
    title: str
    state: str
    detail: str
    path: Path | None


@dataclass
class GameInstallation:
    data: Path
    folders: dict[str, Path] = field(default_factory=dict)
    preview_backgrounds: dict[str, Path] = field(default_factory=dict)
    # SetN folders picked by hand (Change Set > Browse), kept for the session.
    custom_sets: dict[int, Path] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        """The game installation folder: the one holding the program and Data."""
        return _game_root(self.data)

    @property
    def edition(self) -> str:
        return detect_edition(self.data)

    @classmethod
    def suggest(cls, chosen: str | Path) -> "GameInstallation":
        """Guess every resource folder from the game folder or its Data folder.

        Like the engine (uaResolvePath in common/utils.cpp), Levels and Music
        are read from Data first and then from the game root, where the
        vanilla layout keeps them. Folder names are matched without case.
        """
        data = locate_data(chosen)
        bases = (data,) if _game_root(data) == data else (data, _game_root(data))
        edition = detect_edition(data)
        if edition == EXPANSION:
            # The expansion is an add-on: an installed one sits beside the base
            # game and reads from it what it does not replace (skies, music).
            base_game = find_existing_path(_game_root(data), "Data")
            if base_game is not None and base_game.is_dir() and base_game != data:
                bases += (base_game,)
        levels = _first_dir(bases, "Levelsxp" if data.name.casefold() == "dataxp" else "Levels")
        if not levels.is_dir():
            levels = _first_dir(bases, "Levels")
        folders = {
            "briefings": _child(levels, "Mbpix"),
            "skies": _first_dir(bases, "Objects"),
            "music": _first_dir(bases, "Music"),
            "movies": _child(data, "Mov"),
            "sets": _child(data, "Sets"),
            "scripts": _child(data, "Scripts"),
            "levels": levels,
            "models": _child(data, "Models"),
            "graphics": _child(data, "Graphics"),
            "collisions": _child(data, "Collisions"),
        }
        # Vanilla keeps every set as a loose SetN folder, with no parent "Sets" folder.
        if not folders["sets"].is_dir() and _has_loose_sets(data):
            folders["sets"] = data
        missing = {key for key, path in folders.items() if not path.is_dir()
                   and (edition == OPENNEOUA or key not in OPENNEOUA_ONLY)}
        if missing and data.is_dir():
            aliases = {"briefings": "mbpix", "skies": "objects", "music": "music",
                       "movies": "mov", "sets": "sets", "scripts": "scripts",
                       "levels": "levels", "models": "models", "graphics": "graphics",
                       "collisions": "collisions"}
            candidates = {key: [] for key in missing}
            for directory, children, files in os.walk(data):
                # A set's own Objects and Scripts folders are not the game's.
                children[:] = [name for name in children if name.casefold() not in _IGNORED_FOLDERS
                               and not _SET_NAME.fullmatch(name)]
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
            "graphics": count_recursive("graphics", lambda p: p.suffix.casefold() in {".png", ".svg"}),
            "collisions": count_recursive("collisions", lambda p: p.suffix.casefold() == ".collision"),
        }

    def report(self) -> list[ResourceStatus]:
        """Every resource with what was really found, in the order of ``FOLDERS``."""
        counts = self.inventory()
        found = {"briefings": (counts["mb"] + counts["db"], "images"),
                 "skies": (counts["skies"], "skies"), "music": (counts["music"], "tracks"),
                 "movies": (counts["movies"], "movies"), "sets": (counts["sets"], "sets"),
                 "scripts": (counts["scripts"], "scripts"), "levels": (counts["levels"], "levels"),
                 "models": (counts["models"], "models"), "graphics": (counts["graphics"], "images"),
                 "collisions": (counts["collisions"], "shapes")}
        edition = self.edition
        statuses = []
        for key in FOLDERS:
            title, path = FOLDER_TITLES[key], self.folder(key)
            number, unit = found[key]
            if key in OPENNEOUA_ONLY and edition != OPENNEOUA:
                statuses.append(ResourceStatus(key, title, NOT_IN_EDITION, "Not in this game", None))
            elif number:
                statuses.append(ResourceStatus(key, title, READY, f"{number} {unit}", path))
            elif key in REQUIRED_FOLDERS:
                statuses.append(ResourceStatus(key, title, REQUIRED_MISSING, "Required: not found", None))
            else:
                statuses.append(ResourceStatus(key, title, MISSING, "Not found", None))
        return statuses

    def to_json(self) -> dict:
        return {"data": str(self.data), "folders": {key: str(self.folder(key)) for key in FOLDERS},
                "preview_backgrounds": {key: str(path) for key, path in self.preview_backgrounds.items()}}

    @classmethod
    def from_json(cls, payload: dict) -> "GameInstallation":
        data = Path(payload["data"])
        suggested = cls.suggest(data)
        folders = {}
        for key in FOLDERS:
            saved = payload.get("folders", {}).get(key)
            # A remembered folder that no longer exists is guessed again.
            folders[key] = Path(saved) if saved and Path(saved).is_dir() else suggested.folder(key)
        return cls(data, folders, {key: Path(value) for key, value in payload.get("preview_backgrounds", {}).items()})


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
                or Path.home() / ".config")
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
