"""Read prototype scripts in manifest/include order without executing them."""
from pathlib import Path
import re

from studio.exchange.portable import find_existing_path

SUFFIXES = {'.cfg', '.scr', '.ini'}


def script_texts(scripts: Path, extra: str = '', with_source=False, with_offsets=False):
    def chunk(lines, source, start):
        text = '\n'.join(lines)
        if with_source:
            return (text, source, start) if with_offsets else (text, source)
        return text

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

    def include_path(value, directory):
        value = value.strip().strip('"').replace('\\', '/')
        lower = value.casefold()
        if lower.startswith('script:'):
            return find_existing_path(scripts, value[len('script:'):])
        if lower.startswith(('data:', 'data/')):
            value = value[5:]
            if value.casefold().startswith('scripts/'):
                return find_existing_path(scripts, value[len('scripts/'):])
            return find_existing_path(scripts.parent, value)
        return find_existing_path(directory, value) or find_existing_path(scripts.parent, value)

    def expand(text, directory, stack, source):
        pending = []
        pending_start = 0
        for line_number, line in enumerate(text.splitlines()):
            code = line.split(';', 1)[0].strip()
            match = re.fullmatch(r'include\s+(.+)', code, re.I)
            if match:
                if pending:
                    yield chunk(pending, source, pending_start)
                    pending.clear()
                path = include_path(match[1], directory)
                if path is not None and path.is_file() and path not in stack:
                    yield from expand(decode(path), path.parent, stack | {path}, label(path))
            else:
                if not pending:
                    pending_start = line_number
                pending.append(line)
        if pending:
            yield chunk(pending, source, pending_start)

    entry = find_existing_path(scripts, 'Startup.cfg') or find_existing_path(scripts, 'startup.scr')
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
