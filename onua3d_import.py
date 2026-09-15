"""ONUA3D V1 position-only import into the embedded Complete Asset Family.

GLB is a source of validated local point deltas, never a native asset source.
Native parsing, writing, staging validation and output publication stay in the
existing AssetFamily, SKLT and verified_io paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import tempfile
import zipfile

from asset_family import AssetFamily, load_asset_family
from asset_family_package import (
    AssetFamilyPackageError, MANIFEST_NAME, PackageEntry,
    family_semantic_snapshot, validate_family_package, write_package_manifest,
)
from onua3d_export import build_scene
from onua3d_package import (Onua3dImportError, TRANSFORM_EPSILON, node_matrix,
    _Scene, _require, _json, _read_package, validate_scene_positions)
from sklt_parser import SkltParseError, parse_sklt_file, save_sklt_with_poo2_points
from verified_io import commit_verified_files


@dataclass
class Onua3dImportResult:
    family: AssetFamily
    output_root: Path
    changed_points: int
    changed_skeletons: tuple[str, ...]


def _validated(root):
    result = validate_family_package(root)
    _require(result.valid and result.family is not None,
             "Invalid Complete Asset Family: " + "; ".join(result.errors))
    return result


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse_or_link(path: Path, info) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _destination_path(output_root) -> Path:
    """Return an absolute destination after checking every existing component.

    The walk intentionally uses ``lstat`` before ``resolve``.  A symlink,
    junction or Windows reparse point in any existing ancestor would otherwise
    let the selected destination escape into a different tree.
    """

    try:
        absolute = Path(os.path.abspath(os.fspath(output_root)))
    except (TypeError, ValueError, OSError) as exc:
        raise Onua3dImportError(
            f"Invalid Asset Family destination: {output_root}") from exc

    current = Path(absolute.anchor) if absolute.anchor else Path.cwd()
    for part in absolute.parts:
        if part == absolute.anchor:
            continue
        current /= part
        info = _lstat(current)
        if info is None:
            break
        _require(
            not _is_reparse_or_link(current, info),
            "Asset Family destination uses a symlink/junction/reparse point: "
            + str(current),
        )
        _require(
            stat.S_ISDIR(info.st_mode),
            "Asset Family destination ancestor is not a directory: "
            + str(current),
        )

    info = _lstat(absolute)
    if info is not None:
        _require(
            not _is_reparse_or_link(absolute, info),
            "Asset Family destination uses a symlink/junction/reparse point: "
            + str(absolute),
        )
        _require(
            stat.S_ISDIR(info.st_mode),
            "Asset Family destination is not a directory: " + str(absolute),
        )
        try:
            _require(
                not any(absolute.iterdir()),
                "Asset Family destination must be empty: " + str(absolute),
            )
        except OSError as exc:
            raise Onua3dImportError(
                "Cannot inspect Asset Family destination: " + str(absolute)
            ) from exc

    try:
        return absolute.resolve(strict=False)
    except OSError as exc:
        raise Onua3dImportError(
            "Cannot resolve Asset Family destination: " + str(absolute)
        ) from exc


def _destination_inventory(root: Path) -> tuple[set[str], set[str]]:
    files, entries = set(), set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = _lstat(path)
        _require(info is not None,
                 "Asset Family output entry disappeared: " + relative)
        _require(
            not _is_reparse_or_link(path, info),
            "Asset Family output contains a symlink/junction/reparse point: "
            + relative,
        )
        if stat.S_ISREG(info.st_mode):
            files.add(relative)
        elif not stat.S_ISDIR(info.st_mode):
            raise Onua3dImportError(
                "Asset Family output contains a non-file entry: " + relative)
        entries.add(relative)
    return files, entries


def _expected_inventory(files: set[str]) -> set[str]:
    entries = set(files)
    for name in files:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            entries.add(parent.as_posix())
            parent = parent.parent
    return entries


def _verify_materialized(root: Path, expected_files: set[str]):
    actual_files, actual_entries = _destination_inventory(root)
    _require(
        actual_files == expected_files,
        "Published Asset Family file inventory differs from the staged inventory",
    )
    _require(
        actual_entries == _expected_inventory(expected_files),
        "Published Asset Family directory inventory differs from the staged inventory",
    )
    return _validated(root)


@dataclass
class PreparedOnua3d:
    """Fully validated native staging kept alive by :func:`prepare_onua3d`."""

    staging_root: Path
    expected_files: frozenset[str]
    family: AssetFamily
    changed_points: int
    changed_skeletons: tuple[str, ...]
    _materialized: bool = False

    def materialize(self, output_root) -> Onua3dImportResult:
        """Publish this staging tree into the exact new/empty destination."""

        _require(
            not self._materialized,
            "Prepared ONUA3D staging has already been materialized",
        )
        output = _destination_path(output_root)
        expected_files = set(self.expected_files)
        files = [
            (self.staging_root / name, output / name)
            for name in sorted(expected_files)
        ]
        _require(
            all(source.is_file() for source, _ in files),
            "Prepared ONUA3D staging is incomplete",
        )

        # Repeat the destination check immediately before the no-clobber
        # publication.  The first check protects the caller's selection while
        # the staged file list is built; this one closes the normal race
        # window before commit_verified_files starts creating output entries.
        output = _destination_path(output)
        verified: list = []

        def verify() -> None:
            verified.append(_verify_materialized(output, expected_files))

        commit_verified_files(
            files,
            verify=verify,
            replace_existing=False,
        )
        _require(
            verified and verified[0].family is not None,
            "Published Asset Family did not pass final validation",
        )
        self._materialized = True
        return Onua3dImportResult(
            verified[0].family,
            output,
            self.changed_points,
            self.changed_skeletons,
        )


@contextmanager
def prepare_onua3d(source):
    """Validate and stage an ONUA3D package before choosing its destination.

    The temporary directory remains alive through ``yield`` so callers can
    inspect or materialize the already-validated native staging without
    repeating the importer pipeline.
    """

    try:
        manifest, members = _read_package(source)
        with tempfile.TemporaryDirectory(
                prefix="OpenNeoUAStudio_onua3d_import_") as temporary:
            root = Path(temporary)
            for name, data in members.items():
                if name.startswith("ua_family/"):
                    target = root / name[len("ua_family/"):]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            _json((root / MANIFEST_NAME).read_bytes())
            validation = _validated(root)
            native = validation.manifest
            _require(
                manifest.get("semantic_sha256") == native["semantic_sha256"],
                "Embedded family semantic hash mismatch",
            )
            expected_files = {
                MANIFEST_NAME,
                *[
                    e["exported_path"] for e in native["entries"]
                    if e["status"] == "exported"
                ],
            }
            canonical_files = {
                name.casefold(): name for name in expected_files
            }
            _require(
                {
                    p.relative_to(root).as_posix()
                    for p in root.rglob("*")
                    if p.is_file()
                }
                == expected_files,
                "Unexpected files in embedded family",
            )
            source_owner = manifest["nodes"][0]["source_owner_path"]
            baseline, _, mapping, _ = build_scene(
                validation.family, source_owner_path=source_owner)
            _require(
                manifest["nodes"] == mapping,
                "Manifest owner/point/polygon/corner/block mapping differs from native baseline",
            )
            updates = validate_scene_positions(
                manifest, baseline, members["scene.glb"])
            expected_snapshot = copy.deepcopy(native["semantic_snapshot"])
            physical, changed_owners = {}, {}
            for obj in validation.family.all_objects():
                model = obj.skeleton
                if model is None:
                    continue
                # Preserve the original storage representation (including
                # signed zeros) for every point without an actual coordinate
                # change.
                points = [
                    updates.get((obj.owner_path, i), point)
                    for i, point in enumerate(model.points)
                ]
                points = [
                    tuple(a if a == b else b for a, b in zip(old, new))
                    for old, new in zip(model.points, points)
                ]
                ref = obj.skeleton_ref
                changed = [
                    i for i, point in enumerate(points)
                    if point != model.points[i]
                ]
                if ref is None or ref.path is None:
                    _require(
                        not changed,
                        f"{obj.owner_path}: POO2 is not a standalone SKLT resource",
                    )
                    continue
                path = Path(ref.path).resolve()
                relative = canonical_files[
                    path.relative_to(root).as_posix().casefold()
                ]
                _require(
                    relative not in physical
                    or physical[relative][1] == points,
                    f"Conflicting positions for shared native SKLT resource: {relative}",
                )
                physical[relative] = (model, points, changed)
                if changed:
                    changed_owners[obj.owner_path] = points

            def update_snapshot(obj):
                if obj["path"] in changed_owners:
                    obj["skeleton"]["points"] = [
                        list(p) for p in changed_owners[obj["path"]]
                    ]
                for kid in obj["kids"]:
                    update_snapshot(kid)

            update_snapshot(expected_snapshot["object_tree"])
            changed_files, count = [], 0
            for relative, (model, points, changed) in physical.items():
                if not changed:
                    continue
                target = root / relative
                original = target.read_bytes()
                save_sklt_with_poo2_points(
                    model, points, target, update_sensors=False)
                actual = target.read_bytes()
                expected = bytearray(original)
                for i in changed:
                    struct.pack_into(
                        ">fff",
                        expected,
                        model.poo2_payload_offset + i * 12,
                        *points[i],
                    )
                _require(
                    actual == expected,
                    f"Writer changed bytes outside requested POO2 points: {relative}",
                )
                _require(
                    parse_sklt_file(target).points == points,
                    f"SKLT readback mismatch: {relative}",
                )
                changed_files.append(relative)
                count += len(changed)
            if changed_files:
                edited = load_asset_family(
                    root / native["entry_base"], isolated_root=root)
                _require(
                    family_semantic_snapshot(edited) == expected_snapshot,
                    "Imported family changed semantics outside POO2",
                )
                write_package_manifest(
                    root,
                    native["entry_base"],
                    edited,
                    [PackageEntry(**row) for row in native["entries"]],
                    source_name=native["source_name"],
                )
            final_validation = _validated(root)
            yield PreparedOnua3d(
                root,
                frozenset(expected_files),
                final_validation.family,
                count,
                tuple(sorted(changed_files)),
            )
    except Onua3dImportError:
        raise
    except (AssetFamilyPackageError, OSError, SkltParseError, ValueError,
            KeyError, TypeError, IndexError, AttributeError, struct.error,
            OverflowError, zipfile.BadZipFile) as exc:
        raise Onua3dImportError(f"ONUA3D import failed: {exc}") from exc


def import_onua3d(source, output_root):
    """Backward-compatible validate, stage and publish wrapper."""

    with prepare_onua3d(source) as prepared:
        return prepared.materialize(output_root)
