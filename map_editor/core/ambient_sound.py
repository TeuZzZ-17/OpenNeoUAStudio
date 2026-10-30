"""Level ambient sound (`ambient_sound` in begin_level): a file or a folder inside the game's Data folder.

The engine (yw_ambient.cpp) reads the value as `data:<path>`. A folder means one of its audio files
is picked at random every time the level starts, and it loops. The value must stay inside Data.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from studio.exchange.portable import find_existing_path

# Same list as yw_IsAmbientAudioFile in the engine.
AUDIO_SUFFIXES = {'.wav', '.ogg', '.mp3', '.flac', '.opus', '.m4a', '.aac', '.wma'}
DEFAULT_FOLDER = ('Sounds', 'New', 'Ambient')


def ambient_sound_value(value) -> str:
    """The path as written in the level: forward slashes, no trailing slash, '' when unusable."""
    text = str(value or '').strip().replace('\\', '/').rstrip('/')
    if any(c in text for c in '\r\n;'):
        return ''
    return text


def ambient_candidates(data: Path, value: str) -> list[Path]:
    """Audio files the engine could play for this value (the file itself, or the folder's audio files)."""
    text = ambient_sound_value(value)
    if not text:
        return []
    path = find_existing_path(Path(data), text)
    if path is None:
        return []
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted((p for p in path.iterdir() if p.is_file() and p.suffix.casefold() in AUDIO_SUFFIXES),
                      key=lambda p: p.name.casefold())
    return []


def relative_to_data(data: Path, path: Path) -> str | None:
    """Level value for a path inside Data, or None when it lies outside."""
    try:
        return Path(path).resolve().relative_to(Path(data).resolve()).as_posix()
    except ValueError:
        return None


def default_folder(data: Path, create: bool = False) -> Path | None:
    """Sounds/New/Ambient as spelled on disk (matched without case, so Linux finds `sounds/new/ambient`).

    With `create`, missing parts are created using the default spelling.
    """
    current = Path(data)
    for part in DEFAULT_FOLDER:
        found = find_existing_path(current, part)
        if found is None:
            if not create:
                return None
            found = current / part
            found.mkdir(exist_ok=True)
        current = found
    return current


def import_into_data(data: Path, source: Path) -> str:
    """Copy a sound from outside Data into Sounds/New/Ambient and return its level value.

    Raises FileExistsError when a different file with the same name is already there.
    """
    folder = default_folder(data, create=True)
    target = folder / Path(source).name
    if target.exists():
        if target.stat().st_size != Path(source).stat().st_size:
            raise FileExistsError(target.name)
    else:
        shutil.copy2(source, target)
    return relative_to_data(data, target)
