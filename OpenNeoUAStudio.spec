# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata
from pathlib import Path

# ctypes loads CoACD before Qt in the bake worker, so keep its OpenMP
# dependency next to the CoACD DLL rather than only in Qt's package folder.
coacd_binaries = collect_dynamic_libs('coacd')
coacd_binaries += [
    (source, 'coacd')
    for source, _destination in collect_dynamic_libs('shiboken6')
    if Path(source).name.lower() == 'vcomp140.dll'
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=coacd_binaries,
    datas=[('.\\icons', 'icons')] + copy_metadata('coacd'),
    hiddenimports=[
        'wireframe_editor.window',
        'map_editor.editor',
        'map_editor.core.asset_bridge',
        'map_editor.core.game_installation',
        'map_editor.core.ldf_model',
        'map_editor.core.resource_catalog',
        'map_editor.render.gpu_viewport',
        'map_editor.render.gpu_renderer',
        'map_editor.ui.dialogs',
        'map_editor.ui.main_window',
        'collision_editor.editor',
        'collision_editor.shape',
        'coacd',
        'OpenGL',
        'OpenGL.GL',
        'OpenGL.GL.shaders',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Windows supplies these libraries. Image tools on PATH can expose DLLs with
# the same names but incompatible exports, preventing QtCore from loading.
system_dlls = {'ucrtbase.dll', 'icuuc.dll', 'icuin.dll', 'icudt.dll'}
a.binaries = [entry for entry in a.binaries
              if entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower() not in system_dlls
              and not entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower().startswith(
                  ('api-ms-win-', 'ext-ms-win-'))]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OpenNeoUAStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icons\\OpenNeoUAStudio.ico'],
)
