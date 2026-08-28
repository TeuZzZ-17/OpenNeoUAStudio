from __future__ import annotations

import ast
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import indexed_renderer
from asset_family import AssetFamily, FamilyObject, MaterialGroup
from base_parser import AmeshBlock, BaseObject, BaseTransform, TextureRef
from ilbm_parser import IlbmImage
from indexed_family_adapter import (
    IndexedFamilyAdapter,
    UnsupportedIndexedMaterialError,
)
from indexed_renderer import (
    IndexedPiece,
    IndexedRasterizer,
    IndexedSurface,
    IndexedTables,
    retail_source_face_front_facing,
)


def _palette():
    return tuple((i, i, i) for i in range(256))


def _simple_tables(changes=None):
    shader = bytearray(bytes(range(256)) * 256)
    tracy = bytearray()
    for background in range(256):
        tracy.extend(bytes((background,)) * 256)
    for (background, source), output in (changes or {}).items():
        tracy[background * 256 + source] = output
    return IndexedTables(_palette(), bytes(shader), bytes(tracy))


def _surface(index, tracy="none"):
    return IndexedSurface(
        name=f"solid-{index}", kind="solid", indices=None,
        width=0, height=0, solid_index=index,
        shade_mode="none", shade_value=0, tracy_mode=tracy,
        map_mode="linear")


def _piece(surface, face="face", order=0, depth=0.0):
    return IndexedPiece(
        source_face_id=face,
        polygon_id=order + 1,
        screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        uvs=(),
        camera_vertices=((0.0, 0.0, 0.0),) * 3,
        surface=surface,
        source_order=order,
        sort_depth=depth,
    )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack(">I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def _form(kind: bytes, chunks: bytes) -> bytes:
    payload = kind + chunks
    return b"FORM" + struct.pack(">I", len(payload)) + payload


def _write_palette(path: Path, palette) -> None:
    cmap = bytes(channel for rgb in palette for channel in rgb)
    path.write_bytes(_form(b"ILBM", _chunk(b"CMAP", cmap)))


def _write_vbmp(path: Path, pixels: bytes) -> None:
    payload = _chunk(b"HEAD", struct.pack(">HHH", 256, 256, 0))
    payload += _chunk(b"BODY", pixels)
    path.write_bytes(_form(b"VBMP", payload))


def _ua_shader() -> bytes:
    data = bytearray(bytes(range(256)) * 256)
    for shade in range(256):
        data[shade * 256] = 0
    data[6] = 0
    data[255 * 256:] = bytes(256)
    return bytes(data)


def _ua_tracy() -> bytes:
    # Start with background-preserving rows.
    data = bytearray()
    for background in range(256):
        data.extend(bytes((background,)) * 256)
    # Required source columns are opaque source identity.
    for source in (8, 10, 11, 12, 14, 15):
        for background in range(256):
            data[background * 256 + source] = source
    # Source zero preserves background, except retail row 13 maps it to zero.
    for background in range(256):
        data[background * 256] = 0 if background == 13 else background
    # Background 13 is source identity.
    for source in range(256):
        data[13 * 256 + source] = source
    for (background, source), output in {
        (31, 220): 212,
        (220, 31): 200,
        (13, 255): 255,
        (255, 13): 177,
        (156, 185): 54,
        (159, 185): 106,
        (0, 156): 223,
        (0, 159): 255,
        (223, 159): 207,
        (255, 156): 191,
    }.items():
        data[background * 256 + source] = output
    return bytes(data)


class IndexedViewerPolicySourceTests(unittest.TestCase):
    @classmethod
    def _method_source(cls, name: str) -> str:
        path = Path(__file__).resolve().parents[1] / "assembly_viewer.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                segment = ast.get_source_segment(source, node)
                if segment is None:
                    break
                return segment
        raise AssertionError(f"method {name!r} not found in assembly_viewer.py")

    def test_textured_mode_is_single_retail_full_resolution_path(self):
        render_scene = self._method_source("_render_scene")
        self.assertIn('mode == "textured"', render_scene)
        self.assertIn("_render_indexed_model", render_scene)
        self.assertIn('"viewport_target_width": target_width', render_scene)
        self.assertIn('"viewport_render_width": target_width', render_scene)
        for obsolete in (
                "textured_indexed", "fit_render_size",
                "openneoua_preview", "transient_proxy",
                "viewport_dynamic_quality", "viewport_scaled_preview"):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, render_scene)

    def test_old_rgb_textured_renderer_and_ui_choice_are_removed(self):
        root = Path(__file__).resolve().parents[1]
        viewer = (root / "assembly_viewer.py").read_text(encoding="utf-8")
        window = (root / "assembly_window.py").read_text(encoding="utf-8")
        batch = (root / "snapshot_studio" / "batch_export.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def _draw_textured", viewer)
        self.assertNotIn("_indexed_transient_proxy_active", viewer)
        self.assertNotIn("_indexed_dynamic_quality_active", viewer)
        self.assertNotIn("textured_indexed", viewer + window + batch)
        self.assertNotIn("snapshot_renderer_combo", window + batch)

    def test_snapshot_forces_retail_and_fails_closed_independently_of_ui_mode(self):
        snapshot = self._method_source("render_snapshot")
        self.assertIn("force_textured=True", snapshot)
        self.assertNotIn('self._mode == "textured"', snapshot)
        self.assertIn('self._last_effective_renderer != "retail_indexed_reconstructed"',
                      snapshot)


class IndexedRendererPortTests(unittest.TestCase):

    @staticmethod
    def _fade_tables():
        palette = _palette()
        shader = bytes(
            shade for shade in range(256) for _source in range(256))
        tracy = bytes(
            source for _background in range(256)
            for source in range(256))
        return IndexedTables(palette, shader, tracy)

    @staticmethod
    def _fade_piece(surface, distance=50.0):
        return IndexedPiece(
            source_face_id="fade", polygon_id=1,
            screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            uvs=((0.0, 0.0),) * 3 if surface.kind == "texture" else (),
            camera_vertices=((0.0, 0.0, 0.0),) * 3,
            surface=surface, distance_fade=True,
            fade_start=0.0, fade_length=100.0,
            vertex_distances=(distance,) * 3)

    def test_area_distance_fade_affects_only_flagged_piece(self):
        tables = self._fade_tables()
        surface = IndexedSurface(
            "opaque", "texture", b"\x0a", 1, 1, None,
            "none", 0, "none", "linear")
        faded = IndexedRasterizer.render(
            1, 1, (self._fade_piece(surface),), tables)
        plain = IndexedRasterizer.render(
            1, 1, (IndexedPiece(
                source_face_id="plain", polygon_id=1,
                screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
                uvs=((0.0, 0.0),) * 3,
                camera_vertices=((0.0, 0.0, 0.0),) * 3,
                surface=surface),), tables)
        self.assertEqual(int(faded.indices[0][0]), 128)
        self.assertEqual(int(plain.indices[0][0]), 10)
        self.assertEqual(faded.stats["distance_fade_samples"], 1)

    def test_area_fade_keeps_clear_zero_and_fades_nonzero_before_tracy(self):
        tables = self._fade_tables()
        background = _piece(_surface(20), "background", order=0)
        clear_zero = IndexedSurface(
            "clear-zero", "texture", b"\x00", 1, 1, None,
            "none", 0, "clear", "linear")
        zero = IndexedRasterizer.render(
            1, 1, (background, self._fade_piece(clear_zero)), tables)
        self.assertEqual(int(zero.indices[0][0]), 20)
        self.assertEqual(zero.stats["clear_source_zero_skipped"], 1)

        flat = IndexedSurface(
            "flat", "texture", b"\x0a", 1, 1, None,
            "none", 0, "flat", "linear")
        composed = IndexedRasterizer.render(
            1, 1, (background, self._fade_piece(flat)), tables)
        # The test TRACY table returns its source.  A 50% fog contribution
        # therefore proves SHADERMP was applied before flat-TRACY composition.
        self.assertEqual(int(composed.indices[0][0]), 128)

    def test_area_fade_and_depth_mapping_are_deterministic_on_both_backends(self):
        tables = self._fade_tables()
        surface = IndexedSurface(
            "depth", "texture", b"\x0a", 1, 1, None,
            "none", 0, "none", "depth")
        piece = self._fade_piece(surface, 75.0)
        accelerated = IndexedRasterizer.render(1, 1, (piece,), tables)
        with patch.object(indexed_renderer, "_np", None):
            portable = IndexedRasterizer.render(1, 1, (piece,), tables)
        self.assertEqual(int(accelerated.indices[0][0]), 191)
        self.assertEqual(accelerated.indices.tolist(), portable.indices)
        self.assertEqual(accelerated.stats["depth_mapped_samples"], 1)

    def test_whole_face_cull_uses_preclip_3d_winding(self):
        camera_vertices = [
            (-0.16202796, -1.97493340, 3.61461335),
            (-1.48634709, 1.25880337, 3.78281788),
            (-0.81305745, 0.57071037, 3.98094668),
        ]
        self.assertTrue(retail_source_face_front_facing(camera_vertices))
        self.assertFalse(retail_source_face_front_facing([
            camera_vertices[0], camera_vertices[2], camera_vertices[1]]))

    def test_tracy_uses_background_then_raw_source(self):
        tables = _simple_tables({(20, 31): 44, (31, 20): 55})
        self.assertEqual(tables.tracy_index(20, 31), 44)
        self.assertEqual(tables.tracy_index(31, 20), 55)

    def test_flat_tracy_reads_live_framebuffer_and_is_order_sensitive(self):
        tables = _simple_tables({(20, 31): 44, (0, 31): 33})
        opaque = _piece(_surface(20), "opaque", order=0)
        effect = _piece(_surface(31, "flat"), "effect", order=1, depth=2.0)
        result = IndexedRasterizer.render(1, 1, (opaque, effect), tables)
        self.assertEqual(int(result.indices[0][0]), 44)
        self.assertEqual(
            result.stats["tracy_lookup_axis_order"],
            "TRACYRMP[background][raw_source]",
        )

    def test_flat_tracy_source_face_is_composed_once_per_pixel(self):
        tables = _simple_tables({(20, 31): 44, (44, 31): 55})
        opaque = _piece(_surface(20), "opaque", order=0)
        first = _piece(_surface(31, "flat"), "one-source-face", order=1)
        duplicate = _piece(
            _surface(31, "flat"), "one-source-face", order=2)
        result = IndexedRasterizer.render(
            1, 1, (opaque, first, duplicate), tables)
        self.assertEqual(int(result.indices[0][0]), 44)
        self.assertEqual(
            result.stats["flat_tracy_duplicate_samples_skipped"], 1)

    def test_clear_tracy_skips_numeric_source_zero_only(self):
        tables = _simple_tables()
        texture = IndexedSurface(
            name="clear", kind="texture", indices=b"\x00", width=1, height=1,
            solid_index=None,
            shade_mode="none", shade_value=0, tracy_mode="clear",
            map_mode="linear")
        base = _piece(_surface(21), "base", order=0)
        clear = IndexedPiece(
            source_face_id="clear", polygon_id=2,
            screen=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            uvs=((0.0, 0.0),) * 3,
            camera_vertices=((0.0, 0.0, 0.0),) * 3,
            surface=texture, source_order=1, sort_depth=1.0)
        result = IndexedRasterizer.render(1, 1, (base, clear), tables)
        self.assertEqual(int(result.indices[0][0]), 21)
        self.assertEqual(result.stats["clear_source_zero_skipped"], 1)

    def test_flat_transparent_faces_replay_by_descending_publish_depth(self):
        tables = _simple_tables({
            (10, 5): 20,
            (20, 6): 30,
            (10, 6): 40,
            (40, 5): 50,
        })
        base = _piece(_surface(10), "base", order=0)
        near = _piece(_surface(5, "flat"), "near", order=1, depth=2.0)
        far = _piece(_surface(6, "flat"), "far", order=2, depth=1.0)
        result = IndexedRasterizer.render(1, 1, (base, far, near), tables)
        # Depth-descending replay applies source 5 first, then source 6.
        self.assertEqual(int(result.indices[0][0]), 30)

    def test_numpy_and_python_fallback_produce_same_indices(self):
        tables = _simple_tables({(10, 5): 20, (20, 6): 30})
        pieces = (
            _piece(_surface(10), "base", order=0),
            _piece(_surface(5, "flat"), "first", order=1, depth=2.0),
            _piece(_surface(6, "flat"), "second", order=2, depth=1.0),
        )
        accelerated = IndexedRasterizer.render(3, 3, pieces, tables)
        with patch.object(indexed_renderer, "_np", None):
            portable = IndexedRasterizer.render(3, 3, pieces, tables)
        self.assertEqual(
            bytes(accelerated.indices.reshape(-1).tolist()),
            bytes(value for row in portable.indices for value in row),
        )
        self.assertEqual(
            accelerated.stats["index_buffer_sha256"],
            portable.stats["index_buffer_sha256"],
        )


class IndexedAdapterPortTests(unittest.TestCase):
    def _family(self, root: Path, block: AmeshBlock, image: IlbmImage) -> AssetFamily:
        palette = list(_palette())
        (root / "PALETTE").mkdir(parents=True)
        (root / "REMAP").mkdir(parents=True)
        palette_path = root / "PALETTE" / "STANDARD.PAL"
        _write_palette(palette_path, palette)
        _write_vbmp(root / "REMAP" / "SHADERMP.ILB", _ua_shader())
        _write_vbmp(root / "REMAP" / "TRACYRMP.ILB", _ua_tracy())
        group = MaterialGroup(
            label=block.texture.name if block.texture else "solid",
            texture_name=block.texture.name if block.texture else "",
            kind="ilbm" if block.texture else "",
            block=block,
        )
        obj = FamilyObject(
            base_object=BaseObject(name="test", ades=[block]),
            materials=[group], owner_path="root")
        return AssetFamily(
            root_object=obj,
            textures={image.source_name: image},
            external_palette=palette,
            external_palette_path=palette_path,
            search_roots=[str(root)],
        )

    def test_adapter_uses_existing_asset_family_and_resolves_flat_tracy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            block = AmeshBlock(
                polflags=0x82 | 0x08,
                texture=TextureRef(kind="ilbm", name="FX.ILBM"))
            image = IlbmImage(
                source_name="FX.ILBM", kind="VBMP", width=1, height=1,
                pixels=b"\x1f")
            family = self._family(root, block, image)
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            face = SimpleNamespace(
                owner="root", block_index=0, poly_id=0,
                mapped=True, shade=180)
            material = SimpleNamespace(
                label="FX.ILBM", anim_frames=[], anim_bitmap_names=[],
                anim_uv_groups=[])
            surface = adapter.resolve_surface(face, material, 0)
            self.assertEqual(surface.tracy_mode, "flat")
            self.assertEqual(surface.shade_mode, "none")
            self.assertEqual(surface.indices, b"\x1f")
            self.assertIs(surface, adapter.resolve_surface(face, material, 0))

    def test_selected_palette_set_remaps_are_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            other = Path(tmp) / "Set2"
            block = AmeshBlock(
                polflags=0x82 | 0x08,
                texture=TextureRef(kind="ilbm", name="FX.ILBM"))
            image = IlbmImage(
                source_name="FX.ILBM", kind="VBMP", width=1, height=1,
                pixels=b"\x1f")
            family = self._family(root, block, image)

            (other / "PALETTE").mkdir(parents=True)
            (other / "REMAP").mkdir(parents=True)
            _write_palette(other / "PALETTE" / "STANDARD.PAL", list(_palette()))
            alternate_shader = bytearray(_ua_shader())
            alternate_shader[1 * 256 + 2] = 99
            _write_vbmp(
                other / "REMAP" / "SHADERMP.ILB", bytes(alternate_shader))
            _write_vbmp(other / "REMAP" / "TRACYRMP.ILB", _ua_tracy())
            family.search_roots.append(str(other))

            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.shader_path.parent.parent, root.resolve())

    def test_malformed_remap_disables_indexed_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            block = AmeshBlock(
                polflags=0x82 | 0x08,
                texture=TextureRef(kind="ilbm", name="FX.ILBM"))
            image = IlbmImage(
                source_name="FX.ILBM", kind="VBMP", width=1, height=1,
                pixels=b"\x1f")
            family = self._family(root, block, image)
            (root / "REMAP" / "SHADERMP.ILB").write_bytes(b"not-an-iff")

            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertIsNone(adapter)
            self.assertTrue(reason)

    def test_none_face_shade_reuses_material_block_shade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            block = AmeshBlock(
                polflags=0x32 | 0x08, shade_val=100,
                texture=TextureRef(kind="ilbm", name="SHADE.ILBM"))
            image = IlbmImage(
                source_name="SHADE.ILBM", kind="VBMP", width=1, height=1,
                pixels=b"\x21")
            family = self._family(root, block, image)
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            face = SimpleNamespace(
                owner="root", block_index=0, poly_id=0,
                mapped=True, shade=None)
            material = SimpleNamespace(
                label="SHADE.ILBM", anim_frames=[], anim_bitmap_names=[],
                anim_uv_groups=[])
            surface = adapter.resolve_surface(face, material, 0)
            self.assertEqual(surface.shade_mode, "gradient")
            self.assertEqual(surface.shade_value, (253 * 100 + 0x180) >> 8)

    def test_unsupported_material_fails_only_when_visible_surface_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            block = AmeshBlock(
                polflags=0xC2 | 0x08,
                texture=TextureRef(kind="ilbm", name="BAD.ILBM"))
            image = IlbmImage(
                source_name="BAD.ILBM", kind="VBMP", width=1, height=1,
                pixels=b"\x01")
            family = self._family(root, block, image)
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            self.assertIsNotNone(adapter)
            face = SimpleNamespace(
                owner="root", block_index=0, poly_id=0,
                mapped=True, shade=0)
            material = SimpleNamespace(
                label="BAD.ILBM", anim_frames=[], anim_bitmap_names=[],
                anim_uv_groups=[])
            with self.assertRaises(UnsupportedIndexedMaterialError):
                adapter.resolve_surface(face, material, 0)

    def test_vanm_frame_selection_keeps_asset_vislimit_fade_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Set1"
            block = AmeshBlock(
                area_flags=1, polflags=0x02 | 0x08,
                texture=TextureRef(kind="bmpanim", name="FLASH.ANM"))
            first = IlbmImage(
                source_name="FRAME0.ILBM", kind="VBMP",
                width=1, height=1, pixels=b"\x08")
            family = self._family(root, block, first)
            family.root_object.base_object.transform = BaseTransform(
                vis_limit=1400)
            family.textures["FRAME1.ILBM"] = IlbmImage(
                source_name="FRAME1.ILBM", kind="VBMP",
                width=1, height=1, pixels=b"\x0a")
            adapter, reason = IndexedFamilyAdapter.try_create(family)
            self.assertEqual(reason, "")
            face = SimpleNamespace(
                owner="root", block_index=0, poly_id=0,
                mapped=True, shade=0)
            material = SimpleNamespace(
                label="FLASH.ANM",
                anim_frames=[(1, 0, 0), (1, 1, 0)],
                anim_bitmap_names=["FRAME0.ILBM", "FRAME1.ILBM"],
                anim_uv_groups=[[(0, 0), (255, 0), (0, 255)]])
            first_surface = adapter.resolve_surface(face, material, 0)
            second_surface = adapter.resolve_surface(face, material, 1)
            self.assertEqual(first_surface.indices, b"\x08")
            self.assertEqual(second_surface.indices, b"\x0a")
            self.assertEqual(adapter.distance_fade_profile(face), {
                "vis_limit": 1400.0,
                "fade_start": 800.0,
                "fade_length": 600.0,
            })
            self.assertEqual(
                adapter.distance_fade_profile(face, {
                    "use_asset": False,
                    "vis_limit": 2000.0,
                    "fade_start": 1000.0,
                    "fade_length": 1000.0,
                })["fade_start"],
                1000.0)


if __name__ == "__main__":
    unittest.main()
