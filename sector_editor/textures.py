"""Custom set textures written where the engine looks first: SetN/Loose/ILBM."""

from __future__ import annotations

from pathlib import Path
import shutil

from map_editor import bootstrap
from studio.formats.images import (
    IlbmImage, TextureConvertError, _qimage_to_indices, parse_ilbm_file,
    parse_pal_file, write_image_as_ilbm,
)
from studio.formats.setbas import decode_texture
from studio.exchange.portable import backup_existing_file

ILBM_SUFFIXES = {".ilbm", ".ilb", ".iff"}
IMAGE_SUFFIXES = {".png", ".bmp", ".jpg", ".jpeg", ".tga", ".tif", ".tiff"}
# The engine remaps these HI effect sheets per sky; they are not set textures.
SKIPPED_NAMES = {"fx1", "fx2", "fx3"}
# Runtime Loose overrides are read only for these sets (IFFile setLooseCurrentSetId).
LOOSE_SETS = range(1, 8)


def set_textures(archive):
    """Embedded set textures by lower-case stem: STA1 -> resource."""
    textures = {}
    for resource in archive.resources if archive is not None else ():
        if resource.class_id.casefold() == "ilbm.class":
            stem = Path(resource.resource_name.replace("\\", "/")).stem.casefold()
            if stem not in SKIPPED_NAMES:
                textures.setdefault(stem, resource)
    return textures


def texture_files(paths):
    """Expand chosen files and folders into image files, without subfolders."""
    found = []
    for path in map(Path, paths):
        for item in (sorted(path.iterdir()) if path.is_dir() else [path]):
            if item.is_file() and item.suffix.casefold() in ILBM_SUFFIXES | IMAGE_SUFFIXES:
                found.append(item)
    return found


def _indexed_image(source, width, height, palette):
    """Fit any readable image to the original size and map it to the set palette."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    image = QImage(str(source))
    if image.isNull():
        raise TextureConvertError(f"{source.name} cannot be read as an image.")
    if (image.width(), image.height()) != (width, height):
        image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    pixels, _nearest = _qimage_to_indices(image, palette)
    return IlbmImage(source_name=str(source), kind="ILBM", width=width, height=height,
                     palette=palette, pixels=pixels)


def import_textures(set_folder, archive, files):
    """Write each file that has a vanilla texture name; return (written, skipped).

    ILBM files are checked and copied. Other images are fitted to the
    original size and converted to the set palette (Palette/STANDARD.PAL),
    so the game and Studio show them with the set colors.
    """
    textures = set_textures(archive)
    palettes = bootstrap.find_ci(Path(set_folder), "Palette")
    pal_path = bootstrap.find_ci(palettes, "STANDARD.PAL") if palettes else None
    palette = parse_pal_file(pal_path) if pal_path else None
    loose = bootstrap.find_ci(Path(set_folder), "Loose") or Path(set_folder) / "Loose"
    folder = bootstrap.find_ci(loose, "ILBM") or loose / "ILBM"
    written, skipped = [], []
    for source in texture_files(files):
        resource = textures.get(source.stem.casefold())
        if resource is None:
            skipped.append(f"{source.name}: no texture with this name in the set")
            continue
        name = Path(resource.resource_name.replace("\\", "/")).name
        target = bootstrap.find_ci(folder, name) or folder / name
        try:
            if source.suffix.casefold() in ILBM_SUFFIXES:
                parse_ilbm_file(source)
                folder.mkdir(parents=True, exist_ok=True)
                backup_existing_file(target)
                shutil.copyfile(source, target)
            elif palette is None:
                raise TextureConvertError("Palette/STANDARD.PAL is missing in this set.")
            else:
                original = decode_texture(archive, resource)
                image = _indexed_image(source, original.width, original.height, palette)
                write_image_as_ilbm(image, target, source=source)
            written.append(target)
        except (OSError, ValueError, TextureConvertError) as exc:
            skipped.append(f"{source.name}: {exc}")
    return written, skipped
