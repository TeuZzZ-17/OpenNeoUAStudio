from pathlib import Path
import sys


def _unique_paths(paths):
    seen = set()
    result = []
    for path in paths:
        resolved = Path(path).resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


PACKAGE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    executable_dir = Path(sys.executable).resolve().parent
    bundle_root = Path(getattr(sys, "_MEIPASS", executable_dir)).resolve()
    WRITABLE_BASE_DIR = executable_dir / "map_editor"
    BUNDLED_BASE_DIR = bundle_root / "map_editor"
else:
    WRITABLE_BASE_DIR = PACKAGE_DIR
    BUNDLED_BASE_DIR = PACKAGE_DIR

RESOURCE_SEARCH_DIRS = _unique_paths(
    [
        WRITABLE_BASE_DIR,
        BUNDLED_BASE_DIR,
        PACKAGE_DIR,
        Path.cwd() / "map_editor",
        Path.cwd(),
    ]
)


def resource_path(relative_path):
    """Return a path to an integrated, bundled, or user-overridden resource."""

    relative = Path(relative_path)
    for base_dir in RESOURCE_SEARCH_DIRS:
        candidate = base_dir / relative
        if candidate.exists():
            return str(candidate)
    return str(BUNDLED_BASE_DIR / relative)


def writable_path(relative_path):
    """Return a stable path for Map Editor-generated or overridden files."""

    target = WRITABLE_BASE_DIR / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)
