"""Checks and installs a custom sky picked inside the game's Data folder.

Archives use the legacy ``objects/<name>.bas`` level reference. OpenNeoUA also accepts a
complete external BASE family through a canonical ``Data/...`` reference.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import bootstrap

NAME_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")


@dataclass(frozen=True)
class CustomSky:
    stem: str = ""
    archive: Path | None = None      # the chosen .bas archive or .base file
    needs_copy: bool = False         # the archive is not yet in Data/Objects
    problems: tuple = ()
    notes: tuple = ()
    external_value: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.stem) and not self.problems

    @property
    def level_value(self) -> str:
        return self.external_value or f"objects/{self.stem}.bas"


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _archive_problems(archive: Path) -> tuple[list[str], int]:
    """Problems of a sky archive and its texture count."""
    bootstrap.ensure_studio_on_path()
    from studio.formats.setbas import read_setbas

    try:
        resources = read_setbas(archive).resources
    except Exception as exc:
        return [f"{archive.name} is not a readable archive ({exc})."], 0
    textures = sum(r.class_id.casefold() == "ilbm.class" for r in resources)
    skeletons = sum(r.class_id.casefold() == "sklt.class" for r in resources)
    problems = []
    if not textures:
        problems.append("The archive has no sky texture (ilbm.class).")
    if not skeletons:
        problems.append("The archive has no sky skeleton (sklt.class).")
    return problems, textures


def _family_problems(path: Path) -> list[str]:
    """Check the actual BASE and all referenced skeletons and textures with Studio's loader."""
    from studio.assets.family import load_asset_family
    from studio.formats.setbas import read_setbas
    try:
        family = load_asset_family(path, setbas=read_setbas(path), isolated_root=path.parent)
    except Exception as exc:
        return [f'The sky cannot be loaded: {exc}']
    # Palette and remap tables belong to the map's set, not to the sky family.
    # The preview already receives that same active-set palette from the catalog.
    set_profile = {'palette', 'shader_remap', 'tracy_remap'}
    problems = [f'Missing or invalid {dep.kind}: {dep.raw_ref}' for dep in family.dependencies
                if dep.kind not in set_profile
                and dep.status in ('missing', 'ambiguous', 'failed_load', 'unresolved')]
    if not any(obj.skeleton is not None and obj.skeleton.polygons for obj in family.all_objects()):
        problems.append('The sky has no usable skeleton geometry.')
    if not family.textures:
        problems.append('The sky has no usable texture.')
    return problems


def inspect_custom_sky(selection, data_dir, objects_dir) -> CustomSky:
    """Validate a chosen ``.bas`` archive or ``.base`` family; nothing is written."""
    selection, data_dir, objects_dir = Path(selection), Path(data_dir), Path(objects_dir)
    if not selection.exists():
        return CustomSky(problems=("The chosen path does not exist.",))
    if not _inside(selection, data_dir):
        return CustomSky(problems=("Choose a sky file inside the game's Data folder.",))
    if not selection.is_file() or selection.suffix.casefold() not in ('.bas', '.base'):
        return CustomSky(problems=("Choose a .bas archive or a complete .base sky file.",))
    if selection.suffix.casefold() == '.base':
        return CustomSky(
            stem=selection.stem, archive=selection, problems=tuple(_family_problems(selection)),
            notes=('External BASE skies require OpenNeoUA; original Urban Assault does not resolve this Data/... reference.',),
            external_value='Data/' + selection.resolve().relative_to(data_dir.resolve()).as_posix())
    notes = []
    archive = selection
    stem = archive.stem
    problems, _textures = _archive_problems(archive)
    if not problems:
        problems.extend(_family_problems(archive))
    if not NAME_PATTERN.fullmatch(stem):
        problems.append("Rename the file using only letters, digits, - and _ (the level stores its name).")
    target = objects_dir / f"{stem}.bas"
    needs_copy = not (archive.resolve() == target.resolve() if target.exists() else False)
    if needs_copy and target.exists():
        if target.stat().st_size == archive.stat().st_size and target.read_bytes() == archive.read_bytes():
            needs_copy = False
        else:
            problems.append(f"A different sky named {stem}.bas already exists in Data/Objects; rename this file.")
    if needs_copy and not problems:
        notes.append(f"It will be copied to Data/Objects/{stem}.bas, the only place the game reads skies from.")
    if not problems:
        notes.append(f"Original Urban Assault finds it only as Data/Objects/{stem}.bas: ship that file with the level.")
    return CustomSky(stem=stem, archive=archive, needs_copy=needs_copy and not problems,
                     problems=tuple(problems), notes=tuple(notes))


def install_custom_sky(sky: CustomSky, objects_dir) -> Path:
    """Copy the archive into Data/Objects when needed (never overwriting) and return its path."""
    if not sky.valid or sky.archive is None:
        raise ValueError("The sky is not valid.")
    if sky.external_value:
        return sky.archive
    target = Path(objects_dir) / f"{sky.stem}.bas"
    if sky.needs_copy:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"{target.name} already exists.")
        shutil.copy2(sky.archive, target)
    return target
