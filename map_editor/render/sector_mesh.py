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
from ..core.vehicle_defs import load_vehicle_files
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

    def __init__(self, assets: SetAssets):
        import assembly_viewer as av
        self.assets = assets
        self.resolver = SectorResolver(assets.sdf)
        self._loader = av.AssetViewport()
        self._loader.load_family(assets.family, visible_owners=set())
        self.adapter = self._loader._indexed_adapter
        if self.adapter is None:
            raise RuntimeError(self._loader._indexed_unavailable_reason)
        self._material_index: dict = {}
        self._base_cache: dict[str, list] = {}
        self._meshes: dict[tuple[int, int], SectorMesh] = {}
        self._filler_templates: dict[tuple, tuple] = {}
        scripts = (bootstrap.installation().folder('scripts')
                   if bootstrap.installation() else bootstrap.game_data_dir() / 'Scripts')
        self.buildings = load_building_files(scripts)
        self.vehicles = load_vehicle_files(scripts)
        self.scripts = scripts
        self._external_surfaces = {}
        from vp_manager import parse_visproto
        directory = bootstrap.find_ci(assets.set_dir, 'scripts')
        path = bootstrap.find_ci(directory, 'visproto.lst') if directory else None
        self._vp_names = ([entry.base_name for entry in parse_visproto(path).entries]
                          if path else [obj.base_object.name for obj in
                                        assets.family.root_object.kids[0].kids])
        self._gun_meshes = {}
        self._host_meshes = {}
        self._vehicle_meshes = {}
        self._collision_skeletons = {}
        self._material_adapters = {}

    @property
    def tables(self):
        return self.adapter.tables

    def collision_skeleton(self, name):
        """Read the SDF collision SKLT, independent of the visible BASE faces."""
        key = name.casefold()
        if key not in self._collision_skeletons:
            from sklt_parser import parse_sklt_bytes
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

    def _base_faces(self, base_name: str) -> list | None:
        key = base_name.lower()
        if key in self._base_cache:
            return self._base_cache[key]
        obj = self.assets.base_object(base_name)
        faces = None
        if obj is not None:
            family = self.assets._loose_families.get(self.assets._key(base_name))
            if family is not None:
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

    def actor_mesh(self, vehicle_id: int) -> SectorMesh:
        """Return a vehicle body, composing mounted guns for Host Stations."""
        visual = self.vehicles.get(vehicle_id)
        if visual is not None and visual.model.casefold() == 'robo':
            return self.host_mesh(vehicle_id)
        return self.vehicle_mesh(vehicle_id)

    def vehicle_mesh(self, vehicle_id: int) -> SectorMesh:
        """Use the same visual and material loader for guns and squad members."""
        if vehicle_id in self._vehicle_meshes:
            return self._vehicle_meshes[vehicle_id]
        mesh = SectorMesh(-1)
        visual = self.vehicles.get(vehicle_id)
        if visual is not None:
            if visual.three_ds_normal:
                path = self._visual_path(visual.three_ds_normal)
                if path is not None:
                    try:
                        mesh.faces = [(face, 0, 0) for face in self._three_ds_faces(path)]
                    except (OSError, ValueError, struct.error):
                        mesh.faces = []
                    if mesh.faces:
                        self._vehicle_meshes[vehicle_id] = mesh
                        return mesh
            base = visual.base_normal
            if base:
                path = self._visual_path(base)
                if path is not None:
                    # Exact paths win over same-named files elsewhere in a set.
                    from asset_family import load_asset_family
                    obj = load_asset_family(path, [self.assets.set_dir], {}, self.assets.archive)
                    faces = self._family_faces(obj.root_object, obj)
                else:
                    faces = None
            else:
                base = (self._vp_names[visual.vp_normal]
                        if 0 <= visual.vp_normal < len(self._vp_names) else '')
                faces = self._base_faces(base) if base else None
            if faces is None:
                # The engine falls back to the positional VP when an external BASE fails.
                fallback = (self._vp_names[visual.vp_normal]
                            if 0 <= visual.vp_normal < len(self._vp_names) else '')
                faces = self._base_faces(fallback) if fallback else None
            if faces is None:
                mesh.missing.append(base or f'VP {visual.vp_normal}')
            else:
                for face in faces:
                    mesh.faces.append((face, 0, 0))
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
        from assembly_viewer import ViewFace
        from indexed_renderer import IndexedSurface
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
        from indexed_family_adapter import IndexedFamilyAdapter
        adapter = IndexedFamilyAdapter(family)
        self._loader._faces = []
        material_index = {}
        for obj in root.iter_tree():
            self._loader._load_object(obj, family, material_index,
                                      primary=True, owner=obj.owner_path)
        faces = [face for face in self._loader._faces if face.mapped]
        for face in faces:
            self._material_adapters[face.material] = adapter
        self._loader._faces = []
        return faces

    def building_preview(self, building_id):
        definition = self.buildings.get(building_id)
        if definition is None:
            return SectorMesh(-1)
        sector = self.mesh(definition.sec_type)
        guns = self.building_mesh(building_id)
        return SectorMesh(definition.sec_type, faces=sector.faces + guns.faces,
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
