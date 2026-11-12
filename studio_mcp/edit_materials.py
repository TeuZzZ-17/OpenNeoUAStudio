"""Modeling operations that touch materials, other models and effects (registered into edit.OPS).

They reuse the editor's own routines: material block clipboard/paste, ANM element clipboard, texture retargeting.
Nothing here re-implements a format; each op only prepares inputs and checks results.
"""

from __future__ import annotations

import copy
import functools

from studio.assets.mapping import (
    _block_material_metadata,
    build_material_block_clipboard,
    delete_material_block,
    MappingEditError,
    MappingIndex,
    paste_material_block,
    rewrite_ade_point_template,
    rewrite_block_texture_template,
    transfer_material_resources,
)
from studio.formats.animation import export_anm_bytes
from studio.formats.images import parse_ilbm_file
from studio.formats.setbas import decode_texture
from studio.rendering.geometry import (
    append_fx_element_clipboard,
    build_fx_element_clipboard,
    detect_fx_elements,
    GeometryClipboardError,
    prepare_fx_clipboard,
    stage_fx_clipboard,
)
from studio_mcp import core
from studio_mcp.edit import _need, _vec, clipboard_copy, op, op_transform, paste_records, records_of
from studio_mcp.select import select_polygons, view_of_object


def _source_object(spec, set_hint: str = "", owner_path: str = ""):
    """(family, object) of another model: 'set1:77', 'set1:VP_X' or a .BASE path."""
    if not spec:
        raise core.McpToolError("give `source`: 'set1:77', 'set1:VPwGLID2' or a .BASE path")
    source = core.resolve_source(spec, set_hint)
    family = core.load_family(source)
    if family.root_object is None:
        raise core.McpToolError(f"Could not load {source.label}")
    objects = [o for o in family.all_objects() if o.skeleton is not None]
    if owner_path:
        objects = [o for o in objects if o.owner_path == owner_path]
    if not objects:
        raise core.McpToolError("The source has no object with a skeleton")
    return family, objects[0]


def _find_equal_block(lm, source_block) -> int | None:
    """Index of a block in the target with the same material (texture, shading, flags), if any."""
    wanted = _block_material_metadata(source_block)
    for index, block in enumerate(lm.blocks):
        if (block.class_id or "").lower() == "amesh.class" and _block_material_metadata(block) == wanted:
            return index
    return None


def _guard_errors(function):
    """Editor routines raise their own exception types; the MCP reports them as plain tool errors."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (MappingEditError, GeometryClipboardError) as exc:
            raise core.McpToolError(str(exc)) from exc
    return wrapper


# --- geometry from another model -------------------------------------------------------------


@op("import_geometry")
@_guard_errors
def op_import_geometry(ctx, source=None, select=None, translate=None, rotate_deg=None, scale=None,
                       exclude_fx: bool = False, source_owner: str = "", save_as=None):
    """Paste polygons from ANOTHER model (cross-model copy/paste): `source` is 'set1:77' / 'set1:VP_X' / a .BASE
    path; `select` is a polygon selector on the SOURCE (omit = the whole model). Materials are matched to existing
    blocks (same texture and shading) or added as new blocks. translate/rotate_deg/scale then place the copy
    (around its own center). exclude_fx=true leaves out flame/smoke (ANM) polygons, which can only travel with
    studio_add_fx. Returns the new polygon/point indices and any new blocks."""
    lm = ctx.lm
    lm.require_topology_edit()
    family, obj = _source_object(source, lm.set_hint, source_owner)
    view = view_of_object(obj)
    ids = select_polygons(select, view)
    fx = detect_fx_elements(obj, family.animations)
    if exclude_fx:
        in_fx = {pid for element in fx for pid in element.poly_ids}
        ctx.notes.append(f"{len(set(ids) & in_fx)} ANM polygon(s) were left out")
        ids = [pid for pid in ids if pid not in in_fx]
    if not ids:
        raise core.McpToolError("The selector matched no polygons in the source")
    records, others = records_of(obj, ids)
    mapped, new_blocks = {}, []

    def target_block(source_index: int) -> int:
        """Matching block of this model for a source block, adding a new slot when none matches."""
        if source_index not in mapped:
            source_block = obj.base_object.ades[source_index]
            found = _find_equal_block(lm, source_block)
            if found is None:
                material = build_material_block_clipboard(family, obj, source_index)
                found = paste_material_block(lm.family, lm.fam_obj, material).block_index
                new_blocks.append(found)
            mapped[source_index] = found
        return mapped[source_index]

    for record in records:
        target_block(record.block)
    new_polys, new_points = paste_records(lm, records, obj.skeleton.points, mapped, ctx.notes)
    if others:
        refs = MappingIndex(obj).refs
        for source_index in sorted({refs[pid][0].block_index for pid in others if refs.get(pid)}):
            try:
                transfer_material_resources(lm.family, build_material_block_clipboard(family, obj, source_index))
            except MappingEditError as exc:
                ctx.notes.append(f"material of block #{source_index} could not travel: {exc}")
        more_polys, more_points = clipboard_copy(lm, obj, others, ctx.notes)
        new_polys += more_polys
        new_points += more_points
    if not new_polys:
        raise core.McpToolError("Nothing could be imported: " + "; ".join(ctx.notes))
    if translate is not None or rotate_deg is not None or scale is not None:
        op_transform(ctx, select=new_points, translate=translate, rotate_deg=rotate_deg, scale=scale)
    return {"polygons": new_polys, "points": new_points, "new_blocks": new_blocks,
            "from": f"{len(ids)} polygon(s) of {source}"}


# --- material blocks -----------------------------------------------------------------------------


@op("add_material_block")
@_guard_errors
def op_add_material_block(ctx, from_block: int = 0, source=None, source_owner: str = ""):
    """Add an empty material slot (same texture and shading) copied from block `from_block` of this model, or of
    another model when `source` is given. Use it to get a block that new polygons can be assigned to."""
    lm = ctx.lm
    if source:
        family, obj = _source_object(source, lm.set_hint, source_owner)
    else:
        family, obj = lm.family, lm.fam_obj
    material = build_material_block_clipboard(family, obj, int(from_block))
    result = paste_material_block(lm.family, lm.fam_obj, material)
    return {"block": result.block_index, "imported_resources": [list(r) for r in result.imported_resources]}


@op("delete_material_block")
@_guard_errors
def op_delete_material_block(ctx, block: int = -1, move_polygons_to=None):
    """Remove a material block. Its polygons must go somewhere: move_polygons_to = another amesh block number
    (numbers are the ones BEFORE the deletion). Later block numbers shift down by one."""
    lm = ctx.lm
    block = int(block)
    if not 0 <= block < len(lm.blocks):
        raise core.McpToolError(f"Material block #{block} does not exist")
    table = lm.slot_table()
    owned = sorted(pid for pid, (bi, _) in table.items() if bi == block)
    if owned:
        if move_polygons_to is None:
            raise core.McpToolError(f"Block #{block} still has {len(owned)} polygon(s): pass move_polygons_to "
                                    "or delete/reassign them first")
        if int(move_polygons_to) == block:
            raise core.McpToolError("move_polygons_to must be a different block")
        for pid in owned:
            lm.move_polygon_to_block(pid, int(move_polygons_to), table=lm.slot_table())
    delete_material_block(lm.fam_obj, block)
    return {"deleted_block": block, "moved_polygons": len(owned), "blocks_left": len(lm.blocks)}


@op("copy_from_state")
@_guard_errors
def op_copy_from_state(ctx, source=None, textures: bool = True, shape: bool = True, state_faces: bool = False):
    """Bring this model in line with another state of the same model (Normal from Fire, for example): textures,
    UVs and moved points of the faces both states share. Animated faces stay as they are. state_faces also copies
    the static faces only the source has."""
    from studio_mcp.state_sync import apply_plan, build_plan, SyncOptions, texture_files

    lm = ctx.lm
    family, obj = _source_object(source, lm.set_hint)
    plan = build_plan(obj, lm.fam_obj)
    options = SyncOptions(textures=textures, shape=shape, added_faces=False,
                          state_faces=state_faces, deleted_faces=False)
    apply_plan(lm, plan, family, obj, options)
    lm.extra_files.update(texture_files(lm, plan, family, obj, options))
    return {"match": round(plan.match_share, 3), **plan.counts(options)}


def _ensure_texture(lm, name: str) -> str:
    """Decoded texture name usable in the model: already loaded, a loose ILBM in the model's folder (made with
    studio_make_texture), or decoded from the Set.BAS."""
    if not name:
        raise core.McpToolError("give `texture`, e.g. 'FAERROBO.ILBM' (studio_list_textures shows the choices)")
    for key in lm.family.textures:
        if key.casefold() == name.casefold():
            return key
    loose = next((p for p in sorted(lm.base_path.parent.rglob("*"))
                  if p.is_file() and p.name.casefold() == name.casefold()), None)
    if loose is not None:
        lm.family.textures[loose.name] = parse_ilbm_file(loose)
        return loose.name
    archive = getattr(lm.family, "setbas_archive", None) or core._setbas(lm.set_hint or "set1")[0]
    resource = next((r for r in archive.find(name, "ilbm.class") if r.decodable), None)
    if resource is None:
        raise core.McpToolError(f"Texture {name!r} is not in the model, its folder or the set (see studio_list_textures)")
    lm.family.textures[resource.resource_name] = decode_texture(archive, resource)
    return resource.resource_name


@op("set_texture")
@_guard_errors
def op_set_texture(ctx, select=None, texture=None, slot: str = "texture"):
    """Give the chosen polygons another texture (e.g. 'FAERROBO.ILBM'). A material block shared with other
    polygons is split, so only the chosen ones change. slot: 'texture' or 'tracy_texture'."""
    lm = ctx.lm
    lm.require_topology_edit()
    if slot not in ("texture", "tracy_texture"):
        raise core.McpToolError("slot must be 'texture' or 'tracy_texture'")
    ids = set(ctx.polygons(_need(select, "polygon")))
    loaded = _ensure_texture(lm, texture)
    table = lm.slot_table()
    per_block: dict[int, set[int]] = {}
    for pid in ids:
        per_block.setdefault(table[pid][0], set()).add(pid)
    changed, split = [], []
    for block_index, chosen in sorted(per_block.items()):
        block = lm.blocks[block_index]
        if getattr(block, slot, None) is None:
            raise core.McpToolError(f"Block #{block_index} has no {slot} to replace")
        template = rewrite_block_texture_template(block, loaded, slot)
        positions = [i for i, entry in enumerate(block.atts) if entry.poly_id in chosen]
        if len(positions) == len(block.atts):
            getattr(block, slot).name = loaded
            block.source_objt_bytes = template
            changed.append(block_index)
            continue
        if (block.class_id or "").lower() != "amesh.class":
            raise core.McpToolError(f"Block #{block_index} is {block.class_id}: it cannot be split")
        atts_only = block.uses_atts_only_mapping
        clone = copy.deepcopy(block)
        clone.atts = [copy.deepcopy(block.atts[i]) for i in positions]
        clone.olpl = [] if atts_only else [copy.deepcopy(block.olpl[i]) for i in positions]
        block.atts = [e for i, e in enumerate(block.atts) if i not in positions]
        if not atts_only:
            block.olpl = [g for i, g in enumerate(block.olpl) if i not in positions]
        getattr(clone, slot).name = loaded
        clone.source_objt_bytes = template
        lm.blocks.append(clone)
        split.append(len(lm.blocks) - 1)
    return {"texture": loaded, "changed_blocks": changed, "new_blocks": split, "polygons": sorted(ids)}


@op("generate_genesis")
@_guard_errors
def op_generate_genesis(ctx):
    """Turn the whole model into its Genesis version (the animated spawn-in look): every polygon gets the Gharghoil III
    Genesis animation material, geometry stays. Needed .ANM files for big polygons are written beside the BASE.
    Emitters are kept. Do this on a COPY (genesis model slot), the normal materials are replaced."""
    from model_editor.genesis_generator import generate_genesis   # imported here: the package pulls in the Qt window
    lm = ctx.lm
    lm.require_topology_edit()
    archive = getattr(lm.family, "setbas_archive", None) or core._setbas(lm.set_hint or "set1")[0]
    generate_genesis(lm.family, lm.fam_obj)
    created = []
    for name, animation in lm.family.animations.items():
        # animations the set already holds (GENES3-1, GENES4-1 ...) are found by the game itself: no loose copy
        if not archive.find(name, "bmpanim.class") and name not in lm.extra_files:
            lm.extra_files[name] = export_anm_bytes(animation)
            created.append(name)
    if created:
        ctx.notes.append(f"new animation file(s) written beside the BASE: {created}")
    return {"blocks": len(lm.blocks), "new_animations": created}


@op("animate_polygons")
@_guard_errors
def op_animate_polygons(ctx, select=None, shifts=None, frames=None, name: str = "ANIM"):
    """Make polygons play a UV animation on their own texture (blinking lights, screens). Every polygon keeps its
    current UVs as state 0; `shifts` = [[du, dv], ...] moves them to states 1, 2, ... `frames` = [[state, ms], ...]
    in play order, looping: shifts [[0,-36]] + frames [[1,1500],[0,700]] = state 1 for 1.5 s, state 0 for 0.7 s.
    Polygons with equal UVs share one .ANM written beside the BASE (named NAME, at most 5 letters, + a letter). The polygons leave
    their amesh block and each gets an opaque animated block."""
    from studio.assets.animated_polygons import animated_polygon_block, build_vanm
    from studio.formats.animation import VanmData, VanmFrame
    lm = ctx.lm
    lm.require_topology_edit()
    ids = ctx.polygons(_need(select, "polygon"))
    shifts = [[0, 0]] + [[int(c) for c in shift] for shift in (shifts or [])]
    if len(shifts) < 2 or not frames:
        raise core.McpToolError("give `shifts` (the other states, e.g. [[0,-36]]) and `frames` [[state, ms], ...]")
    clean = name.upper().replace(".ANM", "")[:5]   # block template name space: 10 characters
    table = lm.slot_table()
    per_uv: dict[tuple, list[int]] = {}
    for pid in ids:
        block_index, atts_index = table[pid]
        block = lm.blocks[block_index]
        if (block.class_id or "").lower() != "amesh.class" or block.uses_atts_only_mapping:
            raise core.McpToolError(f"Polygon #{pid} is not an ordinary textured face")
        per_uv.setdefault(tuple(map(tuple, block.olpl[atts_index])), []).append(pid)
    created = {}
    for letter, (uvs, polys) in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", per_uv.items()):
        groups = []
        for du, dv in shifts:
            moved = [(u + du, v + dv) for u, v in uvs]
            if any(not 0 <= c <= 255 for uv in moved for c in uv):
                raise core.McpToolError(f"shift {[du, dv]} moves the UVs of polygons {polys} outside the texture")
            groups.append(moved)
        sequence = []
        for state, ms in frames:
            if not 0 <= int(state) < len(shifts) or int(ms) < 1:
                raise core.McpToolError("frames = [[state, ms], ...] with state 0..%d and ms > 0" % (len(shifts) - 1))
            sequence.append(VanmFrame(max(1, round(int(ms) * 1024 / 1000)), 0, int(state)))
        first = lm.blocks[table[polys[0]][0]]
        source = VanmData(source_name=f"{clean}{letter}.ANM", bitmap_class="ilbm.class",
                          bitmap_names=[first.texture.name], frames=sequence)
        anm_name = f"{clean}{letter}.ANM"
        animation = build_vanm(source, groups, anm_name)
        animation.source_name = anm_name
        lm.family.animations[anm_name] = animation
        lm.extra_files[anm_name] = export_anm_bytes(animation)
        created[anm_name] = polys
        for pid in polys:
            block_index, atts_index = lm.slot_table()[pid]
            old = lm.blocks[block_index]
            old.atts.pop(atts_index)
            old.olpl.pop(atts_index)
            lm.blocks.append(animated_polygon_block(anm_name, pid))
    return {"animations": created, "polygons": sorted(ids)}


# --- effects: ANM elements and particle emitters --------------------------------------------------


def _fx_list(lm):
    return detect_fx_elements(lm.fam_obj, lm.family.animations)


def _fx_at(elements, index, what="fx"):
    if index is None or not 0 <= int(index) < len(elements):
        raise core.McpToolError(f"{what} must be an index 0..{len(elements) - 1} (studio_effects lists them)")
    return elements[int(index)]


@op("delete_fx")
@_guard_errors
def op_delete_fx(ctx, fx: int = -1):
    """Delete one ANM element (flame/smoke/atlas polygons) by its index from studio_effects."""
    lm = ctx.lm
    lm.require_topology_edit()
    element = _fx_at(_fx_list(lm), fx)
    result = lm.delete_polygons(element.poly_ids)
    ctx.apply_remap(result["point_remap"], result["polygon_remap"])
    return {"deleted": result["deleted"], "name": element.fx_name}


@op("add_fx")
@_guard_errors
def op_add_fx(ctx, fx: int = 0, source=None, translate=None, rotate_deg=None, scale=None,
              source_owner: str = "", save_as=None):
    """Clone an ANM element (flame/smoke/atlas polygons): `fx` is its index from studio_effects, in this model or
    (with `source`) in another model. The copy gets its own points; translate/rotate_deg/scale then place it."""
    lm = ctx.lm
    lm.require_topology_edit()
    delta = (0.0, 0.0, 0.0)
    if source:
        family, obj = _source_object(source, lm.set_hint, source_owner)
        element = _fx_at(detect_fx_elements(obj, family.animations), fx)
        clipboard = prepare_fx_clipboard(lm.fam_obj, family, obj, element)
        staged_family, staged, result = stage_fx_clipboard(lm.family, lm.fam_obj, clipboard, delta)
        lm.model.points[:] = staged.skeleton.points
        lm.model.polygons[:] = staged.skeleton.polygons
        lm.model.parsed_polygon_count = staged.skeleton.parsed_polygon_count
        lm.fam_obj.base_object.ades[:] = staged.base_object.ades
        for name in ("textures", "texture_refs", "animations", "animation_refs"):
            target = getattr(lm.family, name)
            target.clear()
            target.update(getattr(staged_family, name))
    else:
        element = _fx_at(_fx_list(lm), fx)
        clipboard = build_fx_element_clipboard(lm.fam_obj, element, lm.family.animations)
        result = append_fx_element_clipboard(lm.fam_obj, clipboard, delta, lm.family.animations)
    new_points, new_polys = list(result.point_indices), list(result.polygon_indices)
    if translate is not None or rotate_deg is not None or scale is not None:
        op_transform(ctx, select=new_points, translate=translate, rotate_deg=rotate_deg, scale=scale)
    return {"polygons": new_polys, "points": new_points, "name": element.fx_name}


def _emitters(lm):
    return [(i, b) for i, b in enumerate(lm.blocks) if (b.class_id or "").lower() == "particle.class"]


def _emitter_point(ctx, point, position) -> int:
    lm = ctx.lm
    if position is not None:
        return lm.append_point(_vec(position, "position"))
    if point is None:
        raise core.McpToolError("give `point` (an existing point index) or `position` [x, y, z] for a new anchor")
    return lm.check_point_ids([point])[0]


@op("add_emitter")
@_guard_errors
def op_add_emitter(ctx, source=None, source_block: int = -1, point=None, position=None, source_owner: str = ""):
    """Copy a particle emitter (fire, smoke, sparks) from another model and attach it to a point of this one:
    `source_block` is the emitter's block number from studio_effects(source); `point` is an existing point index or
    `position` [x,y,z] creates a free anchor point (emitters keep following their point if points are renumbered)."""
    lm = ctx.lm
    family, obj = _source_object(source, lm.set_hint, source_owner)
    blocks = obj.base_object.ades
    if not 0 <= int(source_block) < len(blocks) or (blocks[int(source_block)].class_id or "").lower() != "particle.class":
        raise core.McpToolError(f"Block #{source_block} of the source is not a particle emitter (studio_effects shows them)")
    anchor = _emitter_point(ctx, point, position)
    block = copy.deepcopy(blocks[int(source_block)])
    block.source_objt_bytes = rewrite_ade_point_template(block, anchor)
    block.ade_point_id = anchor
    lm.blocks.append(block)
    return {"block": len(lm.blocks) - 1, "point": anchor}


@op("move_emitter")
@_guard_errors
def op_move_emitter(ctx, block: int = -1, point=None, position=None):
    """Attach an existing emitter (block number from studio_effects) to another point, or to a new free point."""
    lm = ctx.lm
    block = int(block)
    emitter = dict(_emitters(lm)).get(block)
    if emitter is None:
        raise core.McpToolError(f"Block #{block} is not a particle emitter")
    anchor = _emitter_point(ctx, point, position)
    emitter.source_objt_bytes = rewrite_ade_point_template(emitter, anchor)
    emitter.ade_point_id = anchor
    return {"block": block, "point": anchor}


@op("delete_emitter")
@_guard_errors
def op_delete_emitter(ctx, block: int = -1):
    """Remove a particle emitter (block number from studio_effects)."""
    lm = ctx.lm
    block = int(block)
    if block not in dict(_emitters(lm)):
        raise core.McpToolError(f"Block #{block} is not a particle emitter")
    lm.blocks.pop(block)
    return {"deleted_block": block, "blocks_left": len(lm.blocks)}

