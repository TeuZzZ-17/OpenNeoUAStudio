"""Native terrain AREA UVs must persist through the MCP writer."""
from pathlib import Path
from tempfile import TemporaryDirectory
import pytest
from studio_mcp import core, edit


@pytest.mark.parametrize('polygon,uvs',[(0,[[128,0],[255,0],[128,127]]),
                                     (1,[[0,0],[0,127],[127,127],[127,0]])])
def test_static_area_uvs_are_written_and_reread(polygon,uvs):
    with TemporaryDirectory(dir=core.work_dir(),prefix='area_uv_') as folder:
        out=core.export_model(core.resolve_source('set1:S32H'),folder,'UV_STATIC','UVSH')
        path=out['base_file']
        before=core.geometry(core.resolve_source(path,'set1'),with_uvs=True)
        result=edit.run(path,[{'op':'set_uvs','select':[polygon],'mode':'explicit',
                              'uvs':{str(polygon):uvs}}],set_hint='set1')
        after=core.geometry(core.resolve_source(path,'set1'),with_uvs=True)
        assert result['results'][0]['updated']==1
        assert after['polygons'][polygon]['uv']==uvs
        assert after['points']==before['points']
        assert [p['points'] for p in after['polygons']]==[p['points'] for p in before['polygons']]
        assert all(p['uv']==before['polygons'][p['id']]['uv'] for p in after['polygons'] if p['id']!=polygon)


def test_animated_area_keeps_its_animation_uvs():
    source=core.resolve_source('set1:VP_MFLSH')
    family=core.load_family(source)
    stem=Path(family.root_object.base_object.skeleton_name).stem
    with TemporaryDirectory(dir=core.work_dir(),prefix='animated_uv_') as folder:
        out=core.export_model(source,folder,'UV_ANIM',('UVANIM00')[:len(stem)])
        path=out['base_file'];before=Path(path).read_bytes()
        obj=core.load_family(core.resolve_source(path,'set1')).root_object
        block=next(b for b in obj.base_object.ades if b.class_id=='area.class')
        result=edit.run(path,[{'op':'set_uvs','select':[block.ade_poly_id],
                              'mode':'rect','rect':[0,0,127,127]}],set_hint='set1')
        assert result['results'][0]['updated']==0
        assert Path(path).read_bytes()==before
