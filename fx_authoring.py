"""FX authoring through the existing geometry and material clipboard writers."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace

from base_mapping_editor import (
    MaterialBlockClipboard, build_material_block_clipboard,
    paste_material_block, transfer_material_resources,
)
from fx_element_editor import (
    FxElementClipboard, append_fx_element_clipboard,
    build_fx_element_clipboard,
)
from geometry_editor import GeometryClipboardError
from indexed_family_adapter import (
    validate_retail_polygon_flags, UnsupportedIndexedMaterialError,
)


@dataclass(frozen=True)
class ImportedFxClipboard(FxElementClipboard):
    materials: tuple[MaterialBlockClipboard, ...] = ()


def prepare_fx_clipboard(target, source_family, source, element):
    """Capture all source data now; opening/cancelling a preview changes nothing."""
    clipboard = build_fx_element_clipboard(source, element, source_family.animations)
    materials = tuple(
        build_material_block_clipboard(source_family, source, index)
        for index in dict.fromkeys(clipboard.block_indices))
    values = dict(clipboard.__dict__)
    values.update(owner=target.owner_path, model_identity=id(target.skeleton),
                  source_poly_ids=tuple(-1 for _ in clipboard.polygons),
                  original_to_local=(),
                  polygons=tuple(replace(face, source_poly_id=-1)
                                 for face in clipboard.polygons))
    return ImportedFxClipboard(**values, materials=materials)


def stage_fx_clipboard(family, target, clipboard, delta=(0.0, 0.0, 0.0)):
    """Build and validate a complete result without touching the target family."""
    if clipboard.owner != target.owner_path \
            or clipboard.model_identity != id(target.skeleton):
        raise GeometryClipboardError("the FX target changed during preview")
    staged_family = copy.copy(family)
    for name in ("textures", "texture_refs", "animations", "animation_refs"):
        setattr(staged_family, name, dict(getattr(family, name)))
    staged = copy.copy(target)
    staged.skeleton = copy.deepcopy(target.skeleton)
    staged.base_object = copy.copy(target.base_object)
    staged.base_object.ades = copy.deepcopy(target.base_object.ades)
    block_indices = {}
    for material in clipboard.materials:
        try:
            validate_retail_polygon_flags(material.block_template.polflags)
        except UnsupportedIndexedMaterialError as exc:
            raise GeometryClipboardError(str(exc)) from exc
        if material.source_family_identity == id(family):
            material = replace(material, source_family_identity=id(staged_family))
        if material.class_id == "area.class":
            transfer_material_resources(staged_family, material)
            # AREA carries its own source OBJT and is appended per polygon by
            # the typed FX writer, rather than creating an empty AREA slot.
            block_indices[material.source_block_index] = material.source_block_index
        else:
            result = paste_material_block(staged_family, staged, material)
            block_indices[material.source_block_index] = result.block_index
    faces = tuple(replace(face, block_index=block_indices[face.block_index])
                  for face in clipboard.polygons)
    values = {name: getattr(clipboard, name)
              for name in FxElementClipboard.__dataclass_fields__}
    values.update(model_identity=id(staged.skeleton), polygons=faces,
                  block_indices=tuple(face.block_index for face in faces),
                  material_signature=tuple((face.block_index, face.texture)
                                           for face in faces))
    local = FxElementClipboard(**values)
    result = append_fx_element_clipboard(
        staged, local, delta, staged_family.animations)
    return staged_family, staged, result

