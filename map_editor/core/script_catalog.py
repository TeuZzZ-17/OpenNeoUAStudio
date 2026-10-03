"""Read prototype scripts in manifest/include order without executing them."""
from pathlib import Path
import re

SUFFIXES = {'.cfg', '.scr', '.ini'}


def script_texts(scripts: Path, extra: str = '', with_source=False):
    def label(path):
        try:
            return str(path.relative_to(scripts))
        except ValueError:
            return str(path)
    def decode(path):
        try:
            return path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            return path.read_text(encoding='cp1252', errors='replace')

    def find(parent, parts):
        for part in parts:
            if not parent.is_dir():
                return None
            parent = next((p for p in parent.iterdir() if p.name.casefold() == part.casefold()), None)
            if parent is None:
                return None
        return parent

    def include_path(value, directory):
        value = value.strip().strip('"').replace('\\', '/')
        lower = value.casefold()
        if lower.startswith('data:'):
            value = value[5:]
            return find(scripts, value.split('/')[1:]) if value.casefold().startswith('scripts/') else find(scripts.parent, value.split('/'))
        if lower.startswith('data/'):
            value = value[5:]
            return find(scripts, value.split('/')[1:]) if value.casefold().startswith('scripts/') else find(scripts.parent, value.split('/'))
        path = find(directory, value.split('/'))
        return path or find(scripts.parent, value.split('/'))

    def expand(text, directory, stack, source):
        pending = []
        for line in text.splitlines():
            code = line.split(';', 1)[0].strip()
            match = re.fullmatch(r'include\s+(.+)', code, re.I)
            if match:
                if pending:
                    yield ('\n'.join(pending), source) if with_source else '\n'.join(pending)
                    pending.clear()
                path = include_path(match[1], directory)
                if path is not None and path.is_file() and path not in stack:
                    yield from expand(decode(path), path.parent, stack | {path}, label(path))
            else:
                pending.append(line)
        if pending:
            yield ('\n'.join(pending), source) if with_source else '\n'.join(pending)

    entry = find(scripts, ['Startup.cfg']) or find(scripts, ['startup.scr'])
    if entry is not None:
        yield from expand(decode(entry), entry.parent, {entry}, label(entry))
    elif scripts.is_dir():
        # Loose installations without a manifest keep script discovery.
        for path in sorted(scripts.rglob('*'), key=lambda p: (p.name.casefold() in
                           ('buildings.cfg', 'vehicles.cfg'), str(p).casefold())):
            if path.is_file() and path.suffix.casefold() in SUFFIXES:
                yield from expand(decode(path), path.parent, {path}, label(path))
    if extra:
        yield from expand(extra, scripts, set(), 'Map script')
