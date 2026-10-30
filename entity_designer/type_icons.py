"""Icon Selector logic: TYPE_NS.FON symbols and the cells of its font page. Pure Python, no Qt.

The unit icons are drawn from the H_FINDER font page: a grid of cells, each
``width x height`` pixels plus a 1-pixel separator, starting at (1, 1).
A vehicle's ``type_icon`` is a key (symbol) that TYPE_NS.FON maps to a cell.
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
    read_font,
)

TYPE_ICON_FONT = "TYPE_NS"

# Vanilla reads one byte per symbol, in the .FON and in the script. A new key
# is therefore one byte; ASCII keys stay readable by vanilla in any script
# encoding, Latin-1 keys only through OpenNeoUA in a UTF-8 script.
# ';' '#' '=' cannot stand bare after "type_icon =", U+00AD is invisible.
_ASCII_KEYS = tuple(code for code in range(0x21, 0x7f) if chr(code) not in ";#=")
_LATIN1_KEYS = tuple(code for code in range(0xa1, 0x100) if code != 0xad)
_SYMBOL_RE = re.compile(r"U\+[0-9A-Fa-f]{1,6}")

FREE, SHARED, TAKEN, INVALID = "free", "shared", "taken", "invalid"


@dataclass(frozen=True)
class IconAtlas:
    font: FontDefinition
    image_path: Path
    image_width: int
    image_height: int

    @property
    def cell_width(self) -> int:
        """Width of a unit icon: the most common glyph width of the font."""

        widths = Counter(glyph.width for glyph in self.font.glyphs)
        return widths.most_common(1)[0][0] if widths else 20

    @property
    def cell_height(self) -> int:
        return self.font.height

    @property
    def columns(self) -> int:
        return max(0, (self.image_width - 1) // (self.cell_width + 1))

    @property
    def rows(self) -> int:
        return max(0, (self.image_height - 1) // (self.cell_height + 1))

    def origin(self, cell: tuple[int, int]) -> tuple[int, int]:
        column, row = cell
        return (1 + column * (self.cell_width + 1),
                1 + row * (self.cell_height + 1))

    def cell_of(self, origin: tuple[int, int]) -> tuple[int, int] | None:
        """Grid cell whose top-left corner is ``origin`` (None when off the grid)."""

        x, y = origin
        column, x_rest = divmod(x - 1, self.cell_width + 1)
        row, y_rest = divmod(y - 1, self.cell_height + 1)
        if x_rest or y_rest or not (0 <= column < self.columns and 0 <= row < self.rows):
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
        for pool in (_ASCII_KEYS, _LATIN1_KEYS):
            free = [code for code in pool if code not in used]
            if free:
                return (rng or random).choice(free)
        return None


@lru_cache(maxsize=4)
def _cached_atlas(fon: str, fon_mtime: int, image: str,
                  width: int, height: int) -> IconAtlas:
    return IconAtlas(read_font(fon), Path(image), width, height)


def load_atlas(data_root: str | Path | None, image_size) -> IconAtlas | None:
    """Read TYPE_NS.FON of ``data_root``; ``image_size(path)`` gives the page size."""

    if data_root is None:
        return None
    fon, image = locate_font(data_root, TYPE_ICON_FONT)
    if fon is None or image is None:
        return None
    try:
        width, height = image_size(image)
        return _cached_atlas(str(fon), fon.stat().st_mtime_ns, str(image),
                             int(width), int(height))
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
                    note: str = "") -> bool:
    """Map a new ``code`` to ``cell`` in TYPE_NS.FON. Returns True when a line was added.

    A key that already draws this cell is left alone; a key bound to another
    cell is refused, since changing it would change every unit that uses it.
    """

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
