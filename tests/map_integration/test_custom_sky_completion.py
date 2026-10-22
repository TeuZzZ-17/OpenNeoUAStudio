import shutil
import pytest

from PySide6.QtWidgets import QApplication

from map_editor import bootstrap
from map_editor.core.custom_sky import inspect_custom_sky, install_custom_sky
from map_editor.core.game_installation import GameInstallation
from map_editor.core.ldf_model import LdfDocument, dumps_ldf, loads_ldf
from map_editor.core.resource_catalog import SkyResource, preview_image
from map_editor.ui.dialogs import LevelInfoPanel


def test_external_base_sky_validates_previews_and_survives_level_reopen(tmp_path):
    app = QApplication.instance() or QApplication([])
    data = tmp_path / 'Data'
    folder = data / 'Models' / 'Sky'
    folder.mkdir(parents=True)
    source = bootstrap.game_data_dir() / 'Objects' / 'x7.bas'
    target = folder / 'Custom.BASE'
    shutil.copyfile(source, target)
    objects = data / 'Objects'
    objects.mkdir()
    sky = inspect_custom_sky(target, data, objects)
    assert sky.valid, sky.problems
    assert sky.level_value == 'Data/Models/Sky/Custom.BASE'
    assert install_custom_sky(sky, objects) == target
    assert list(objects.iterdir()) == []
    image = preview_image(SkyResource(sky.stem, target))
    assert not image.isNull()
    doc = LdfDocument(mw=7, mh=7)
    doc.lvl_info['sky'] = sky.level_value
    doc = loads_ldf(dumps_ldf(doc))
    old = bootstrap.installation()
    try:
        bootstrap.set_installation(GameInstallation(data, {'skies': objects}))
        panel = LevelInfoPanel(doc)
        assert panel.sky_list.currentItem() is not None
        assert panel.values()['sky'] == sky.level_value
        panel.dispose()
    finally:
        bootstrap.set_installation(old)


def test_unrelated_or_incomplete_archive_cannot_be_applied(tmp_path):
    data = tmp_path / 'Data'
    data.mkdir()
    archive = data / 'Broken.bas'
    archive.write_bytes(b'FORM\x00\x00\x00\x04BASE')
    sky = inspect_custom_sky(archive, data, data / 'Objects')
    assert not sky.valid
    outside = tmp_path / 'outside.BASE'
    outside.write_bytes(archive.read_bytes())
    assert not inspect_custom_sky(outside, data, data / 'Objects').valid


def test_custom_sky_rejects_directory_selection(tmp_path):
    data = tmp_path / 'Data'
    objects = data / 'Objects'
    objects.mkdir(parents=True)
    shutil.copyfile(bootstrap.game_data_dir() / 'Objects' / 'x7.bas', objects / 'x7.bas')
    folder = data / 'Elsewhere' / 'x7'
    folder.mkdir(parents=True)
    (folder / 'NEWSKY1.png').write_bytes(b'not a texture')
    sky = inspect_custom_sky(folder, data, objects)
    assert not sky.valid
    assert 'sky file' in sky.problems[0]


def test_extracted_sky_requires_every_referenced_file(tmp_path):
    from studio.formats.images import iff_chunk, read_iff_file
    from studio.formats.setbas import read_setbas
    source = bootstrap.game_data_dir() / 'Objects' / 'x7.bas'
    archive = read_setbas(source)
    tree = read_iff_file(source)
    folder = tmp_path / 'Data' / 'Models' / 'Sky'
    folder.mkdir(parents=True)

    def without_embed(chunk):
        if chunk.tag == 'FORM' and chunk.form_type.strip() == 'EMBD':
            return b''
        if chunk.tag == 'FORM':
            return iff_chunk(b'FORM', chunk.form_type.encode('ascii') + b''.join(without_embed(child) for child in chunk.children))
        return tree.data[chunk.offset:chunk.offset + 8 + chunk.size + (chunk.size & 1)]

    base = folder / 'Sky.BASE'
    base.write_bytes(b''.join(without_embed(chunk) for chunk in tree.roots))
    for resource in archive.resources:
        path = folder / resource.resource_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive.payload_bytes(resource))
    data = tmp_path / 'Data'
    sky = inspect_custom_sky(base, data, data / 'Objects')
    assert sky.valid, sky.problems
    assert not preview_image(SkyResource('Sky', base)).isNull()
    (folder / 'NEWSKY1.ILBM').unlink()
    assert not inspect_custom_sky(base, data, data / 'Objects').valid


@pytest.mark.parametrize('filename', ['grau.bas', 'x7.bas'])
def test_sky_uses_active_set_palette_not_an_ambiguous_archive_palette(tmp_path, filename):
    from map_editor.core.resource_catalog import ResourceCatalog
    from map_editor.ui.sky_dialog import CustomSkyDialog
    from PySide6.QtWidgets import QPushButton

    app = QApplication.instance() or QApplication([])
    data = tmp_path / 'Data'
    objects = data / 'Objects'
    objects.mkdir(parents=True)
    archive = objects / filename
    shutil.copyfile(bootstrap.game_data_dir() / 'Objects' / filename, archive)
    # The sky shares the map's palette; several installed sets are normal.
    palette_source = bootstrap.set_dir(1) / 'Palette' / 'STANDARD.PAL'
    for number in (1, 2):
        palette = data / 'Sets' / f'Set{number}' / 'Palette' / 'STANDARD.PAL'
        palette.parent.mkdir(parents=True)
        shutil.copyfile(palette_source, palette)
    old = bootstrap.installation()
    try:
        installation = GameInstallation.suggest(data)
        bootstrap.set_installation(installation)
        catalog = ResourceCatalog(installation, 1)
        assert catalog.palette is not None
        dialog = CustomSkyDialog(catalog)
        dialog.inspect(archive)
        assert dialog.sky.valid, dialog.sky.problems
        assert dialog.apply_button.isEnabled()
        assert dialog.preview.pixmap() and not dialog.preview.pixmap().isNull()
        assert not any(button.text() == 'Choose folder' for button in dialog.findChildren(QPushButton))
        dialog.apply()
        assert dialog.value == f'objects/{archive.name}'
        doc = LdfDocument(mw=7, mh=7)
        doc.lvl_info['sky'] = dialog.value
        assert loads_ldf(dumps_ldf(doc)).lvl_info['sky'] == dialog.value
        dialog.close()
    finally:
        bootstrap.set_installation(old)
