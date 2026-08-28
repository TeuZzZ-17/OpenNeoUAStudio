"""Pure VISPROTO model, editing, and embedded SET.BAS reconstruction.

The original game assigns visual-prototype IDs by position.  Every parsed
entry, including ``dummy.base``, therefore occupies one real zero-based slot.
This module deliberately contains no UI and performs no asset writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from base_parser import parse_base_file


DUMMY_BASE_NAME = "dummy.base"
AMBIGUOUS_VP = "?"


class VPError(Exception):
    """Base error for VISPROTO model operations."""


class VPParseError(VPError, ValueError):
    """Raised when VISPROTO text cannot be represented safely."""


class VPIndexError(VPError, IndexError):
    """Raised for an invalid zero-based VP index."""


class VPConflictError(VPError):
    """Raised when an assignment would replace a slot without consent."""

    def __init__(self, index: int, current: str, requested: str) -> None:
        super().__init__(
            f"VP {index} already contains {current!r}; "
            f"refusing to replace it with {requested!r} silently.")
        self.index = index
        self.current = current
        self.requested = requested


class VPEmbeddedError(VPError):
    """Raised when the source-backed SET.BAS VP container is unavailable."""


def _validate_base_name(name: str) -> str:
    text = str(name).strip()
    if not text:
        raise VPParseError("VISPROTO entries need a non-empty BASE name.")
    if any(char.isspace() or char in ";#>" for char in text):
        raise VPParseError(
            f"VISPROTO BASE name contains a token delimiter: {text!r}")
    if "\x00" in text:
        raise VPParseError("VISPROTO BASE names cannot contain NUL bytes.")
    return text


def normalize_base_name(name: str) -> str:
    """Normalize a BASE reference for case-insensitive relation lookups."""

    text = str(name).replace("\\", "/").replace(":", "/").strip("/")
    bare = text.rsplit("/", 1)[-1].strip()
    if bare and not bare.lower().endswith(".base"):
        bare += ".base"
    return bare.casefold()


def normalize_skeleton_name(name: str) -> str:
    """Normalize SKL/SKLT aliases to one case-insensitive bare filename."""

    text = str(name).replace("\\", "/").replace(":", "/").strip("/")
    bare = text.rsplit("/", 1)[-1].strip()
    if not bare:
        return ""
    stem, suffix = Path(bare).stem, Path(bare).suffix.casefold()
    if suffix in ("", ".skl", ".sklt"):
        return f"{stem}.sklt".casefold()
    return bare.casefold()


@dataclass(frozen=True)
class VPEntry:
    """One real zero-based VISPROTO slot."""

    index: int
    base_name: str
    comment: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool) \
                or self.index < 0:
            raise VPIndexError(f"invalid VP index: {self.index!r}")
        object.__setattr__(self, "base_name",
                           _validate_base_name(self.base_name))
        comment = str(self.comment).strip()
        if "\r" in comment or "\n" in comment:
            raise VPParseError("VISPROTO comments must stay on one line.")
        object.__setattr__(self, "comment", comment)

    @property
    def vp_id(self) -> int:
        return self.index

    @property
    def is_dummy(self) -> bool:
        return normalize_base_name(self.base_name) == DUMMY_BASE_NAME


@dataclass(frozen=True)
class VPSourceComment:
    """One full-line VISPROTO comment anchored between positional slots."""

    anchor: int
    text: str
    after_terminator: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, int) or isinstance(self.anchor, bool) \
                or self.anchor < 0:
            raise VPIndexError(f"invalid VISPROTO comment anchor: {self.anchor!r}")
        text = str(self.text)
        if "\r" in text or "\n" in text:
            raise VPParseError("VISPROTO comments must stay on one line.")
        if not text.lstrip().startswith((";", "#")):
            raise VPParseError(
                "full-line VISPROTO comments must start with ';' or '#'.")
        object.__setattr__(self, "text", text)


@dataclass(frozen=True)
class VPTable:
    """Immutable VISPROTO table with index-preserving edit operations."""

    entries: tuple[VPEntry, ...]
    source_encoding: str = "unicode"
    source_newline: str = "\n"
    had_terminator: bool = False
    source_comments: tuple[VPSourceComment, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        for expected, entry in enumerate(entries):
            if entry.index != expected:
                raise VPIndexError(
                    "VP indices must be contiguous and zero-based: "
                    f"expected {expected}, got {entry.index}.")
        object.__setattr__(self, "entries", entries)
        if not self.source_encoding:
            raise VPParseError("source encoding label cannot be empty.")
        if self.source_newline not in ("\n", "\r\n"):
            raise VPParseError("source newline must be LF or CRLF.")
        source_comments = tuple(self.source_comments)
        for comment in source_comments:
            if not isinstance(comment, VPSourceComment):
                raise VPParseError(
                    "source_comments must contain VPSourceComment values.")
            if comment.anchor > len(entries):
                raise VPIndexError(
                    "VISPROTO comment anchor exceeds the table length: "
                    f"{comment.anchor} > {len(entries)}.")
        object.__setattr__(self, "source_comments", source_comments)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def entry(self, index: int) -> VPEntry:
        if not isinstance(index, int) or isinstance(index, bool) \
                or not 0 <= index < len(self.entries):
            raise VPIndexError(f"VP index out of range: {index!r}")
        return self.entries[index]

    def assign(
            self, index: int, base_name: str, *,
            allow_replace: bool = False,
            expected_current: str | None = None) -> "VPTable":
        """Return a table with one assignment changed explicitly.

        A semantically different value never replaces an occupied slot unless
        ``allow_replace`` is true or ``expected_current`` matches the current
        value.  This gives UI callers both an explicit confirmation path and
        an optimistic-concurrency path without silent overwrites.
        """

        current = self.entry(index)
        requested = _validate_base_name(base_name)
        current_key = normalize_base_name(current.base_name)
        requested_key = normalize_base_name(requested)
        if expected_current is not None \
                and current_key != normalize_base_name(expected_current):
            raise VPConflictError(index, current.base_name, requested)
        if current_key != requested_key \
                and not allow_replace and expected_current is None:
            raise VPConflictError(index, current.base_name, requested)
        if current.base_name == requested:
            return self
        changed = list(self.entries)
        changed[index] = replace(current, base_name=requested)
        return replace(self, entries=tuple(changed))

    def swap(self, first: int, second: int) -> "VPTable":
        """Return a table with two explicit BASE assignments exchanged.

        Comments remain attached to their VP slots; only assignments move.
        """

        first_entry = self.entry(first)
        second_entry = self.entry(second)
        if first == second or first_entry.base_name == second_entry.base_name:
            return self
        changed = list(self.entries)
        changed[first] = replace(
            first_entry, base_name=second_entry.base_name)
        changed[second] = replace(
            second_entry, base_name=first_entry.base_name)
        return replace(self, entries=tuple(changed))

    def clear(self, index: int) -> "VPTable":
        """Replace one assignment with ``dummy.base`` without renumbering."""

        return self.assign(index, DUMMY_BASE_NAME, allow_replace=True)

    def append(self, base_name: str = DUMMY_BASE_NAME, *,
               comment: str = "") -> "VPTable":
        """Append exactly one new trailing slot; existing IDs stay stable."""

        changed = list(self.entries)
        old_length = len(changed)
        changed.append(VPEntry(len(changed), base_name, comment))
        # Full-line comments at the old end stay trailing, immediately before
        # the terminator, instead of unexpectedly moving above the new slot.
        source_comments = tuple(
            replace(item, anchor=old_length + 1)
            if not item.after_terminator and item.anchor == old_length
            else item
            for item in self.source_comments
        )
        return replace(
            self, entries=tuple(changed), source_comments=source_comments)

    def duplicate_assignment(
            self, source: int, destination: int | None = None, *,
            allow_replace: bool = False) -> "VPTable":
        """Copy a BASE reference to an existing or newly appended slot."""

        assignment = self.entry(source).base_name
        if destination is None:
            return self.append(assignment)
        return self.assign(
            destination, assignment, allow_replace=allow_replace)

    def remove_trailing(self, index: int | None = None) -> "VPTable":
        """Physically remove only the final slot, never shifting earlier IDs."""

        if len(self.entries) <= 1:
            raise VPConflictError(0, self.entries[0].base_name, "<remove>")
        trailing = len(self.entries) - 1
        requested = trailing if index is None else index
        self.entry(requested)
        if requested != trailing:
            current = self.entry(requested).base_name
            raise VPConflictError(
                requested, current,
                f"<remove only trailing VP {trailing}>")
        new_length = trailing
        source_comments = tuple(
            replace(item, anchor=new_length)
            if not item.after_terminator and item.anchor > new_length
            else item
            for item in self.source_comments
        )
        return replace(
            self, entries=self.entries[:-1],
            source_comments=source_comments)

    def vps_for_base(self, base_name: str) -> tuple[int, ...]:
        key = normalize_base_name(base_name)
        return tuple(
            entry.index for entry in self.entries
            if normalize_base_name(entry.base_name) == key)

    def vp_for_base(self, base_name: str) -> int | str | None:
        matches = self.vps_for_base(base_name)
        if not matches:
            return None
        return matches[0] if len(matches) == 1 else AMBIGUOUS_VP


def _split_entry_line(line: str) -> tuple[str, str]:
    token_end = len(line)
    for index, char in enumerate(line):
        if char.isspace() or char in ";#":
            token_end = index
            break
    token = _validate_base_name(line[:token_end])
    remainder = line[token_end:]
    marker_positions = [
        position for marker in ";#"
        if (position := remainder.find(marker)) >= 0
    ]
    if marker_positions:
        comment = remainder[min(marker_positions) + 1:].strip()
    else:
        # The engine consumes only the first whitespace-delimited token.
        # Retaining any trailing text as a comment avoids silently losing it
        # when the UI exports the canonical representation.
        comment = remainder.strip()
    return token, comment


def parse_visproto_text(
        text: str, *, require_terminator: bool = False,
        source_encoding: str = "unicode",
        source_newline: str | None = None) -> VPTable:
    """Parse VISPROTO text while retaining full-line and inline comments.

    ``>`` is optional for UI imports unless ``require_terminator`` is true.
    Content after a terminator is rejected, apart from comments, blanks, or a
    repeated terminator.  Inline ``;`` and ``#`` comments are retained.
    """

    if not isinstance(text, str):
        raise TypeError("parse_visproto_text expects str input.")
    entries: list[VPEntry] = []
    source_comments: list[VPSourceComment] = []
    terminated = False
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith((";", "#")):
            source_comments.append(VPSourceComment(
                len(entries), raw_line,
                after_terminator=terminated))
            continue
        if line.startswith(">"):
            terminated = True
            continue
        if terminated:
            raise VPParseError(
                f"line {line_number}: content appears after VISPROTO "
                "terminator.")
        try:
            base_name, comment = _split_entry_line(line)
        except VPParseError as exc:
            raise VPParseError(f"line {line_number}: {exc}") from exc
        entries.append(VPEntry(len(entries), base_name, comment))
    if require_terminator and not terminated:
        raise VPParseError("VISPROTO terminator '>' is required.")
    if not entries:
        raise VPParseError("VISPROTO contains no BASE entries.")
    if source_newline is None:
        source_newline = "\r\n" if "\r\n" in text else "\n"
    return VPTable(
        tuple(entries), source_encoding=source_encoding,
        source_newline=source_newline,
        had_terminator=terminated,
        source_comments=tuple(source_comments))


def decode_visproto_bytes(data: bytes) -> tuple[str, str]:
    """Decode legacy VISPROTO bytes without lossy replacement characters."""

    if not isinstance(data, bytes):
        raise TypeError("decode_visproto_bytes expects bytes input.")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32"), "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def parse_visproto_bytes(
        data: bytes, *, require_terminator: bool = False) -> VPTable:
    text, encoding = decode_visproto_bytes(data)
    return parse_visproto_text(
        text, require_terminator=require_terminator,
        source_encoding=encoding,
        source_newline="\r\n" if "\r\n" in text else "\n")


def parse_visproto(
        source: str | bytes | PathLike[str], *,
        require_terminator: bool = False) -> VPTable:
    """Parse text/bytes directly, or read a path supplied as ``PathLike``.

    Plain ``str`` is intentionally treated as content so a filename-like
    entry cannot unexpectedly trigger filesystem access.  Use ``Path`` or
    :func:`load_visproto` for files.
    """

    if isinstance(source, bytes):
        return parse_visproto_bytes(
            source, require_terminator=require_terminator)
    if isinstance(source, PathLike):
        return load_visproto(
            source, require_terminator=require_terminator)
    return parse_visproto_text(
        source, require_terminator=require_terminator)


def load_visproto(
        path: str | PathLike[str], *,
        require_terminator: bool = False) -> VPTable:
    data = Path(path).read_bytes()
    return parse_visproto_bytes(
        data, require_terminator=require_terminator)


def export_visproto(
        table: VPTable, *, include_comments: bool = True,
        newline: str | None = None) -> str:
    """Return canonical VISPROTO text, always terminated by ``>``."""

    if newline is None:
        newline = table.source_newline
    if newline not in ("\n", "\r\n"):
        raise ValueError("newline must be '\\n' or '\\r\\n'.")
    lines: list[str] = []
    comments_by_anchor: dict[int, list[str]] = {}
    after_terminator: list[str] = []
    if include_comments:
        for comment in table.source_comments:
            if comment.after_terminator:
                after_terminator.append(comment.text)
            else:
                comments_by_anchor.setdefault(
                    comment.anchor, []).append(comment.text)
    for index, entry in enumerate(table.entries):
        lines.extend(comments_by_anchor.get(index, ()))
        line = entry.base_name
        if include_comments and entry.comment:
            line += f" ; {entry.comment}"
        lines.append(line)
    lines.extend(comments_by_anchor.get(len(table.entries), ()))
    lines.append(">")
    lines.extend(after_terminator)
    return newline.join(lines) + newline


def export_visproto_bytes(
        table: VPTable, *, include_comments: bool = True,
        encoding: str | None = None, newline: str | None = None) -> bytes:
    """Encode canonical output strictly; unsupported characters are errors."""

    if encoding is None:
        encoding = table.source_encoding
        if encoding in ("unicode", "embedded"):
            encoding = "cp1252"
    return export_visproto(
        table, include_comments=include_comments,
        newline=newline).encode(encoding, errors="strict")


class VPManager:
    """Small in-memory editor with undo/redo around immutable operations."""

    def __init__(self, table: VPTable) -> None:
        self._table = table
        self._undo: list[VPTable] = []
        self._redo: list[VPTable] = []
        self._clipboard_assignment: str | None = None

    @property
    def table(self) -> VPTable:
        return self._table

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def has_clipboard_assignment(self) -> bool:
        return self._clipboard_assignment is not None

    @property
    def clipboard_assignment(self) -> str | None:
        return self._clipboard_assignment

    def _apply(self, table: VPTable) -> bool:
        if table == self._table:
            return False
        self._undo.append(self._table)
        self._table = table
        self._redo.clear()
        return True

    def assign(
            self, index: int, base_name: str, *,
            allow_replace: bool = False,
            expected_current: str | None = None) -> bool:
        return self._apply(self._table.assign(
            index, base_name, allow_replace=allow_replace,
            expected_current=expected_current))

    def swap(self, first: int, second: int) -> bool:
        return self._apply(self._table.swap(first, second))

    def clear(self, index: int) -> bool:
        return self._apply(self._table.clear(index))

    def append(self, base_name: str = DUMMY_BASE_NAME, *,
               comment: str = "") -> bool:
        return self._apply(self._table.append(base_name, comment=comment))

    def duplicate_assignment(
            self, source: int, destination: int | None = None, *,
            allow_replace: bool = False) -> bool:
        return self._apply(self._table.duplicate_assignment(
            source, destination, allow_replace=allow_replace))

    def copy_assignment(self, index: int) -> str:
        self._clipboard_assignment = self._table.entry(index).base_name
        return self._clipboard_assignment

    def paste_assignment(self, index: int, *,
                         allow_replace: bool = False) -> bool:
        if self._clipboard_assignment is None:
            raise VPConflictError(
                index, self._table.entry(index).base_name,
                "<empty VP clipboard>")
        return self._apply(self._table.assign(
            index, self._clipboard_assignment,
            allow_replace=allow_replace))

    def remove_trailing(self, index: int | None = None) -> bool:
        return self._apply(self._table.remove_trailing(index))

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._table)
        self._table = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._table)
        self._table = self._redo.pop()
        return True


@dataclass(frozen=True)
class EmbeddedVPEntry:
    """One VP object reconstructed from the first SET.BAS root child."""

    index: int
    base_name: str
    skeleton_name: str
    source_objt_offset: int = -1

    @property
    def normalized_base(self) -> str:
        return normalize_base_name(self.base_name)

    @property
    def normalized_skeleton(self) -> str:
        return normalize_skeleton_name(self.skeleton_name)


@dataclass(frozen=True)
class VPRelations:
    """Immutable BASE/skeleton relation indices."""

    base_to_vps: Mapping[str, tuple[int, ...]]
    base_to_vp: Mapping[str, int | str]
    skeleton_to_vps: Mapping[str, tuple[int, ...]]
    skeleton_to_vp: Mapping[str, int | str]

    def vps_for_base(self, name: str) -> tuple[int, ...]:
        return self.base_to_vps.get(normalize_base_name(name), ())

    def vp_for_base(self, name: str) -> int | str | None:
        return self.base_to_vp.get(normalize_base_name(name))

    def vps_for_skeleton(self, name: str) -> tuple[int, ...]:
        return self.skeleton_to_vps.get(normalize_skeleton_name(name), ())

    def vp_for_skeleton(self, name: str) -> int | str | None:
        return self.skeleton_to_vp.get(normalize_skeleton_name(name))


def _freeze_relation(
        values: dict[str, list[int]]) -> Mapping[str, tuple[int, ...]]:
    return MappingProxyType({
        key: tuple(indices) for key, indices in values.items()
    })


def _single_relation(
        values: Mapping[str, tuple[int, ...]]) -> Mapping[str, int | str]:
    return MappingProxyType({
        key: indices[0] if len(indices) == 1 else AMBIGUOUS_VP
        for key, indices in values.items()
    })


def build_vp_relations(entries: Iterable[EmbeddedVPEntry]) -> VPRelations:
    base_values: dict[str, list[int]] = {}
    skeleton_values: dict[str, list[int]] = {}
    for entry in entries:
        base_values.setdefault(entry.normalized_base, []).append(entry.index)
        if entry.normalized_skeleton:
            skeleton_values.setdefault(
                entry.normalized_skeleton, []).append(entry.index)
    base_to_vps = _freeze_relation(base_values)
    skeleton_to_vps = _freeze_relation(skeleton_values)
    return VPRelations(
        base_to_vps=base_to_vps,
        base_to_vp=_single_relation(base_to_vps),
        skeleton_to_vps=skeleton_to_vps,
        skeleton_to_vp=_single_relation(skeleton_to_vps),
    )


@dataclass(frozen=True)
class EmbeddedVPSet:
    """Source-backed positional VP reconstruction from SET.BAS."""

    source_path: Path
    container_source_offset: int
    entries: tuple[EmbeddedVPEntry, ...]

    def as_table(self) -> VPTable:
        return VPTable(tuple(
            VPEntry(entry.index, entry.base_name)
            for entry in self.entries
        ), source_encoding="embedded", had_terminator=False)

    def relations(self) -> VPRelations:
        return build_vp_relations(self.entries)


def _base_filename(object_name: str, index: int) -> str:
    name = str(object_name).strip()
    if not name:
        raise VPEmbeddedError(
            f"embedded VP {index} has no BASE object NAME.")
    if not name.lower().endswith(".base"):
        name += ".base"
    try:
        return _validate_base_name(name)
    except VPParseError as exc:
        raise VPEmbeddedError(
            f"embedded VP {index} has an invalid BASE name: {exc}") from exc


def reconstruct_embedded_vps(
        set_bas_path: str | PathLike[str]) -> EmbeddedVPSet:
    """Rebuild the engine VP order from parsed SET.BAS.

    The engine treats the first child of the SET root positionally as the
    visual-prototype container.  It does not identify that container by NAME,
    so this function intentionally uses ``root.kids[0]`` and preserves every
    child, including repeated dummy objects, in source order.
    """

    path = Path(set_bas_path)
    asset = parse_base_file(path)
    root = asset.root
    if root is None:
        raise VPEmbeddedError(f"{path} has no parsed BASE root object.")
    if not root.kids:
        raise VPEmbeddedError(
            f"{path} has no first SET child for visual prototypes.")
    container = root.kids[0]
    if not container.kids:
        raise VPEmbeddedError(
            f"{path} first SET child contains no visual prototypes.")
    entries = tuple(
        EmbeddedVPEntry(
            index=index,
            base_name=_base_filename(obj.name, index),
            skeleton_name=str(obj.skeleton_name or ""),
            source_objt_offset=int(
                getattr(obj, "source_objt_offset", -1)),
        )
        for index, obj in enumerate(container.kids)
    )
    return EmbeddedVPSet(
        source_path=path,
        container_source_offset=int(
            getattr(container, "source_objt_offset", -1)),
        entries=entries,
    )


__all__ = [
    "AMBIGUOUS_VP",
    "DUMMY_BASE_NAME",
    "EmbeddedVPEntry",
    "EmbeddedVPSet",
    "VPConflictError",
    "VPEmbeddedError",
    "VPEntry",
    "VPError",
    "VPIndexError",
    "VPManager",
    "VPParseError",
    "VPRelations",
    "VPTable",
    "build_vp_relations",
    "decode_visproto_bytes",
    "export_visproto",
    "export_visproto_bytes",
    "load_visproto",
    "normalize_base_name",
    "normalize_skeleton_name",
    "parse_visproto",
    "parse_visproto_bytes",
    "parse_visproto_text",
    "reconstruct_embedded_vps",
]
