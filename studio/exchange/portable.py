"""Portable core shared by every Studio workspace: plain Python, no Qt, no native UA parsers.

Sections, in order: filesystem commits, logical asset-name resolver, portable package
paths and the Complete Asset Family manifest constants.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import tempfile

# ----------------------------------------------------------------------------
# Coordinated filesystem commits for verified Studio outputs.
# (formerly verified_io.py)
# ----------------------------------------------------------------------------

class VerifiedCommitError(OSError):
    def __init__(self, message: str, *, rollback_complete: bool) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete


# Safety copies of files about to be overwritten (File Backup menu of every workspace).
# Off by default; the Studio UI turns it on from its saved setting at startup.
_backup_enabled = False


def set_backup_enabled(enabled: bool) -> None:
    global _backup_enabled
    _backup_enabled = bool(enabled)


def backup_enabled() -> bool:
    return _backup_enabled


def next_backup_path(source: Path) -> Path:
    """Return a free ``.bak`` path beside ``source``; older backups are never overwritten."""

    source = Path(source)
    candidate = source.with_name(source.name + ".bak")
    index = 1
    while candidate.exists():
        candidate = source.with_name(source.name + f".{index}.bak")
        index += 1
    return candidate


def backup_existing_file(path: Path | str) -> Path | None:
    """Copy an existing file to ``<name>.bak`` before it is overwritten.

    Does nothing (returns None) when the Backup option is disabled or the file does
    not exist yet, so every writer can call it unconditionally.
    """

    path = Path(path)
    if not _backup_enabled or not path.is_file():
        return None
    backup = next_backup_path(path)
    shutil.copy2(path, backup)
    return backup


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
                backup_existing_file(target)
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
        # Files can be removed outside Studio after this shared scan. A stale
        # loose hit must not prevent fallback to the installed SET resources.
        return [path for path in self.by_name.get(filename.lower(), [])
                if path.is_file()]

    def find_relative(self, logical_path: str) -> list[Path]:
        normalized = normalize_logical_name(logical_path).casefold()
        return [path for path in self.by_relative.get(normalized, [])
                if path.is_file()]


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
