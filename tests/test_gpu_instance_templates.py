"""A sector template rebuilt under the same key (animation frame) must reach the GPU."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

from map_editor.render import gpu_renderer


class FakeBuffer:
    def __init__(self):
        self.uploads = []

    def upload(self, data):
        self.uploads.append(data)

    def delete(self):
        pass


def _renderer():
    renderer = object.__new__(gpu_renderer.GpuRenderer)
    renderer.buffers, renderer.instance_buffers, renderer.instance_templates = {}, {}, {}
    renderer.flat_buffer, renderer.flat_offsets = FakeBuffer(), {}
    renderer.material_count, renderer.atlas_size = 0, (0, 0, 0)
    renderer.scene = None
    return renderer


def _scene(template, key):
    return SimpleNamespace(
        materials=[], chunks={}, changed_templates={key},
        instances={key: (template, np.zeros((1, 4), np.float32))})


def test_rebuilt_template_is_uploaded_again(monkeypatch):
    monkeypatch.setattr(gpu_renderer, "gl", MagicMock())
    monkeypatch.setattr(gpu_renderer, "Buffer", FakeBuffer)
    key = ("sector", 5, 0)
    first = SimpleNamespace(opaque=np.zeros((3, 16), np.float32))
    second = SimpleNamespace(opaque=np.ones((3, 16), np.float32))
    renderer = _renderer()
    renderer.scene = scene = _scene(first, key)
    renderer.set_scene(scene, set())
    buffer = renderer.instance_buffers[key][0]
    assert len(buffer.uploads) == 1
    scene.instances[key] = (second, np.zeros((1, 4), np.float32))     # next animation frame
    renderer.set_scene(scene, set())
    assert len(buffer.uploads) == 2 and buffer.uploads[1] is second.opaque
    renderer.set_scene(scene, set())                                  # same template: nothing to resend
    assert len(buffer.uploads) == 2
