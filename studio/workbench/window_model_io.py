"""AssemblyWindow mixin: model files: bundle paths, save / save as / overwrite, SKLT writing and
Blender (ONUA3D) import/export."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import shutil
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QMessageBox,
)

from studio.assets.family import (
    AssetFamily,
    load_asset_family,
    PackageEntry,
    validate_family_package,
    write_package_manifest,
)
from studio.assets.mapping import (
    diagnose_polygon_references,
    export_base_object_bytes,
    MappingEditError,
    save_model_base_copy,
    StructuralBlockState,
    TextureNameEdit,
    UVEdit,
)
from studio.exchange.portable import (
    AssetFamilyPackageError,
    backup_enabled,
    commit_verified_files as _commit_verified_files,
    DirectoryIndex,
    MANIFEST_NAME as ASSET_FAMILY_MANIFEST,
    next_backup_path as _next_backup_path,
    package_relative_path,
    VerifiedCommitError as _BundleCommitError,
)
from studio.formats.animation import AnmParseError, export_anm_bytes, parse_anm_bytes
from studio.formats.base_format import parse_base_bytes
from studio.formats.images import TextureConvertError, write_image_as_ilbm
from studio.formats.sklt import (
    parse_sklt_bytes,
    parse_sklt_file,
    save_sklt_with_poo2_points,
    save_sklt_with_poo2_pol2_structure,
    sen2_points_for_poo2,
    SkltParseError,
)
from studio.rendering.indexed import IndexedFamilyAdapter
from studio.rendering.geometry import (
    build_topology_states,
)


class _ModelFilesMixin:
    """Part of AssemblyWindow; `self` is always the AssemblyWindow."""

    @staticmethod
    def _flat_asset_family_path(relative: Path) -> Path:
        """Keep model assets flat; PALETTE/REMAP are the only subfolders."""

        return Path(relative.name)

    def _bundle_skeleton_relative_path(self, fam_obj) -> Path:
        logical = (fam_obj.base_object.skeleton_name
                   or getattr(fam_obj.skeleton, "source_name", "")
                   or "MODEL.SKLT")
        if logical.upper().startswith("SET.BAS:"):
            logical = logical[len("SET.BAS:"):]
        relative = package_relative_path(
            logical, default_name="MODEL.SKLT",
            extensions=(".SKLT", ".SKL"))
        return self._flat_asset_family_path(relative)

    @staticmethod
    def _bundle_animation_relative_path(name: str) -> Path:
        relative = package_relative_path(
            name, default_name="ANIMATION.ANM",
            extensions=(".ANM", ".VANM"))
        return _ModelFilesMixin._flat_asset_family_path(relative)

    @staticmethod
    def _bundle_texture_relative_path(name: str) -> Path:
        relative = package_relative_path(
            name, default_name="TEXTURE.ILBM",
            extensions=(".ILBM", ".ILB", ".LBM", ".IFF", ".VBMP"))
        return _ModelFilesMixin._flat_asset_family_path(relative)

    @staticmethod
    def _matching_ref(mapping: dict, logical_name: str):
        return next((value for key, value in mapping.items()
                     if str(key).casefold() == str(logical_name).casefold()),
                    None)

    def _texture_export_source(self, family: AssetFamily, name: str):
        """Return raw texture bytes and its loose source path when available."""

        ref = self._matching_ref(family.texture_refs, name)
        source = (Path(ref.path) if ref is not None
                  and getattr(ref, "path", None) else None)
        if source is not None and source.is_file():
            return source.read_bytes(), source
        archive = family.setbas_archive or self._setbas
        if archive is not None:
            resources = [entry for entry in archive.find(
                name, "ilbm.class") if entry.decodable]
            if len(resources) > 1:
                raise MappingEditError(
                    f"Texture dependency {name} has {len(resources)} "
                    "embedded matches; none can be exported safely.")
            if len(resources) == 1:
                return archive.payload_bytes(resources[0]), None
        return None, source

    def _bundle_base_edits(self, owner: str, fam_obj):
        texture_edits: list[TextureNameEdit] = []
        for (changed_owner, block_index, binding_slot), original in \
                self._texture_original.items():
            if changed_owner != owner:
                continue
            blocks = fam_obj.base_object.ades
            if not (0 <= block_index < len(blocks)) \
                    or binding_slot not in ("texture", "tracy_texture") \
                    or getattr(blocks[block_index], binding_slot) is None:
                raise MappingEditError(
                    "an edited texture binding no longer exists")
            name = getattr(blocks[block_index], binding_slot).name
            if name.casefold() != original.casefold():
                texture_edits.append(TextureNameEdit(
                    "root", block_index, name, binding_slot))

        uv_edits: list[UVEdit] = []
        for changed_owner, block_index, atts_index in self._uv_original:
            if changed_owner != owner:
                continue
            blocks = fam_obj.base_object.ades
            if block_index >= len(blocks) \
                    or atts_index >= len(blocks[block_index].olpl):
                raise MappingEditError(
                    "an edited UV group no longer exists in the selected BASE")
            if (blocks[block_index].class_id or "").casefold() == "area.class":
                continue
            uv_edits.append(UVEdit(
                "root", block_index, atts_index,
                list(blocks[block_index].olpl[atts_index])))
        return uv_edits, texture_edits

    def _model_save_context(self, owner: str | None = None):
        owner = owner or self._selected_owner
        family = self._family
        fam_obj = self._owner_to_obj.get(owner) if owner else None
        model = getattr(fam_obj, "skeleton", None)
        base_asset = getattr(family, "base_asset", None)
        tree = getattr(base_asset, "tree", None)
        if owner is None or fam_obj is None or model is None \
                or tree is None:
            QMessageBox.information(
                self, "Export unavailable",
                "This model does not contain both model and BASE data to "
                "export.")
            return None
        return owner, family, fam_obj, model, tree

    def _bundle_topology_states(
            self, data: bytes, fam_obj) -> list[StructuralBlockState]:
        return build_topology_states(
            data, fam_obj,
            self._family.animations if self._family else {})

    @staticmethod
    def _adopt_saved_block_sources(fam_obj, data: bytes) -> None:
        parsed = parse_base_bytes(data, "<saved-block-sources>")
        if parsed.root is None \
                or len(parsed.root.ades) != len(fam_obj.base_object.ades):
            raise MappingEditError(
                "verified BASE block sources do not match in-memory ADES")
        source_fields = (
            "payload_form_type", "source_ades_index",
            "source_objt_offset", "source_objt_size", "source_objt_bytes",
            "ade_strc_chunk_offset", "ade_strc_chunk_size",
            "atts_chunk_offset", "atts_chunk_size",
            "olpl_chunk_offset", "olpl_chunk_size",
        )
        for current, saved in zip(
                fam_obj.base_object.ades, parsed.root.ades):
            if (current.class_id or "").lower() \
                    != (saved.class_id or "").lower():
                raise MappingEditError(
                    "verified BASE block class order changed")
            for name in source_fields:
                setattr(current, name, copy.deepcopy(getattr(saved, name)))

    @staticmethod
    def _write_verified_sklt(model, output_path: str | Path):
        """Write a SKLT copy and verify its geometry round-trip."""

        original = parse_sklt_bytes(
            model.original_data, "<model-save-baseline>")
        structural = (
            len(model.points) != len(original.points)
            or model.polygons != original.polygons
        )
        if structural:
            save_sklt_with_poo2_pol2_structure(
                model, model.points, model.polygons, output_path)
        else:
            save_sklt_with_poo2_points(model, model.points, output_path)
        verified = parse_sklt_file(output_path)
        expected_sensors = sen2_points_for_poo2(model, model.points)
        matches = (
            len(verified.points) == len(model.points)
            and verified.polygons == model.polygons
            and len(verified.sensors) == len(expected_sensors)
            and all(
                abs(a[axis] - b[axis])
                <= 1e-3 + abs(b[axis]) * 1e-5
                for a, b in zip(verified.sensors, expected_sensors)
                for axis in range(3)
            )
            and all(
                abs(a[axis] - b[axis])
                <= 1e-3 + abs(b[axis]) * 1e-5
                for a, b in zip(verified.points, model.points)
                for axis in range(3)
            )
        )
        if not matches:
            raise MappingEditError(
                "exported SKLT failed coordinate round-trip verification")
        return verified

    def _owners_for_component(self, kind: str, name: str) -> list[str]:
        family = self._family
        if family is None:
            return []
        folded = str(name).casefold()
        owners = []
        for fam_obj in family.all_objects():
            matched = False
            if kind == "skeleton":
                matched = str(fam_obj.base_object.skeleton_name).casefold() \
                    == folded
            else:
                animation_names = set()
                texture_names = set()
                for root_block in fam_obj.base_object.ades:
                    blocks = (root_block.iter_visual_blocks()
                              if hasattr(root_block, "iter_visual_blocks")
                              else (root_block,))
                    for block in blocks:
                        for texture in (
                                getattr(block, "texture", None),
                                getattr(block, "tracy_texture", None)):
                            texture_name = str(
                                getattr(texture, "name", "") or "")
                            if not texture_name:
                                continue
                            if str(getattr(texture, "kind", "")).casefold() \
                                    == "bmpanim":
                                animation_names.add(texture_name)
                            else:
                                texture_names.add(texture_name)
                if kind == "animation":
                    matched = any(value.casefold() == folded
                                  for value in animation_names)
                else:
                    matched = any(value.casefold() == folded
                                  for value in texture_names)
                    if not matched:
                        for animation_name in animation_names:
                            animation = next((
                                value for key, value in family.animations.items()
                                if str(key).casefold()
                                == animation_name.casefold()), None)
                            if any(str(bitmap).casefold() == folded for bitmap in
                                   getattr(animation, "bitmap_names", ())):
                                matched = True
                                break
            if matched:
                owners.append(fam_obj.owner_path)
        return owners

    def _export_owner_for_selection(self) -> str | None:
        component = self._last_component_selection
        if component is None:
            item = self.asset_tree.currentItem()
            data = item.data(
                0, Qt.ItemDataRole.UserRole) if item is not None else None
            if data and data[0] in ("skeleton", "texture", "animation"):
                kind, payload = data
                name = (payload.base_object.skeleton_name
                        if kind == "skeleton" else str(payload))
                component = kind, name
        if component is None:
            return self._selected_owner
        kind, name = component
        owners = self._owners_for_component(kind, name)
        if not owners:
            QMessageBox.information(
                self, "Asset Family unavailable",
                f"No BASE owner could be proven for {name}; nothing was "
                "selected arbitrarily.")
            return None
        if len(owners) == 1:
            return owners[0]
        labels = []
        label_to_owner = {}
        for owner in owners:
            fam_obj = self._owner_to_obj[owner]
            label = f"{self._base_entry_name(self._family, fam_obj, owner)} [{owner}]"
            labels.append(label)
            label_to_owner[label] = owner
        chosen, accepted = QInputDialog.getItem(
            self, "Choose BASE owner",
            f"{name} is shared by multiple BASE entries. Export which family?",
            labels, 0, False)
        return label_to_owner.get(chosen) if accepted else None

    def _blender_export_object(self):
        family = self._family
        if family is None or family.base_asset is None or family.root_object is None:
            return None
        if family.base_path and Path(family.base_path).suffix.casefold() == ".bas":
            # The active archive BASE is already proven by the existing VP
            # selection path. Never export the entire SET or guess an owner.
            if len(self._base_entry_names) != 1:
                return None
            return self._owner_to_obj.get(next(iter(self._base_entry_names)))
        return family.root_object

    def _import_onua3d(self) -> None:
        """Open a validated position-only import as a new Complete Asset Family."""
        from studio.exchange.onua3d import prepare_onua3d

        source, _ = QFileDialog.getOpenFileName(
            self, "Import OpenNeoUA 3D", str(self._last_directory),
            "OpenNeoUA 3D package (*.onua3d)")
        if not source:
            return
        try:
            with prepare_onua3d(source) as prepared:
                if not self._confirm_discard_geometry():
                    return
                target = QFileDialog.getExistingDirectory(
                    self,
                    "Choose empty Asset Family destination folder",
                    str(self._last_directory),
                )
                if not target:
                    return
                result = prepared.materialize(target)
        except (AssetFamilyPackageError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "OpenNeoUA 3D import failed", str(exc))
            return
        self._overrides = {}
        self._trial_names = set()
        self._kept_names = set()
        self._skipped_names = set()
        self._last_directory = result.output_root
        self._set_family(result.family)
        self._notify(
            f"Imported {Path(source).name}: {result.changed_points} SKLT point(s) updated. "
            f"Complete Asset Family saved to {target}.", 15000)

    def _export_to_blender(self) -> None:
        """Export the loaded BASE root, including KIDS, through bundle staging."""
        from studio.exchange.onua3d import write_onua3d

        family = self._family
        obj = self._blender_export_object()
        if obj is None or self.viewport.paste_preview_active:
            self._notify("Load a BASE family before exporting OpenNeoUA 3D.", 5000)
            return
        stem = re.sub(r'[^A-Za-z0-9_.-]', "_", obj.base_object.name or "MODEL")
        stem = Path(stem).stem or "MODEL"
        output, _ = QFileDialog.getSaveFileName(
            self, "Export OpenNeoUA 3D", str(self._last_directory / (stem + ".onua3d")),
            "OpenNeoUA 3D package (*.onua3d)")
        if not output:
            return
        target = Path(output).with_suffix(".onua3d")
        try:
            relative = self._bundle_skeleton_relative_path(obj)
            with tempfile.TemporaryDirectory(prefix="OpenNeoUAStudio_onua3d_") as temp:
                root = Path(temp)
                exported = self._write_model_files(
                    obj.owner_path, family, obj, root / relative,
                    root / (stem + ".BASE"), ask_replace=False,
                    staged_consumer=lambda staged, validation: write_onua3d(
                        staged, target, validation=validation,
                        source_owner_path=obj.owner_path))
        except (AssetFamilyPackageError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "OpenNeoUA 3D export failed", str(exc))
            return
        if exported:
            self._last_directory = target.parent
            self._notify(f"Exported {target.name} as an OpenNeoUA 3D package.", 12000)

    def _save_model_as(self) -> None:
        owner = self._export_owner_for_selection()
        if owner is None:
            return
        context = self._model_save_context(owner)
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        skeleton_relative = self._bundle_skeleton_relative_path(fam_obj)
        base_label = (fam_obj.base_object.name
                      or skeleton_relative.stem)
        base_stem = re.sub(r'[^A-Za-z0-9_.-]', "_",
                           base_label) or "MODEL"
        if Path(base_stem).suffix.casefold() == ".base":
            base_stem = Path(base_stem).stem or "MODEL"
        output = QFileDialog.getExistingDirectory(
            self, "Choose Asset Family destination folder",
            str(self._last_directory))
        if not output:
            self._notify("Asset-family export cancelled.", 3000)
            return
        output_root = Path(output)
        base_target = output_root / f"{base_stem}.BASE"
        skeleton_target = output_root / skeleton_relative
        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=True):
            self._bundle_targets[owner] = (skeleton_target, base_target)
            self._last_directory = output_root
            self._sync_geometry_save_controls()
            self._notify(
                f"Exported complete asset family rooted at "
                f"{base_target.name}.", 9000)

    def _overwrite_model(self) -> None:
        sklt_context = self._sklt_save_context()
        if sklt_context is not None:
            owner, _family, fam_obj, model = sklt_context
            source = self._standalone_sklt_source(fam_obj)
            if source is not None:
                self._notify(
                    f"Overwrite requested for {source.name}; confirmation is "
                    "required." + (" A .bak backup will be created."
                                   if backup_enabled() else ""), 12000)
                answer = QMessageBox.question(
                    self, "Overwrite SKLT",
                    f"Overwrite the imported skeleton?\n\n{source}"
                    + ("\n\nA .bak copy will be created first."
                       if backup_enabled() else ""),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                backup = _next_backup_path(source) if backup_enabled() else None
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(
                            prefix="OpenNeoUAStudio_overwrite_sklt_") as temp_dir:
                        temporary = Path(temp_dir) / source.name
                        self._write_verified_sklt(model, temporary)
                        warnings = _commit_verified_files(
                            [(temporary, source)])
                except (MappingEditError, OSError, SkltParseError,
                        _BundleCommitError) as exc:
                    QMessageBox.critical(
                        self, "Overwrite SKLT failed - original preserved",
                        f"{exc}\n\nBackup: "
                        f"{backup if backup and backup.exists() else '-'}")
                    return
                self._geom_dirty.pop(owner, None)
                self._geom_original.pop(owner, None)
                self._topology_original.pop(owner, None)
                details = f" Backup: {backup.name}." if backup else ""
                if warnings:
                    details += " " + " ".join(warnings)
                self._sync_geometry_save_controls()
                self._update_editor_status()
                self._notify(
                    f"SKLT overwritten safely: {source.name}.{details}",
                    10000)
                return

        context = self._model_save_context()
        if context is None:
            return
        owner, family, fam_obj, _model, _tree = context
        targets = self._bundle_targets.get(owner)
        inplace = False
        if targets is None:
            targets = self._loose_base_targets(fam_obj)
            inplace = targets is not None
        if targets is None:
            self._notify(
                "Export Asset Family before Overwrite.", 5000)
            return
        skeleton_target, base_target = targets
        if inplace:
            answer = QMessageBox.question(
                self, "Overwrite BASE",
                f"Overwrite the model opened from disk?\n\n{base_target}\n"
                f"{skeleton_target}"
                + ("\n\nA .bak copy of each file is created first."
                   if backup_enabled() else ""),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self._write_model_files(
                owner, family, fam_obj, skeleton_target, base_target,
                ask_replace=False, inplace=inplace):
            self._notify(
                (f"BASE and skeleton overwritten safely"
                 f"{' (.bak kept)' if backup_enabled() else ''}: "
                 f"{base_target.name}." if inplace else
                 f"Overwritten complete asset family rooted at "
                 f"{base_target.name}."), 9000)

    @staticmethod
    def _visual_reference_names(family_objects) -> tuple[set[str], set[str]]:
        """Collect direct animation and texture refs for model objects."""

        animation_names: set[str] = set()
        texture_names: set[str] = set()
        for current_obj in family_objects:
            for block in current_obj.base_object.ades:
                visual_blocks = (block.iter_visual_blocks()
                                 if hasattr(block, "iter_visual_blocks")
                                 else (block,))
                for visual in visual_blocks:
                    for texture in (
                            getattr(visual, "texture", None),
                            getattr(visual, "tracy_texture", None)):
                        name = str(getattr(texture, "name", "") or "")
                        if not name:
                            continue
                        if str(getattr(texture, "kind", "")).casefold() \
                                == "bmpanim":
                            animation_names.add(name)
                        else:
                            texture_names.add(name)
        return animation_names, texture_names

    def _write_model_files(self, owner: str, family: AssetFamily, fam_obj,
                           skeleton_target: Path, base_target: Path,
                           *, ask_replace: bool, staged_consumer=None,
                           inplace: bool = False) -> bool:
        """Export one portable Asset Family package.

        ``inplace`` overwrites the BASE and skeletons the model was opened from:
        the package is still built and validated in a temporary folder, but only
        the BASE and skeleton files are replaced (after .bak copies, if enabled) and the
        game folder receives no other file.
        """

        model = fam_obj.skeleton
        tree = family.base_asset.tree
        source_archive_digest = hashlib.sha256(tree.data).digest()
        base_source = Path(family.base_path) if family.base_path else None
        package_root = base_target.parent.resolve()
        manifest_target = package_root / ASSET_FAMILY_MANIFEST

        # Scope dependencies to the selected BASE object and its KIDS.  A
        # SET.BAS session can contain hundreds of unrelated resources; an
        # asset-family export must never dump the whole archive by accident.
        family_objects = list(fam_obj.iter_tree())
        skeleton_exports = []
        skeleton_targets_by_key: dict[str, tuple[object, Path]] = {}
        for object_index, current_obj in enumerate(family_objects):
            current_model = getattr(current_obj, "skeleton", None)
            if current_model is None:
                if staged_consumer is not None and not current_obj.base_object.skeleton_name:
                    # A structural BASE container can legitimately own only KIDS.
                    continue
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Skeleton dependency for {current_obj.display_name} is "
                    "unresolved; the asset family would be incomplete.")
                return False
            try:
                relative = self._bundle_skeleton_relative_path(current_obj)
            except AssetFamilyPackageError as exc:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    str(exc))
                return False
            ref = getattr(current_obj, "skeleton_ref", None)
            ref_path = (Path(ref.path) if ref is not None
                        and getattr(ref, "path", None) else None)
            if object_index == 0:
                target = skeleton_target
            elif inplace and ref_path is not None and ref_path.is_file():
                target = ref_path
            else:
                target = base_target.parent / relative
            key = str(target.resolve()).casefold()
            previous = skeleton_targets_by_key.get(key)
            if previous is not None:
                previous_model, previous_target = previous
                if previous_model != current_model:
                    QMessageBox.critical(
                        self, "Export failed - current files unchanged",
                        f"Two different skeletons resolve to the same output "
                        f"path: {previous_target}")
                    return False
                continue
            skeleton_targets_by_key[key] = (current_model, target)
            ref = getattr(current_obj, "skeleton_ref", None)
            source = (Path(ref.path) if ref is not None
                      and getattr(ref, "path", None) else None)
            skeleton_exports.append(
                (current_obj, current_model, relative, target, source))

        animation_names, texture_names = self._visual_reference_names(
            family_objects)

        animation_exports = []
        for canonical in sorted(animation_names, key=str.casefold):
            animation = next((
                value for key, value in family.animations.items()
                if key.casefold() == canonical.casefold()), None)
            if animation is None:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Animation dependency {canonical} is unresolved; the "
                    "asset family would be incomplete.")
                return False
            try:
                relative = self._bundle_animation_relative_path(canonical)
            except AssetFamilyPackageError as exc:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    str(exc))
                return False
            animation_exports.append((canonical, animation, relative))
            texture_names.update(animation.bitmap_names)

        texture_exports = []
        for canonical in sorted(texture_names, key=str.casefold):
            try:
                raw_data, loose_source = self._texture_export_source(
                    family, canonical)
            except OSError as exc:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Texture dependency {canonical} could not be read: "
                    f"{exc}")
                return False
            image = next((
                value for key, value in family.textures.items()
                if key.casefold() == canonical.casefold()), None)
            if raw_data is None and image is None:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    f"Texture dependency {canonical} is unresolved; the asset "
                    "family would be incomplete.")
                return False
            try:
                relative = self._bundle_texture_relative_path(canonical)
            except AssetFamilyPackageError as exc:
                QMessageBox.critical(
                    self, "Export failed - current files unchanged",
                    str(exc))
                return False
            texture_exports.append((
                canonical, raw_data, image, relative, loose_source))

        indexed_adapter, indexed_reason = IndexedFamilyAdapter.try_create(
            family)
        if indexed_adapter is None:
            QMessageBox.critical(
                self, "Export failed - current files unchanged",
                "An Asset Family must contain one coherent SET "
                "palette/SHADERMP/TRACYRMP profile.\n\n" + indexed_reason)
            return False
        profile_exports = [
            ("SET palette", "palette.class", indexed_adapter.palette_path,
             Path("PALETTE") / indexed_adapter.palette_path.name,
             "Retail indexed palette"),
            ("SHADERMP", "shadermap.class", indexed_adapter.shader_path,
             Path("REMAP") / indexed_adapter.shader_path.name,
             "Retail indexed shade lookup"),
            ("TRACYRMP", "tracymap.class", indexed_adapter.tracy_path,
             Path("REMAP") / indexed_adapter.tracy_path.name,
             "Retail indexed transparency lookup"),
        ]

        forbidden = [
            path.resolve() for path in (base_source,)
            if path is not None and path.exists()]
        for _obj, _model, _relative, _target, source in skeleton_exports:
            if source is not None and source.exists():
                forbidden.append(source.resolve())
        for canonical, _animation, _relative in animation_exports:
            ref_entry = self._matching_ref(
                family.animation_refs, canonical)
            source = (Path(ref_entry.path)
                      if ref_entry is not None and ref_entry.path else None)
            if source is not None and source.exists():
                forbidden.append(source.resolve())
        for _canonical, _raw, _image, _relative, source in texture_exports:
            if source is not None and source.exists():
                forbidden.append(source.resolve())
        forbidden.extend(
            source.resolve() for _logical, _class, source, _relative,
            _relation in profile_exports if source.exists())

        skeleton_targets = [entry[3] for entry in skeleton_exports]
        animation_targets = [
            base_target.parent / relative
            for _name, _animation, relative in animation_exports]
        texture_targets = [
            base_target.parent / relative
            for _name, _raw, _image, relative, _source in texture_exports]
        profile_targets = [
            package_root / relative
            for _logical, _class, _source, relative, _relation
            in profile_exports]
        all_targets = [
            *skeleton_targets, base_target,
            *animation_targets, *texture_targets,
            *profile_targets]
        target_keys = [str(path.resolve()).casefold() for path in all_targets]
        if len(target_keys) != len(set(target_keys)):
            QMessageBox.critical(
                self, "Export failed - current files unchanged",
                "Two asset dependencies resolve to the same output path. "
                "Rename the conflicting source resources before exporting.")
            return False
        if not inplace and any(
                target.resolve() in forbidden for target in all_targets):
            QMessageBox.warning(
                self, "Choose another output folder",
                "Asset-family export cannot replace a BASE, skeleton, "
                "texture or animation currently open in the editor. Choose "
                "another folder, or use File > Overwrite to replace the "
                "model you opened.")
            return False

        existing = [path for path in all_targets if path.exists()]
        if ask_replace and existing:
            self._notify(
                f"Asset Family export would replace {len(existing)} existing "
                "file(s); confirmation is required.", 12000)
            answer = QMessageBox.warning(
                self, "Replace exported files?",
                "The following file(s) already exist:\n"
                + "\n".join(str(path) for path in existing)
                + "\n\nReplace them?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False

        verified_base_bytes: bytes | None = None
        try:
            standalone = self._bundle_base_snapshots.get(owner)
            if standalone is None:
                standalone = export_base_object_bytes(
                    tree.data, fam_obj.base_object)
            uv_edits, texture_edits = self._bundle_base_edits(owner, fam_obj)
            structural_blocks = (
                [] if staged_consumer is not None and model is None
                and not fam_obj.base_object.ades else
                self._bundle_topology_states(standalone, fam_obj))
            with tempfile.TemporaryDirectory(
                    prefix="OpenNeoUAStudio_bundle_") as temp_dir:
                temp_root = Path(temp_dir)

                def staged_path(target: Path) -> Path:
                    try:
                        relative_target = target.resolve().relative_to(
                            package_root)
                    except ValueError as exc:
                        raise AssetFamilyPackageError(
                            f"Package output escapes its root: {target}"
                        ) from exc
                    return temp_root / relative_target

                temp_base = staged_path(base_target)
                temp_base.parent.mkdir(parents=True, exist_ok=True)
                temp_skeletons = []
                package_entries: list[PackageEntry] = []
                for object_index, (
                        _current_obj, current_model, relative, target,
                        source) in enumerate(skeleton_exports):
                    temp_skeleton = staged_path(target)
                    temp_skeleton.parent.mkdir(parents=True, exist_ok=True)
                    self._write_verified_sklt(current_model, temp_skeleton)
                    temp_skeletons.append((temp_skeleton, target))
                    package_entries.append(PackageEntry(
                        logical_reference=(
                            _current_obj.base_object.skeleton_name
                            or relative.as_posix()),
                        asset_class="sklt.class",
                        resolved_source=(
                            str(source) if source is not None else
                            "SET.BAS:" + (
                                _current_obj.base_object.skeleton_name
                                or relative.as_posix())),
                        exported_path=target.resolve().relative_to(
                            package_root).as_posix(),
                        dependency_relation="OBJT sklt.class NAME",
                        owner_node=_current_obj.owner_path,
                    ))

                notes = save_model_base_copy(
                    standalone, uv_edits, texture_edits, temp_base,
                    structural_blocks=structural_blocks)
                verified_base_bytes = temp_base.read_bytes()
                package_entries.insert(0, PackageEntry(
                    logical_reference=base_target.name,
                    asset_class="base.class",
                    resolved_source=(
                        str(base_source) if base_source is not None else
                        str(family.setbas_path or "in-memory BASE")),
                    exported_path=base_target.resolve().relative_to(
                        package_root).as_posix(),
                    dependency_relation=(
                        "package entry point; inline KIDS and embedded "
                        "resources preserved"),
                    owner_node="root",
                ))

                temp_animations = []
                for canonical, animation, relative in animation_exports:
                    data = export_anm_bytes(animation)
                    verified = parse_anm_bytes(data, canonical)
                    if verified.texcoord_groups != animation.texcoord_groups:
                        raise AnmParseError(
                            f"{canonical} failed UV round-trip verification")
                    target = base_target.parent / relative
                    temp_animation = staged_path(target)
                    temp_animation.parent.mkdir(parents=True, exist_ok=True)
                    temp_animation.write_bytes(data)
                    temp_animations.append((temp_animation, target))
                    ref_entry = self._matching_ref(
                        family.animation_refs, canonical)
                    source = (str(ref_entry.path)
                              if ref_entry is not None and ref_entry.path
                              else f"SET.BAS:{canonical}")
                    package_entries.append(PackageEntry(
                        logical_reference=canonical,
                        asset_class="bmpanim.class",
                        resolved_source=source,
                        exported_path=target.resolve().relative_to(
                            package_root).as_posix(),
                        dependency_relation="ADES BANI animation",
                    ))

                from studio.formats.images import parse_ilbm_bytes
                temp_textures = []
                for canonical, raw_data, image, relative, _source in \
                        texture_exports:
                    target = base_target.parent / relative
                    temp_texture = staged_path(target)
                    temp_texture.parent.mkdir(parents=True, exist_ok=True)
                    parsed_raw = (parse_ilbm_bytes(raw_data, canonical)
                                  if raw_data is not None else None)
                    raw_is_loose_vbmp = (
                        parsed_raw is not None
                        and parsed_raw.kind == "VBMP"
                        and relative.suffix.casefold() == ".vbmp")
                    if parsed_raw is not None \
                            and parsed_raw.kind == "ILBM":
                        if not parsed_raw.has_body:
                            raise TextureConvertError(
                                f"{canonical}: ILBM has no decodable BODY")
                        temp_texture.write_bytes(raw_data)
                    elif raw_is_loose_vbmp:
                        if not parsed_raw.has_body:
                            raise TextureConvertError(
                                f"{canonical}: VBMP has no decodable BODY")
                        temp_texture.write_bytes(raw_data)
                    else:
                        export_image = image or parsed_raw
                        if export_image is None or not export_image.has_body:
                            raise TextureConvertError(
                                f"{canonical}: texture has no decodable BODY")
                        palette = (getattr(export_image, "palette", None)
                                   or family.external_palette)
                        if palette is None and self._setbas is not None:
                            palette, _palette_source = self._setbas_palette()
                        # Embedded SET.BAS textures are FORM VBMP. Loose
                        # dependencies expect a standalone FORM ILBM.
                        write_image_as_ilbm(
                            export_image, temp_texture, palette,
                            source=canonical)
                    verified_texture = parse_ilbm_bytes(
                        temp_texture.read_bytes(), canonical)
                    if not verified_texture.has_body:
                        raise TextureConvertError(
                            f"{canonical}: exported texture failed verification")
                    temp_textures.append((temp_texture, target))
                    ref_entry = self._matching_ref(
                        family.texture_refs, canonical)
                    source = (str(ref_entry.path)
                              if ref_entry is not None and ref_entry.path
                              else f"SET.BAS:{canonical}")
                    package_entries.append(PackageEntry(
                        logical_reference=canonical,
                        asset_class="ilbm.class",
                        resolved_source=source,
                        exported_path=target.resolve().relative_to(
                            package_root).as_posix(),
                        dependency_relation=(
                            "ADES texture/TRACY or VANM frame bitmap"),
                    ))

                temp_profiles = []
                for logical, asset_class, source, relative, relation in \
                        profile_exports:
                    target = package_root / relative
                    staged = staged_path(target)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, staged)
                    if hashlib.sha256(staged.read_bytes()).digest() \
                            != hashlib.sha256(source.read_bytes()).digest():
                        raise AssetFamilyPackageError(
                            f"Profile staging hash mismatch: {source}")
                    temp_profiles.append((staged, target))
                    package_entries.append(PackageEntry(
                        logical_reference=logical,
                        asset_class=asset_class,
                        resolved_source=str(source),
                        exported_path=relative.as_posix(),
                        dependency_relation=relation,
                    ))

                temp_manifest, _manifest = write_package_manifest(
                    temp_root,
                    temp_base.relative_to(temp_root),
                    family,
                    package_entries,
                    root_object=fam_obj,
                    source_name=str(family.base_path or family.setbas_path
                                    or base_target.name),
                )
                package_validation = validate_family_package(temp_root)
                if not package_validation.valid:
                    raise AssetFamilyPackageError(
                        "Asset Family validation failed:\n"
                        + "\n".join(package_validation.errors))
                reloaded_obj = package_validation.family.root_object
                reloaded_model = (
                    reloaded_obj.skeleton if reloaded_obj is not None
                    else None)
                if (model is not None and (reloaded_model is None
                        or reloaded_model.polygons != model.polygons)):
                    raise MappingEditError(
                        "exported BASE/SKLT family failed isolated topology "
                        "reload")
                roundtrip_issues = [
                    issue for issue in (
                        diagnose_polygon_references(reloaded_obj)
                        if reloaded_model is not None else [])
                    if "missing typed handler" not in issue]
                if roundtrip_issues:
                    raise MappingEditError(
                        "exported reference graph failed verification: "
                        + "; ".join(roundtrip_issues[:4]))
                if hashlib.sha256(tree.data).digest() \
                        != source_archive_digest:
                    raise MappingEditError(
                        "the read-only source BASE/SET.BAS bytes changed")

                # Derived portable containers consume exactly the canonical,
                # verified staging package. Do not commit loose files or mark
                # editor changes as saved when exporting a derived payload.
                if staged_consumer is not None:
                    staged_consumer(temp_root, package_validation)
                    return True

                commit_pairs = [
                    *temp_skeletons,
                    (temp_base, base_target),
                ]
                if not inplace:
                    commit_pairs += [
                        *temp_animations, *temp_textures, *temp_profiles]

                def verify_destination_package() -> None:
                    # The JSON manifest is a staging/verification aid, not a
                    # runtime asset.  Reopen the committed family in isolated
                    # mode so validation stays strict without exporting it.
                    DirectoryIndex.clear_cache()
                    committed = load_asset_family(
                        base_target, isolated_root=package_root)
                    if committed.root_object is None:
                        raise AssetFamilyPackageError(
                            "committed Asset Family entry BASE failed to parse")
                    failures = [
                        dep for dep in committed.dependencies
                        if dep.status in ("missing", "ambiguous", "failed_load")]
                    if failures:
                        details = "; ".join(
                            f"{dep.status} {dep.kind}: {dep.raw_ref}"
                            for dep in failures[:8])
                        raise AssetFamilyPackageError(
                            "committed Asset Family failed isolated dependency "
                            "reload: " + details)
                    adapter, reason = IndexedFamilyAdapter.try_create(committed)
                    if adapter is None:
                        raise AssetFamilyPackageError(
                            "committed indexed profile is incomplete: " + reason)

                def verify_inplace() -> None:
                    # The game folder does not hold the exported textures and
                    # profiles, so reload the way the editor opened the model.
                    DirectoryIndex.clear_cache()
                    committed = load_asset_family(
                        base_target, setbas=family.setbas_path)
                    reloaded = committed.root_object
                    if reloaded is None or reloaded.skeleton is None:
                        raise AssetFamilyPackageError(
                            "overwritten BASE failed to reload")
                    if (reloaded.skeleton.polygons != model.polygons
                            or len(reloaded.skeleton.points)
                            != len(model.points)):
                        raise AssetFamilyPackageError(
                            "overwritten skeleton does not match the edited one")

                if inplace and backup_enabled():
                    for _temp_file, final_file in commit_pairs:
                        if final_file.exists():
                            notes.append(
                                f"backup {_next_backup_path(final_file).name}")
                notes.extend(_commit_verified_files(
                    commit_pairs,
                    verify=verify_inplace if inplace else
                    verify_destination_package))
                # Eradicate any stale manifest created by an older Studio
                # export in the same destination.  The current exporter never
                # leaves this tool-side verification file in the asset family.
                if not inplace and manifest_target.exists():
                    try:
                        manifest_target.unlink()
                    except OSError as exc:
                        notes.append(
                            f"warning: stale {ASSET_FAMILY_MANIFEST} could not "
                            f"be removed: {exc}")
        except _BundleCommitError as exc:
            title = (
                "Export failed - current files unchanged"
                if exc.rollback_complete else
                "Export failed - inspect output files")
            QMessageBox.critical(self, title, str(exc))
            return False
        except (AnmParseError, AssetFamilyPackageError, MappingEditError,
                SkltParseError, TextureConvertError, OSError) as exc:
            QMessageBox.critical(
                self, "Export failed - current files unchanged", str(exc))
            return False

        if verified_base_bytes is None:
            QMessageBox.critical(
                self, "Export verification failed",
                "The files were written, but the verified BASE snapshot was "
                "not retained. Reload the exported model before editing again.")
            return False
        self._bundle_base_snapshots[owner] = verified_base_bytes
        self._adopt_saved_block_sources(fam_obj, verified_base_bytes)

        self._geom_dirty.pop(owner, None)
        self._geom_original[owner] = list(model.points)
        if owner in self._topology_original:
            current = self._capture_topology_state(owner)
            if current is not None:
                self._topology_original[owner] = current
        for key in [key for key in self._uv_original if key[0] == owner]:
            del self._uv_original[key]
        for key in [key for key in self._texture_original if key[0] == owner]:
            del self._texture_original[key]
        for key in self._owner_vanm_uv_keys(owner):
            del self._vanm_uv_original[key]
        self._pending_repairs = []
        self._repair_plan = None
        self._sync_geometry_save_controls()
        self._update_editor_status()
        self._sync_uv_add_vertex_button()
        self._log("Asset family exported: " + "; ".join(notes[-3:]))
        return True
