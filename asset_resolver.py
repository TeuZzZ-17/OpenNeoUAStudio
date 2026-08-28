"""Non-destructive recursive resolver for Urban Assault asset references.

A .base file references resources by logical name, e.g.
``Skeleton/BP_FLAK1.sklt`` or ``MTL.ILBM``.  On disk those names may live in
several layouts (developer CD tree, OpenNeoUA Data/SetN/Loose, BASet extraction
output) and may use legacy extension aliases:

    .base <-> .bas          .sklt <-> .skl
    .ilbm <-> .ilb / .lbm   .anm  <-> .vanm

Amiga path separators ``:`` behave like ``/`` and matching is
case-insensitive.  ``.info`` files are Amiga Workbench sidecars, never assets.

The resolver only reads directory listings; it never writes or moves files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


EXTENSION_ALIASES: dict[str, tuple[str, ...]] = {
    ".base": (".base", ".bas"),
    ".bas": (".bas", ".base"),
    ".sklt": (".sklt", ".skl"),
    ".skl": (".skl", ".sklt"),
    ".ilbm": (".ilbm", ".ilb", ".lbm", ".iff"),
    ".ilb": (".ilb", ".ilbm", ".lbm", ".iff"),
    ".lbm": (".lbm", ".ilbm", ".ilb", ".iff"),
    ".anm": (".anm", ".vanm"),
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


def _alias_names(file_name: str) -> list[str]:
    stem, dot, ext = file_name.rpartition(".")
    if not dot:
        return [file_name]
    aliases = EXTENSION_ALIASES.get("." + ext.lower(), ("." + ext.lower(),))
    return [stem + alias for alias in aliases]


class AssetResolver:
    """Searches an ordered list of root directories for referenced assets.

    ``overrides`` maps a logical name (case-insensitive) to a user-chosen
    file path; it wins over any directory search.  Overrides are session
    state only — nothing is ever written to disk by the resolver.
    """

    def __init__(self, roots: list[Path | str],
                 overrides: dict[str, Path] | None = None):
        self.roots: list[Path] = []
        for root in roots:
            path = Path(root)
            if path.is_file():
                path = path.parent
            if path.is_dir() and path not in self.roots:
                self.roots.append(path)
        self.overrides: dict[str, Path] = {}
        for name, target in (overrides or {}).items():
            self.set_override(name, target)

    def set_override(self, logical_name: str, target: Path | str) -> None:
        key = normalize_logical_name(logical_name).lower()
        self.overrides[key] = Path(target)

    def clear_override(self, logical_name: str) -> None:
        self.overrides.pop(normalize_logical_name(logical_name).lower(), None)

    def add_root(self, root: Path | str) -> None:
        path = Path(root)
        if path.is_file():
            path = path.parent
        if path.is_dir() and path not in self.roots:
            self.roots.append(path)

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
        names = _alias_names(file_name)

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

        # Priority 2/3: recursive filename match (Windows filesystems are
        # case-insensitive, so exact and case-insensitive coincide here) over
        # the one-time index of every root.
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


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="Resolve an asset reference.")
    cli.add_argument("root", help="asset root directory (or a file inside it)")
    cli.add_argument("name", help="logical resource name, e.g. Skeleton/BP_FLAK1.sklt")
    cli.add_argument("--kind", default="any",
                     choices=sorted(TYPE_HINTS.keys()))
    args = cli.parse_args()

    resolver = AssetResolver([args.root])
    res = resolver.resolve(args.name, args.kind)
    print(f"{res.logical_name}: {res.status}")
    if res.path:
        print(f"  -> {res.path}")
    for candidate in res.candidates:
        print(f"  candidate: {candidate}")
