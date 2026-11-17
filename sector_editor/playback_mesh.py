"""Animation samples consumed by the existing sector renderer, without moving its camera."""
from dataclasses import replace

from map_editor.render.sector_mesh import SectorMesh


class PlaybackMesh:
    def __init__(self, library, camera):
        self.library = library
        self.camera = camera
        self.time_ms = 0.0
        self.effects = []
        self.sources = {}

    def __getattr__(self, name):
        return getattr(self.library, name)

    def billboard(self, face):
        center = face.billboard_center
        if center is None:
            return face
        cy, sy, cp, sp = self.camera._trig()
        right, up = (cy, 0, sy), (sp * sy, -cp, -sp * cy)
        radius = abs(face.vertices[0][0] - center[0])
        vertices = [tuple(center[i] + radius * (x * right[i] + y * up[i]) for i in range(3))
                    for x, y in ((-1, -1), (-1, 1), (1, 1), (1, -1))]
        return replace(face, vertices=vertices)

    def mesh(self, typ, state=0):
        original = self.library.mesh(typ, state)
        loader = self.library._loader
        # Keep the map's original faces, offsets, picking and camera. Only the
        # sampled texture frames and emitter sprites change with the clock.
        placements = {(face.owner, ox, oz, id(self.library._material_adapters.get(face.material,
                                                                               self.library.adapter)))
                      for face, ox, oz in original.faces}
        emitters, faces = loader._particle_emitters, loader._faces
        emitter_places = {}
        for emitter in emitters:
            adapters = {id(self.library._material_adapters.get(material, self.library.adapter))
                        for material, _, _ in emitter.stages}
            positions = {(ox, oz) for owner, ox, oz, adapter in placements
                         if adapter in adapters and (owner == emitter.owner or
                                                      emitter.owner.startswith(owner + "/"))}
            if positions:
                emitter_places[(emitter.owner, emitter.block_index)] = positions
        loader._particle_emitters = [emitter for emitter in emitters
                                    if (emitter.owner, emitter.block_index) in emitter_places]
        try:
            loader.seek_animation(self.time_ms)
            particles = [face for face in loader._faces if face.billboard_center is not None]
        finally:
            loader._particle_emitters, loader._faces = emitters, faces
        result = list(original.faces)
        for face in particles:
            result.extend((self.billboard(face), ox, oz)
                          for ox, oz in emitter_places[(face.owner, face.emitter_block)])
        result.extend((self.billboard(face), 0, 0) for face in self.effects)
        return SectorMesh(typ, faces=result, resolved=original.resolved, missing=original.missing)

    def surface_for(self, face):
        source = self.sources.get(id(face))
        if source is not None:
            loader = source
            adapter = loader._indexed_adapter
        else:
            if face.material < 0:
                return self.library.surface_for(face)
            loader = self.library._loader
            adapter = self.library._material_adapters.get(face.material, self.library.adapter)
        frame = loader._anim_states.get(face.material, (0, 1))[0]
        return adapter.resolve_surface(face, loader._materials[face.material], frame)

    def face_uvs(self, face):
        loader = self.sources.get(id(face))
        return (loader._face_uvs(face, loader._materials[face.material]) if loader is not None
                else self.library.face_uvs(face))

    def sample_effects(self, sampler, simulation, time_ms):
        from entity_designer.fx_viewport import _transform
        from studio.rendering.asset_viewport_common import ViewFace
        self.effects, self.sources = [], {}
        for item in simulation.items_at(time_ms):
            helper = sampler._fx_external_for(item)
            if helper is None:
                continue
            helper.seek_animation(time_ms)
            for face, vertices in sampler._fx_helper_faces(helper, item):
                sampled = replace(face, vertices=[_transform(item, vertex) for vertex in vertices])
                self.effects.append(sampled)
                self.sources[id(sampled)] = helper
        specs = {key: sampler.fx_vp_emitter_specs(key) for key in simulation.emitter_keys()}
        for sprite in simulation.particles_at(time_ms, specs):
            helper = (sampler._fx_external_helper(sprite.vp[0]) if isinstance(sprite.vp, tuple)
                      else sampler._fx_set_vp_helper(sprite.vp))
            source = None if isinstance(sprite.vp, tuple) else sampler._fx_vp_source(sprite.vp)
            if helper is None and source is not None:
                # Emitter-only VPs have no polygon faces, but the cached loader
                # still owns their stage materials.
                helper = sampler._fx_external_cache.get(f"set:{id(source[0])}")
            if helper is None:
                continue
            emitters = [e for e in helper._particle_emitters if source is None or e.owner == source[1]]
            if sprite.emitter >= len(emitters):
                continue
            material, stage_id, stage = emitters[sprite.emitter].stages[sprite.stage]
            x, y, z = sprite.position
            radius = sprite.size * .5
            face = ViewFace([(x-radius,y-radius,z),(x-radius,y+radius,z),
                             (x+radius,y+radius,z),(x+radius,y-radius,z)],
                            list(stage.texture.outline_uvs) if stage.texture else [], material,
                            shade=stage.shade_val, primary=False, block_index=stage_id,
                            owner=emitters[sprite.emitter].owner,
                            billboard_center=sprite.position)
            # Orient now so the surface lookup retains this face's identity.
            face = self.billboard(face)
            face.billboard_center = None
            self.effects.append(face)
            self.sources[id(face)] = helper
