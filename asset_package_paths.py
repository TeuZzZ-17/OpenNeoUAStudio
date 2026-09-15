"""Portable Complete Asset Family paths and manifest constants, shared unchanged."""
from pathlib import Path
import re
from asset_resolver import normalize_logical_name

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

