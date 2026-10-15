import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('NME_RENDERER','software')
import pytest
from PySide6.QtCore import QPointF,Qt
from PySide6.QtWidgets import QApplication,QTreeWidgetItem
from entity_designer.editor import CollisionEditorWindow,_MODEL_NAME_ROLE
from tests.test_selection_runtime_v3 import _selection_family

@pytest.fixture
def window():
    app=QApplication.instance() or QApplication([])
    window=CollisionEditorWindow()
    family,_,_=_selection_family()
    window.family=family
    window._current_owner='root'
    window.viewport.load_family(family,{'root'},primary_owner='root')
    window.viewport.capture_reset_view()
    yield window
    window._modified=False
    window.close()

@pytest.mark.parametrize('target',['fire_points_tab_index','gun_points_tab_index','collision_tab_index'])
def test_leaving_fx_restores_exact_editing_camera_and_reset_view(window,target):
    viewport=window.viewport
    viewport._yaw=47.;viewport._pitch=21.;viewport._zoom=1.7
    viewport._pan=QPointF(13.,-9.)
    normal=viewport._camera_state();home=viewport._home_camera_state
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    viewport._center=(1000.,-800.,90.)
    viewport._scale=.002;viewport._zoom=3.
    window._sync_fx_preview()
    window.properties_tabs.setCurrentIndex(getattr(window,target))
    assert viewport._camera_state()==normal
    assert viewport._home_camera_state==home
    assert not viewport.fx_preview_active
    viewport.reset_view()
    assert viewport._camera_state()==home
    # Re-entering FX must capture the current normal view, not an older visit.
    viewport._zoom=2.4
    second=viewport._camera_state()
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    viewport._center=(700.,700.,700.)
    window.properties_tabs.setCurrentIndex(getattr(window,target))
    assert viewport._camera_state()==second


def test_model_selected_inside_fx_restores_new_model_framing(window):
    window.properties_tabs.setCurrentIndex(window.fx_tab_index)
    window.viewport._center=(5000.,-9000.,0.)
    window.project.model_scale_x=2.
    item=QTreeWidgetItem()
    item.setData(0,Qt.ItemDataRole.UserRole,'root')
    item.setData(0,_MODEL_NAME_ROLE,'new-model.base')
    window._show_model_item(item)
    new_home=window.viewport._home_camera_state
    window._sync_fx_preview()
    window.viewport._center=(9000.,9000.,9000.)
    window.properties_tabs.setCurrentIndex(window.gun_points_tab_index)
    assert window.viewport._camera_state()==new_home
    assert window.viewport._home_camera_state==new_home
