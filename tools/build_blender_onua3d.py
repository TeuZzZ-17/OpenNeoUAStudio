"""Build a self-contained Blender add-on ZIP from the canonical shared sources.

Only intra-package imports are made relative; no copies of core code are kept
in the repository. The bundle includes no Qt or native UA parser/writer.
"""
import argparse
import ast
from pathlib import Path
import zipfile

MODULES = ('onua3d_package', 'onua3d_contract', 'onua3d_identity',
           'asset_package_paths', 'asset_resolver', 'verified_io')


def build_addon(target):
    root = Path(__file__).resolve().parents[1]
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    class RelativeImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module in MODULES and node.level == 0:
                node.level = 1
            return node
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ('blender_onua3d', *MODULES):
            tree = RelativeImports().visit(ast.parse((root/(name+'.py')).read_text(encoding='utf-8')))
            source = ast.unparse(tree)+'\n'
            filename = '__init__.py' if name == 'blender_onua3d' else name+'.py'
            info = zipfile.ZipInfo('openneoua_onua3d/'+filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.encode('utf-8'))
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    print(build_addon(parser.parse_args().output))
