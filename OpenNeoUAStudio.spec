# -*- mode: python ; coding: utf-8 -*-

import ast
import sys
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata

# ctypes loads CoACD before Qt in the bake worker, so keep its OpenMP
# dependency next to the CoACD DLL rather than only in Qt's package folder.
coacd_binaries = collect_dynamic_libs('coacd')
coacd_binaries += [
    (source, 'coacd')
    for source, _destination in collect_dynamic_libs('shiboken6')
    if Path(source).name.lower() == 'vcomp140.dll'
]

# --- Two-layer build cache -------------------------------------------------
# Scanning PySide6, numpy, PIL... takes ~50 s and PyInstaller redoes it as soon
# as ONE Studio file changes. So the third-party libraries get their own
# Analysis (cached: it only reruns when the libraries or the import list
# change) and the Studio code gets a second, fast one that skips them.
ROOT = Path(SPECPATH)

hidden_local = [
    'wireframe_editor.window',
    'sector_editor.window',
    'map_editor.editor',
    'map_editor.core.asset_bridge',
    'map_editor.core.game_installation',
    'map_editor.core.ldf_model',
    'map_editor.core.resource_catalog',
    'map_editor.render.gpu_viewport',
    'map_editor.render.gpu_renderer',
    'map_editor.ui.dialogs',
    'map_editor.ui.main_window',
    'entity_designer.editor',
    'entity_designer.shape',
]
hidden_third_party = ['coacd', 'OpenGL', 'OpenGL.GL', 'OpenGL.GL.shaders', 'numpy']

# Own modules = folders/files of this project, found by name.
def _module_file(parts):
    """File of an own module (package __init__ or .py), or None."""
    base = ROOT.joinpath(*parts)
    if (base / '__init__.py').is_file():
        return base / '__init__.py'
    if base.with_suffix('.py').is_file():
        return base.with_suffix('.py')
    return None


# Follow the imports of main.py and the hidden imports, one own file at a time
# (so files nobody imports, like the MCP server, add no libraries). Third-party
# imports are collected as module -> names, so "from PIL import Image" is
# resolved exactly as before.
third_party = {}
visited, todo = set(), [('main',)] + [tuple(m.split('.')) for m in hidden_local]
while todo:
    parts = todo.pop()
    # Importing a.b.c also runs a/__init__.py and a/b/__init__.py.
    for i in range(1, len(parts) + 1):
        source = _module_file(parts[:i])
        if source is None or source in visited:
            continue
        visited.add(source)
        package = source.relative_to(ROOT).with_suffix('').parts[:-1]
        try:
            tree = ast.parse(source.read_text(encoding='utf-8'))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [(tuple(a.name.split('.')), None) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[:len(package) - (node.level - 1)]
                    module = base + tuple(node.module.split('.') if node.module else ())
                else:
                    module = tuple(node.module.split('.'))
                targets = [(module, [a.name for a in node.names])]
            else:
                continue
            for module, names in targets:
                if not module or module[0] == '__future__':
                    continue
                if _module_file(module[:1]) is not None:
                    todo.append(module)
                    # "from pkg import name" may name a submodule.
                    todo.extend(module + (n,) for n in names or ())
                elif module[0] not in sys.stdlib_module_names:
                    third_party.setdefault('.'.join(module), set()).update(names or ())

third_party_tops = {m.split('.')[0] for m in third_party} | {m.split('.')[0] for m in hidden_third_party}

# Stub scanned by the library Analysis. Rewritten only when the text changes,
# so its timestamp (and the cache) stays stable otherwise.
stub = Path(workpath) / 'third_party_imports.py'
stub_text = ''.join(
    (f"from {m} import {', '.join(sorted(n))}" if n else f'import {m}') + chr(10)
    for m, n in sorted(third_party.items())
)
stub.parent.mkdir(parents=True, exist_ok=True)
if not stub.exists() or stub.read_text(encoding='utf-8') != stub_text:
    stub.write_text(stub_text, encoding='utf-8')


# The library Analysis gets its own work folder: every Analysis rewrites
# base_library.zip there, and the Studio one would invalidate the library cache.
studio_workpath = CONF['workpath']
CONF['workpath'] = str(Path(studio_workpath) / 'libs')
Path(CONF['workpath']).mkdir(parents=True, exist_ok=True)
libs = Analysis(
    [str(stub)],
    pathex=[],
    binaries=coacd_binaries,
    datas=copy_metadata('coacd'),
    hiddenimports=hidden_third_party,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

CONF['workpath'] = studio_workpath

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('.\\icons', 'icons'), ('studio/ui/studio.qss', 'studio/ui'), ('studio/themes', 'studio/themes')],
    hiddenimports=hidden_local,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # '__main__' is listed because PyInstaller appends it to this list; without it
    # the saved list never matches and the cache is thrown away every time.
    excludes=sorted(third_party_tops) + ['__main__'],
    noarchive=False,
    optimize=0,
)


def merge(*tocs):
    """Join TOC lists, keeping the first entry for each name."""
    seen, out = set(), []
    for toc in tocs:
        for entry in toc:
            if entry[0] not in seen:
                seen.add(entry[0])
                out.append(entry)
    return out


# Runtime hooks (Qt plugin paths...) must run before main.py; the library
# stub itself is never executed.
rthooks = [e for e in libs.scripts if e[0].startswith('pyi_rth_')]
scripts = merge(a.scripts[:-1], rthooks, a.scripts[-1:])
binaries = merge(a.binaries, libs.binaries)
datas = merge(a.datas, libs.datas)
pure = merge(a.pure, libs.pure)

# Windows supplies these libraries. Image tools on PATH can expose DLLs with
# the same names but incompatible exports, preventing QtCore from loading.
system_dlls = {'ucrtbase.dll', 'icuuc.dll', 'icuin.dll', 'icudt.dll'}
binaries = [entry for entry in binaries
            if entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower() not in system_dlls
            and not entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower().startswith(
                ('api-ms-win-', 'ext-ms-win-'))]
pyz = PYZ(pure)

exe = EXE(
    pyz,
    scripts,
    binaries,
    datas,
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
