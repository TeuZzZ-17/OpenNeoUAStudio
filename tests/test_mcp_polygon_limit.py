"""Reject native-incompatible large faces before handing a model to the engine."""
import math
from tempfile import TemporaryDirectory
import pytest
from studio_mcp import core, edit, analysis


@pytest.mark.parametrize('corners,invalid', [(12, False), (16, True)])
def test_analyzer_reports_native_face_capacity(corners, invalid):
    if not core.under_data('Sets/Set1/Objects/SET.BAS').exists():
        pytest.skip('Retail Set1 fixture is unavailable')
    with TemporaryDirectory(dir=core.work_dir(), prefix='test_face_limit_') as folder:
        result = core.new_model(folder, 'LIMIT', 'LIMTEST1')
        polygon = [[10*math.cos(i*math.tau/corners), 0, 10*math.sin(i*math.tau/corners)]
                   for i in range(corners)]
        edit.run(result['base_file'], [{'op':'add_polygons','polygons':[polygon],
                                       'material':{'block':0}}])
        report = analysis.analyze(core.resolve_source(result['base_file']))
        oversized = [p for p in report['problems'] if '12 vertices per face' in p]
        assert bool(oversized) == invalid
