"""Read-only index and preview decoding for the selected game installation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QImage

from .. import bootstrap
from .game_installation import (GameInstallation, IMAGE_SUFFIXES,
                                MOVIE_SUFFIXES, MUSIC_SUFFIXES)


LEGACY_SUFFIXES = {".iff", ".ilbm", ".lbm", ".vbmp"}


@dataclass(frozen=True)
class SkyResource:
    name: str
    archive: Path
    loose_preview: Path | None = None


class ResourceCatalog:
    def __init__(self, installation: GameInstallation, set_number: int):
        self.installation = installation
        self.palette = self._palette(set_number)
        self.briefings = self._briefings()
        self.skies = self._skies()
        self.music = self._names("music", MUSIC_SUFFIXES)
        self.movies = self._names("movies", MOVIE_SUFFIXES)

    def _briefings(self) -> dict[str, dict[str, Path]]:
        result = {"mb": {}, "db": {}}
        directory = self.installation.folder("briefings")
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES:
                    prefix = path.stem[:2].casefold()
                    if prefix in result:
                        result[prefix][path.name.casefold()] = path
        return result

    def briefing(self, prefix: str, value: str) -> Path | None:
        name = Path(value.replace("\\", "/")).name.casefold()
        found = self.briefings[prefix].get(name)
        if found is not None:
            return found
        stem = Path(name).stem
        return next((path for path in self.briefings[prefix].values()
                     if path.stem.casefold() == stem), None)

    def _skies(self) -> dict[str, SkyResource]:
        directory = self.installation.folder("skies")
        loose = directory / "loose"
        result = {}
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.casefold() == ".bas" and path.stem.casefold() != "set":
                    preview = None
                    sky_loose = loose / path.stem
                    if sky_loose.is_dir():
                        preview = next((p for p in sky_loose.iterdir()
                                        if p.stem.casefold() == "newsky1" and p.suffix.casefold() in IMAGE_SUFFIXES), None)
                    result[path.stem.casefold()] = SkyResource(path.stem, path, preview)
        return dict(sorted(result.items()))

    def _names(self, folder: str, suffixes: set[str]) -> list[str]:
        directory = self.installation.folder(folder)
        if not directory.is_dir():
            return []
        return sorted((p.name for p in directory.iterdir()
                       if p.is_file() and p.suffix.casefold() in suffixes), key=str.casefold)

    @staticmethod
    def _palette(set_number: int):
        bootstrap.ensure_studio_on_path()
        from ilbm_parser import parse_pal_file

        set_path = bootstrap.set_dir(set_number)
        for name in ("NORMAL.PAL", "STANDARD.PAL"):
            path = set_path / "Palette" / name
            if path.is_file():
                return parse_pal_file(path)
        return None


def preview_image(source: Path | SkyResource, palette=None) -> QImage:
    """Reuse decoded local previews; file changes invalidate their cache entry."""
    path = (source.loose_preview or source.archive) if isinstance(source, SkyResource) else source
    stat = path.stat()
    colors = tuple(tuple(c) for c in palette) if palette is not None else None
    return QImage(_cached_preview(source, colors, stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=48)
def _cached_preview(source, palette, _mtime, _size):
    bootstrap.ensure_studio_on_path()
    from ilbm_parser import parse_ilbm_file
    from setbas_reader import decode_texture, read_setbas

    if isinstance(source, SkyResource):
        if source.loose_preview is not None:
            return preview_image(source.loose_preview, palette)
        archive = read_setbas(source.archive)
        candidates = archive.find("NEWSKY1.ILBM", "ilbm.class")
        if not candidates:
            candidates = [r for r in archive.resources if r.class_id.casefold() == "ilbm.class"]
        if not candidates:
            return QImage()
        decoded = decode_texture(archive, candidates[0])
    elif source.suffix.casefold() in LEGACY_SUFFIXES:
        decoded = parse_ilbm_file(source)
    else:
        return QImage(str(source))
    rgba = decoded.to_rgba_bytes(palette_override=palette, alpha_mode="opaque")
    if not rgba:
        return QImage()
    return QImage(rgba, decoded.width, decoded.height, decoded.width * 4,
                  QImage.Format.Format_RGBA8888).copy()


def music_path(installation: GameInstallation, value: str) -> Path | None:
    """The game's ambiencetrack ID resolves to Music/<ID>.ogg."""
    raw = str(value or "").strip()
    if not raw or raw.casefold() == "none":
        return None
    name = Path(raw.replace("\\", "/")).name
    track_id = Path(name).stem if Path(name).suffix.casefold() == ".ogg" else name.split("_", 1)[0]
    directory = installation.folder("music")
    if not directory.is_dir():
        return None
    return next((path for path in directory.iterdir()
                 if path.is_file() and path.name.casefold() == f"{track_id}.ogg".casefold()), None)
