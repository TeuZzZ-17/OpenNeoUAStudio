"""Icon Selector logic: TYPE_NS.FON symbols and the cells of its font page. Pure Python, no Qt.

The unit icons are drawn from the H_FINDER font page: a grid of cells, each
``width x height`` pixels plus a 1-pixel separator, starting at (1, 1).
A vehicle's ``type_icon`` is a key (symbol) that TYPE_NS.FON maps to a cell.

OpenNeoUA adds a second page, Graphics/UnitIcons/H_FINDER_OpenNeoUA, whose keys
replace the same keys of TYPE_NS.FON. One grid addresses both pages: a cell row
at or after ``base_rows`` belongs to the OpenNeoUA page, and its glyphs are
shifted down by ``extra_shift`` pixels in the merged font.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import random
import re

from studio.formats.font_def import (
    FontDefinition,
    FontGlyph,
    append_glyph,
    glyph_token,
    locate_font,
    locate_openua_sheet,
    read_font,
)

TYPE_ICON_FONT = "TYPE_NS"

# Vanilla reads one byte per symbol, in the .FON and in the script. A new key
# is therefore one byte; ASCII keys stay readable by vanilla in any script
# encoding, Latin-1 keys only through OpenNeoUA in a UTF-8 script.
# ';' '#' '=' '\' cannot stand bare after "type_icon =" ('\' continues the line in
# vanilla), U+00AD is invisible.
_ASCII_KEYS = tuple(code for code in range(0x21, 0x7f) if chr(code) not in ";#=\\")
_LATIN1_KEYS = tuple(code for code in range(0xa1, 0x100) if code != 0xad)
# Unicode private use: keys of the OpenNeoUA page only (vanilla never reads that page).
_PRIVATE_KEYS = tuple(range(0xe000, 0xf900))
_SYMBOL_RE = re.compile(r"U\+[0-9A-Fa-f]{1,6}")

FREE, SHARED, TAKEN, INVALID = "free", "shared", "taken", "invalid"


@dataclass(frozen=True)
class IconAtlas:
    font: FontDefinition
    image_path: Path
    image_width: int
    image_height: int
    # OpenNeoUA page (None when the game has none): image, size and the .FON that stores its keys.
    extra_path: Path | None = None
    extra_width: int = 0
    extra_height: int = 0
    extra_font: Path | None = None

    @property
    def cell_width(self) -> int:
        """Width of a unit icon: the most common glyph width of the font."""

        widths = Counter(glyph.width for glyph in self.font.glyphs)
        return widths.most_common(1)[0][0] if widths else 20

    @property
    def cell_height(self) -> int:
        return self.font.height

    @property
    def base_columns(self) -> int:
        return max(0, (self.image_width - 1) // (self.cell_width + 1))

    @property
    def base_rows(self) -> int:
        """Rows of the original page; the OpenNeoUA page starts at this row."""

        return max(0, (self.image_height - 1) // (self.cell_height + 1))

    @property
    def extra_columns(self) -> int:
        if self.extra_path is None:
            return 0
        return max(0, (self.extra_width - 1) // (self.cell_width + 1))

    @property
    def extra_rows(self) -> int:
        if self.extra_path is None:
            return 0
        return max(0, (self.extra_height - 1) // (self.cell_height + 1))

    @property
    def columns(self) -> int:
        return max(self.base_columns, self.extra_columns)

    @property
    def rows(self) -> int:
        return self.base_rows + self.extra_rows

    @property
    def extra_shift(self) -> int:
        """Pixels that the OpenNeoUA page is moved down in the shared grid."""

        return self.base_rows * (self.cell_height + 1)

    def in_extra(self, cell: tuple[int, int]) -> bool:
        return self.extra_path is not None and cell[1] >= self.base_rows

    def has_cell(self, cell: tuple[int, int]) -> bool:
        """Whether the cell lies on one of the pages."""

        column, row = cell
        if column < 0 or row < 0:
            return False
        if row < self.base_rows:
            return column < self.base_columns
        return row < self.rows and column < self.extra_columns

    def origin(self, cell: tuple[int, int]) -> tuple[int, int]:
        column, row = cell
        return (1 + column * (self.cell_width + 1),
                1 + row * (self.cell_height + 1))

    def cell_of(self, origin: tuple[int, int]) -> tuple[int, int] | None:
        """Grid cell whose top-left corner is ``origin`` (None when off the grid)."""

        x, y = origin
        column, x_rest = divmod(x - 1, self.cell_width + 1)
        row, y_rest = divmod(y - 1, self.cell_height + 1)
        if x_rest or y_rest or not self.has_cell((column, row)):
            return None
        return column, row

    def owner(self, code: int) -> FontGlyph | None:
        return self.font.glyph(code)

    def keys_on(self, cell: tuple[int, int]) -> list[FontGlyph]:
        """Symbols the engine draws with this cell (each key counted once)."""

        origin = self.origin(cell)
        result = []
        for code in dict.fromkeys(glyph.code for glyph in self.font.glyphs):
            glyph = self.font.glyph(code)
            if (glyph.x, glyph.y) == origin and glyph.width == self.cell_width:
                result.append(glyph)
        return result

    def cell_of_key(self, code: int) -> tuple[int, int] | None:
        glyph = self.owner(code)
        if glyph is None or glyph.width != self.cell_width:
            return None
        return self.cell_of((glyph.x, glyph.y))

    def check(self, code: int, cell: tuple[int, int]) -> tuple[str, FontGlyph | None]:
        """FREE: new key; SHARED: the key already draws this cell; TAKEN: another cell."""

        glyph = self.owner(code)
        if glyph is None:
            return FREE, None
        if glyph.width == self.cell_width and (glyph.x, glyph.y) == self.origin(cell):
            return SHARED, glyph
        return TAKEN, glyph

    def random_free_key(self, rng: random.Random | None = None) -> int | None:
        """A free key, vanilla-readable ASCII while any is left."""

        used = {glyph.code for glyph in self.font.glyphs}
        pools = (_ASCII_KEYS, _LATIN1_KEYS) + ((_PRIVATE_KEYS,) if self.extra_path else ())
        for pool in pools:
            free = [code for code in pool if code not in used]
            if free:
                return (rng or random).choice(free)
        return None


@lru_cache(maxsize=4)
def _cached_atlas(fon: str, fon_mtime: int, image: str, width: int, height: int,
                  extra_fon: str, extra_mtime: int, extra_image: str,
                  extra_width: int, extra_height: int) -> IconAtlas:
    font = read_font(fon)
    base = IconAtlas(font, Path(image), width, height)
    if not extra_fon:
        return base
    # The OpenNeoUA keys come last, so they win over TYPE_NS.FON like in the engine.
    extra = read_font(extra_fon)
    shift = base.extra_shift
    merged = FontDefinition(font.path, font.page, font.height, font.glyphs + tuple(
        FontGlyph(glyph.code, glyph.x, glyph.y + shift, glyph.width, glyph.note)
        for glyph in extra.glyphs))
    return IconAtlas(merged, Path(image), width, height,
                     Path(extra_image), extra_width, extra_height, Path(extra_fon))


def load_atlas(data_root: str | Path | None, image_size) -> IconAtlas | None:
    """Read TYPE_NS.FON (and the OpenNeoUA page) of ``data_root``.

    ``image_size(path)`` gives the size of a page image.
    """

    if data_root is None:
        return None
    fon, image = locate_font(data_root, TYPE_ICON_FONT)
    if fon is None or image is None:
        return None
    extra_fon, extra_image = locate_openua_sheet(data_root)
    try:
        width, height = image_size(image)
        extra = ("", 0, "", 0, 0)
        if extra_fon is not None and extra_image is not None:
            extra_width, extra_height = image_size(extra_image)
            extra = (str(extra_fon), extra_fon.stat().st_mtime_ns, str(extra_image),
                     int(extra_width), int(extra_height))
        return _cached_atlas(str(fon), fon.stat().st_mtime_ns, str(image),
                             int(width), int(height), *extra)
    except (OSError, ValueError):
        return None


def vanilla_readable(code: int) -> bool:
    """True when vanilla reads this type_icon from any script (plain ASCII)."""

    return code in _ASCII_KEYS


def new_key_allowed(code: int) -> bool:
    """Whether a key TYPE_NS.FON does not know yet may be added (one byte)."""

    return code in _ASCII_KEYS or code in _LATIN1_KEYS


def parse_symbol(text: str) -> int | None:
    """Key typed in the Symbol field: one visible character or U+XXXX."""

    text = text.strip()
    if _SYMBOL_RE.fullmatch(text):
        code = int(text[2:], 16)
    elif len(text) == 1 and not text.isspace():
        code = ord(text)
    else:
        return None
    if code < 0x21 or code > 0x10ffff or 0xd800 <= code <= 0xdfff or code == 0xfffd:
        return None
    return code


def symbol_text(code: int) -> str:
    """How a key is shown in the Symbol field."""

    character = chr(code)
    if code >= 0x21 and character.isprintable() and code != 0xad:
        return character
    return f"U+{code:04X}"


def ensure_font_key(data_root: str | Path, code: int, cell: tuple[int, int],
                    note: str = "", image_size=None) -> bool:
    """Map a new ``code`` to ``cell``. Returns True when a line was added.

    A key that already draws this cell is left alone; a key bound to another
    cell is refused, since changing it would change every unit that uses it.
    With the OpenNeoUA page present (``image_size`` given), new keys go only to
    its cells, in H_FINDER_OpenNeoUA.FON; TYPE_NS.FON and the original page stay untouched.
    """

    atlas = load_atlas(data_root, image_size) if image_size is not None else None
    if atlas is not None and atlas.extra_path is not None:
        status, glyph = atlas.check(code, cell)
        if status == SHARED:
            return False
        if status == TAKEN:
            raise ValueError(
                f"type_icon {glyph_token(code)} is already used by "
                f"{glyph.note or 'another icon'}.")
        if not atlas.in_extra(cell):
            raise ValueError(
                "New icons go to the OpenNeoUA page; the original H_FINDER page "
                "is left untouched.")
        x, y = atlas.origin(cell)
        clean = re.sub(r"[^\x20-\x7e]+", "", note).strip()
        append_glyph(atlas.extra_font,
                     FontGlyph(code, x, y - atlas.extra_shift, atlas.cell_width, clean),
                     raw_key=False)
        return True

    fon, image = locate_font(data_root, TYPE_ICON_FONT)
    if fon is None:
        raise ValueError(f"{TYPE_ICON_FONT}.FON was not found in {data_root}.")
    font = read_font(fon)
    probe = IconAtlas(font, image or fon, 1 << 16, 1 << 16)
    status, glyph = probe.check(code, cell)
    if status == SHARED:
        return False
    if status == TAKEN:
        raise ValueError(
            f"type_icon {glyph_token(code)} is already used by "
            f"{glyph.note or 'another icon'}.")
    if not new_key_allowed(code):
        raise ValueError(
            f"type_icon {glyph_token(code)} is not a one-byte symbol: the "
            "vanilla game could not read it from TYPE_NS.FON.")
    x, y = probe.origin(cell)
    clean = re.sub(r"[^\x20-\x7e]+", "", note).strip()
    append_glyph(fon, FontGlyph(code, x, y, probe.cell_width, clean))
    return True
