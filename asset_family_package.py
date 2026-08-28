"""Canonical portable package workflow for a complete :mod:`asset_family`.

The module does not parse asset formats itself.  It records files produced by
the existing BASE/SKLT/ANM/ILBM writers, validates them by reopening the BASE
through :func:`asset_family.load_asset_family`, and installs only a previously
validated package through the shared coordinated commit helper.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Iterable

from asset_family import AssetFamily, FamilyObject, load_asset_family
from asset_resolver import DirectoryIndex, normalize_logical_name
from indexed_family_adapter import IndexedFamilyAdapter
from sklt_parser import sen2_points_for_poo2
from verified_io import VerifiedCommitError, commit_verified_files


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


@dataclass(frozen=True)
class PackageEntry:
    logical_reference: str
    asset_class: str
    resolved_source: str
    exported_path: str
    dependency_relation: str
    owner_node: str = "root"
    sha256: str = ""
    status: str = "exported"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PackageValidation:
    root: Path
    manifest_path: Path | None = None
    valid: bool = False
    manifest: dict | None = None
    family: AssetFamily | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PackageImportResult:
    source_root: Path
    destination_root: Path
    entry_base: Path
    base_name: str = ""
    vp_assignment: str = ""
    copied: list[Path] = field(default_factory=list)
    identical: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation: PackageValidation | None = None


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _storage_float(value: float) -> float:
    """Canonical IEEE-754 value used by BASE/SKLT on-disk structures."""

    return struct.unpack(">f", struct.pack(">f", float(value)))[0]


def _storage_value(value):
    if isinstance(value, float):
        return _storage_float(value)
    if isinstance(value, dict):
        return {key: _storage_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_storage_value(item) for item in value]
    return value


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


def _block_snapshot(block) -> dict:
    def texture_snapshot(texture):
        if texture is None:
            return None
        return {
            "class": texture.class_id,
            "kind": texture.kind,
            "name": texture.name,
            "outline_uvs": [list(uv) for uv in texture.outline_uvs],
            "anim_type": texture.anim_type,
        }

    return {
        "class": block.class_id,
        "payload_form_type": block.payload_form_type,
        "ade_flags": block.ade_flags,
        "ade_point_id": block.ade_point_id,
        "ade_poly_id": block.ade_poly_id,
        "area_flags": block.area_flags,
        "polflags": block.polflags,
        "color": block.color_val,
        "tracy": block.tracy_val,
        "shade": block.shade_val,
        "texture": texture_snapshot(block.texture),
        "tracy_texture": texture_snapshot(block.tracy_texture),
        "atts": [
            [entry.poly_id, entry.color_val, entry.shade_val,
             entry.tracy_val, entry.pad]
            for entry in block.atts],
        "olpl": [[list(uv) for uv in group] for group in block.olpl],
        "particle_stages": [
            _block_snapshot(stage) for stage in block.particle_stages],
    }


def _object_snapshot(obj: FamilyObject, *, path: str,
                     animation_names: set[str],
                     texture_names: set[str]) -> dict:
    blocks = []
    for block in obj.base_object.ades:
        blocks.append(_block_snapshot(block))
        for visual in block.iter_visual_blocks():
            for texture in (visual.texture, visual.tracy_texture):
                if texture is None or not texture.name:
                    continue
                if texture.kind == "bmpanim":
                    animation_names.add(texture.name)
                else:
                    texture_names.add(texture.name)
    skeleton = obj.skeleton
    transform = obj.base_object.transform
    sensor_points = (
        sen2_points_for_poo2(skeleton, skeleton.points)
        if skeleton is not None else ())
    return {
        "path": path,
        "name": obj.base_object.name,
        "skeleton_name": obj.base_object.skeleton_name,
        "transform": (
            _storage_value(asdict(transform))
            if transform is not None else None),
        "ades": blocks,
        "embedded": [
            [resource.class_id, resource.resource_name,
             resource.payload_tag, resource.payload_form_type,
             resource.payload_size]
            for resource in obj.base_object.embedded],
        "unknown_chunks": list(obj.base_object.unknown_chunks),
        "skeleton": None if skeleton is None else {
            "points": [_storage_value(point) for point in skeleton.points],
            "polygons": [list(poly) for poly in skeleton.polygons],
            "sensors": [_storage_value(point) for point in sensor_points],
        },
        "kids": [
            _object_snapshot(
                kid, path=f"{path}/kid[{index}]",
                animation_names=animation_names,
                texture_names=texture_names)
            for index, kid in enumerate(obj.kids)],
    }


def family_semantic_snapshot(
        family: AssetFamily,
        root_object: FamilyObject | None = None) -> dict:
    """Stable semantic tree used by export/import round-trip validation."""

    root = root_object or family.root_object
    if root is None:
        raise AssetFamilyPackageError("asset family has no BASE root object")
    animation_names: set[str] = set()
    texture_names: set[str] = set()
    object_tree = _object_snapshot(
        root, path="root", animation_names=animation_names,
        texture_names=texture_names)

    animations = {}
    for name in sorted(animation_names, key=str.casefold):
        animation = next((value for key, value in family.animations.items()
                          if key.casefold() == name.casefold()), None)
        if animation is None:
            animations[name] = None
            continue
        texture_names.update(animation.bitmap_names)
        animations[name] = {
            "bitmap_class": animation.bitmap_class,
            "bitmap_names": list(animation.bitmap_names),
            "texcoord_groups": [
                [list(uv) for uv in group]
                for group in animation.texcoord_groups],
            "frames": [
                [frame.frame_time, frame.frame_id, frame.texcoords_id]
                for frame in animation.frames],
        }

    textures = {}
    for name in sorted(texture_names, key=str.casefold):
        image = next((value for key, value in family.textures.items()
                      if key.casefold() == name.casefold()), None)
        if image is None:
            textures[name] = None
            continue
        palette = (getattr(image, "palette", None)
                   or family.external_palette)
        palette_bytes = (
            bytes(channel for rgb in palette for channel in rgb)
            if palette is not None else None)
        textures[name] = {
            "width": image.width,
            "height": image.height,
            "pixels_sha256": _sha256_bytes(image.pixels),
            "palette_sha256": _sha256_bytes(palette_bytes),
        }

    dependency_graph = []
    for path, object_snapshot in _walk_snapshot_objects(object_tree):
        skeleton_name = object_snapshot["skeleton_name"]
        if skeleton_name:
            dependency_graph.append([
                path, "skeleton", skeleton_name,
                "OBJT sklt.class NAME"])
        for block_index, block in enumerate(object_snapshot["ades"]):
            for slot in ("texture", "tracy_texture"):
                texture = block[slot]
                if texture is None or not texture["name"]:
                    continue
                kind = ("animation" if texture["kind"] == "bmpanim"
                        else slot)
                dependency_graph.append([
                    path, kind, texture["name"],
                    f"ADES block #{block_index} {slot}"])
    for animation_name, animation in animations.items():
        if animation is None:
            continue
        for bitmap in animation["bitmap_names"]:
            dependency_graph.append([
                "family", "anm_bitmap", bitmap,
                f"VANM {animation_name} bitmap list"])
    dependency_graph.sort(
        key=lambda item: tuple(str(value).casefold() for value in item))
    return {
        "object_tree": object_tree,
        "animations": animations,
        "textures": textures,
        "dependency_graph": dependency_graph,
    }


def _walk_snapshot_objects(root: dict):
    yield root["path"], root
    for kid in root["kids"]:
        yield from _walk_snapshot_objects(kid)


def semantic_sha256(snapshot: dict) -> str:
    payload = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_package_manifest(
        package_root: str | Path, entry_base: str | Path,
        source_family: AssetFamily, entries: Iterable[PackageEntry], *,
        root_object: FamilyObject | None = None,
        source_name: str | None = None) -> tuple[Path, dict]:
    """Write a manifest after all staged package files have been verified."""

    root = Path(package_root).resolve()
    entry = Path(entry_base)
    if entry.is_absolute():
        try:
            entry = entry.resolve().relative_to(root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                "package entry BASE is outside the package root") from exc
    entry_path = (root / entry).resolve()
    try:
        entry_path.relative_to(root)
    except ValueError as exc:
        raise AssetFamilyPackageError(
            "package entry BASE escapes the package root") from exc
    if not entry_path.is_file():
        raise AssetFamilyPackageError(
            f"package entry BASE is missing: {entry_path}")

    normalized_entries = []
    seen: set[str] = set()
    for item in entries:
        relative = Path(item.exported_path)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                f"manifest output escapes package root: {relative}") from exc
        key = relative.as_posix().casefold()
        if key in seen:
            raise AssetFamilyPackageError(
                f"duplicate package output path: {relative}")
        seen.add(key)
        if item.status == "exported" and not path.is_file():
            raise AssetFamilyPackageError(
                f"exported package file is missing: {relative}")
        encoded = item.to_dict()
        if item.status == "exported":
            encoded["sha256"] = sha256_file(path)
        normalized_entries.append(encoded)

    snapshot = family_semantic_snapshot(source_family, root_object)
    manifest = {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "source_name": source_name or (
            str(source_family.base_path) if source_family.base_path else ""),
        "entry_base": entry.as_posix(),
        "semantic_sha256": semantic_sha256(snapshot),
        "semantic_snapshot": snapshot,
        "entries": normalized_entries,
    }
    target = root / MANIFEST_NAME
    target.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n")
    return target, manifest


def _load_manifest(root: Path, manifest_relative: str | Path) -> dict:
    relative = Path(manifest_relative)
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AssetFamilyPackageError(
            "package manifest escapes the package root") from exc
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetFamilyPackageError(
            f"could not read {relative.as_posix()}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise AssetFamilyPackageError("package manifest root must be an object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise AssetFamilyPackageError("not a Complete Asset Family manifest")
    if manifest.get("version") != MANIFEST_VERSION:
        raise AssetFamilyPackageError(
            f"unsupported package manifest version: "
            f"{manifest.get('version')!r}")
    if not isinstance(manifest.get("entries"), list):
        raise AssetFamilyPackageError("package manifest entries must be a list")
    return manifest


def validate_family_package(
        root: str | Path, *,
        manifest_relative: str | Path = MANIFEST_NAME
        ) -> PackageValidation:
    """Validate hashes, layout, isolated dependency resolution and semantics."""

    package_root = Path(root).resolve()
    relative_manifest = Path(manifest_relative)
    result = PackageValidation(
        root=package_root,
        manifest_path=(package_root / relative_manifest).resolve())
    try:
        manifest = _load_manifest(package_root, relative_manifest)
        result.manifest = manifest
        entry_text = manifest.get("entry_base")
        if not isinstance(entry_text, str) or not entry_text:
            raise AssetFamilyPackageError("manifest entry_base is missing")
        entry_base = (package_root / Path(entry_text)).resolve()
        try:
            entry_base.relative_to(package_root)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                "manifest entry_base escapes package root") from exc

        seen: set[str] = set()
        exported_paths: set[str] = set()
        entry_key = Path(entry_text).as_posix().casefold()
        entry_rows = []
        for index, item in enumerate(manifest["entries"]):
            if not isinstance(item, dict):
                raise AssetFamilyPackageError(
                    f"manifest entry #{index} is not an object")
            relative_text = item.get("exported_path")
            if not isinstance(relative_text, str) or not relative_text:
                raise AssetFamilyPackageError(
                    f"manifest entry #{index} has no exported_path")
            relative = Path(relative_text)
            target = (package_root / relative).resolve()
            try:
                target.relative_to(package_root)
            except ValueError as exc:
                raise AssetFamilyPackageError(
                    f"manifest entry escapes package root: {relative_text}"
                ) from exc
            key = relative.as_posix().casefold()
            if key in seen:
                raise AssetFamilyPackageError(
                    f"duplicate case-insensitive manifest path: {relative_text}")
            seen.add(key)
            if item.get("status") != "exported":
                continue
            exported_paths.add(key)
            if key == entry_key and item.get("asset_class") == "base.class":
                entry_rows.append(item)
            if not target.is_file():
                result.errors.append(f"missing exported file: {relative_text}")
                continue
            expected = item.get("sha256")
            actual = sha256_file(target)
            if not isinstance(expected, str) or actual != expected:
                result.errors.append(
                    f"hash mismatch: {relative_text} "
                    f"(expected {expected}, got {actual})")
        if not entry_base.is_file():
            result.errors.append(f"entry BASE is missing: {entry_text}")
        if len(entry_rows) != 1:
            result.errors.append(
                "manifest must contain exactly one exported base.class row "
                f"for entry BASE {entry_text}")
        if result.errors:
            return result

        DirectoryIndex.clear_cache()
        family = load_asset_family(
            entry_base, isolated_root=package_root)
        result.family = family
        if family.root_object is None:
            result.errors.append("entry BASE failed to parse")
        required_failures = [
            dependency for dependency in family.dependencies
            if dependency.status in ("missing", "ambiguous", "failed_load")]
        for dependency in required_failures:
            result.errors.append(
                f"{dependency.status} {dependency.kind}: "
                f"{dependency.raw_ref}")
        for dependency in family.dependencies:
            if dependency.resolved_path is None:
                continue
            resolved = dependency.resolved_path.resolve()
            try:
                relative = resolved.relative_to(package_root)
            except ValueError:
                result.errors.append(
                    f"dependency resolved outside package: "
                    f"{dependency.raw_ref} -> {resolved}")
                continue
            if relative.as_posix().casefold() not in exported_paths:
                result.errors.append(
                    f"resolved dependency is absent from manifest: "
                    f"{relative.as_posix()}")

        adapter, reason = IndexedFamilyAdapter.try_create(family)
        if adapter is None:
            result.errors.append(
                "Retail indexed profile is incomplete or inconsistent: "
                + reason)
        else:
            for profile_path in (
                    adapter.palette_path, adapter.shader_path,
                    adapter.tracy_path):
                relative = profile_path.resolve().relative_to(package_root)
                if relative.as_posix().casefold() not in exported_paths:
                    result.errors.append(
                        "indexed profile file is absent from manifest: "
                        + relative.as_posix())

        expected_snapshot = manifest.get("semantic_snapshot")
        expected_hash = manifest.get("semantic_sha256")
        if not isinstance(expected_snapshot, dict) \
                or semantic_sha256(expected_snapshot) != expected_hash:
            result.errors.append(
                "manifest semantic snapshot/hash is inconsistent")
        elif family.root_object is not None:
            actual_snapshot = family_semantic_snapshot(family)
            actual_hash = semantic_sha256(actual_snapshot)
            if actual_hash != expected_hash:
                result.errors.append(
                    "reopened package is not semantically equal to its "
                    "exported source")

        result.warnings.extend(
            warning for warning in family.warnings
            if warning not in result.warnings)
        result.valid = not result.errors
    except (AssetFamilyPackageError, OSError, ValueError) as exc:
        result.errors.append(str(exc))
    return result


def import_family_package(
        source_root: str | Path,
        destination_root: str | Path, *,
        runtime_loose: bool = False) -> PackageImportResult:
    """Install one validated package without silent differing overwrites.

    ``runtime_loose`` relocates runtime-consumed assets into the canonical
    OpenNeoUA folders: ``BASE/``, ``SKLT/``, ``ILBM/`` and ``ANM/``.  The
    entry BASE uses its internal BASE NAME; unsupported package-only files
    keep their portable package paths.  A destination manifest reflecting the
    relocation is staged and validated with the files.
    """

    source = Path(source_root).resolve()
    validation = validate_family_package(source)
    if not validation.valid or validation.manifest is None:
        raise AssetFamilyPackageError(
            "package validation failed:\n" + "\n".join(validation.errors))

    destination = Path(destination_root).resolve()
    manifest = validation.manifest
    source_entry = Path(manifest["entry_base"])
    base_name = ""
    vp_assignment = ""
    destination_entry = source_entry
    destination_manifest = json.loads(json.dumps(manifest))
    runtime_targets: dict[str, Path] = {}
    if runtime_loose:
        root_object = (
            validation.family.root_object
            if validation.family is not None else None)
        base_name = str(
            getattr(getattr(root_object, "base_object", None), "name", "")
            or "")
        if not base_name:
            raise AssetFamilyPackageError(
                "runtime VP import requires a non-empty internal BASE NAME")
        safe_name = package_relative_path(
            base_name, default_name="MODEL.BASE",
            extensions=(".BASE", ".BAS"))
        if len(safe_name.parts) != 1:
            raise AssetFamilyPackageError(
                "runtime VP BASE NAME cannot contain path separators")
        destination_entry = Path("BASE") / safe_name.with_suffix(".BASE")
        vp_assignment = safe_name.with_suffix(".base").name
        destination_manifest["entry_base"] = destination_entry.as_posix()
        source_entry_key = source_entry.as_posix().casefold()
        class_folders = {
            "base.class": "BASE",
            "sklt.class": "SKLT",
            "ilbm.class": "ILBM",
            "bmpanim.class": "ANM",
        }
        matching_entries = 0
        for item in destination_manifest["entries"]:
            if item.get("status") != "exported":
                continue
            source_relative = Path(str(item.get("exported_path", "")))
            source_key = source_relative.as_posix().casefold()
            if source_key == source_entry_key:
                target_relative = destination_entry
                matching_entries += 1
            else:
                folder = class_folders.get(str(item.get("asset_class", "")))
                target_relative = (Path(folder) / source_relative.name
                                   if folder else source_relative)
            runtime_targets[source_key] = target_relative
            item["exported_path"] = target_relative.as_posix()
        if matching_entries != 1:
            raise AssetFamilyPackageError(
                "manifest does not identify exactly one exported entry BASE")

    manifest_bytes = (
        json.dumps(destination_manifest, indent=2, ensure_ascii=False)
        + "\n").encode("utf-8")
    exported: list[tuple[Path, Path]] = []
    target_keys: set[str] = set()
    for item in manifest["entries"]:
        if item.get("status") != "exported":
            continue
        source_relative = Path(item["exported_path"])
        target_relative = runtime_targets.get(
            source_relative.as_posix().casefold(), source_relative)
        key = target_relative.as_posix().casefold()
        if key in target_keys:
            raise AssetFamilyPackageError(
                f"import entries collide at {target_relative}")
        target_keys.add(key)
        exported.append((source_relative, target_relative))
    result = PackageImportResult(
        source_root=source, destination_root=destination,
        entry_base=destination / destination_entry,
        base_name=base_name, vp_assignment=vp_assignment)
    copies: list[tuple[Path, Path]] = []
    new_targets: list[Path] = []
    for source_relative, target_relative in exported:
        source_file = (source / source_relative).resolve()
        target = (destination / target_relative).resolve()
        try:
            source_file.relative_to(source)
            target.relative_to(destination)
        except ValueError as exc:
            raise AssetFamilyPackageError(
                f"package path escapes its root: {target_relative}") from exc
        if target.is_file():
            if sha256_file(target) == sha256_file(source_file):
                result.identical.append(target)
                continue
            raise AssetFamilyPackageError(
                "import collision with different content: " + str(target))
        if target.exists():
            raise AssetFamilyPackageError(
                "import target exists but is not a file: " + str(target))
        copies.append((source_file, target))
        new_targets.append(target)

    destination_manifest_relative = Path(MANIFEST_NAME)
    if runtime_loose:
        destination_manifest_relative = destination_entry.with_suffix(
            ".asset_family_manifest.json")
    manifest_target = destination / destination_manifest_relative
    if manifest_target.is_file():
        if manifest_target.read_bytes() == manifest_bytes:
            result.identical.append(manifest_target)
        else:
            raise AssetFamilyPackageError(
                "import collision with different content: "
                + str(manifest_target))
    elif manifest_target.exists():
        raise AssetFamilyPackageError(
            "import target exists but is not a file: "
            + str(manifest_target))
    else:
        new_targets.append(manifest_target)

    # Validation above is read-only.  Only now may the destination tree be
    # created and populated.
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
                prefix="OpenNeoUAStudio_import_",
                dir=destination.parent) as temp_dir:
            stage_root = Path(temp_dir)
            staged_pairs = []
            for source_file, target in copies:
                relative = target.relative_to(destination)
                staged = stage_root / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, staged)
                if sha256_file(staged) != sha256_file(source_file):
                    raise AssetFamilyPackageError(
                        f"staging hash mismatch: {relative}")
                staged_pairs.append((staged, target))
            if manifest_target in new_targets:
                staged_manifest = stage_root / destination_manifest_relative
                staged_manifest.parent.mkdir(parents=True, exist_ok=True)
                staged_manifest.write_bytes(manifest_bytes)
                staged_pairs.append((staged_manifest, manifest_target))
            validation_holder: dict[str, PackageValidation] = {}

            def verify_import() -> None:
                DirectoryIndex.clear_cache()
                imported_validation = validate_family_package(
                    destination,
                    manifest_relative=destination_manifest_relative)
                validation_holder["result"] = imported_validation
                if not imported_validation.valid:
                    raise AssetFamilyPackageError(
                        "installed package failed isolated reload:\n"
                        + "\n".join(imported_validation.errors))

            result.warnings.extend(commit_verified_files(
                staged_pairs, verify=verify_import))
        result.copied.extend(new_targets)
        result.validation = validation_holder["result"]
    except (OSError, VerifiedCommitError, AssetFamilyPackageError) as exc:
        rollback_errors = []
        for target in reversed(new_targets):
            try:
                if target.is_file():
                    target.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        message = str(exc)
        if rollback_errors:
            message += ("\nRollback cleanup failed:\n"
                        + "\n".join(rollback_errors))
        raise AssetFamilyPackageError(message) from exc
    return result


__all__ = [
    "MANIFEST_NAME",
    "AssetFamilyPackageError",
    "PackageEntry",
    "PackageValidation",
    "PackageImportResult",
    "package_relative_path",
    "family_semantic_snapshot",
    "semantic_sha256",
    "write_package_manifest",
    "validate_family_package",
    "import_family_package",
]
