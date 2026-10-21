"""Portable core of the ONUA3D exchange format.

Everything in this file is plain Python (no Qt, no native UA parsers) so the very same
source can be bundled into the Blender add-on by ``tools/build_blender_onua3d.py``.

Sections, in order: filesystem commits, logical asset-name resolver, portable package
paths, ONUA3D constants, vertex identity and the package/GLB validator.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import copy
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import zipfile

# ----------------------------------------------------------------------------
# Coordinated filesystem commits for verified Studio outputs.
# (formerly verified_io.py)
# ----------------------------------------------------------------------------

class VerifiedCommitError(OSError):
    def __init__(self, message: str, *, rollback_complete: bool) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete


def commit_verified_files(
        files: list[tuple[Path, Path]], *,
        verify: Callable[[], None] | None = None,
        replace_existing: bool = True) -> list[str]:
    """Commit pre-validated files with coordinated rollback.

    Sources are copied to staging files beside every destination.  Existing
    destinations are moved aside only after all staging copies succeed when
    ``replace_existing`` is true.  With ``replace_existing=False`` publication
    uses a same-directory hard link so an existing destination can never be
    overwritten, including when it appears between preflight and publication.
    Duplicate destination paths are rejected before any write.  An optional
    final verifier runs while rollback copies are still available.
    """

    destination_keys: set[str] = set()
    normalized: list[tuple[Path, Path]] = []
    for source, target in files:
        source = Path(source)
        target = Path(target)
        key = str(target.resolve(strict=False)).casefold()
        if key in destination_keys:
            raise VerifiedCommitError(
                f"duplicate verified destination: {target}",
                rollback_complete=True)
        destination_keys.add(key)
        if not source.is_file():
            raise VerifiedCommitError(
                f"verified staging source is missing: {source}",
                rollback_complete=True)
        if not replace_existing and (target.exists() or target.is_symlink()):
            raise VerifiedCommitError(
                f"verified destination already exists: {target}",
                rollback_complete=True)
        if target.exists() and not target.is_file() \
                and not target.is_symlink():
            raise VerifiedCommitError(
                f"verified destination exists but is not a file: {target}",
                rollback_complete=True)
        normalized.append((source, target))

    records: list[dict] = []
    created_directories: set[Path] = set()
    try:
        for source, target in normalized:
            missing_parents = []
            parent = target.parent
            while not parent.exists():
                missing_parents.append(parent)
                if parent == parent.parent:
                    break
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            created_directories.update(missing_parents)
            fd, stage_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".stage",
                dir=target.parent)
            os.close(fd)
            record = {
                "target": target,
                "stage": Path(stage_name),
                "backup": None,
                "committed": False,
            }
            records.append(record)
            shutil.copy2(source, record["stage"])

        for record in records:
            target = record["target"]
            if not replace_existing:
                # os.link is the no-clobber publication primitive.  The
                # target may have appeared after preflight; in that case the
                # operation fails atomically and the rollback below removes
                # any earlier files from this batch.
                if target.exists() or target.is_symlink():
                    raise FileExistsError(target)
                os.link(record["stage"], target)
                record["committed"] = True
                # Mark the record committed before unlinking the staging
                # name so an unlink failure still rolls the published file
                # back through the normal cleanup path.
                record["stage"].unlink()
                continue
            # ``Path.exists`` is false for a broken symlink.  It still needs a
            # rollback record before the staged file replaces the link.
            if target.exists() or target.is_symlink():
                fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".rollback",
                    dir=target.parent)
                os.close(fd)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
                record["backup"] = backup
            os.replace(record["stage"], target)
            record["committed"] = True
        if verify is not None:
            verify()
    except Exception as exc:
        rollback_errors = []
        for record in reversed(records):
            target = record["target"]
            backup = record["backup"]
            try:
                if backup is not None and backup.exists():
                    if target.exists():
                        target.unlink()
                    os.replace(backup, target)
                    record["backup"] = None
                elif record["committed"] and target.exists():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        for record in records:
            stage = record["stage"]
            try:
                if stage.exists():
                    stage.unlink()
            except OSError as cleanup_exc:
                rollback_errors.append(f"{stage}: {cleanup_exc}")
        for directory in sorted(
                created_directories,
                key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                rollback_errors.append(f"{directory}: {cleanup_exc}")
        message = str(exc)
        if rollback_errors:
            message += ("\n\nRollback was incomplete; inspect these paths:\n"
                        + "\n".join(rollback_errors))
        raise VerifiedCommitError(
            message, rollback_complete=not rollback_errors) from exc

    warnings = []
    for record in records:
        backup = record["backup"]
        if backup is not None and backup.exists():
            try:
                backup.unlink()
            except OSError as exc:
                warnings.append(
                    "saved, but temporary backup cleanup failed: "
                    f"{backup} ({exc})")
        stage = record["stage"]
        if stage.exists():
            try:
                stage.unlink()
            except OSError as exc:
                warnings.append(
                    f"saved, but staging cleanup failed: {stage} ({exc})")
    return warnings



def same_path(first: Path, second: Path) -> bool:
    """Compare existing files safely, with a fallback for new outputs."""

    try:
        if first.exists() and second.exists():
            return first.samefile(second)
    except OSError:
        pass
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return first.absolute() == second.absolute()


def find_existing_path(parent: Path | str, *parts: str) -> Path | None:
    """Return ``parent/parts`` as spelled on disk, matching each name without case.

    Urban Assault data mixes casings (DATA, Levels, SET.BAS, set.sdf). Windows
    ignores case, Linux and macOS usually do not. A part may hold several
    names separated by ``/`` or ``\\``. An exact spelling wins over a
    case-insensitive one. Returns None when the path does not exist.
    """

    current = Path(parent)
    for part in parts:
        for name in str(part).replace("\\", "/").split("/"):
            if not name or name == ".":
                continue
            folded = name.casefold()
            try:
                entries = [entry for entry in current.iterdir()
                           if entry.name.casefold() == folded]
            except OSError:
                return None
            if not entries:
                return None
            current = next((entry for entry in entries if entry.name == name),
                           entries[0])
    return current if current.exists() else None


# ----------------------------------------------------------------------------
# Non-destructive recursive resolver for Urban Assault asset references.
# (formerly asset_resolver.py)
# ----------------------------------------------------------------------------

EXTENSION_ALIASES: dict[str, tuple[str, ...]] = {
    ".base": (".base", ".bas"),
    ".bas": (".bas", ".base"),
    ".sklt": (".sklt", ".skl"),
    ".skl": (".skl", ".sklt"),
    ".ilbm": (".ilbm", ".ilb", ".lbm", ".iff"),
    ".ilb": (".ilb", ".ilbm", ".lbm", ".iff"),
    ".lbm": (".lbm", ".ilbm", ".ilb", ".iff"),
    ".anm": (".ANM", ".VANM"),
    ".vanm": (".vanm", ".anm"),
    ".pal": (".pal",),
}


# Folder-name hints per resource kind.  These are RANKING hints only (they
# order candidates); they are never required paths and never make an
# ambiguous reference auto-load.
TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "skeleton": ("skeleton",),
    "texture": ("_normal", "_releu", "pictures", "textures"),
    "animation": ("rsrcpool", "projects"),
    "base": ("objects",),
    "palette": ("palette", "_normal"),
    "any": (),
}


# Directories never scanned by the recursive index.
IGNORED_DIRS = {".git", "__pycache__", "build", "dist", ".venv",
                "node_modules", ".idea", ".vscode"}


SETBAS_EXPORT_DIRS = {"raw", "textures_ilbm", "textures_png"}


class DirectoryIndex:
    """One-time recursive scan of a root directory.

    Maps lowercase filenames to every path carrying that name, skipping
    development folders and Amiga ``.info`` sidecars.  Cached per process so
    hundreds of families under the same root share one scan.
    """

    _cache: dict[Path, "DirectoryIndex"] = {}

    def __init__(self, root: Path):
        self.root = root
        self.by_name: dict[str, list[Path]] = {}
        self.by_relative: dict[str, list[Path]] = {}
        self.file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            dirnames[:] = [
                directory for directory in dirnames
                if directory.lower() not in IGNORED_DIRS
                # SET.BAS extraction creates manifest.json beside raw dumps
                # and converted texture previews.  Those inspection outputs
                # are not engine loose overrides and must not shadow archive
                # resources when a broader root is indexed.  Choosing one of
                # these output folders itself as the root still works.
                and not (
                    directory.lower() in SETBAS_EXPORT_DIRS
                    and (current / "manifest.json").is_file()
                )
            ]
            for filename in filenames:
                if filename.lower().endswith(".info"):
                    continue
                path = Path(dirpath) / filename
                self.by_name.setdefault(filename.lower(), []).append(path)
                try:
                    relative = path.relative_to(root).as_posix().casefold()
                    self.by_relative.setdefault(relative, []).append(path)
                except ValueError:
                    pass
                self.file_count += 1

    @classmethod
    def get(cls, root: Path, refresh: bool = False) -> "DirectoryIndex":
        root = Path(root)
        if refresh or root not in cls._cache:
            cls._cache[root] = cls(root)
        return cls._cache[root]

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

    def find_name(self, filename: str) -> list[Path]:
        return list(self.by_name.get(filename.lower(), []))

    def find_relative(self, logical_path: str) -> list[Path]:
        normalized = normalize_logical_name(logical_path).casefold()
        return list(self.by_relative.get(normalized, []))


@dataclass
class ResolvedFile:
    logical_name: str
    # "found" | "missing" | "ambiguous" | "manual" | "setbas" | "manual (SET.BAS)"
    status: str = "missing"
    path: Path | None = None
    candidates: list[Path] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    # where the resource actually came from: "loose" | "manual" | "SET.BAS" | ""
    source: str = ""
    # Stable, user-visible explanation of the rule that produced this result.
    resolution_rule: str = "unresolved"
    source_root: Path | None = None
    # the same logical name also exists as an embedded SET.BAS resource
    embedded_available: bool = False
    # display names of embedded candidates ("SET.BAS:<resource_name>")
    embedded_candidates: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.path is not None or self.status in ("setbas",
                                                        "manual (SET.BAS)")

    @property
    def display_path(self) -> str:
        if self.path is not None:
            return str(self.path)
        if self.status in ("setbas", "manual (SET.BAS)"):
            return self.embedded_candidates[0] if self.embedded_candidates \
                else "SET.BAS (embedded)"
        return "-"

    @property
    def provenance(self) -> dict:
        return {
            "logical_reference": self.logical_name,
            "status": self.status,
            "resolved_path": str(self.path) if self.path else None,
            "source": self.source,
            "source_root": str(self.source_root) if self.source_root else None,
            "resolution_rule": self.resolution_rule,
            "candidates": [str(path) for path in self.candidates],
            "embedded_candidates": list(self.embedded_candidates),
        }


def normalize_logical_name(name: str) -> str:
    """Amiga ``assign:path`` and backslashes both act as ``/``."""

    return name.replace("\\", "/").replace(":", "/").strip("/")


def _rank_candidates(matches: list[Path], file_name: str, kind: str,
                     roots: list[Path]) -> list[Path]:
    """Deterministic ranking: exact filename first, then kind folder hints,
    then earliest root, shallowest path, alphabetical.  Ranking orders the
    candidate list; it never auto-selects among ambiguous candidates."""

    hints = TYPE_HINTS.get(kind, ())

    def root_rank(path: Path) -> int:
        for index, root in enumerate(roots):
            try:
                path.relative_to(root)
                return index
            except ValueError:
                continue
        return len(roots)

    def score(path: Path):
        parts_lower = [p.lower() for p in path.parts]
        hint_rank = next(
            (i for i, hint in enumerate(hints)
             if any(hint == part for part in parts_lower)), len(hints)
        )
        return (
            0 if path.name.lower() == file_name.lower() else 1,
            hint_rank,
            root_rank(path),
            len(path.parts),
            str(path).lower(),
        )

    return sorted(matches, key=score)


# A BASE may reference a resource without extension (e.g. the SET.BAS animation
# "PANZERKETTEN"). When such a resource is exported, the package stores it with the
# default extension, so the resolver must also try that name.
_EXTENSIONLESS_CANDIDATES = {
    "animation": (".ANM", ".VANM"),
    "texture": (".ILBM", ".ILB", ".LBM", ".IFF", ".VBMP"),
}


def _alias_names(file_name: str, kind: str = "any") -> list[str]:
    stem, dot, ext = file_name.rpartition(".")
    if not dot:
        return [file_name] + [
            file_name + suffix
            for suffix in _EXTENSIONLESS_CANDIDATES.get(kind, ())]
    aliases = EXTENSION_ALIASES.get("." + ext.lower(), ("." + ext.lower(),))
    return [stem + alias for alias in aliases]


class AssetResolver:
    """Searches an ordered list of root directories for referenced assets.

    ``overrides`` maps a logical name (case-insensitive) to a user-chosen
    file path; it wins over any directory search.  Overrides are session
    state only — nothing is ever written to disk by the resolver.
    """

    def __init__(self, roots: list[Path | str],
                 overrides: dict[str, Path] | None = None, *,
                 prefer_earliest_root: bool = False):
        self.roots: list[Path] = []
        for root in roots:
            path = Path(root)
            if path.is_file():
                path = path.parent
            if path.is_dir() and path not in self.roots:
                self.roots.append(path)
        # Standalone BASE families use ordered search roots as real fallback
        # layers: a unique dependency beside the BASE (or in the next local
        # package root) must not become ambiguous merely because historical
        # extra roots contain same-named assets. Direct AssetResolver callers
        # retain the conservative all-roots ambiguity behavior by default.
        self.prefer_earliest_root = bool(prefer_earliest_root)
        self.overrides: dict[str, Path] = {}
        for name, target in (overrides or {}).items():
            self.set_override(name, target)

    def set_override(self, logical_name: str, target: Path | str) -> None:
        key = normalize_logical_name(logical_name).lower()
        self.overrides[key] = Path(target)



    def resolve(self, logical_name: str, kind: str = "any") -> ResolvedFile:
        result = ResolvedFile(logical_name=logical_name)
        normalized = normalize_logical_name(logical_name)
        if not normalized:
            result.resolution_rule = "empty logical reference"
            return result

        override = self.overrides.get(normalized.lower())
        if override is None and "/" in normalized:
            # Also accept an override keyed by bare filename.
            override = self.overrides.get(normalized.rsplit("/", 1)[-1].lower())
        if override is not None:
            result.searched.append(f"<manual override: {override}>")
            if override.is_file():
                result.status = "manual"
                result.path = override
                result.candidates = [override]
                result.source = "manual"
                result.source_root = override.parent
                result.resolution_rule = "explicit session override"
                return result
            result.status = "missing"
            result.candidates = []
            result.source = "manual"
            result.resolution_rule = "explicit session override is missing"
            return result

        parts = normalized.split("/")
        file_name = parts[-1]
        has_subpath = len(parts) > 1
        names = _alias_names(file_name, kind)

        # Priority 1: exact relative path as written in the .base
        # ("Skeleton/BP_FLAK1.sklt"), tried against every root.  Applies only
        # to references that actually carry a directory component; a bare
        # filename is not a path and goes to filename matching instead.
        if has_subpath:
            exact_matches: list[tuple[Path, Path]] = []
            for root in self.roots:
                index = DirectoryIndex.get(root)
                for name in names:
                    candidate = root.joinpath(*parts[:-1], name)
                    result.searched.append(str(candidate.parent))
                    if candidate.is_file():
                        exact_matches.append((candidate, root))
                    relative_name = "/".join((*parts[:-1], name))
                    exact_matches.extend(
                        (match, root)
                        for match in index.find_relative(relative_name))
                if exact_matches:
                    # Package/base-local paths have explicit precedence over
                    # later SET roots.  Do not merge a lower-priority root
                    # into an otherwise exact package-local result.
                    break
            if exact_matches:
                unique = []
                for candidate, root in exact_matches:
                    if candidate not in [item[0] for item in unique]:
                        unique.append((candidate, root))
                exact_name = [
                    item for item in unique
                    if item[0].name.casefold() == file_name.casefold()]
                result.candidates = [item[0] for item in unique]
                selected = (exact_name[0] if len(exact_name) == 1
                            else unique[0] if len(unique) == 1 else None)
                if selected is not None:
                    result.status = "found"
                    result.path, result.source_root = selected
                    result.source = "filesystem"
                    result.resolution_rule = (
                        "exact logical path in highest-priority root")
                else:
                    result.status = "ambiguous"
                    result.resolution_rule = (
                        "multiple extension aliases match the exact logical "
                        "path in the same priority root")
                return result

        # Priority 2: for a standalone BASE family, ordered roots are true
        # fallback layers.  This is what makes an exported flat Asset Family
        # self-contained: e.g. ``Skeleton/SHIP.SKLT`` can resolve to the unique
        # ``SHIP.SKLT`` beside the BASE without unrelated copies in old search
        # roots turning it ambiguous.  Ambiguity *inside* the first root that
        # contains matches is still surfaced and never guessed away.
        if self.prefer_earliest_root:
            for root in self.roots:
                index = DirectoryIndex.get(root)
                result.searched.append(
                    f"<priority index {root} ({index.file_count} files)>")
                root_matches: list[Path] = []
                for name in names:
                    for hit in index.find_name(name):
                        if hit not in root_matches:
                            root_matches.append(hit)
                if not root_matches:
                    continue
                root_matches = _rank_candidates(
                    root_matches, file_name, kind, [root])
                result.candidates = root_matches
                exact = [
                    match for match in root_matches
                    if match.name.casefold() == file_name.casefold()]
                # A file beside the BASE belongs to the family; same-named
                # files in sub-folders (e.g. other exported families sharing
                # the destination folder) must not make it ambiguous.
                beside_base = [match for match in exact if match.parent == root]
                if len(root_matches) == 1 or len(exact) == 1:
                    result.status = "found"
                    result.path = (exact or root_matches)[0]
                    result.source = "filesystem"
                    result.source_root = root
                    result.resolution_rule = (
                        "unique exact filename in highest-priority root"
                        if len(exact) == 1 else
                        "unique filename or shared extension alias in "
                        "highest-priority root")
                elif len(beside_base) == 1:
                    result.status = "found"
                    result.path = beside_base[0]
                    result.source = "filesystem"
                    result.source_root = root
                    result.resolution_rule = (
                        "unique exact filename beside the BASE; same-named "
                        "files in sub-folders are ignored")
                else:
                    result.status = "ambiguous"
                    result.path = None
                    result.resolution_rule = (
                        "multiple filename or extension-alias candidates in "
                        "highest-priority root")
                return result

        # Priority 2/3 (conservative mode): recursive filename match (Windows
        # filesystems are case-insensitive, so exact and case-insensitive
        # coincide here) over the one-time index of every root.
        matches: list[Path] = []
        for root in self.roots:
            index = DirectoryIndex.get(root)
            result.searched.append(f"<index {root} ({index.file_count} files)>")
            for name in names:
                for hit in index.find_name(name):
                    if hit not in matches:
                        matches.append(hit)

        matches = _rank_candidates(matches, file_name, kind, self.roots)
        result.candidates = matches

        exact = [m for m in matches if m.name.lower() == file_name.lower()]
        if not matches:
            result.status = "missing"
            result.resolution_rule = "no logical-path or filename match"
        elif len(matches) == 1 or len(exact) == 1:
            # A single match, or a single exact-name match among alias
            # extensions, is a strong candidate: auto-load it.
            result.status = "found"
            result.path = (exact or matches)[0]
            result.source = "filesystem"
            result.resolution_rule = (
                "unique exact filename" if len(exact) == 1
                else "unique filename or shared extension alias")
            for root in self.roots:
                try:
                    result.path.relative_to(root)
                    result.source_root = root
                    break
                except ValueError:
                    continue
        else:
            # Multiple plausible candidates: NEVER silently pick one.  The
            # ranked list is exposed so callers/UI can trial-load a choice.
            result.status = "ambiguous"
            result.path = None
            result.resolution_rule = (
                "multiple filename or extension-alias candidates")
        return result


# ----------------------------------------------------------------------------
# Portable Complete Asset Family paths and manifest constants, shared unchanged.
# (formerly asset_package_paths.py)
# ----------------------------------------------------------------------------

MANIFEST_NAME = "asset_family_manifest.json"


MANIFEST_FORMAT = "OpenNeoUAStudio Complete Asset Family"


MANIFEST_VERSION = 1


_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class AssetFamilyPackageError(RuntimeError):
    pass


def package_relative_path(
        logical_name: str, *, default_name: str,
        extensions: tuple[str, ...]) -> Path:
    """Map one logical reference to a safe, case-preserving package path.

    Amiga colons and backslashes use the canonical resolver normalization.
    Unsafe or unrepresentable names fail visibly instead of being renamed.
    """

    raw = str(logical_name or "").strip()
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", "\\\\")):
        raise AssetFamilyPackageError(
            f"absolute logical path is not portable: {logical_name!r}")
    normalized = normalize_logical_name(raw)
    parts = normalized.split("/") if normalized else [default_name]
    if any(part in ("", ".", "..") for part in parts):
        raise AssetFamilyPackageError(
            f"unsafe logical path: {logical_name!r}")
    for part in parts:
        if any(ord(char) < 32 or char in '<>"|?*' for char in part):
            raise AssetFamilyPackageError(
                f"logical path is not filesystem-portable: {logical_name!r}")
        if part.rstrip(" .") != part:
            raise AssetFamilyPackageError(
                f"logical path has a trailing dot/space: {logical_name!r}")
        if Path(part).stem.casefold() in _WINDOWS_RESERVED:
            raise AssetFamilyPackageError(
                f"logical path uses a reserved filename: {logical_name!r}")
    relative = Path(*parts)
    allowed = tuple(ext.casefold() for ext in extensions)
    suffix = relative.suffix.casefold()
    if not suffix:
        relative = relative.with_suffix(extensions[0])
    elif suffix not in allowed:
        raise AssetFamilyPackageError(
            f"logical resource {logical_name!r} has unsupported extension "
            f"{relative.suffix!r}")
    return relative


# ----------------------------------------------------------------------------
# Shared ONUA3D wire constants and exact axis reflection; no Qt/native I/O.
# (formerly onua3d_contract.py)
# ----------------------------------------------------------------------------

FORMAT = "OpenNeoUA ONUA3D"


VERSION = 1


MANIFEST = "onua3d_manifest.json"


# UA's downward Y becomes glTF's upward Y. Preserve numerical UA units;
# 1 UA unit is represented by 1 glTF metre, a bridge convention, not metrology.
AXES = (1.0, -1.0, 1.0)


UV_DIVISOR = 256.0


COORDINATES = {
    "ua_to_gltf": "(x, y, z) -> (x, -y, z)",
    "meters_per_ua_unit": 1.0,
    "physical_scale_known": False,
    "uv": "(u/256, v/256), top-left origin; no image flip",
    "winding": "UA fan (0,j,j-1), reversed after Y reflection: (0,j-1,j)",
    "transforms": "Studio independent object placement: T*S*R; identity owner hierarchy",
}


def convert_point(point):
    return tuple(float(value) * axis for value, axis in zip(point, AXES))


# ----------------------------------------------------------------------------
# Persistent ONUA3D identity contract; no GLB/native parser or import writes.
# (formerly onua3d_identity.py)
# ----------------------------------------------------------------------------

ATTRIBUTE = "_ONUA3D_VERTEX_ID"


# Every integer in this range is exactly representable as a glTF float32.
MAX_VERTEX_ID = 1 << 24


IDENTITY_CONTRACT = {
    "version": 1,
    "attribute": ATTRIBUTE,
    "component_type": 5126,
    "accessor_type": "SCALAR",
    "duplicates": "identical_position",
}


class VertexIdentityError(ValueError):
    """Missing, unsupported or inconsistent persistent vertex identity."""


def _vertex_id(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 1 <= value <= MAX_VERTEX_ID or int(value) != value):
        raise VertexIdentityError(f"Invalid ONUA3D vertex ID: {value!r}")
    return int(value)


def vertex_identity_mapping(manifest):
    """Flatten the canonical owner-scoped manifest table into ID -> UA origin.

    Legacy packages remain readable by the family loader, but are not eligible
    for this position-only contract until regenerated from their ua_family.
    This validates identity metadata only, not package hashes or native data.
    """
    if not isinstance(manifest, dict) or manifest.get("vertex_identity") != IDENTITY_CONTRACT:
        raise VertexIdentityError("Missing or unsupported persistent vertex mapping; "
                                  "regenerate the GLB from the embedded ua_family")
    nodes = manifest.get("nodes")
    if not isinstance(nodes, list):
        raise VertexIdentityError("Missing ONUA3D owner mapping")
    result, owners = {}, set()
    for node in nodes:
        if not isinstance(node, dict):
            raise VertexIdentityError("Invalid ONUA3D owner mapping")
        owner, vertices = node.get("owner_path"), node.get("vertex_instances")
        if not isinstance(owner, str) or not owner or owner in owners:
            raise VertexIdentityError("Missing or ambiguous ONUA3D owner_path")
        owners.add(owner)
        if not isinstance(vertices, dict):
            raise VertexIdentityError(f"{owner}: missing vertex_instances mapping")
        for key, origin in vertices.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal():
                raise VertexIdentityError(f"Invalid manifest vertex ID: {key!r}")
            ident = _vertex_id(int(key))
            if key != str(ident) or ident in result:
                raise VertexIdentityError(f"Ambiguous manifest vertex ID: {key}")
            if not isinstance(origin, dict):
                raise VertexIdentityError(f"ID {ident}: invalid native origin")
            for origin_key in ("point_id", "polygon_id", "corner", "block_index"):
                value = origin.get(origin_key)
                if type(value) is not int or value < (-1 if origin_key == "block_index" else 0):
                    raise VertexIdentityError(f"ID {ident}: invalid {origin_key}")
            if "vanm_frame" in origin and (type(origin["vanm_frame"]) is not int
                                           or origin["vanm_frame"] < 0):
                raise VertexIdentityError(f"ID {ident}: invalid vanm_frame")
            result[ident] = {**origin, "owner_path": owner}
    if not result:
        raise VertexIdentityError("Empty persistent vertex mapping")
    return result


def canonical_vertex_positions(manifest, primitives):
    """Validate decoded primitive attributes and return sorted ID -> POSITION.

    Each item supplies ATTRIBUTE (scalar values) and POSITION (triples), in
    matching accessor order. The caller decodes GLB accessors and checks the
    scene/placement/topology contract separately. Nodes, primitive order,
    normals and other rendering attributes are not identity keys.

    Blender may materialize multiple vertices for one ID (normal/UV splits).
    All copies MUST have exactly equal finite positions. No epsilon, averaging,
    nearest-point matching or arbitrary selection is performed. Different IDs
    at the same position remain different logical instances.
    """
    expected = vertex_identity_mapping(manifest)
    result = {}
    for primitive in primitives:
        if not isinstance(primitive, dict) or ATTRIBUTE not in primitive or "POSITION" not in primitive:
            raise VertexIdentityError("Primitive is missing persistent ID or POSITION")
        ids, positions = primitive[ATTRIBUTE], primitive["POSITION"]
        if (not isinstance(ids, (list, tuple)) or not isinstance(positions, (list, tuple))
                or len(ids) != len(positions)):
            raise VertexIdentityError("ID/POSITION accessor count mismatch")
        for raw_id, position in zip(ids, positions):
            ident = _vertex_id(raw_id)
            if ident not in expected:
                raise VertexIdentityError(f"Unknown ONUA3D vertex ID: {ident}")
            if (not isinstance(position, (list, tuple)) or len(position) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)):
                raise VertexIdentityError(f"ID {ident}: invalid POSITION")
            # Normalize signed zero so equivalent groups also have a stable
            # representation independent of encounter order.
            point = tuple(0.0 if v == 0 else float(v) for v in position)
            if ident in result and result[ident] != point:
                raise VertexIdentityError(f"ID {ident}: conflicting POSITION among copies")
            result[ident] = point
    missing = expected.keys() - result.keys()
    if missing:
        raise VertexIdentityError(f"Missing ONUA3D vertex IDs: {sorted(missing)[:8]}")
    return dict(sorted(result.items()))


# ----------------------------------------------------------------------------
# Shared ONUA3D package/GLB validation. No native parser, writer or Qt dependency.
# (formerly onua3d_package.py)
# ----------------------------------------------------------------------------

# ONLY Object Mode matrix equivalence. Never use this for vertex positions.
TRANSFORM_EPSILON = 1e-6


class Onua3dImportError(AssetFamilyPackageError):
    pass


def _require(condition, message):
    if not condition:
        raise Onua3dImportError(message)


def _load_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise Onua3dImportError(f"Non-finite JSON value: {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _index(rows, index):
    _require(type(index) is int and 0 <= index < len(rows), "Invalid glTF index")
    return rows[index]


def _vector(values, size):
    _require(isinstance(values, list) and len(values) == size
             and all(type(v) in (int, float) and math.isfinite(v) for v in values),
             "Invalid or non-finite glTF transform")
    return values


def node_matrix(node):
    """Canonical column-major local matrix; glTF T*R*S, without corrections."""
    if "matrix" in node:
        _require(not any(k in node for k in ("translation", "rotation", "scale")),
                 "Node mixes matrix and TRS")
        return _vector(node["matrix"], 16)
    x, y, z, w = _vector(node.get("rotation", [0, 0, 0, 1]), 4)
    sx, sy, sz = _vector(node.get("scale", [1, 1, 1]), 3)
    tx, ty, tz = _vector(node.get("translation", [0, 0, 0]), 3)
    # Do not normalize or repair the imported quaternion.
    result = [(1-2*(y*y+z*z))*sx, 2*(x*y+z*w)*sx, 2*(x*z-y*w)*sx, 0,
              2*(x*y-z*w)*sy, (1-2*(x*x+z*z))*sy, 2*(y*z+x*w)*sy, 0,
              2*(x*z+y*w)*sz, 2*(y*z-x*w)*sz, (1-2*(x*x+y*y))*sz, 0,
              tx, ty, tz, 1]
    return _vector(result, 16)


class _Scene:
    """Decode the uncompressed embedded glTF subset used by this contract."""

    def __init__(self, data):
        _require(len(data) >= 28, "Truncated GLB")
        _require(struct.unpack_from("<4sII", data) == (b"glTF", 2, len(data)),
                 "Invalid GLB header/version/length")
        length, tag = struct.unpack_from("<I4s", data, 12)
        _require(tag == b"JSON" and length % 4 == 0 and 28 + length <= len(data),
                 "Invalid GLB JSON chunk")
        self.doc = _load_json(data[20:20+length])
        size, tag = struct.unpack_from("<I4s", data, 20+length)
        _require(tag == b"BIN\0" and size % 4 == 0 and 28+length+size == len(data),
                 "Invalid GLB binary chunk")
        self.binary = data[28+length:]
        d = self.doc
        _require(d["asset"]["version"] == "2.0" and not d.get("skins"),
                 "Unsupported glTF version or skinning")
        buffers = d["buffers"]
        _require(len(buffers) == 1 and "uri" not in buffers[0]
                 and type(buffers[0]["byteLength"]) is int
                 and 0 <= size - buffers[0]["byteLength"] <= 3,
                 "GLB must use one embedded buffer")
        self.buffer_size = buffers[0]["byteLength"]
        self.nodes, self.parents = {}, {}
        for index, node in enumerate(d["nodes"]):
            _require(not any(k in node for k in ("skin", "weights", "extensions")),
                     "Unsupported glTF node deformation/extension")
            meta = node.get("extras", {}).get("onua3d")
            _require(isinstance(meta, dict) and "owner_path" in meta and "role" in meta,
                     "Missing ONUA3D node metadata")
            key = json.dumps(meta, sort_keys=True, allow_nan=False)
            _require(key not in self.nodes, "Ambiguous ONUA3D node metadata")
            self.nodes[key] = (index, node)
            for child in node.get("children", []):
                _index(d["nodes"], child)
                _require(child not in self.parents, "Node has multiple parents")
                self.parents[child] = index
        self.keys = {index: key for key, (index, _) in self.nodes.items()}
        _require(len(d["scenes"]) == 1 and d.get("scene", 0) == 0,
                 "V1 requires one scene")
        roots = d["scenes"][0]["nodes"]
        for index in roots:
            _index(d["nodes"], index)
        _require(len(set(roots)) == len(roots)
                 and set(roots) == set(self.keys) - self.parents.keys(),
                 "Invalid scene roots")
        for index in self.keys:
            seen = set()
            while index in self.parents:
                _require(index not in seen, "Cyclic node hierarchy")
                seen.add(index)
                index = self.parents[index]
        # VANM preview visibility is derived (Blender resamples its scale
        # curves). It never edits native ANM. Object transform animation and
        # deformation channels, however, cannot bypass the Object Mode gate.
        for animation in d.get("animations", []):
            for channel in animation["channels"]:
                target = channel["target"]
                node = _index(d["nodes"], target["node"])
                _require(node["extras"]["onua3d"]["role"] == "vanm_frame"
                         and target["path"] == "scale",
                         "Object transform/geometry animation editing is not supported")

    def values(self, index, kind, components):
        a = _index(self.doc["accessors"], index)
        _require(a.get("type") == kind and a.get("componentType") in components
                 and not a.get("normalized", False) and "sparse" not in a,
                 "Unsupported or corrupt glTF accessor encoding")
        view = _index(self.doc["bufferViews"], a["bufferView"])
        _require(view.get("buffer") == 0 and not view.get("extensions"),
                 "Unsupported glTF buffer view")
        width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3}[kind]
        fmt = {5121: "B", 5123: "H", 5125: "I", 5126: "f"}[a["componentType"]]
        element = struct.calcsize("<" + fmt * width)
        start, size = view.get("byteOffset", 0), view["byteLength"]
        offset, count = a.get("byteOffset", 0), a["count"]
        stride = view.get("byteStride", element)
        _require(all(type(v) is int and v >= 0 for v in (start, size, offset, count, stride))
                 and count > 0 and stride >= element and start+size <= self.buffer_size
                 and offset+(count-1)*stride+element <= size,
                 "glTF accessor is outside its buffer view")
        rows = [struct.unpack_from("<" + fmt * width, self.binary, start+offset+i*stride)
                for i in range(count)]
        return [r[0] for r in rows] if width == 1 else rows

    def geometry(self):
        attributes, triangles, uvs, meshes = [], Counter(), {}, set()
        for key, (_, node) in self.nodes.items():
            if "mesh" not in node:
                continue
            mesh_index = node["mesh"]
            mesh = _index(self.doc["meshes"], mesh_index)
            _require(not mesh.get("weights"), "Morph weights are not supported")
            meshes.add(mesh_index)
            for primitive in mesh["primitives"]:
                _require(primitive.get("mode", 4) == 4 and not primitive.get("targets")
                         and not primitive.get("extensions"), "V1 requires uncompressed triangles")
                attrs = primitive["attributes"]
                _require(ATTRIBUTE in attrs and "POSITION" in attrs,
                         "Primitive is missing persistent ID or POSITION")
                ids = self.values(attrs[ATTRIBUTE], "SCALAR", (5126,))
                points = self.values(attrs["POSITION"], "VEC3", (5126,))
                attributes.append({ATTRIBUTE: ids, "POSITION": points})
                indices = (self.values(primitive["indices"], "SCALAR", (5121, 5123, 5125))
                           if "indices" in primitive else list(range(len(points))))
                _require(len(ids) == len(points) and len(indices) % 3 == 0
                         and all(0 <= i < len(ids) for i in indices), "Invalid triangle indices/count")
                material = (_index(self.doc.get("materials", []), primitive["material"])
                            if "material" in primitive else {})
                material_key = json.dumps(material.get("extras", {}).get("onua3d"), sort_keys=True)
                for offset in range(0, len(indices), 3):
                    tri = tuple(ids[i] for i in indices[offset:offset+3])
                    tri = min(tri, tri[1:]+tri[:1], tri[2:]+tri[:2])
                    triangles[(key, material_key, tri)] += 1
                if "TEXCOORD_0" in attrs:
                    uv = self.values(attrs["TEXCOORD_0"], "VEC2", (5126,))
                    _require(len(uv) == len(ids), "UV accessor count mismatch")
                    for i in set(indices):
                        ident = ids[i]
                        _require(ident not in uvs or uvs[ident] == uv[i], "Conflicting UV copies")
                        uvs[ident] = uv[i]
        _require(meshes == set(range(len(self.doc.get("meshes", [])))), "Unreferenced glTF mesh")
        return attributes, triangles, uvs


def validate_scene_positions(manifest, baseline_glb, imported_glb):
    """Return (owner_path, point_id) -> UA local position, after all V1 checks."""
    try:
        origins = vertex_identity_mapping(manifest)
        baseline, imported = _Scene(baseline_glb), _Scene(imported_glb)
        _require(baseline.nodes.keys() == imported.nodes.keys(), "ONUA3D node mapping changed")
        for key, (index, node) in baseline.nodes.items():
            other_index, other = imported.nodes[key]
            _require(baseline.keys.get(baseline.parents.get(index))
                     == imported.keys.get(imported.parents.get(other_index)), "Node hierarchy changed")
            _require(("mesh" in node) == ("mesh" in other), "Node mesh placement changed")
            error = max(abs(a-b) for a, b in zip(node_matrix(node), node_matrix(other)))
            _require(error <= TRANSFORM_EPSILON,
                     f"Object Mode transform changed: {node['extras']['onua3d']} "
                     f"(maximum matrix difference {error:.9g} > {TRANSFORM_EPSILON:g})")
        base_attrs, base_triangles, base_uvs = baseline.geometry()
        attrs, triangles, uvs = imported.geometry()
        canonical_vertex_positions(manifest, base_attrs)
        positions = canonical_vertex_positions(manifest, attrs)
        _require(base_triangles == triangles, "Topology or owner/block/material assignment changed")
        _require(all(uvs.get(ident) == uv for ident, uv in base_uvs.items()),
                 "UV editing is not supported by ONUA3D V1")
        result = {}
        for ident, position in positions.items():
            origin = origins[ident]
            key = (origin["owner_path"], origin["point_id"])
            point = convert_point(position)  # reflection is its own inverse
            _require(key not in result or result[key] == point,
                     f"Conflicting POSITION for SKLT point {key}: distinct ONUA3D IDs disagree")
            result[key] = point
        return result
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, struct.error, OverflowError) as exc:
        raise Onua3dImportError(f"Invalid ONUA3D scene: {exc}") from exc


def _read_package(source):
    with zipfile.ZipFile(source) as archive:
        members, seen = {}, set()
        for info in archive.infolist():
            name = info.filename
            path = PurePosixPath(name)
            _require(not path.is_absolute() and path.as_posix() == name
                     and all(part not in ("", ".", "..") and not part.endswith((".", " "))
                             and not any(c in part for c in '\\:<>"|?*\0') for part in path.parts)
                     and not info.is_dir() and not stat.S_ISLNK(info.external_attr >> 16),
                     f"Unsafe ONUA3D member: {name}")
            _require(name.casefold() not in seen, f"Duplicate ONUA3D member: {name}")
            _require(package_relative_path(name, default_name="", extensions=(path.suffix,)).as_posix() == name,
                     f"Non-canonical ONUA3D member: {name}")
            seen.add(name.casefold())
            members[name] = archive.read(info)
    _require(MANIFEST in members, "Missing ONUA3D manifest")
    manifest = _load_json(members.pop(MANIFEST))
    _require(manifest.get("format") == FORMAT and type(manifest.get("version")) is int
             and manifest["version"] == VERSION, "Unsupported ONUA3D format/version")
    _require(manifest.get("scene") == "scene.glb"
             and manifest.get("family_manifest") == "ua_family/" + MANIFEST_NAME
             and manifest.get("coordinates") == COORDINATES, "Invalid ONUA3D manifest layout/coordinates")
    vertex_identity_mapping(manifest)
    _require(isinstance(manifest.get("files"), dict) and members.keys() == manifest["files"].keys(),
             "ONUA3D manifest member list mismatch")
    for name, data in members.items():
        expected = manifest["files"][name]
        _require(type(expected.get("size")) is int and expected["size"] == len(data)
                 and expected.get("sha256") == hashlib.sha256(data).hexdigest(),
                 f"ONUA3D member hash/size mismatch: {name}")
    _require("scene.glb" in members and "ua_family/" + MANIFEST_NAME in members,
             "Missing scene or embedded Complete Asset Family")
    return manifest, members


def read_onua3d(source):
    """Validate portable container, embedded manifest and ID/native snapshot links.

    No native resource is reconstructed here. Studio additionally reopens the
    embedded native files and checks its regenerated mapping before import.
    """
    try:
        manifest, members = _read_package(source)
        native = _load_json(members['ua_family/' + MANIFEST_NAME])
        _require(native['format'] == MANIFEST_FORMAT and type(native['version']) is int
                 and native['version'] == MANIFEST_VERSION, 'Invalid embedded family manifest')
        snapshot = native['semantic_snapshot']
        semantic = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                            separators=(',', ':')).encode('utf-8')).hexdigest()
        _require(semantic == native['semantic_sha256'] == manifest['semantic_sha256'],
                 'Embedded family semantic hash mismatch')
        expected = {'ua_family/' + MANIFEST_NAME}
        seen = set()
        entries = []
        for entry in native['entries']:
            name = entry['exported_path']
            _require(name.casefold() not in seen, 'Duplicate embedded family entry')
            seen.add(name.casefold())
            if entry['status'] != 'exported':
                continue
            key = 'ua_family/' + name
            expected.add(key)
            _require(key in members and hashlib.sha256(members[key]).hexdigest() == entry['sha256'],
                     f'Embedded family member hash mismatch: {name}')
            if name == native['entry_base'] and entry['asset_class'] == 'base.class':
                entries.append(entry)
        _require(len(entries) == 1 and expected == {n for n in members if n.startswith('ua_family/')},
                 'Invalid embedded family inventory/entry BASE')
        owners = {}
        def visit(node):
            _require(node['path'] not in owners, 'Ambiguous native snapshot owner')
            owners[node['path']] = node
            for child in node['kids']: visit(child)
        visit(snapshot['object_tree'])
        for origin in vertex_identity_mapping(manifest).values():
            node = owners[origin['owner_path']]
            skeleton = node['skeleton']
            polygon = _index(skeleton['polygons'], origin['polygon_id'])
            _index(skeleton['points'], origin['point_id'])
            _require(_index(polygon, origin['corner']) == origin['point_id'],
                     'Vertex mapping disagrees with embedded polygon/corner')
            if origin['block_index'] != -1:
                _index(node['ades'], origin['block_index'])
        validate_scene_positions(manifest, members['scene.glb'], members['scene.glb'])
        return manifest, members
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, zipfile.BadZipFile) as exc:
        raise Onua3dImportError(f'Invalid ONUA3D package: {exc}') from exc


def repack_onua3d(source, scene_glb, target, *, source_sha256):
    """Copy a pinned source package, replacing only scene.glb and its hash/size.

    A NEW output is required. The original package, embedded native members,
    other ZIP members and all other manifest values are preserved.
    """
    source, target = Path(source), Path(target)
    _require(source.is_file(), 'Source ONUA3D package is missing; open it again')
    _require(not target.exists() and not target.is_symlink(), 'Choose a NEW ONUA3D output file')
    data = source.read_bytes()
    _require(hashlib.sha256(data).hexdigest() == source_sha256,
             'Source ONUA3D package changed; open it again before exporting')
    manifest, members = read_onua3d(io.BytesIO(data))
    validate_scene_positions(manifest, members['scene.glb'], scene_glb)
    updated = copy.deepcopy(manifest)
    updated['files']['scene.glb'].update(size=len(scene_glb), sha256=hashlib.sha256(scene_glb).hexdigest())
    replacement_manifest = (json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    def verify(path):
        actual_manifest, actual_members = read_onua3d(path)
        _require(actual_manifest == updated, 'ONUA3D manifest readback mismatch')
        _require(actual_members['scene.glb'] == scene_glb, 'ONUA3D scene readback mismatch')
        _require(all(actual_members[name] == content for name, content in members.items()
                     if name != 'scene.glb'), 'ONUA3D repack changed an original member')
    with tempfile.TemporaryDirectory(prefix='OpenNeoUA_onua3d_repack_') as temporary:
        staged = Path(temporary) / 'result.onua3d'
        with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(staged, 'w') as output:
            for info in original.infolist():
                payload = (replacement_manifest if info.filename == MANIFEST else
                           scene_glb if info.filename == 'scene.glb' else members[info.filename])
                output.writestr(info, payload)
        verify(staged)
        commit_verified_files([(staged, target)], replace_existing=False, verify=lambda: verify(target))
    return updated
