"""UA font definition files (Fonts/HFonts/*.FON). Pure Python, no Qt.

A .FON file names its font page image and glyph height on the first line; every
other line maps one key to a rectangle of that page: ``key x y width ; note``.
Reading follows the engine loader (yw_LoadFont + GlyphKey::Parse).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from studio.exchange.portable import backup_existing_file, find_existing_path

# Characters that a script or .FON line cannot hold as a bare key: they are
# written as U+XXXX instead (';' starts a comment, '=' separates, '#' is numeric).
_UNSAFE_BARE_KEYS = frozenset(";#=")
_C_LONG_RE = re.compile(rb"[+-]?(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)")


def glyph_key(token: bytes, *, font_numeric: bool = True) -> int:
    """Match the engine's legacy byte, UTF-8 and explicit Unicode font keys."""

    if token.startswith(b'U+') and len(token) > 2:
        key = int(token[2:], 16)
    elif font_numeric and token.startswith(b'#') and len(token) > 1:
        digits = token[1:]
        key = int(digits, 8 if len(digits) > 1 and digits.startswith(b'0') and digits.isdigit() else 0)
    elif len(token) == 1 or token[0] < 0x80:
        key = token[0]
    else:
        key = ord(token.decode('utf-8'))
    if not 0 <= key <= 0x10ffff or 0xd800 <= key <= 0xdfff or key == 0xfffd:
        raise ValueError('Invalid glyph key')
    return key


def glyph_token(code: int) -> str:
    """Spell a key so it survives any script or .FON encoding."""

    if 0x21 <= code <= 0x7e and chr(code) not in _UNSAFE_BARE_KEYS:
        return chr(code)
    return f"U+{code:04X}"


def script_glyph_key(text: str) -> int | None:
    """Key of a type_icon value as read by the engine's script parser, or None."""

    token = text.strip()
    if not token:
        return None
    try:
        return glyph_key(token.encode('utf-8'), font_numeric=False)
    except (ValueError, UnicodeError):
        return None


@dataclass(frozen=True)
class FontGlyph:
    code: int
    x: int
    y: int
    width: int
    note: str = ""


@dataclass(frozen=True)
class FontDefinition:
    path: Path
    page: str
    height: int
    glyphs: tuple[FontGlyph, ...]

    def glyph(self, code: int) -> FontGlyph | None:
        """The rectangle drawn for ``code``; a later line wins, like the engine."""

        found = None
        for glyph in self.glyphs:
            if glyph.code == code:
                found = glyph
        return found


def _c_long(token: bytes) -> int:
    """Read the leading number like std::stol(..., 0)."""

    match = _C_LONG_RE.match(token)
    if match is None:
        raise ValueError(token)
    text = match.group(0).decode('ascii')
    digits = text.lstrip('+-')
    sign = -1 if text.startswith('-') else 1
    if digits[:2].casefold() == '0x':
        return sign * int(digits, 16)
    return sign * int(digits, 8 if len(digits) > 1 and digits[0] == '0' else 10)


def read_font(path: str | Path) -> FontDefinition:
    path = Path(path)
    lines = path.read_bytes().splitlines()
    if not lines:
        raise ValueError(f"{path.name}: empty font definition")
    header = lines[0].removeprefix(b'\xef\xbb\xbf').split(b';')[0].decode('latin-1').split()
    if len(header) < 2:
        raise ValueError(f"{path.name}: font definition file corrupt")
    page, height = header[0], int(header[1], 0)
    glyphs = []
    for line in lines[1:]:
        tokens = line.split()
        if not tokens:
            continue
        try:
            if line.startswith(b' '):
                code, numbers = ord(' '), tokens[:3]
            else:
                code, numbers = glyph_key(tokens[0]), tokens[1:4]
            if len(numbers) < 3:
                continue
            x, y, width = (_c_long(value) for value in numbers)
        except (ValueError, UnicodeError):
            continue
        note = line.split(b';', 1)[1].decode('latin-1').strip() if b';' in line[1:] else ""
        glyphs.append(FontGlyph(code, x, y, width, note))
    return FontDefinition(path, page, height, tuple(glyphs))


def locate_font(data_root: str | Path, font: str) -> tuple[Path | None, Path | None]:
    """Return the .FON file of ``font`` and its font page image (PNG or ILBM)."""

    def find(directory, *names):
        for name in names:
            found = find_existing_path(directory, name)
            if found is not None:
                return found
        return None

    data = Path(data_root)
    directory = find(data, 'Fonts')
    if directory is None and data.name.casefold() == 'data':
        # Like the engine, fall back to the game root used by the vanilla layout.
        directory = find(data.parent, 'Fonts')
    definitions = find(directory, 'HFonts') if directory else None
    path = find(definitions, font + '.FON', font + '.font') if definitions else None
    if path is None:
        return None, None
    try:
        page = read_font(path).page
    except (OSError, ValueError):
        return path, None
    stem = Path(page).stem
    return path, find(directory, stem + '.PNG', page, stem + '.ILB', stem + '.ILBM')


def append_glyph(path: str | Path, glyph: FontGlyph) -> None:
    """Add one glyph line at the end of a .FON file, keeping its line endings."""

    path = Path(path)
    data = path.read_bytes()
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    if data and not data.endswith((b"\n", b"\r")):
        data += newline
    line = f"  {glyph.x}  {glyph.y} {glyph.width}"
    if glyph.note:
        line += f" ; {glyph.note}"
    backup_existing_file(path)
    path.write_bytes(data + font_key_bytes(glyph.code)
                     + line.encode('ascii', 'replace') + newline)


def font_key_bytes(code: int) -> bytes:
    """Key as written in a .FON line.

    The vanilla loader reads only the first byte of the key, so a one-byte
    key is written as that raw byte. U+XXXX is read by OpenNeoUA only: in
    vanilla it would become the letter U and replace that glyph.
    """

    return bytes([code]) if code <= 0xff else f"U+{code:04X}".encode('ascii')
