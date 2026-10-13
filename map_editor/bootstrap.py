"""Where the Map Editor finds the game installation and the Studio libraries.

``set_installation`` / ``installation`` hold the chosen game folders for the whole process,
``game_data_dir`` / ``set_dir`` resolve paths inside them, ``find_ci`` looks files up
case-insensitively and ``ensure_studio_on_path`` makes the ``studio`` package importable.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_DEFAULT_STUDIO = ROOT.parent
_installation = None


def set_installation(installation) -> None:
    global _installation
    _installation = installation


def installation():
    return _installation


def studio_dir() -> Path:
    return Path(os.environ.get("NME_STUDIO_DIR") or _DEFAULT_STUDIO)


def game_data_dir() -> Path:
    if _installation is not None:
        return _installation.data
    override = os.environ.get("NME_GAME_DATA")
    if override:
        return Path(override)
    raise RuntimeError("Choose a game installation before loading game resources")


def ensure_studio_on_path() -> Path:
    path = studio_dir()
    if not getattr(sys, "frozen", False) and not (path / "studio" / "formats" / "setbas.py").is_file():
        raise RuntimeError(
            f"OpenNeoUA Studio resource readers not found in {path}.")
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)
    return path


def set_dir(set_number: int) -> Path:
    if _installation is not None:
        found = _installation.set_directories().get(set_number)
        if found is not None:
            return found
    base = _installation.folder("sets") if _installation is not None else game_data_dir() / "Sets"
    if base.is_dir():
        for path in base.iterdir():
            if path.is_dir() and path.name.casefold() == f"set{set_number}":
                return path
    return base / f"Set{set_number}"


def find_ci(directory: Path, *names: str) -> Path | None:
    if not directory.is_dir():
        return None
    wanted = {n.lower() for n in names}
    for entry in directory.iterdir():
        if entry.name.lower() in wanted:
            return entry
    return None
