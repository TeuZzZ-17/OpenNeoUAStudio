"""Adapter from OpenNeoUAStudio AssetFamily data to the optional indexed renderer.

The adapter is deliberately fail-closed.  It accepts only material combinations
whose retail dispatch semantics are established and only uses palette/remap
resources from one bounded Urban Assault SET profile.
"""

from __future__ import annotations

import hashlib
import operator
from pathlib import Path
from typing import Any

from ilbm_parser import parse_ilbm_file
from indexed_renderer import IndexedSurface, IndexedTables

_RETAIL_IGNORED_POLFLAG_BITS = 0x001 | 0x008 | 0x100
_RETAIL_NNN_CODE = 0x00
_RETAIL_LINEAR_CODES = frozenset((0x02, 0x32, 0x42, 0x72, 0x82))
_RETAIL_DEPTH_CODES = frozenset((0x06, 0x36, 0x46, 0x76))
_RETAIL_GRADIENT_CODES = frozenset((0x32, 0x36, 0x72, 0x76))
_RETAIL_CLEAR_CODES = frozenset((0x42, 0x46, 0x72, 0x76))
_RETAIL_FLAT_TRACY_CODE = 0x82
_RETAIL_MATERIAL_CODES = frozenset(
    (_RETAIL_NNN_CODE,) + tuple(_RETAIL_LINEAR_CODES) + tuple(_RETAIL_DEPTH_CODES)
)
_AMBIGUOUS_TEXTURE = object()
VANILLA_GAMEPLAY_VIS_LIMIT = 1400.0
VANILLA_FADE_LENGTH = 600.0
VANILLA_GAMEPLAY_FADE_START = (
    VANILLA_GAMEPLAY_VIS_LIMIT - VANILLA_FADE_LENGTH)


class IndexedFamilyAdapterError(RuntimeError):
    pass


class UnsupportedIndexedMaterialError(IndexedFamilyAdapterError):
    pass


class IndexedSourceError(IndexedFamilyAdapterError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _material_code(polflags: Any) -> int:
    return int(polflags) & ~_RETAIL_IGNORED_POLFLAG_BITS


def _constant_shade_row(raw_shade: int) -> int:
    return (253 * raw_shade + 0x180) >> 8


def _as_byte(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise UnsupportedIndexedMaterialError(
            f"{label} must be an integer in the range 0..255")
    try:
        decoded = operator.index(value)
    except TypeError as exc:
        raise UnsupportedIndexedMaterialError(
            f"{label} must be an integer in the range 0..255") from exc
    if not 0 <= decoded <= 255:
        raise UnsupportedIndexedMaterialError(
            f"{label} is outside the range 0..255: {decoded}")
    return decoded


def _candidate_set_roots(family) -> list[Path]:
    roots: list[Path] = []

    def add(value) -> None:
        if not value:
            return
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError, TypeError):
            return
        if path.is_file():
            path = path.parent
        candidates = [path]
        if path.name.casefold() in ("palette", "remap", "objects"):
            candidates.insert(0, path.parent)
        # Search only a few ancestors; never recurse through unrelated trees.
        candidates.extend(list(path.parents)[:4])
        for candidate in candidates:
            if candidate not in roots:
                roots.append(candidate)

    add(getattr(family, "external_palette_path", None))
    add(getattr(family, "setbas_path", None))
    add(getattr(family, "base_path", None))
    for root in getattr(family, "search_roots", ()):
        add(root)
    return roots


def _find_named(root: Path, folder: str, stems: tuple[str, ...]) -> Path | None:
    if not root.is_dir():
        return None
    try:
        directory = next(
            (entry for entry in root.iterdir()
             if entry.is_dir() and entry.name.casefold() == folder.casefold()),
            None,
        )
    except OSError:
        return None
    if directory is None:
        return None
    wanted = {name.casefold() for name in stems}
    try:
        for entry in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if entry.is_file() and entry.name.casefold() in wanted:
                return entry.resolve()
    except OSError:
        return None
    return None


def _find_profile(family) -> tuple[Path, Path, Path]:
    palette_path = getattr(family, "external_palette_path", None)
    palette_path = Path(palette_path).resolve() if palette_path else None
    palette_memory = getattr(family, "external_palette", None)
    if palette_memory is None:
        raise IndexedSourceError("no resolved 256-entry SET palette is available")

    canonical_refs = getattr(family, "indexed_profile_refs", {}) or {}
    if canonical_refs:
        shader_ref = canonical_refs.get("shader")
        tracy_ref = canonical_refs.get("tracy")
        failures = []
        for label, ref in (("SHADERMP", shader_ref),
                           ("TRACYRMP", tracy_ref)):
            if ref is None or ref.path is None or ref.status == "ambiguous":
                status = getattr(ref, "status", "missing")
                rule = getattr(ref, "resolution_rule", "unresolved")
                failures.append(f"{label}: {status} ({rule})")
        if failures:
            raise IndexedSourceError(
                "the palette-selected SET has no unique complete REMAP "
                "profile: " + "; ".join(failures))
        if palette_path is None or not palette_path.is_file():
            raise IndexedSourceError(
                "the canonical palette provenance has no source file")
        return palette_path, shader_ref.path.resolve(), tracy_ref.path.resolve()

    roots = _candidate_set_roots(family)
    if palette_path is not None and palette_path.parent.name.casefold() == "palette":
        preferred = palette_path.parent.parent
        shader = _find_named(
            preferred, "REMAP", ("SHADERMP.ILB", "SHADERMP.ILBM"))
        tracy = _find_named(
            preferred, "REMAP", ("TRACYRMP.ILB", "TRACYRMP.ILBM"))
        if shader is not None and tracy is not None:
            return palette_path, shader, tracy
        if shader is not None or tracy is not None:
            raise IndexedSourceError(
                "the SET selected by the external palette has an incomplete "
                "REMAP pair; refusing to mix remap tables from another root")
        roots = [preferred] + [root for root in roots if root != preferred]

    complete: list[tuple[Path, Path, Path]] = []
    for root in roots:
        shader = _find_named(root, "REMAP", ("SHADERMP.ILB", "SHADERMP.ILBM"))
        tracy = _find_named(root, "REMAP", ("TRACYRMP.ILB", "TRACYRMP.ILBM"))
        candidate_palette = palette_path
        if candidate_palette is None or not candidate_palette.is_file():
            candidate_palette = _find_named(
                root, "PALETTE", ("STANDARD.PAL", "NORMAL.PAL"))
        if candidate_palette is not None and shader is not None and tracy is not None:
            complete.append((candidate_palette.resolve(), shader, tracy))

    # Prefer the exact SET containing the already-selected palette.
    if palette_path is not None:
        matching = [profile for profile in complete if profile[0] == palette_path]
        if matching:
            complete = matching
    if not complete:
        raise IndexedSourceError(
            "matching PALETTE + REMAP/SHADERMP + REMAP/TRACYRMP were not found "
            "inside the bounded SET roots")

    # Multiple identical profiles are harmless; conflicting ones are not.
    unique: dict[tuple[str, str, str], tuple[Path, Path, Path]] = {}
    for profile in complete:
        hashes = tuple(_sha256(path) for path in profile)
        unique.setdefault(hashes, profile)
    if len(unique) > 1:
        raise IndexedSourceError(
            "palette/SET origin is ambiguous: multiple complete profiles have "
            "different palette or remap hashes")
    return next(iter(unique.values()))


class IndexedFamilyAdapter:
    """Resolve an AssetFamily into exact indexed surfaces and source metadata."""

    def __init__(self, family) -> None:
        self.family = family
        palette_path, shader_path, tracy_path = _find_profile(family)
        self.palette_path = palette_path
        self.shader_path = shader_path
        self.tracy_path = tracy_path

        palette = getattr(family, "external_palette", None)
        if palette is None or len(palette) != 256:
            raise IndexedSourceError("resolved external palette must contain 256 colors")
        shader = parse_ilbm_file(shader_path)
        tracy = parse_ilbm_file(tracy_path)
        self.tables = IndexedTables.from_decoded(palette, shader, tracy)
        self.source_hashes = {
            "palette": _sha256(palette_path),
            "shader": _sha256(shader_path),
            "tracy": _sha256(tracy_path),
        }
        self.source_info = {
            "palette": {
                "path": str(palette_path),
                "sha256": self.source_hashes["palette"],
                "provenance": getattr(
                    getattr(family, "external_palette_ref", None),
                    "provenance", {}),
            },
            "shader": {
                "path": str(shader_path),
                "sha256": self.source_hashes["shader"],
                "provenance": getattr(
                    getattr(family, "indexed_profile_refs", {}).get(
                        "shader"), "provenance", {}),
            },
            "tracy": {
                "path": str(tracy_path),
                "sha256": self.source_hashes["tracy"],
                "provenance": getattr(
                    getattr(family, "indexed_profile_refs", {}).get(
                        "tracy"), "provenance", {}),
            },
            "tracy_lookup_axis_order": "TRACYRMP[background][raw_source]",
            "shade_model": (
                "raw 0..2 bypass SHADERMP; 3..253 uses "
                "(253*raw+0x180)>>8; 254..255 becomes opaque index 0"),
        }

        self.block_map: dict[tuple[str, int], Any] = {}
        self.fade_profiles: dict[tuple[str, int], dict[str, float]] = {}
        unsupported = []
        depth_fade = []
        for obj in family.all_objects():
            owner = str(getattr(obj, "owner_path", "root"))
            for block_index, group in enumerate(getattr(obj, "materials", ())):
                block = getattr(group, "block", None)
                if block is None:
                    continue
                self.block_map[(owner, block_index)] = block
                code = _material_code(getattr(block, "polflags", 0))
                if code not in _RETAIL_MATERIAL_CODES:
                    unsupported.append({
                        "owner": owner,
                        "block": block_index,
                        "polflags": int(getattr(block, "polflags", 0)),
                        "dispatch_code": code,
                    })
                if bool(getattr(block, "depth_fade", False)):
                    depth_fade.append(f"{owner} block {block_index}")
                    self.fade_profiles[(owner, block_index)] = {
                        "vis_limit": VANILLA_GAMEPLAY_VIS_LIMIT,
                        "fade_start": VANILLA_GAMEPLAY_FADE_START,
                        "fade_length": VANILLA_FADE_LENGTH,
                    }
        self.source_info["unsupported_material_codes"] = unsupported
        self.source_info["depth_fade_blocks"] = depth_fade
        self.source_info["area_distance_fade_profiles"] = {
            f"{owner} block {block}": dict(profile)
            for (owner, block), profile in self.fade_profiles.items()
        }
        self._texture_lookup_revision: tuple[int, int] | None = None
        self._texture_exact: dict[str, Any] = {}
        self._texture_basename: dict[str, Any] = {}
        self._surface_cache: dict[tuple[Any, ...], IndexedSurface] = {}
        self._solid_surfaces = {
            "palette-index-0": IndexedSurface(
                "palette-index-0", "solid", None, 0, 0, 0,
                "none", 0, "none", "linear"),
            "palette-index-0-black-shade": IndexedSurface(
                "palette-index-0-black-shade", "solid", None, 0, 0, 0,
                "none", 0, "none", "linear"),
        }
        # Keep the adapter usable when unsupported blocks exist elsewhere in
        # the family. Exact rendering fails closed only if a visible polygon
        # actually resolves to one of those combinations.
        self.available = True
        self.availability_reason = ""

    @classmethod
    def try_create(cls, family):
        try:
            adapter = cls(family)
        except Exception as exc:
            # Textured rendering is exact/fail-closed. Non-textured inspection
            # modes remain available when its indexed resources are invalid.
            return None, str(exc)
        if not adapter.available:
            return None, adapter.availability_reason
        return adapter, ""

    def block_for(self, owner: str, block_index: int):
        return self.block_map.get((str(owner), int(block_index)))

    def distance_fade_profile(
            self, face, enabled: bool = False
            ) -> dict[str, float] | None:
        """Return the recovered retail gameplay AREA fade profile.

        The BASE/AREA data only marks eligible geometry with
        ``AREA_FLAG_DPTHFADE``.  The normal Urban Assault world runtime
        supplies visLimit=1400 and fadeLength=600, therefore fadeStart=800.
        The viewer exposes only an opt-in toggle; no editable preview profile
        is accepted here.
        """

        if not enabled:
            return None
        key = (str(getattr(face, "owner", "root")),
               int(getattr(face, "block_index", -1)))
        profile = self.fade_profiles.get(key)
        return dict(profile) if profile is not None else None

    def active_texture_name(self, material, frame_index: int) -> str:
        frames = list(getattr(material, "anim_frames", ()) or ())
        names = list(getattr(material, "anim_bitmap_names", ()) or ())
        if frames:
            frame_index = max(0, min(int(frame_index), len(frames) - 1))
            _duration, image_id, _uv_id = frames[frame_index]
            if 0 <= int(image_id) < len(names):
                return str(names[int(image_id)])
        return str(getattr(material, "label", ""))

    def active_uvs(self, material, frame_index: int):
        frames = list(getattr(material, "anim_frames", ()) or ())
        groups = list(getattr(material, "anim_uv_groups", ()) or ())
        if not frames:
            return ()
        frame_index = max(0, min(int(frame_index), len(frames) - 1))
        _duration, _image_id, uv_id = frames[frame_index]
        if 0 <= int(uv_id) < len(groups):
            return tuple(tuple(pair) for pair in groups[int(uv_id)])
        return ()

    def _texture(self, name: str):
        textures = getattr(self.family, "textures", {})
        revision = (id(textures), len(textures))
        if revision != self._texture_lookup_revision:
            exact: dict[str, Any] = {}
            basename: dict[str, Any] = {}
            for key, value in textures.items():
                normalized = str(key).replace("\\", "/").casefold()
                exact[normalized] = value
                base = Path(normalized).name
                previous = basename.get(base)
                if previous is None or previous is value:
                    basename[base] = value
                else:
                    basename[base] = _AMBIGUOUS_TEXTURE
            self._texture_exact = exact
            self._texture_basename = basename
            self._texture_lookup_revision = revision
            self._surface_cache.clear()
        wanted = str(name).replace("\\", "/").casefold()
        image = self._texture_exact.get(wanted)
        if image is None:
            image = self._texture_basename.get(Path(wanted).name)
            if image is _AMBIGUOUS_TEXTURE:
                image = None
        if image is None or getattr(image, "pixels", None) is None:
            raise UnsupportedIndexedMaterialError(
                f"raw indexed texture {name!r} is unavailable")
        return image

    def resolve_surface(self, face, material, frame_index: int = 0) -> IndexedSurface:
        owner = str(getattr(face, "owner", "root"))
        block_index = int(getattr(face, "block_index", -1))
        block = self.block_for(owner, block_index)
        if block is None:
            raise UnsupportedIndexedMaterialError(
                f"no exact material block for {owner} block {block_index}")
        if not bool(getattr(face, "mapped", True)):
            raise UnsupportedIndexedMaterialError(
                f"polygon {getattr(face, 'poly_id', -1)} has no ATTS mapping")

        polflags = int(getattr(block, "polflags", 0))
        code = _material_code(polflags)
        if code not in _RETAIL_MATERIAL_CODES:
            raise UnsupportedIndexedMaterialError(
                f"unsupported retail polygon flags 0x{polflags:x} "
                f"(dispatch code 0x{code:x})")

        # NNN is the retail zero-span path; ATTS color is not consulted.
        if code == _RETAIL_NNN_CODE:
            return self._solid_surfaces["palette-index-0"]

        map_mode = "depth" if code in _RETAIL_DEPTH_CODES else "linear"
        tracy_mode = (
            "flat" if code == _RETAIL_FLAT_TRACY_CODE else
            "clear" if code in _RETAIL_CLEAR_CODES else "none")
        shade_mode = "gradient" if code in _RETAIL_GRADIENT_CODES else "none"
        shade_source = getattr(face, "shade", None)
        if shade_source is None:
            shade_source = getattr(block, "shade_val", 0)
        raw_shade = _as_byte(shade_source, "shade value")
        shade_value = 0
        if shade_mode == "gradient" and raw_shade <= 2:
            shade_mode = "none"
        elif shade_mode == "gradient" and raw_shade >= 254:
            return self._solid_surfaces["palette-index-0-black-shade"]
        elif shade_mode == "gradient":
            shade_value = _constant_shade_row(raw_shade)

        name = self.active_texture_name(material, frame_index)
        image = self._texture(name)
        pixels = getattr(image, "pixels", None)
        cache_key = (
            id(image), id(pixels), name, int(image.width), int(image.height),
            shade_mode, shade_value, tracy_mode, map_mode,
        )
        cached = self._surface_cache.get(cache_key)
        if cached is not None:
            return cached
        surface = IndexedSurface(
            name=name,
            kind="texture",
            indices=pixels,
            width=int(image.width),
            height=int(image.height),
            solid_index=None,
            shade_mode=shade_mode,
            shade_value=shade_value,
            tracy_mode=tracy_mode,
            map_mode=map_mode,
        )
        self._surface_cache[cache_key] = surface
        return surface


__all__ = [
    "IndexedFamilyAdapter",
    "IndexedFamilyAdapterError",
    "IndexedSourceError",
    "UnsupportedIndexedMaterialError",
]
