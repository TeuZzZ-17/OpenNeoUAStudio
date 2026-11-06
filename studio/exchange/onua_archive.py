"""The .ONUA archive: one model with every resource it needs, in a single file.

A .ONUA file is a plain ZIP (any archiver such as 7-Zip or WinRAR can open it). Inside there
is exactly one Complete Asset Family package, the same one "Export Model" writes to a folder:
the BASE, its skeletons (SKLT), textures (ILBM), animations (ANM) and
``asset_family_manifest.json``, which lists every file with its SHA-256 hash.

Writing packs a validated package folder. Reading never trusts the archive: member names are
checked, the files are unpacked into a temporary folder, the whole package is validated again
(hashes, dependencies, indexed rendering) and only then copied to the chosen destination.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import os
import shutil
import stat
import tempfile
import zipfile

from studio.assets.family import validate_family_package
from studio.exchange.portable import (
    AssetFamilyPackageError,
    MANIFEST_NAME,
    package_relative_path,
)

ONUA_SUFFIX = ".onua"
FILE_FILTER = "OpenNeoUA archive (*.onua)"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)   # same input -> same archive bytes


def _package_files(manifest: dict) -> list[str]:
    """Manifest first, then every exported file, as relative POSIX names."""
    names = [MANIFEST_NAME]
    names += [entry["exported_path"] for entry in manifest["entries"]
              if entry.get("status") == "exported"]
    return names


def write_onua(package_root, target, *, validation=None) -> dict:
    """Pack a validated Complete Asset Family folder into ``target`` (.onua)."""

    root, target = Path(package_root), Path(target)
    validation = validation or validate_family_package(root)
    if not validation.valid or validation.family is None:
        raise AssetFamilyPackageError(
            "Invalid Asset Family package: " + "; ".join(validation.errors))
    names = _package_files(validation.manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name in names:
                info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (root / name).read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or sorted(archive.namelist()) != sorted(names):
                raise AssetFamilyPackageError("ONUA archive readback failed")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return validation.manifest


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Every member must be a plain relative file; no links, no escapes, no duplicates."""
    seen, members = set(), []
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (info.is_dir() or path.is_absolute() or path.as_posix() != name
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise AssetFamilyPackageError(f"Unsafe member in ONUA archive: {name}")
        if package_relative_path(name, default_name="", extensions=(path.suffix,)).as_posix() != name:
            raise AssetFamilyPackageError(f"Non-canonical member in ONUA archive: {name}")
        if name.casefold() in seen:
            raise AssetFamilyPackageError(f"Duplicate member in ONUA archive: {name}")
        seen.add(name.casefold())
        members.append(info)
    if MANIFEST_NAME.casefold() not in seen:
        raise AssetFamilyPackageError(
            f"Not an OpenNeoUA archive: {MANIFEST_NAME} is missing")
    return members


def import_onua(source, output_root) -> Path:
    """Unpack and validate ``source`` into the empty folder ``output_root``.

    Returns the path of the entry BASE inside ``output_root``. Nothing is written to ``output_root`` unless
    the whole package validates.
    """

    output_root = Path(output_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise AssetFamilyPackageError(
            f"Destination folder is not empty: {output_root}")
    try:
        with zipfile.ZipFile(source) as archive, \
                tempfile.TemporaryDirectory(prefix="OpenNeoUAStudio_onua_") as temp:
            staging = Path(temp)
            for info in _safe_members(archive):
                destination = staging / info.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))
            validation = validate_family_package(staging)
            if not validation.valid:
                raise AssetFamilyPackageError(
                    "Invalid ONUA archive: " + "; ".join(validation.errors))
            entry = validation.manifest["entry_base"]
            output_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staging, output_root, dirs_exist_ok=True)
    except (zipfile.BadZipFile, OSError) as exc:
        raise AssetFamilyPackageError(f"Cannot read ONUA archive: {exc}") from exc
    return output_root / entry
