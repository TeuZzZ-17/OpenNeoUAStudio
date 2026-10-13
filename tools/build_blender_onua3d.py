"""Build a self-contained Blender add-on ZIP from the canonical shared sources.

The add-on is ``studio/exchange/blender_addon.py`` (its ``__init__``) plus the Qt-free core
``studio/exchange/portable.py``. Only the import of that core is made relative; no copy of
core code is kept in the repository. The bundle includes no Qt or native UA parser/writer.
"""
import argparse
import ast
from pathlib import Path
import zipfile

CORE_MODULE = 'studio.exchange.portable'
# file inside the add-on -> source file of the repository
BUNDLE = (
    ('__init__.py', 'studio/exchange/blender_addon.py'),
    ('portable.py', 'studio/exchange/portable.py'),
)


class RelativeCore(ast.NodeTransformer):
    """``from studio.exchange.portable import x`` -> ``from .portable import x``."""

    def visit_ImportFrom(self, node):
        if node.module == CORE_MODULE and node.level == 0:
            node.module, node.level = 'portable', 1
        return node


def build_addon(target):
    root = Path(__file__).resolve().parents[1]
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, source_path in BUNDLE:
            tree = RelativeCore().visit(ast.parse((root / source_path).read_text(encoding='utf-8')))
            info = zipfile.ZipInfo('openneoua_onua3d/' + filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (ast.unparse(tree) + '\n').encode('utf-8'))
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(build_addon(parser.parse_args().output))
