"""Carry the changes made on one visual state of a model over to its other states.

A vehicle is the same object drawn several times (Normal, Fire, Wait, Dead...), but
every state is a separate BASE + skeleton, usually with its points and polygons in a
different order. So nothing here trusts a polygon number: faces are paired by where
they really are in space, in three steps that go from certain to careful.

  1. Identical faces: the same corners at the same positions.
  2. Same numbering: the leading polygons of both states use the same point numbers
     (states copied from each other), which also survives a moved or rescaled model.
  3. Moved faces: starting from what is already paired, a face is paired through the
     corners it shares with its neighbours. This is what follows an edited vertex.

Whatever stays unpaired belongs to one state only. That is normal (the muzzle flash of
Fire, the closed panel of Normal), so a destination face is never touched unless it
was paired, and animated faces are never carried over. Only the faces that the person
really added or deleted since the file was saved are copied or removed by default.

Nothing here needs Qt: the Model Editor dialog and the MCP share these functions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from studio.assets.family import rebuild_materials
from studio.assets.mapping import (
    build_material_block_clipboard,
    delete_material_block,
    MappingIndex,
    paste_material_block,
    rewrite_block_texture_template,
)
from studio_mcp.loose import LooseModel

# Decimals compared on a position: states are the same model, not nearly the same one.
PRECISION = 2
# Step 2 only counts when the shared numbering covers this share of the smaller state.
SHARED_NUMBERING_SHARE = 0.5


@dataclass(frozen=True)
class SyncOptions:
    """Which kinds of change to carry over."""

    textures: bool = True
    state_textures: bool = False
    shape: bool = True
    added_faces: bool = True
    state_faces: bool = False
    deleted_faces: bool = True


@dataclass
class _Face:
    poly_id: int
    corners: list[int]
    block_index: int
    atts_index: int


@dataclass
class FaceLink:
    """A source face and its destination twin.

    ``order[k]`` is the corner of the destination face that sits on corner k of the
    source face: both can start from a different corner, or wind the other way.
    """

    source: int
    target: int
    order: list[int]


@dataclass
class SyncPlan:
    """How two states relate. It describes the changes and edits nothing."""

    source_faces: dict[int, _Face]
    target_faces: dict[int, _Face]
    links: list[FaceLink] = field(default_factory=list)
    moved_points: dict[int, tuple] = field(default_factory=dict)
    material_changes: list[FaceLink] = field(default_factory=list)
    state_materials: list[FaceLink] = field(default_factory=list)   # different, but not edited now
    added_faces: list[int] = field(default_factory=list)      # new since the file was saved
    state_faces: list[int] = field(default_factory=list)      # only ever in the source state
    deleted_faces: list[int] = field(default_factory=list)    # destination faces deleted in the source

    @property
    def match_share(self) -> float:
        """How much of the source is also in the destination, 0..1."""
        return len(self.links) / len(self.source_faces) if self.source_faces else 0.0

    def counts(self, options: SyncOptions = SyncOptions()) -> dict[str, int]:
        """How many things Apply would change with these options."""
        return {"textures": ((len(self.material_changes) if options.textures else 0)
                             + (len(self.state_materials) if options.state_textures else 0)),
                "points": len(self.moved_points) if options.shape else 0,
                "added": ((len(self.added_faces) if options.added_faces else 0)
                          + (len(self.state_faces) if options.state_faces else 0)),
                "removed": len(self.deleted_faces) if options.deleted_faces else 0}

    def has_work(self, options: SyncOptions = SyncOptions()) -> bool:
        return any(self.counts(options).values())


def _position(point) -> tuple:
    return tuple(round(float(value), PRECISION) for value in point)


def static_faces(fam_obj) -> dict[int, _Face]:
    """Faces with one plain texture and fixed UVs: the only ones safe to pair.

    Animated faces (AREA blocks, BMPANIM textures, emitters) stay out: they are what
    makes one state different from another.
    """
    blocks = fam_obj.base_object.ades
    mapping = MappingIndex(fam_obj)
    faces = {}
    for poly_id, corners in enumerate(fam_obj.skeleton.polygons):
        refs = mapping.refs.get(poly_id, [])
        if len(refs) != 1 or len(corners) < 3:
            continue
        block = blocks[refs[0].block_index]
        index = refs[0].atts_index
        if ((block.class_id or "").casefold() != "amesh.class" or block.uses_atts_only_mapping
                or block.texture is None or block.texture.kind != "ilbm"
                or index >= len(block.olpl) or len(block.olpl[index]) != len(corners)):
            continue
        faces[poly_id] = _Face(poly_id, list(corners), refs[0].block_index, index)
    return faces


def _ring(model, face) -> list[tuple]:
    return [_position(model.points[corner]) for corner in face.corners]


def _canonical(ring) -> tuple[tuple, int]:
    """The ring starting from its smallest corner, so the starting corner stops mattering."""
    start = min(range(len(ring)), key=lambda k: ring[k])
    return tuple(ring[start:] + ring[:start]), start


def shape_key(model, face) -> tuple:
    """Identity of a face by position, whichever way it winds."""
    return tuple(sorted(_ring(model, face)))


def _pair_identical(source, target, source_faces, target_faces) -> list[FaceLink]:
    same_winding, any_winding = defaultdict(list), defaultdict(list)
    for poly_id, face in target_faces.items():
        ring = _ring(target, face)
        key, start = _canonical(ring)
        same_winding[key].append((poly_id, start))
        any_winding[tuple(sorted(ring))].append(poly_id)

    links, used, rest = [], set(), []
    for poly_id, face in source_faces.items():
        ring = _ring(source, face)
        key, start = _canonical(ring)
        twin = next(((t, s) for t, s in same_winding.get(key, ()) if t not in used), None)
        if twin is None:
            rest.append((poly_id, ring))
            continue
        target_id, target_start = twin
        count = len(ring)
        order = [0] * count
        for k in range(count):
            order[(start + k) % count] = (target_start + k) % count
        used.add(target_id)
        links.append(FaceLink(poly_id, target_id, order))

    # Same corners, opposite winding: paired by position, one corner at a time.
    for poly_id, ring in rest:
        twin = next((t for t in any_winding.get(tuple(sorted(ring)), ()) if t not in used), None)
        if twin is None or len(set(ring)) != len(ring):
            continue
        target_ring = _ring(target, target_faces[twin])
        used.add(twin)
        links.append(FaceLink(poly_id, twin, [target_ring.index(corner) for corner in ring]))
    return links


def _pair_shared_numbering(source, target, source_faces, target_faces, links) -> list[FaceLink]:
    shared = 0
    for mine, theirs in zip(source.polygons, target.polygons):
        if list(mine) != list(theirs):
            break
        shared += 1
    if shared < SHARED_NUMBERING_SHARE * min(len(source_faces), len(target_faces)):
        return []
    taken_source = {link.source for link in links}
    taken_target = {link.target for link in links}
    return [FaceLink(poly_id, poly_id, list(range(len(source.polygons[poly_id]))))
            for poly_id in range(shared)
            if poly_id in source_faces and poly_id in target_faces
            and poly_id not in taken_source and poly_id not in taken_target]


def _same_texture_and_uv(source_obj, target_obj, source_face, target_face, order) -> bool:
    mine = source_obj.base_object.ades[source_face.block_index]
    theirs = target_obj.base_object.ades[target_face.block_index]
    if mine.texture.name.casefold() != theirs.texture.name.casefold():
        return False
    uvs = theirs.olpl[target_face.atts_index]
    return all(tuple(uv) == tuple(uvs[order[k]])
               for k, uv in enumerate(mine.olpl[source_face.atts_index]))


def _pair_moved(source_obj, target_obj, source_faces, target_faces, links) -> None:
    """Follow the mesh outwards from the paired faces, one shared corner at a time."""
    known = {}
    for link in links:
        ring = source_faces[link.source].corners
        target_ring = target_faces[link.target].corners
        for k, corner in enumerate(ring):
            known.setdefault(corner, target_ring[link.order[k]])

    free = set(target_faces) - {link.target for link in links}
    faces_of_point = defaultdict(set)
    for poly_id in free:
        for corner in target_faces[poly_id].corners:
            faces_of_point[corner].add(poly_id)

    pending = [poly_id for poly_id in source_faces
               if poly_id not in {link.source for link in links}]
    progress = True
    while progress:
        progress = False
        for poly_id in list(pending):
            face = source_faces[poly_id]
            mapped = [known.get(corner) for corner in face.corners]
            if sum(value is not None for value in mapped) < 2:
                continue
            found = _best_moved_twin(source_obj, target_obj, face, target_faces, mapped,
                                     {t for value in mapped if value is not None
                                      for t in faces_of_point.get(value, ())} & free)
            if found is None:
                continue
            target_id, order = found
            links.append(FaceLink(poly_id, target_id, order))
            free.discard(target_id)
            pending.remove(poly_id)
            ring = target_faces[target_id].corners
            for k, corner in enumerate(face.corners):
                known.setdefault(corner, ring[order[k]])
            progress = True


def _best_moved_twin(source_obj, target_obj, face, target_faces, mapped, candidates):
    """The one destination face this source face can be, or None when in doubt."""
    count = len(face.corners)
    results = []
    for target_id in candidates:
        ring = target_faces[target_id].corners
        if len(ring) != count:
            continue
        for offset in range(count):
            order = [(k + offset) % count for k in range(count)]
            matches = [value is None or value == ring[order[k]] for k, value in enumerate(mapped)]
            if not all(matches):
                continue
            shared = sum(value is not None for value in mapped)
            # Two shared corners only prove a common edge. A real twin has most of its
            # corners in place, or still wears the same piece of texture.
            if shared * 4 >= count * 3 or _same_texture_and_uv(
                    source_obj, target_obj, face, target_faces[target_id], order):
                results.append((shared, target_id, order))
    if not results:
        return None
    results.sort(key=lambda item: -item[0])
    if len(results) > 1 and results[0][0] == results[1][0] and results[0][1] != results[1][1]:
        return None
    return results[0][1], results[0][2]


def _material_key(block) -> tuple:
    tracy = block.tracy_texture
    return ((block.class_id or "").casefold(), block.ade_flags, block.polflags,
            block.texture.kind, block.texture.name.casefold(), block.texture.anim_type,
            tracy.name.casefold() if tracy is not None else "")


def _atts_values(entry) -> tuple:
    return entry.color_val, entry.shade_val, entry.tracy_val, entry.pad


def _material_differs(source_obj, target_obj, source_face, target_face, order) -> bool:
    mine = source_obj.base_object.ades[source_face.block_index]
    theirs = target_obj.base_object.ades[target_face.block_index]
    if _material_key(mine) != _material_key(theirs):
        return True
    if (_atts_values(mine.atts[source_face.atts_index])
            != _atts_values(theirs.atts[target_face.atts_index])):
        return True
    return not _same_texture_and_uv(source_obj, target_obj, source_face, target_face, order)


def _material_signature(fam_obj, face) -> tuple:
    """Everything that makes a face look the way it does, independent of its numbering."""
    block = fam_obj.base_object.ades[face.block_index]
    corners = zip(_ring(fam_obj.skeleton, face), map(tuple, block.olpl[face.atts_index]))
    return _material_key(block), _atts_values(block.atts[face.atts_index]), tuple(sorted(corners))


def build_plan(source_obj, target_obj, saved_source_obj=None) -> SyncPlan:
    """Compare two states. ``saved_source_obj`` is the source as it is on disk, if known.

    With it Studio can tell a face the person just added from one that has always
    belonged to that state only, a deleted face from one that never existed, and a
    texture just edited from one that has always differed between the two states.
    """
    source, target = source_obj.skeleton, target_obj.skeleton
    source_faces, target_faces = static_faces(source_obj), static_faces(target_obj)
    plan = SyncPlan(source_faces, target_faces)
    plan.links = _pair_identical(source, target, source_faces, target_faces)
    plan.links += _pair_shared_numbering(source, target, source_faces, target_faces, plan.links)
    _pair_moved(source_obj, target_obj, source_faces, target_faces, plan.links)

    for link in plan.links:
        source_face, target_face = source_faces[link.source], target_faces[link.target]
        for k, corner in enumerate(source_face.corners):
            point = target_face.corners[link.order[k]]
            position = source.points[corner]
            if _position(position) != _position(target.points[point]):
                plan.moved_points.setdefault(point, tuple(position))
        if _material_differs(source_obj, target_obj, source_face, target_face, link.order):
            plan.material_changes.append(link)

    saved_shapes = None
    if saved_source_obj is not None:
        saved_faces = static_faces(saved_source_obj).values()
        saved_shapes = {shape_key(saved_source_obj.skeleton, face) for face in saved_faces}
        saved_looks = {_material_signature(saved_source_obj, face) for face in saved_faces}
        edited = [link for link in plan.material_changes
                  if _material_signature(source_obj, source_faces[link.source]) not in saved_looks]
        # Nothing edited now means the person saved first: every difference is on offer.
        if edited:
            plan.state_materials = [link for link in plan.material_changes if link not in edited]
            plan.material_changes = edited
    linked = {link.source for link in plan.links}
    for poly_id, face in source_faces.items():
        if poly_id in linked:
            continue
        is_new = saved_shapes is not None and shape_key(source, face) not in saved_shapes
        (plan.added_faces if is_new else plan.state_faces).append(poly_id)

    if saved_shapes is not None:
        current = {shape_key(source, face) for face in source_faces.values()}
        paired = {link.target for link in plan.links}
        for poly_id, face in target_faces.items():
            key = shape_key(target, face)
            if poly_id not in paired and key in saved_shapes and key not in current:
                plan.deleted_faces.append(poly_id)
    return plan


def _block_for(lm, source_family, source_obj, source_index, cache) -> int:
    """The destination block with the source face's material, created when it has none."""
    if source_index in cache:
        return cache[source_index]
    block = source_obj.base_object.ades[source_index]
    key = _material_key(block)
    found = next((index for index, other in enumerate(lm.blocks)
                  if (other.class_id or "").casefold() == "amesh.class"
                  and other.texture is not None and other.atts_chunk_offset >= 0
                  and _material_key(other) == key), None)
    if found is None:
        pasted = paste_material_block(
            lm.family, lm.fam_obj, build_material_block_clipboard(source_family, source_obj, source_index))
        found = pasted.block_index
        clone = lm.blocks[found]
        # The texture may have been changed in memory and not saved yet: the template
        # written to disk must name the texture the block really shows.
        clone.source_objt_bytes = rewrite_block_texture_template(clone, clone.texture.name, "texture")
        if clone.tracy_texture is not None:
            clone.source_objt_bytes = rewrite_block_texture_template(
                clone, clone.tracy_texture.name, "tracy_texture")
    cache[source_index] = found
    return found


def _resolves(family, name: str) -> bool:
    """True when this model finds the texture by itself, without the source's help."""
    wanted = name.casefold()
    archive = family.setbas_archive
    if archive is not None and any(item.decodable for item in archive.find(name, "ilbm.class")):
        return True
    for root in family.search_roots:
        folder = Path(root)
        if folder.is_dir() and any(path.name.casefold() == wanted for path in folder.rglob("*")):
            return True
    return False


def texture_files(lm, plan: SyncPlan, source_family, source_obj,
                  options: SyncOptions = SyncOptions()) -> dict[str, bytes]:
    """Loose textures to copy beside the destination, so it can show what it now uses."""
    faces = []
    if options.textures:
        faces += [link.source for link in plan.material_changes]
    if options.state_textures:
        faces += [link.source for link in plan.state_materials]
    if options.added_faces:
        faces += plan.added_faces
    if options.state_faces:
        faces += plan.state_faces
    files = {}
    for poly_id in faces:
        name = source_obj.base_object.ades[plan.source_faces[poly_id].block_index].texture.name
        if name in files or _resolves(lm.family, name):
            continue
        ref = next((value for key, value in source_family.texture_refs.items()
                    if key.casefold() == name.casefold()), None)
        path = getattr(ref, "path", None)
        if path is not None and getattr(ref, "source", "") == "loose":
            files[Path(str(path)).name] = Path(str(path)).read_bytes()
    return files


def _add_face(lm, plan, source_family, source_obj, poly_id, point_of, cache) -> None:
    face = plan.source_faces[poly_id]
    points = []
    for corner in face.corners:
        if corner not in point_of:
            point_of[corner] = lm.append_point(source_obj.skeleton.points[corner])
        points.append(point_of[corner])
    block = source_obj.base_object.ades[face.block_index]
    lm.append_polygon(points, _block_for(lm, source_family, source_obj, face.block_index, cache),
                      _atts_values(block.atts[face.atts_index]), block.olpl[face.atts_index])


def apply_plan(lm: LooseModel, plan: SyncPlan, source_family, source_obj,
               options: SyncOptions = SyncOptions()) -> None:
    """Edit the destination in memory. ``lm.commit`` then writes it."""
    filled = {index for index, block in enumerate(lm.blocks) if block.atts}
    cache: dict[int, int] = {}

    if options.shape:
        for point, position in plan.moved_points.items():
            lm.points[point] = position

    material_links = ((plan.material_changes if options.textures else [])
                      + (plan.state_materials if options.state_textures else []))
    if material_links:
        for link in material_links:
            face = plan.source_faces[link.source]
            block = source_obj.base_object.ades[face.block_index]
            uvs = [None] * len(link.order)
            for k, uv in enumerate(block.olpl[face.atts_index]):
                uvs[link.order[k]] = tuple(uv)
            lm.move_polygon_to_block(
                link.target, _block_for(lm, source_family, source_obj, face.block_index, cache),
                atts=_atts_values(block.atts[face.atts_index]), uvs=uvs)

    faces_to_add = ((plan.added_faces if options.added_faces else [])
                    + (plan.state_faces if options.state_faces else []))
    if faces_to_add:
        # New faces weld to the points the two states share, so no seam opens.
        point_of = {}
        for link in plan.links:
            ring = plan.target_faces[link.target].corners
            for k, corner in enumerate(plan.source_faces[link.source].corners):
                point_of.setdefault(corner, ring[link.order[k]])
        for poly_id in faces_to_add:
            _add_face(lm, plan, source_family, source_obj, poly_id, point_of, cache)

    if options.deleted_faces and plan.deleted_faces:
        lm.delete_polygons(plan.deleted_faces)

    # A block emptied by the moves above would stay in the file as dead weight.
    for index in sorted(filled, reverse=True):
        if not lm.blocks[index].atts and (lm.blocks[index].class_id or "").casefold() == "amesh.class":
            delete_material_block(lm.fam_obj, index)
    rebuild_materials(lm.fam_obj, lm.family)
