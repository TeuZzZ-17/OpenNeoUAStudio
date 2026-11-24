"""Faces of every sector type and building, in sector-local coordinates (``SectorMeshLibrary``)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property
from pathlib import Path
import math
import struct
import numpy as np

from ..core.asset_bridge import SetAssets
from ..core.sector_resolver import (STATE_INTACT, ResolvedSector,
                                    SectorResolver)
from ..core.vehicle_defs import WHITE_TINT, load_vehicle_files
from ..core.building_defs import load_building_files
from .. import bootstrap


@dataclass
class SectorMesh:
    typ: int
    faces: list = field(default_factory=list)
    resolved: ResolvedSector | None = None
    missing: list[str] = field(default_factory=list)

    @cached_property
    def bounds(self):
        points = [(v[0] + x, v[1], v[2] + z) for face, x, z in self.faces for v in face.vertices]
        if not points:
            return (0.0,) * 6
        return (min(v[0] for v in points), min(v[1] for v in points), min(v[2] for v in points),
                max(v[0] for v in points), max(v[1] for v in points), max(v[2] for v in points))


class SectorMeshLibrary:
    """typ -> 9 sotto-edifici -> facce in coordinate locali della cella.

    Riusa AssetViewport (Studio) solo come caricatore di facce/materiali:
    stesse trasformazioni scale_rot_7, stessi materiali e superfici indicizzate.
    """

    def __init__(self, assets: SetAssets, *, set_only: bool = False):
        import studio.rendering.asset_viewport as av
        self.assets = assets
        self.set_only = set_only
        self.resolver = SectorResolver(assets.sdf)
        self._loader = av.AssetViewport()
        self._loader.load_family(assets.family, visible_owners=set())
        self.adapter = self._loader._indexed_adapter
        if self.adapter is None:
            raise RuntimeError(self._loader._indexed_unavailable_reason)
        self._material_index: dict = {}
        self._base_cache: dict[tuple[str, bool], list] = {}
        self._meshes: dict[tuple[int, int], SectorMesh] = {}
        self._filler_templates: dict[tuple, tuple] = {}
        # Terrain overrides use the same positional IDs as the engine's slurp table.
        self._filler_overrides = {}
        directory = bootstrap.find_ci(assets.set_dir, 'scripts')
        override = bootstrap.find_ci(directory, 'override.lst') if directory else None
        if override is not None:
            for line in override.read_text(encoding='cp1252').splitlines():
                fields = line.split(';', 1)[0].split()
                if len(fields) >= 3 and fields[0].casefold() == 'slurp':
                    try:
                        index = int(fields[1])
                    except ValueError:
                        continue
                    if 0 <= index < 72:
                        self._filler_overrides[index] = fields[2]
        scripts = (bootstrap.installation().folder('scripts')
                   if bootstrap.installation() else bootstrap.game_data_dir() / 'Scripts')
        self.buildings = load_building_files(scripts)
        self.vehicles = load_vehicle_files(scripts)
        self.scripts = scripts
        self._external_surfaces = {}
        self._vp_names = assets.visual_proto_names
        self._gun_meshes = {}
        self._host_meshes = {}
        self._vehicle_meshes = {}
        self._building_decorations = {}
        self._collision_skeletons = {}
        self._material_adapters = {}

    @property
    def tables(self):
        return self.adapter.tables

    def collision_skeleton(self, name):
        """Read the SDF collision SKLT, independent of the visible BASE faces."""
        key = name.casefold()
        if key not in self._collision_skeletons:
            from studio.formats.sklt import parse_sklt_bytes
            skeleton = None
            path = self.assets.set_dir
            for part in name.replace('\\', '/').split('/'):
                path = bootstrap.find_ci(path, part) if path is not None else None
            if path is not None and path.is_file():
                skeleton = parse_sklt_bytes(path.read_bytes(), str(path))
            elif self.assets.archive is not None:
                matches = self.assets.archive.find(name, 'sklt.class')
                if matches:
                    skeleton = parse_sklt_bytes(self.assets.archive.payload_bytes(matches[0]), name)
            self._collision_skeletons[key] = skeleton
        return self._collision_skeletons[key]

    def _base_faces(self, base_name: str, *, set_only: bool = False) -> list | None:
        set_only = set_only or self.set_only
        key = (base_name.lower(), set_only)
        if key in self._base_cache:
            return self._base_cache[key]
        obj = self.assets.base_object(base_name, set_only=set_only)
        faces = None
        if obj is not None:
            family = self.assets._loose_families.get(self.assets._key(base_name))
            if family is not None and obj is family.root_object:
                faces = self._family_faces(obj, family)
                self._base_cache[key] = faces
                return faces
            loader = self._loader
            loader._faces = []
            for fam_obj in obj.iter_tree():
                loader._load_object(fam_obj, self.assets.family,
                                    self._material_index, primary=True,
                                    owner=fam_obj.owner_path)
            faces = [f for f in loader._faces if f.mapped]
            loader._faces = []
        self._base_cache[key] = faces
        return faces

    def surface_for(self, face):
        if face.material < 0:
            return self._external_surfaces[face.material]
        loader = self._loader
        material = loader._materials[face.material]
        adapter = self._material_adapters.get(face.material, self.adapter)
        return adapter.resolve_surface(face, material, 0)

    def face_uvs(self, face):
        if face.material < 0:
            return face.uvs
        loader = self._loader
        return loader._face_uvs(face, loader._materials[face.material])

    def mesh(self, typ: int, state: int = STATE_INTACT) -> SectorMesh:
        key = (typ, state)
        if key in self._meshes:
            return self._meshes[key]
        resolved = self.resolver.resolve_typ(typ, state)
        mesh = SectorMesh(typ, resolved=resolved)
        for sub in resolved.subs:
            if not sub.model:
                continue
            faces = self._base_faces(sub.base_name)
            if faces is None:
                mesh.missing.append(sub.base_name)
                continue
            ox, oz = sub.offset
            for face in faces:
                mesh.faces.append((face, ox, oz))
        self._meshes[key] = mesh
        return mesh

    def _mounted_faces(self, mounts) -> SectorMesh:
        """Render local gun actors with the shared engine-aligned transform."""
        mesh = SectorMesh(-1)
        for mount in mounts:
            if mount.vehicle <= 0:
                continue
            actor = self.vehicle_mesh(mount.vehicle)
            mesh.missing.extend(actor.missing)
            rotation = gun_rotation(mount.direction)
            for face, ox, oz in actor.faces:
                vertices = (np.asarray(face.vertices) + (ox, 0, oz)) @ rotation.T + mount.pos
                mesh.faces.append((replace(face, vertices=[tuple(v) for v in vertices]), 0, 0))
        return mesh

    def building_mesh(self, building_id: int) -> SectorMesh:
        """Mounted actors only: blg IDs refer to scripts, not SDF sub-buildings."""
        if building_id in self._gun_meshes:
            return self._gun_meshes[building_id]
        definition = self.buildings.get(building_id)
        mesh = self._mounted_faces(definition.guns if definition else ())
        self._gun_meshes[building_id] = mesh
        return mesh

    def host_mesh(self, vehicle_id: int) -> SectorMesh:
        """Host model plus its script-defined Robo gun actors."""
        if vehicle_id in self._host_meshes:
            return self._host_meshes[vehicle_id]
        body = self.vehicle_mesh(vehicle_id)
        definition = self.vehicles.get(vehicle_id)
        guns = self._mounted_faces(definition.guns if definition else ())
        mesh = SectorMesh(-1, faces=body.faces + guns.faces,
                          missing=body.missing + guns.missing)
        self._host_meshes[vehicle_id] = mesh
        return mesh

    def vehicle_attachments(self, vehicle_id: int):
        """Mounted guns/flak of one vehicle as placed faces for Snapshot Studio.

        Same mount transform as host_mesh(); 3DS parts have no view material
        and are skipped. Returns (AttachedFaces | None, skipped part count).
        """
        from studio.rendering.asset_viewport import AttachedFaces
        definition = self.vehicles.get(vehicle_id)
        mesh = self._mounted_faces(definition.guns if definition else ())
        faces = [face for face, _ox, _oz in mesh.faces if face.mapped and face.material >= 0]
        skipped = len(mesh.faces) - len(faces)
        if not faces:
            return None, skipped
        ids = {face.material for face in faces}
        return AttachedFaces(
            [replace(face, primary=False) for face in faces],
            {i: self._loader._materials[i] for i in ids},
            {i: self._material_adapters.get(i, self.adapter) for i in ids}), skipped

    def vehicle_for_model(self, path=None, base_name: str = ''):
        """Id of the vehicle with mounts whose normal model is this file or SET.BAS BASE."""
        from studio.assets.visproto import normalize_base_name
        wanted = normalize_base_name(base_name) if base_name else ''
        loose = self.assets._index_loose()
        for vehicle_id, visual in self.vehicles.items():
            if not any(gun.vehicle > 0 for gun in visual.guns):
                continue
            vp = (self._vp_names[visual.vp_normal]
                  if 0 <= visual.vp_normal < len(self._vp_names) else '')
            file = self._visual_path(visual.base_normal) if visual.base_normal else None
            if file is None and vp:
                file = loose.get(normalize_base_name(vp))
            if path is not None and file is not None and Path(path).resolve() == file.resolve():
                return vehicle_id
            if wanted and file is None and vp and normalize_base_name(vp) == wanted:
                return vehicle_id
        return None

    def actor_mesh(self, vehicle_id: int) -> SectorMesh:
        """Return a vehicle body, composing mounted guns for Host Stations and unit_num_guns."""
        visual = self.vehicles.get(vehicle_id)
        if visual is not None and (visual.model.casefold() == 'robo' or visual.guns):
            return self.host_mesh(vehicle_id)
        return self.vehicle_mesh(vehicle_id)

    def _resolve_visual_faces(self, vp: int, base_path: str, three_ds_path: str) -> tuple[list | None, str]:
        """Resolve faces in engine priority order: 3DS -> BASE -> positional VP fallback."""
        if three_ds_path:
            path = self._visual_path(three_ds_path)
            if path is not None:
                try:
                    faces = list(self._three_ds_faces(path))
                    if faces:
                        return faces, three_ds_path
                except (OSError, ValueError, struct.error):
                    pass
        base = base_path
        faces = None
        if base:
            path = self._visual_path(base)
            if path is not None:
                # Exact paths win over same-named files elsewhere in a set.
                from studio.assets.family import load_asset_family
                obj = load_asset_family(path, [self.assets.set_dir], {}, self.assets.archive)
                faces = self._family_faces(obj.root_object, obj)
        else:
            base = (self._vp_names[vp] if 0 <= vp < len(self._vp_names) else '')
            faces = self._base_faces(base, set_only=True) if base else None
        if faces is None:
            # The engine falls back to the positional VP when an external asset fails.
            fallback = (self._vp_names[vp] if 0 <= vp < len(self._vp_names) else '')
            faces = self._base_faces(fallback, set_only=True) if fallback else None
        return faces, (base or f'VP {vp}')

    def vehicle_mesh(self, vehicle_id: int) -> SectorMesh:
        """Use the same visual and material loader for guns and squad members."""
        if vehicle_id in self._vehicle_meshes:
            return self._vehicle_meshes[vehicle_id]
        mesh = SectorMesh(-1)
        visual = self.vehicles.get(vehicle_id)
        if visual is not None:
            faces, label = self._resolve_visual_faces(
                visual.vp_normal, visual.base_normal, visual.three_ds_normal)
            if faces is None:
                mesh.missing.append(label)
            else:
                transformed = _transform_faces(
                    faces, scale=visual.scale, rotation=visual.rotation, tint=visual.tint)
                mesh.faces.extend((face, 0, 0) for face in transformed)
            dec = visual.decoration
            if dec.mode == 'persistent' and dec.tint[3] > 0 and (dec.vp > 0 or dec.base or dec.three_ds):
                dec_faces, _ = self._resolve_visual_faces(dec.vp, dec.base, dec.three_ds)
                if dec_faces:
                    transformed_dec = _transform_faces(
                        dec_faces, scale=dec.scale, tint=dec.tint, offset=dec.offset)
                    mesh.faces.extend((face, 0, 0) for face in transformed_dec)
        else:
            mesh.missing.append(f'vehicle {vehicle_id}')
        self._vehicle_meshes[vehicle_id] = mesh
        return mesh

    def reload_definitions(self, extra=''):
        self.vehicles = load_vehicle_files(self.scripts, extra)
        self.buildings = load_building_files(self.scripts, extra)
        self._vehicle_meshes.clear()
        self._gun_meshes.clear()
        self._host_meshes.clear()
        if hasattr(self, '_building_decorations'):
            self._building_decorations.clear()
        self._external_surfaces.clear()

    def _visual_path(self, value):
        value = value.strip().strip('"').replace('\\', '/')
        if value.casefold().startswith(('data/', 'data:')):
            value = value[5:]
        path = bootstrap.game_data_dir()
        for part in value.split('/'):
            path = bootstrap.find_ci(path, part) if path is not None else None
        return path if path is not None and path.is_file() else None

    def _three_ds_faces(self, path):
        from PIL import Image, ImageChops
        from studio.rendering.asset_viewport import ViewFace
        from studio.rendering.indexed import IndexedSurface
        from ..core.three_ds import read_3ds, Material3DS
        meshes, materials = read_3ds(path)
        palette = np.asarray(self.tables.palette, dtype=np.int32)
        pal = Image.new('P', (1, 1))
        pal.putpalette(palette.astype(np.uint8).ravel().tolist())
        surfaces = {}
        for mesh in meshes:
            for index, polygon in enumerate(mesh.faces):
                material = materials.get(mesh.materials.get(index), Material3DS())
                label = (material.name, bool(mesh.uvs))
                if label not in surfaces:
                    surface = None
                    name = material.texture.replace('\\', '/')
                    texture = None
                    if name and not (name.startswith('/') or ':' in name):
                        texture = path.parent
                        for part in name.split('/'):
                            texture = bootstrap.find_ci(texture, part) if texture is not None else None
                    # Match the engine's fallback for legacy exporter paths.
                    if name and (texture is None or not texture.is_file()):
                        texture = bootstrap.find_ci(path.parent, name.rsplit('/', 1)[-1].rsplit(':', 1)[-1])
                    if name and (texture is None or not texture.is_file()):
                        authored = Path(name)
                        texture = authored if authored.is_file() else self._visual_path(name)
                    if mesh.uvs and material.texture and texture is not None and texture.is_file():
                        with Image.open(texture) as source:
                            rgba = source.convert('RGBA')
                            rgb = ImageChops.multiply(rgba.convert('RGB'), Image.new('RGB', rgba.size, material.diffuse))
                            pixels = np.asarray(rgb.quantize(palette=pal, dither=Image.Dither.NONE)).copy()
                            # Index zero is the shared renderer's transparent texel.
                            pixels[(pixels == 0) & (np.asarray(rgba)[:, :, 3] >= 128)] = 1
                            pixels[np.asarray(rgba)[:, :, 3] < 128] = 0
                            surface = IndexedSurface(str(texture), 'texture', pixels.tobytes(),
                                                     rgba.width, rgba.height, None, 'none', 0,
                                                     'clear' if np.any(pixels == 0) else 'none', 'depth')
                    if surface is None:
                        color = int(np.argmin(((palette[1:] - material.diffuse) ** 2).sum(axis=1))) + 1
                        surface = IndexedSurface(material.name, 'solid', None, 0, 0, color,
                                                 'none', 0, 'none', 'depth')
                    key = -len(self._external_surfaces) - 1
                    self._external_surfaces[key] = surface
                    surfaces[label] = key
                key = surfaces[label]
                uvs = [mesh.uvs[i] for i in polygon] if mesh.uvs else [(0, 0)] * 3
                yield ViewFace([mesh.vertices[i] for i in polygon], uvs, key, poly_id=index)

    def _family_faces(self, root, family):
        if root is None:
            return None
        from studio.rendering.indexed import IndexedFamilyAdapter
        adapter = IndexedFamilyAdapter(family)
        self._loader._faces = []
        emitter_start = len(self._loader._particle_emitters)
        material_index = {}
        for obj in root.iter_tree():
            self._loader._load_object(obj, family, material_index,
                                      primary=True, owner=obj.owner_path)
        faces = [face for face in self._loader._faces if face.mapped]
        for face in faces:
            self._material_adapters[face.material] = adapter
        for emitter in self._loader._particle_emitters[emitter_start:]:
            for material, stage_id, stage in emitter.stages:
                adapter.block_map[(emitter.owner, stage_id)] = stage
                self._material_adapters[material] = adapter
        self._loader._faces = []
        return faces

    def external_mesh(self, sector_file: str, state: int = STATE_INTACT) -> SectorMesh:
        """Compose the requested damage state from exact files and active SET models."""
        from studio.assets.catalog import external_sector_visuals
        from studio.formats.sector_file import SectorFileError, read_sector, resolve_data_path
        from ..core.sector_resolver import ResolvedSub
        key = (sector_file.casefold(), state)
        if key in self._meshes:
            return self._meshes[key]
        mesh = SectorMesh(-1)
        self._meshes[key] = mesh
        try:
            path = resolve_data_path(bootstrap.game_data_dir(), sector_file)
            sector = read_sector(path)
        except (OSError, SectorFileError):
            mesh.missing.append(sector_file)
            return mesh
        models = external_sector_visuals(self.assets, path, sector)
        mesh.missing.extend(model.visual for model in sector.models
                            if models[model.name.casefold()] is None)
        if mesh.missing:
            return mesh
        visuals = {}
        for index, cell in enumerate(sector.cells):
            model = sector.model(sector.piece(cell).state(state))
            choice = models[model.name.casefold()]
            if choice not in visuals:
                try:
                    visuals[choice] = self._sector_visual_faces(choice)
                except (OSError, ValueError, RuntimeError, struct.error):
                    visuals[choice] = None
            faces = visuals[choice]
            if faces is None:
                if model.visual not in mesh.missing:
                    mesh.missing.append(model.visual)
                continue
            ox, oz = ResolvedSub(4 if sector.single else index, -1, -1, None, None).offset
            mesh.faces.extend((face, ox, oz) for face in faces)
        return mesh

    def _sector_visual_faces(self, choice):
        if choice.base_name:
            return self._base_faces(choice.base_name, set_only=True)
        if choice.path.suffix.casefold() == ".3ds":
            return list(self._three_ds_faces(choice.path)) or None
        from studio.assets.family import load_asset_family
        family = load_asset_family(choice.path, [self.assets.set_dir], setbas=self.assets.archive)
        return self._family_faces(family.root_object, family)

    def sector_mesh(self, sector_file, fallback, state=STATE_INTACT):
        if sector_file:
            mesh = self.external_mesh(sector_file, state)
            if mesh.faces and not mesh.missing:
                return mesh
        return self.mesh(fallback, state)

    def definition_mesh(self, definition) -> SectorMesh:
        """Sector of a building definition: its external .sector, else its SET sector."""
        return self.sector_mesh(getattr(definition, "sector_file", ""), definition.sec_type)

    @staticmethod
    def tinted_mesh(mesh: SectorMesh, tint: tuple[int, int, int, int]) -> SectorMesh:
        """Return a SectorMesh whose faces carry ``tint``, or empty when alpha is 0."""
        if tint == WHITE_TINT:
            return mesh
        if tint[3] == 0:
            return SectorMesh(mesh.typ, resolved=mesh.resolved, missing=list(mesh.missing))
        return SectorMesh(
            mesh.typ,
            faces=[(replace(face, tint=tint), ox, oz) for face, ox, oz in mesh.faces],
            resolved=mesh.resolved,
            missing=list(mesh.missing),
        )

    def building_decoration_mesh(self, building_id: int) -> SectorMesh:
        """Persistent decoration_fx mesh for a building, or an empty SectorMesh."""
        cache = getattr(self, '_building_decorations', None)
        if cache is not None and building_id in cache:
            return cache[building_id]
        mesh = SectorMesh(-1)
        definition = self.buildings.get(building_id)
        if definition is not None:
            dec = definition.decoration
            if dec.mode == 'persistent' and dec.tint[3] > 0 and (dec.vp > 0 or dec.base or dec.three_ds):
                faces, _ = self._resolve_visual_faces(dec.vp, dec.base, dec.three_ds)
                if faces:
                    transformed = _transform_faces(
                        faces, scale=dec.scale, tint=dec.tint, offset=dec.offset)
                    mesh.faces.extend((face, 0, 0) for face in transformed)
        if cache is not None:
            cache[building_id] = mesh
        return mesh

    def building_preview(self, building_id):
        definition = self.buildings.get(building_id)
        if definition is None:
            return SectorMesh(-1)
        sector = self.tinted_mesh(self.definition_mesh(definition), definition.tint)
        guns = self.building_mesh(building_id)
        dec = self.building_decoration_mesh(building_id)
        return SectorMesh(definition.sec_type, faces=sector.faces + guns.faces + dec.faces,
                          missing=sector.missing + guns.missing)

    def filler_mesh(self, surface1: int, surface2: int, vertical: bool,
                    heights) -> SectorMesh:
        """Riusa i 72 slurp del SET.BAS e le assegnazioni POO2 del motore."""
        key = (surface1, surface2, vertical)
        if key not in self._filler_templates:
            root = self.assets.family.root_object
            fillers = root.kids[2].kids if len(root.kids) > 2 else []
            index = int(vertical) * 36 + surface1 * 6 + surface2
            if not (0 <= surface1 < 6 and 0 <= surface2 < 6 and index < len(fillers)):
                raise ValueError(f"Missing terrain transition: {surface1}/{surface2}")
            override = self._filler_overrides.get(index)
            obj = self.assets.base_object(override, set_only=True) if override else None
            if obj is not None:
                faces = self._base_faces(override, set_only=True)
            else:
                obj = fillers[index]
                self._loader._faces = []
                self._loader._load_object(obj, self.assets.family, self._material_index,
                                          primary=True, owner=obj.owner_path)
                faces = [f for f in self._loader._faces if f.mapped]
                self._loader._faces = []
            self._filler_templates[key] = (obj.skeleton, faces)
        skeleton, faces = self._filler_templates[key]
        mesh = SectorMesh(-1)
        for face in faces:
            indices = skeleton.polygons[face.poly_id]
            vertices = [(v[0], heights[index], v[2])
                        for v, index in zip(face.vertices, indices)]
            mesh.faces.append((replace(face, vertices=vertices), 0, 0))
        return mesh


def gun_rotation(direction):
    """World::InitialGunRotation().Transpose(), including vertical directions."""
    z = np.asarray(direction, dtype=float)
    length = np.linalg.norm(z)
    if length > .001:
        z = z / length
    if z[1] != 0:
        horizontal = math.hypot(z[0], z[2])
        if horizontal:
            y = np.array((-z[0]*z[1]/horizontal, horizontal,
                          -z[2]*z[1]/horizontal))
        else:
            y = np.array((0., 0., 1.))
    else:
        y = np.array((0., 1., 0.))
    return np.column_stack((np.cross(y, z), y, z))


def visual_rotation_matrix(degrees: tuple[float, float, float]) -> np.ndarray:
    """Match engine Orientation(degrees) = RotateX * RotateY * RotateZ from matrix.h."""
    rx, ry, rz = (math.radians(d) for d in degrees)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    rot_x = np.array(((1.0, 0.0, 0.0), (0.0, cx, sx), (0.0, -sx, cx)), dtype=float)
    rot_y = np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)), dtype=float)
    rot_z = np.array(((cz, sz, 0.0), (-sz, cz, 0.0), (0.0, 0.0, 1.0)), dtype=float)
    return rot_x @ rot_y @ rot_z


def _transform_faces(faces, *, scale=(1.0, 1.0, 1.0), rotation=(0.0, 0.0, 0.0),
                     tint=None, offset=(0.0, 0.0, 0.0)):
    if tint is not None and tint == WHITE_TINT:
        tint = None
    if tint is not None and tint[3] == 0:
        return []
    has_scale = scale != (1.0, 1.0, 1.0)
    has_rot = rotation != (0.0, 0.0, 0.0)
    has_offset = offset != (0.0, 0.0, 0.0)
    if not (has_scale or has_rot or has_offset or tint is not None):
        return list(faces)
    scale_vec = np.asarray(scale, dtype=float) if has_scale else None
    rot = visual_rotation_matrix(rotation) if has_rot else None
    off_vec = np.asarray(offset, dtype=float) if has_offset else None
    result = []
    for face in faces:
        if has_scale or has_rot or has_offset:
            pts = np.asarray(face.vertices, dtype=float)
            if scale_vec is not None:
                pts = pts * scale_vec
            if rot is not None:
                pts = pts @ rot.T
            if off_vec is not None:
                pts = pts + off_vec
            verts = [tuple(float(c) for c in v) for v in pts]
        else:
            verts = face.vertices
        new_tint = tint if tint is not None else getattr(face, 'tint', None)
        result.append(replace(face, vertices=verts, tint=new_tint))
    return result
