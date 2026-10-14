"""Particle-only BASEs, native animation seeking and resource-list playback."""

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QTreeWidgetItem

from studio.assets.family import (
    AssetFamily, FamilyObject, animation_preview_family, load_asset_family,
)
from studio.formats.animation import ParticleAttributes, VanmData, VanmFrame
from studio.formats.base_format import AmeshBlock, BaseObject, TextureRef
from studio.formats.images import IlbmImage
from studio.formats.sklt import SkltModel
from studio.rendering.asset_viewport import PREVIEW_SCALE, AssetViewport
from tests.synthetic_indexed_profile import attach_synthetic_indexed_profile


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def effect(tmp_path):
    a = ParticleAttributes(1, (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0),
                           0, 70, 1, 1000, 0, 200, 50, 800, 40, 2, 0)
    stage = AmeshBlock(
        class_id="area.class", polflags=0x7A,
        texture=TextureRef(kind="ilbm", name="sprite.ilbm",
                           outline_uvs=[(0, 0), (0, 255), (255, 255), (255, 0)]))
    block = AmeshBlock(class_id="particle.class", particle_attributes=a,
                       particle_stages=[stage])
    skeleton = SkltModel(source_name="emitter.sklt", points=[(0.0, 0.0, 0.0)])
    obj = FamilyObject(BaseObject(name="effect.base", ades=[block]),
                       skeleton=skeleton)
    family = AssetFamily(root_object=obj)
    family.textures["sprite.ilbm"] = IlbmImage(
        width=4, height=4, pixels=bytes([8] * 16))
    attach_synthetic_indexed_profile(family, tmp_path)
    return family


def test_particle_only_base_renders_and_seeks_repeatably(app, effect):
    viewport = AssetViewport()
    try:
        viewport.load_family(effect)
        assert viewport.has_model and viewport.has_animation
        assert not effect.root_object.skeleton.polygons
        viewport.apply_snapshot_preset("Front", QSize(96, 96))
        camera = viewport._camera_state()
        viewport.seek_animation(150)
        first = viewport.render_snapshot(QSize(96, 96), None)
        assert any(first.pixelColor(x, y).alpha()
                   for x in range(96) for y in range(96))
        first_faces = [face.vertices for face in viewport._faces]
        viewport.seek_animation(600)
        assert [face.vertices for face in viewport._faces] != first_faces
        viewport.seek_animation(150)
        assert viewport.render_snapshot(QSize(96, 96), None) == first
        assert viewport._camera_state() == camera
        assert viewport._last_effective_renderer == "retail_indexed_reconstructed"
    finally:
        viewport.close()


def test_export_animation_state_restores_particles_and_playback(app, effect):
    viewport = AssetViewport()
    try:
        viewport.load_family(effect)
        viewport.seek_animation(220)
        viewport.set_animation_speed(1.5)
        viewport.play_animation(True)
        before = viewport.capture_animation_state()
        faces = [face.vertices for face in viewport._faces]
        viewport.play_animation(False)
        viewport.seek_animation(600)
        viewport.restore_animation_state(before)
        assert viewport.capture_animation_state() == before
        assert viewport._anim_timer.isActive()
        assert [face.vertices for face in viewport._faces] == faces
    finally:
        viewport.close()


def test_unassigned_anm_has_a_textured_preview_without_mutating_family(app, effect):
    effect.animations["orphan.anm"] = VanmData(
        bitmap_names=["sprite.ilbm"],
        texcoord_groups=[[(0, 0), (255, 0), (255, 255), (0, 255)]],
        frames=[VanmFrame(100, 0, 0), VanmFrame(100, 0, 0)])
    original = effect.root_object
    preview = animation_preview_family(effect, "orphan.anm")
    assert effect.root_object is original
    assert preview.root_object is not original
    assert preview.base_asset is None
    viewport = AssetViewport()
    try:
        viewport.load_family(preview)
        viewport.apply_snapshot_preset("Front", QSize(64, 64))
        assert viewport.has_animation
        image = viewport.render_snapshot(QSize(64, 64), None)
        assert image.pixelColor(32, 32).alpha() > 0
        viewport.seek_animation(110)
        assert viewport._anim_states[0][0] == 1
    finally:
        viewport.close()


def test_both_resource_lists_use_enter_for_play_pause(app, effect):
    from snapshot_studio.window import SnapshotStudioWindow
    from studio.workbench.window import _BAS_KIND_ROLE
    window = SnapshotStudioWindow()
    try:
        window.viewport.load_family(effect)
        window._sync_animation_controls()
        for tree in (window.setbas_tree, window.entity_browser.tree):
            item = QTreeWidgetItem(["Animated effect"])
            item.setData(0, _BAS_KIND_ROLE, "base")
            tree.addTopLevelItem(item)
            tree.setCurrentItem(item)
            QTest.keyClick(tree, Qt.Key.Key_Return)
            assert window.viewport._anim_playing
            QTest.keyClick(tree, Qt.Key.Key_Return)
            assert not window.viewport._anim_playing
    finally:
        window.close()


def test_bas_menu_play_is_disabled_for_static_rows(app, effect):
    from studio.workbench.window import AssemblyWindow, _BAS_KIND_ROLE, _BAS_NAME_ROLE
    window = AssemblyWindow()
    try:
        window._setbas = SimpleNamespace(resources=[], path=Path("Set.BAS"))
        window._setbas_preview_cache = window._setbas, effect
        animated = QTreeWidgetItem(["effect.base"])
        animated.setData(0, _BAS_KIND_ROLE, "base")
        animated.setData(0, _BAS_NAME_ROLE, "effect.base")
        static = QTreeWidgetItem(["sprite.ilbm"])
        static.setData(0, _BAS_KIND_ROLE, "ilbm.class")
        with patch.object(window, "_resolve_setbas_base",
                          return_value=(effect, effect.root_object, -1)):
            assert window._setbas_item_has_animation(animated)
            assert not window._setbas_item_has_animation(static)
    finally:
        window.close()


def test_particle_simulator_is_shared_with_entity_designer():
    from entity_designer.destruction_fx import EmitterSpec as original
    from studio.rendering.particles import EmitterSpec
    assert original is EmitterSpec


def test_particle_preview_uses_engine_rgba_override(app, effect, tmp_path):
    emitter = effect.root_object.base_object.ades[0]
    emitter.particle_attributes = replace(emitter.particle_attributes, start_speed=0)
    emitter.particle_stages[0].polflags = 0x8A
    image = QImage(16, 16, QImage.Format.Format_ARGB32)
    image.fill(QColor(255, 0, 0))
    path = tmp_path / "sprite.png"
    assert image.save(str(path))
    effect.effect_override_paths["sprite.ilbm"] = path
    viewport = AssetViewport()
    try:
        viewport.load_family(effect)
        viewport.apply_snapshot_preset("Front", QSize(64, 64))
        result = viewport.render_snapshot(QSize(64, 64), None)
        color = result.pixelColor(32, 32)
        assert color.red() > 200 and color.green() < 10
        assert viewport._last_effective_renderer == "retail_indexed_with_particle_overrides"
    finally:
        viewport.close()


def test_entity_designer_browser_shares_enter_and_play_menu(app, effect):
    from entity_designer.editor import CollisionEditorWindow
    from studio.ui.browsers import GAME_OBJECT_ROLE
    window = CollisionEditorWindow()
    try:
        window.viewport.load_family(effect)
        window._sync_animation_controls()
        browser = window.game_object_browser
        item = QTreeWidgetItem(["Effect", "Weapon", "3"])
        item.setData(0, GAME_OBJECT_ROLE, SimpleNamespace(
            name="Effect", object_id=3, kind="weapon", source=Path("test.cfg")))
        browser.tree.blockSignals(True)
        browser.tree.addTopLevelItem(item)
        browser.tree.setCurrentItem(item)
        browser.tree.blockSignals(False)
        QTest.keyClick(browser.tree, Qt.Key.Key_Return)
        assert window.viewport._anim_playing
        menu = browser._context_menu(item)
        pause = next(action for action in menu.actions() if action.text() == "Pause")
        assert pause.isEnabled()
        pause.trigger()
        assert not window.viewport._anim_playing
    finally:
        window._modified = False
        window.close()


@pytest.fixture(scope="module")
def retail_family():
    path = Path(__file__).resolve().parents[3] / "UA Rising/Data/Sets/Set1/Objects/Set.BAS"
    if not path.is_file():
        pytest.skip("Local Set1 fixture is not installed")
    return load_asset_family(path)


def test_real_vp129_renders_in_all_canonical_views(app, retail_family):
    from studio.rendering.asset_viewport import VIEW_PRESET_ANGLES
    obj = next(o for o in retail_family.all_objects()
               if o.base_object.name.casefold() == "vpdbrgro")
    assert not obj.skeleton.polygons
    viewport = AssetViewport()
    try:
        viewport.load_family(retail_family, {obj.owner_path}, primary_owner=obj.owner_path)
        assert len(viewport._particle_emitters) == 10
        viewport.seek_animation(300)
        for view in VIEW_PRESET_ANGLES:
            viewport.apply_snapshot_preset(view, QSize(80, 80))
            assert not viewport.render_snapshot(QSize(80, 80), None).isNull()
            assert not viewport._last_render_error
    finally:
        viewport.close()


def test_archive_opens_first_base_and_collapses_other_groups(app, retail_family):
    from studio.workbench.window import AssemblyWindow, _BAS_KIND_ROLE
    window = AssemblyWindow()
    try:
        window.open_setbas(retail_family.base_path)
        current = window.setbas_tree.currentItem()
        assert current.data(0, _BAS_KIND_ROLE) == "base"
        assert current is current.parent().child(0)
        assert current.parent().isExpanded()
        assert all(not window.setbas_tree.topLevelItem(i).isExpanded()
                   for i in range(window.setbas_tree.topLevelItemCount())
                   if window.setbas_tree.topLevelItem(i) is not current.parent())
        assert window.viewport.has_model
    finally:
        window.close()


def _smoke_family(tmp_path, *, with_mesh):
    """A rising smoke emitter at the origin; optionally a small solid body below it."""
    attributes = ParticleAttributes(
        1, (0, -6, 0), (0, -6, 0), (0, -1, 0), (0, -1, 0), 0, 300, 1, 1000, 0, 400, 50, 1500,
        40, 40, 0)
    stage = AmeshBlock(
        class_id="area.class", polflags=0x7A,
        texture=TextureRef(kind="ilbm", name="sprite.ilbm",
                           outline_uvs=[(0, 0), (0, 255), (255, 255), (255, 0)]))
    block = AmeshBlock(class_id="particle.class", particle_attributes=attributes,
                       particle_stages=[stage])
    points = [(0.0, 0.0, 0.0)] + ([(100.0, 0.0, 0.0), (0.0, 100.0, 0.0)] if with_mesh else [])
    skeleton = SkltModel(source_name="smoke.sklt", points=points,
                         polygons=[[0, 1, 2]] if with_mesh else [])
    family = AssetFamily(root_object=FamilyObject(
        BaseObject(name="smoke.base", ades=[block]), skeleton=skeleton))
    family.textures["sprite.ilbm"] = IlbmImage(width=4, height=4, pixels=bytes([8] * 16))
    attach_synthetic_indexed_profile(family, tmp_path)
    return family


def test_smoke_does_not_move_or_resize_the_model_it_comes_from(app, tmp_path):
    """A wreck with a tall smoke column keeps its own framing, bounds and ground."""
    viewport = AssetViewport()
    try:
        viewport.load_family(_smoke_family(tmp_path, with_mesh=True))
        assert viewport._particle_emitters
        # The model alone decides the bounds and the framing center.
        assert viewport._owner_bounds["root"] == (0.0, 0.0, 0.0, 100.0, 100.0, 0.0)
        assert viewport._center[1] == pytest.approx(50.0)
        assert viewport._scale == pytest.approx(PREVIEW_SCALE)
        # The ground is the bottom of the model, whatever the smoke does.
        lowest = max(point[1] for point in viewport._framing_points())
        assert viewport._ground_y == pytest.approx(lowest) == pytest.approx(100.0)
    finally:
        viewport.close()


def test_effect_only_vp_is_framed_and_grounded_on_its_particles(app, tmp_path):
    viewport = AssetViewport()
    try:
        viewport.load_family(_smoke_family(tmp_path, with_mesh=False))
        envelope = viewport._particle_bounds_points
        assert envelope and viewport._framing_points() == list(envelope)
        viewport.frame_owner("root")
        low = min(point[1] for point in envelope)
        high = max(point[1] for point in envelope)
        assert viewport._center[1] == pytest.approx((low + high) / 2)
        # The explosion rises from the grid: no particle is ever below it.
        assert viewport._ground_y == pytest.approx(high)
        assert all(point[1] <= viewport._ground_y + 1e-9 for point in envelope)
    finally:
        viewport.close()


def test_every_preview_uses_the_same_scale_whatever_the_size(app, tmp_path):
    """A small effect and a model keep their real proportions between VPs."""
    viewport = AssetViewport()
    try:
        viewport.load_family(_smoke_family(tmp_path, with_mesh=True))
        with_model = viewport._scale
        viewport.load_family(_smoke_family(tmp_path, with_mesh=False))
        effect_only = viewport._scale
        viewport.frame_all()
        framed_all = viewport._scale
        assert with_model == effect_only == framed_all == PREVIEW_SCALE
    finally:
        viewport.close()
