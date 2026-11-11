"""Effect text drafts and verified, source-scoped script replacement."""

from __future__ import annotations

from dataclasses import dataclass
import copy
from pathlib import Path
import tempfile

from entity_designer import destruction_fx as fx
from entity_designer.script_model import (
    _top_level_assignment_rows,
    find_script_blocks, read_script_file, validate_definition_source, write_script_file,
)
from studio.exchange.portable import commit_verified_files


def effect_text(item) -> str:
    return ("\n".join(item.lines)
            if isinstance(item, (fx.FxBlock, fx.FxKeyGroup, fx.FxBlank)) else item.line)


def _not_an_effect_message(text: str, context: str) -> str:
    """Say why the text is not one effect, naming its first unknown key."""
    from entity_designer import fx_visuals

    weapon = context == fx.CONTEXT_WEAPON
    kinds = ("one begin_fx block, one dest_fx line, the snd_ / pal_ / shk_ keys of one "
             "sound event, or the Model / Ground decal / Crater keys")
    key = next((tokens[0] for line in text.splitlines()
                if (tokens := fx.engine_tokens(line)) is not None), "")
    if not key:
        return f"Enter {kinds}."
    prefix = key.casefold().partition("_")[0]
    if prefix in fx.KEY_GROUP_KINDS:
        events = fx.WEAPON_SOUND_EVENTS if weapon else fx.VEHICLE_SOUND_EVENTS
        return (f"{key}: unknown sound event. Known events: {', '.join(events)}.")
    return f"{key} is not an effect. Enter {kinds}."


def parse_effect_text(text: str, context: str):
    """Validate one complete effect with the existing grammar and interpreter."""
    if not text.strip():
        raise ValueError(
            "Write the effect: a begin_fx block, a dest_fx line or the snd_ / pal_ / "
            "shk_ keys of one sound event.")
    kind = "weapon" if context == fx.CONTEXT_WEAPON else "vehicle"
    wrapped = f"new_{kind} 1\n{text}\nend\n"
    blocks = find_script_blocks(wrapped)
    if len(blocks) != 1 or not blocks[0].complete:
        raise ValueError("Complete the effect's begin_fx / end block before previewing.")
    block = blocks[0]
    lines = wrapped.splitlines()
    if block.end_line != len(lines) - 1:
        raise ValueError("Edit one effect at a time; extra closing blocks are not allowed.")
    ranges = list(fx._fx_item_ranges(lines, block.start_line, block.end_line))
    if len(ranges) != 1:
        raise ValueError(_not_an_effect_message(text, context))
    first, last, _kind = ranges[0]
    if any(fx.engine_tokens(lines[index]) is not None
           for index in range(1, len(lines) - 1) if not first <= index <= last):
        raise ValueError("Only this effect and its comments belong in this editor.")
    item = fx.extract_fx_document(wrapped, block.start_line, block.end_line).items[0]
    if isinstance(item, fx.FxBlock):
        issues = fx.interpret_block(item, context).issues
    elif isinstance(item, fx.FxKeyGroup):
        issues = fx.key_group_issues(item, context)
    else:
        issues = fx.dest_fx_issues(item)
    errors = [issue.message for issue in issues
              if issue.severity in (fx.SEVERITY_FATAL, fx.SEVERITY_IGNORED)]
    if errors:
        raise ValueError("\n".join(errors))
    return item, issues


@dataclass
class EffectSource:
    path: Path
    first: int
    last: int


class FxTextSession:
    """Keep drafts separate from files and from the last valid preview."""

    def __init__(self, document, context, origins=(), expected=None):
        self.context = context
        origins = [(Path(path), first, last) for path, first, last in origins]
        self.origins = list(origins)
        # (file, line of the definition's closing ``end``): where new effects go.
        self.end_line = (origins[-1][0], origins[-1][2]) if origins else None
        self.original_items = copy.deepcopy(document.items)
        self.originals = [effect_text(item) for item in document.items]
        self.drafts = dict(enumerate(self.originals))
        self.sources: list[EffectSource] = []
        self.snapshots: dict[Path, bytes] = {}
        self.encodings = {}
        self.source_error = ""
        source_texts = []
        try:
            for path, first, last in origins:
                path = Path(path)
                text, encoding, bom = read_script_file(path)
                identity = validate_definition_source(text, first, last, expected)
                if expected is None:
                    expected = identity
                self.snapshots[path] = path.read_bytes()
                self.encodings[path] = encoding, bom
                lines = text.splitlines()
                for start, end, _kind in fx._fx_item_ranges(lines, first, last):
                    self.sources.append(EffectSource(path, start, end))
                    source_texts.append("\n".join(lines[start:end + 1]))
            if source_texts != self.originals:
                self.sources.clear()
                self.source_error = (
                    "The loaded effects do not match their current source files. "
                    "Reload the definition before saving.")
        except (OSError, UnicodeError, IndexError, ValueError) as exc:
            self.sources.clear()
            self.source_error = str(exc)

    def can_add(self) -> bool:
        """New effects need the script file they will be written to."""

        return (self.end_line is not None and not self.source_error
                and len(self.sources) == len(self.originals))

    def add_blank(self) -> int:
        """Append an empty effect. It has no lines yet; saving writes it before ``end``."""

        if not self.can_add():
            raise ValueError(self.source_error or "No script source is attached to these effects.")
        path, end = self.end_line
        index = len(self.originals)
        self.originals.append("")
        self.drafts[index] = ""
        self.original_items.append(fx.FxBlank())
        # An empty span at the closing ``end``: saving inserts the text there.
        self.sources.append(EffectSource(path, end, end - 1))
        return index

    def is_pending(self, index) -> bool:
        """True for a new effect that was never saved to its file."""

        source = self.sources[index] if index < len(self.sources) else None
        return (source is not None and source.last < source.first
                and self.originals[index] == "")

    def discard_pending(self, index) -> None:
        """Remove an unsaved placeholder without touching the script."""
        if not self.is_pending(index):
            raise ValueError("Only a new unsaved effect can be redirected.")
        self.originals.pop(index)
        self.original_items.pop(index)
        self.sources.pop(index)
        self.drafts = {new: self.drafts[old] for new, old in enumerate(
            i for i in range(len(self.drafts)) if i != index)}

    @property
    def dirty(self) -> bool:
        return any(self.drafts[index] != text for index, text in enumerate(self.originals))

    def is_dirty(self, index) -> bool:
        return self.drafts[index] != self.originals[index]

    def set_draft(self, index, text):
        self.drafts[index] = text
        return parse_effect_text(text, self.context)

    def revert(self, index):
        self.drafts[index] = self.originals[index]
        return copy.deepcopy(self.original_items[index])

    def save(self, indexes) -> set[Path]:
        """Save only edited effects, preserving encoding and all other bytes."""
        indexes = [index for index in indexes if self.is_dirty(index)]
        if not indexes:
            return set()
        if self.source_error or len(self.sources) != len(self.originals):
            raise ValueError(self.source_error or "No script source is attached to this effect.")
        for index in indexes:
            parse_effect_text(self.drafts[index], self.context)
        paths = {self.sources[index].path for index in indexes}
        self._check_unchanged(paths)
        updated = {}
        for path in paths:
            text = self._decode(path)
            lines = text.splitlines(keepends=True)
            newline = "\r\n" if "\r\n" in text else "\n"
            for index in sorted((i for i in indexes if self.sources[i].path == path),
                                key=lambda i: (self.sources[i].first, i), reverse=True):
                source = self.sources[index]
                trailing = (newline if lines[source.last].endswith(("\n", "\r")) else "")
                replacement = newline.join(self.drafts[index].splitlines()) + trailing
                lines[source.first:source.last + 1] = [replacement]
            updated[path] = "".join(lines)
        self._write_files(updated)
        # Every later source span moves by exactly the changed line count.
        for path in paths:
            changed = {index: (self.sources[index].first, self.sources[index].last,
                               len(self.drafts[index].splitlines()))
                       for index in indexes if self.sources[index].path == path}
            self._shift_origins(path, changed.values())
            for position, source in enumerate(self.sources):
                if source.path != path:
                    continue
                # Effects before this one move it by their change in line count. New
                # effects share one empty span: the lower list position comes first.
                offset = sum(
                    count - (last - first + 1)
                    for other, (first, last, count) in changed.items()
                    if other != position and last < source.first
                    and not (first == source.first and last == source.last and other > position))
                source.first += offset
                source.last = (source.first + changed[position][2] - 1
                               if position in changed else source.last + offset)
            self.snapshots[path] = path.read_bytes()
        for index in indexes:
            self.originals[index] = self.drafts[index]
            self.original_items[index] = parse_effect_text(self.drafts[index], self.context)[0]
        self._check_spans(self.sources, self.originals)
        return paths

    def delete(self, indexes, *, keys=()) -> set[Path]:
        """Remove effects from their script files and from this session.

        Only the lines of the chosen effects leave the files. Drafts of the
        other effects stay as they are, under their new positions.
        """
        removed = sorted(set(indexes))
        if not removed and not keys:
            return set()
        if not self.can_add():
            raise ValueError(self.source_error or "No script source is attached to this effect.")
        gone = set(removed)
        paths = {self.sources[index].path for index in removed}
        if keys:
            # Remove inherited values too, so an earlier definition cannot
            # restore a deleted effect when a modify_* block is reloaded.
            paths.update(path for path, _, _ in self.origins)
        self._check_unchanged(paths)
        updated = {}
        changes = {}
        for path in paths:
            text = self._decode(path)
            spans = {(self.sources[i].first, self.sources[i].last)
                     for i in removed if self.sources[i].path == path}
            if keys:
                starts = {first for source, first, _ in self.origins if source == path}
                for block in find_script_blocks(text):
                    if block.start_line in starts:
                        spans.update((line, line) for line, key, _, _
                                     in _top_level_assignment_rows(text, block) if key in keys)
            lines = text.splitlines(keepends=True)
            for first, last in sorted(spans, reverse=True):
                del lines[first:last + 1]
            changes[path] = [(first, last, 0) for first, last in spans]
            if (patched := "".join(lines)) != text:
                updated[path] = patched
        self._write_files(updated)
        for path, spans in changes.items():
            self._shift_origins(path, spans)
        # Later effects of the same file move up by the number of removed lines.
        kept = [index for index in range(len(self.originals)) if index not in gone]
        sources = []
        for index in kept:
            source = self.sources[index]
            moved = sum(last - first + 1 for first, last, _ in changes.get(source.path, ())
                        if last < source.first)
            sources.append(EffectSource(source.path, source.first - moved, source.last - moved))
        originals = [self.originals[index] for index in kept]
        # The file is already written. The session switches to its new state only
        # after a re-read confirms every remaining effect sits where it should.
        self._check_spans(sources, originals)
        self.sources = sources
        self.originals = originals
        self.original_items = [self.original_items[index] for index in kept]
        self.drafts = {new: self.drafts[old] for new, old in enumerate(kept)}
        for path in paths:
            self.snapshots[path] = path.read_bytes()
        return set(updated)

    # -- shared file steps --------------------------------------------------------------

    def _shift_origins(self, path, changes) -> None:
        changes = list(changes)

        def shifted(line):
            return line + sum(count - (last - first + 1) for first, last, count in changes
                              if last < line)

        self.origins = [(source, shifted(first), shifted(last)) if source == path
                        else (source, first, last) for source, first, last in self.origins]
        if self.origins:
            self.end_line = (self.origins[-1][0], self.origins[-1][2])

    def _check_unchanged(self, paths) -> None:
        for path in paths:
            if path.read_bytes() != self.snapshots[path]:
                raise ValueError(
                    f"{path.name} changed outside the editor. No file was overwritten. "
                    "Keep a copy of your draft, then reload the definition.")

    def _decode(self, path: Path) -> str:
        encoding, bom = self.encodings[path]
        return self.snapshots[path].decode("utf-8-sig" if encoding == "utf-8" and bom else encoding)

    def _write_files(self, updated: dict) -> None:
        """Stage the new texts, replace the files and read them back."""
        with tempfile.TemporaryDirectory(prefix="onua_fx_") as directory:
            files = []
            expected = {}
            for number, (path, text) in enumerate(updated.items()):
                stage = Path(directory) / f"effect_{number}.cfg"
                encoding, bom = self.encodings[path]
                write_script_file(stage, text, encoding, bom)
                expected[path] = stage.read_bytes()
                files.append((stage, path))

            def verify():
                for path, data in expected.items():
                    if path.read_bytes() != data:
                        raise ValueError(f"Saved script verification failed: {path.name}")

            commit_verified_files(files, verify=verify)

    def _check_spans(self, sources, originals) -> None:
        """Cross-check: every effect must be in its file exactly as the session believes."""
        texts = {}
        for source, expected in zip(sources, originals):
            if source.path not in texts:
                text, _encoding, _bom = read_script_file(source.path)
                texts[source.path] = text.splitlines()
            found = "\n".join(texts[source.path][source.first:source.last + 1])
            if found != expected:
                raise RuntimeError(
                    f"{source.path.name} does not contain the effects the editor expects. "
                    "Reload the definition before saving again.")
