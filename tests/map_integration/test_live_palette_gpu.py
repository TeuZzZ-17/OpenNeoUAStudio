"""Real GPU readback, buffer reuse and identical batch/individual framing."""
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from map_editor.core.ldf_model import LdfDocument
from map_editor.ui.main_window import MainWindow


def test_live_gpu_palette_batch_matches_individual_images_and_reuses_buffers():
    app=QApplication.instance() or QApplication([])
    win=MainWindow(LdfDocument(mw=9,mh=9));win.resize(1100,800);win.show()
    app.processEvents();win._icons.stop()
    try:
        if win.view.renderer is None or win.view._software_fallback:
            pytest.skip('Native GPU viewport unavailable')
        lib=win._lib()
        requests=[(0,None),(0x1b,None),(lib.buildings[1].sec_type,1)]
        for ratio in (1,1.5,2):
            size=(round(160*ratio),round(130*ratio))
            options=dict(yaw=12.345,pitch=37.123,revision=win._asset_epoch)
            images=win.view.render_palette_icons(lib,requests,size,**options)
            target=win.view._preview_atlas.handle()
            buffers={key:value[0] for key,value in win.view._preview_renderer._preview_buffers.items()}
            for (typ,bid), image in zip(requests,images):
                single=win.view.render_icon(lib,typ,1,building_id=bid,target_size=size,**options).image
                assert image.size()==single.size()
                aa=np.frombuffer(image.bits(),np.uint8).reshape(size[1],size[0],4)
                bb=np.frombuffer(single.bits(),np.uint8).reshape(size[1],size[0],4)
                if image!=single:
                    image.save('build/atlas-different.png');single.save('build/single-different.png')
                assert np.array_equal(aa,bb), (typ, np.count_nonzero(aa!=bb),np.count_nonzero(aa[::-1]!=bb),np.max(np.abs(aa.astype(int)-bb.astype(int))))
                rgba=image.convertToFormat(image.Format.Format_RGBA8888)
                pixels=np.frombuffer(rgba.bits(),np.uint8).reshape(size[1],size[0],4)
                yy,xx=np.where(pixels[:,:,3]>0)
                assert len(xx) and xx.min()>0 and yy.min()>0
                assert xx.max()<size[0]-1 and yy.max()<size[1]-1
            win.view.render_palette_icons(lib,requests,size,**(options|{'yaw':13.456}))
            assert win.view._preview_atlas.handle()==target
            assert all(win.view._preview_renderer._preview_buffers[key][0] is value for key,value in buffers.items())
    finally:
        win.dirty=False;win._icons.stop();win.close();win._icon_pool.waitForDone(10000);win.view._pool.waitForDone(10000)
