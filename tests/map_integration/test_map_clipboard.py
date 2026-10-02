import importlib.util
from pathlib import Path
import unittest
import sys

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('map_clipboard', ROOT / 'map_editor/tools/map_clipboard.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
copy_grid_cells = module.copy_grid_cells
grid_paste_targets = module.grid_paste_targets


class FakeDoc:
    def __init__(self, w=8, h=8):
        self.mw, self.mh = w, h
        self.grids = {
            'type': [['00'] * w for _ in range(h)],
            'own': [[0] * w for _ in range(h)],
            'hgt': [[30] * w for _ in range(h)],
            'blg': [['00'] * w for _ in range(h)],
        }


class MapClipboardTests(unittest.TestCase):
    def test_multi_cell_copy_keeps_shape_and_values(self):
        doc = FakeDoc()
        doc.grids['own'][2][2] = 1
        doc.grids['own'][2][3] = 2
        doc.grids['own'][3][3] = 3
        clip = copy_grid_cells(doc, {(2, 2), (3, 2), (3, 3)}, 'owner', ('own',))
        targets = grid_paste_targets(doc, clip, (5, 5))
        self.assertEqual([(c, r) for c, r, _ in targets], [(5, 5), (6, 5), (6, 6)])
        self.assertEqual([values[0] for _, _, values in targets], [1, 2, 3])

    def test_interior_paste_rejects_complete_shape_at_border(self):
        doc = FakeDoc()
        clip = copy_grid_cells(doc, {(2, 2), (3, 2)}, 'sector', ('type',))
        self.assertIsNone(grid_paste_targets(doc, clip, (0, 1), interior=True))
        self.assertIsNotNone(grid_paste_targets(doc, clip, (3, 3), interior=True))


if __name__ == '__main__':
    unittest.main()
