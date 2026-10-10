"""Check that building visuals use separate normal, selected and drag states."""
from pathlib import Path

from map_editor.render.building_outline import building_outline_pixels

ROOT = Path(__file__).resolve().parents[2] / 'map_editor'


def test_gpu_building_states_keep_amber_selection_and_grey_press():
    shader = (ROOT / 'render' / 'gpu_renderer.py').read_text(encoding='utf-8')
    viewport = (ROOT / 'render' / 'gpu_viewport.py').read_text(encoding='utf-8')
    editor = (ROOT / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    assert 'states[r, c, 3] = 1' in viewport
    assert 'states[r, c, 3] = 3' in viewport
    assert 'states[r,c,3] = 2' in viewport
    assert 'self.view.preview_cells = {target}' in editor
    assert 'if(whiteOuter)' in shader
    assert 'if(yellowEdge)' in shader
    assert 'building.a==2u' in shader


def test_optional_overlay_error_cannot_trigger_software_fallback():
    code = (ROOT / 'render' / 'gpu_viewport.py').read_text(encoding='utf-8')
    assert "self.owner._overlay_warning(name, exc)" in code
    assert "self.owner._overlay_warning('interaction_overlay', exc)" in code
    assert 'self.owner._fallback(exc)' in code  # Still required for real OpenGL errors.
