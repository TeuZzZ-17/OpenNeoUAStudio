"""Terrain previews must use native slurp overrides and retain height slots."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import pytest
from PySide6.QtWidgets import QApplication
from studio_mcp import core
from map_editor.core.asset_bridge import SetAssets
from map_editor.render.sector_mesh import SectorMeshLibrary


@pytest.mark.parametrize('vertical', [False, True])
def test_override_replaces_material_and_keeps_runtime_height_indices(tmp_path, vertical):
    if not core.under_data('Sets/Set1/Objects/SET.BAS').exists():
        pytest.skip('Retail Set1 fixture is unavailable')
    app = QApplication.instance() or QApplication([])
    assets = SetAssets(1).load()
    suffix = 'V' if vertical else 'H'
    with TemporaryDirectory(dir=core.work_dir(), prefix='test_slurp_') as staging:
        exported = core.export_model(core.resolve_source('set1:S03'+suffix), staging,
                                     'TEST'+suffix, 'T03'+suffix)
        objects = tmp_path/'Objects'
        shutil.copytree(staging, objects)
        scripts = tmp_path/'Scripts'
        scripts.mkdir()
        index = 36*int(vertical)+1
        (scripts/'override.lst').write_text(
            f'slurp invalid ignored.bas\nslurp 72 ignored.bas\nslurp {index} TEST{suffix}.bas ; native override\n',
            encoding='cp1252')
        assets.set_dir = tmp_path
        library = SectorMeshLibrary(assets, set_only=True)
        heights = (-200.,)*4+(-800.,)*4+(-500.,-650.)
        mesh = library.filler_mesh(0, 1, vertical, heights)
        obj = assets.base_object('TEST'+suffix+'.bas', set_only=True)
        assert obj is not None
        assert mesh.faces
        assert {library.surface_for(face).name for face, _, _ in mesh.faces} == {'BODEN5.ILBM'}
        for face, _, _ in mesh.faces:
            for vertex, point in zip(face.vertices, obj.skeleton.polygons[face.poly_id]):
                original = obj.skeleton.points[point]
                assert vertex == (original[0], heights[point], original[2])
        # Both ascending and descending slopes must keep the same native slots.
        reverse = tuple(-h for h in heights)
        reversed_mesh = library.filler_mesh(0, 1, vertical, reverse)
        for face, _, _ in reversed_mesh.faces:
            for vertex, point in zip(face.vertices, obj.skeleton.polygons[face.poly_id]):
                assert vertex[1] == reverse[point]
