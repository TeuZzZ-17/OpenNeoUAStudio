"""Lossless application of editor-owned LDF changes to the original source.

The canonical writer describes the before/after semantic delta only. Unchanged
source, unknown statements, comments and whitespace are never regenerated.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import re

_START = re.compile(r'^(?:begin_|new_|modify_)', re.I)
_GRIDS = {'typ_map', 'own_map', 'hgt_map', 'blg_map'}

@dataclass
class Part:
    key: str
    start: int
    end: int
    text: str
    block: bool = False


class SourceRecord(dict):
    """A normal actor dictionary with editor-only source identity outside its keys."""
    def __init__(self, values=(), origin=None):
        super().__init__(values)
        self.origin = origin


def replace_record(previous, replacement):
    return SourceRecord(replacement, getattr(previous, 'origin', None))


def record_orders(doc):
    groups = {
        'begin_squad': [s for s in doc.squads if doc.cell_is_valid(s)],
        'begin_robo': doc.host_stations_for_save([h for h in doc.host_stations if doc.cell_is_valid(h)]),
    }
    result = {key: [getattr(value, 'origin', None) for value in values] for key, values in groups.items()}
    for kind, values in (('gate',doc.gates),('item',doc.items),('gem',doc.gems)):
        result['begin_'+kind] = [i for i in range(1,getattr(doc,'visible_'+kind+'_slots')+1) if values[i]['x'] != -1]
    return result


def _code(line):
    return line.split(';', 1)[0].strip()


def _key(code):
    words = code.replace('=', ' ').split()
    if not words:
        return ''
    key = words[0].lower()
    if key in ('begin_enable', 'begin_location') or key.startswith(('new_', 'modify_')):
        key += ' ' + (' '.join(words[1:]).lower())
    return key


def parts(text):
    """Index immediate statements, with nested blocks kept opaque at this depth."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    result = []
    i = 0
    while i < len(lines):
        code = _code(lines[i])
        if not code or code.lower() in ('end', 'end_action'):
            i += 1
            continue
        key = _key(code)
        start = i
        block = bool(_START.match(code))
        if block:
            stack = ['end_action' if key == 'begin_action' else 'end']
            raw = key == 'begin_briefing_text'
            i += 1
            while i < len(lines) and stack:
                inner = lines[i].strip() if raw else _code(lines[i])
                low = inner.lower()
                if low == stack[-1]:
                    stack.pop()
                elif not raw and _START.match(inner):
                    stack.append('end_action' if low == 'begin_action' else 'end')
                i += 1
            if stack:
                raise ValueError('Cannot safely save an unterminated LDF block: ' + key)
        elif key in _GRIDS:
            i += 1
            while i < len(lines):
                inner = _code(lines[i])
                if inner and not re.match(r'^[0-9a-fA-F]+(?:\s|$)', inner):
                    break
                i += 1
        else:
            i += 1
        a, b = offsets[start], offsets[i]
        result.append(Part(key, a, b, text[a:b], block))
    return result


def _apply(text, changes):
    for start, end, replacement in sorted(changes, key=lambda p: (p[0], p[1]), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _line_value(text):
    line = text.rstrip('\r\n')
    code = line.split(';', 1)[0]
    match = re.match(r'^([ \t]*[\w]+(?:[ \t]*=[ \t]*|[ \t]+)?)(.*?)([ \t]*)$', code)
    return match, line[len(code):], text[len(line):]


def _merge_field(before, after, source):
    bm, bc, _ = _line_value(before)
    am, ac, _ = _line_value(after)
    sm, sc, newline = _line_value(source)
    if not (bm and am and sm):
        raise ValueError('Cannot safely identify changed LDF field.')
    comment = sc if bc == ac else ac
    # Keep the source's indentation, spelling, equal-sign spacing and inline comment.
    return sm[1] + am[2] + sm[3] + comment + newline


def _tokens(text):
    # Ignore comments while retaining exact character spans for each grid token.
    found = []
    offset = 0
    for line in text.splitlines(keepends=True):
        for match in re.finditer(r'\S+', line.split(';', 1)[0]):
            if match.group() != '=':
                found.append((offset + match.start(), offset + match.end(), match.group()))
        offset += len(line)
    return found


def _merge_grid(before, after, source):
    old, new, raw = _tokens(before), _tokens(after), _tokens(source)
    if len(old) != len(new) or [v[2] for v in old[1:3]] != [v[2] for v in new[1:3]]:
        # A resize replaces this grid only, not its parent begin_maps or the level.
        return after
    if len(raw) != len(old):
        raise ValueError('Cannot safely map changed grid cells to original LDF source.')
    return _apply(source, [(r[0], r[1], n[2]) for o, n, r in zip(old, new, raw) if o[2] != n[2]])


def _merge_part(before, after, source):
    if before.text == after.text:
        return source.text
    if before.key == 'begin_briefing_text':
        # Briefing is intentionally raw text, including semicolons and blank lines.
        a = after.text.splitlines(keepends=True)
        s = source.text.splitlines(keepends=True)
        return s[0] + ''.join(a[1:-1]) + s[-1]
    if before.block:
        b = before.text.splitlines(keepends=True)
        a = after.text.splitlines(keepends=True)
        s = source.text.splitlines(keepends=True)
        return s[0] + merge(''.join(b[1:-1]), ''.join(a[1:-1]), ''.join(s[1:-1])) + s[-1]
    if before.key in _GRIDS:
        return _merge_grid(before.text, after.text, source.text)
    return _merge_field(before.text, after.text, source.text)


def merge(before, after, source, orders=None):
    if before == after:
        return source
    old_parts, new_parts, raw_parts = parts(before), parts(after), parts(source)
    old, new, raw = defaultdict(list), defaultdict(list), defaultdict(list)
    for group, values in ((old, old_parts), (new, new_parts), (raw, raw_parts)):
        for value in values:
            group[value.key].append(value)
    changes = []
    for key in dict.fromkeys([p.key for p in old_parts + new_parts]):
        bs, ns, ss = old[key], new[key], raw[key]
        if [p.text for p in bs] == [p.text for p in ns]:
            continue
        # Defaults omitted from hand-authored source are inserted only when edited.
        if not ss:
            if not ns:
                continue
            insertion = ''.join(p.text for p in ns)
            position = len(source)
            reference = next((i for i, p in enumerate(new_parts) if p.key == key), len(new_parts))
            previous = next((p for p in reversed(new_parts[:reference]) if raw[p.key]), None)
            following = next((p for p in new_parts[reference+1:] if raw[p.key]), None)
            if previous is not None:
                position = raw[previous.key][-1].end
            elif following is not None:
                position = raw[following.key][0].start
            if position and source[position-1] not in '\r\n':
                insertion = '\n' + insertion
            changes.append((position, position, insertion))
            continue
        if len(bs) != len(ss):
            # A repeated scalar assignment follows the parser's last-value-wins rule.
            if len(bs) == len(ns) == 1 and not bs[0].block:
                s = ss[-1]
                changes.append((s.start, s.end, _merge_part(bs[0], ns[0], s)))
                continue
            raise ValueError('Cannot safely match original LDF statements: ' + key)
        if orders and key in orders:
            old_ids, new_ids = orders[key]
            if len(old_ids) != len(bs) or len(new_ids) != len(ns):
                raise ValueError('Cannot safely map original LDF objects: ' + key)
            if len([i for i in new_ids if i is not None]) != len(set(i for i in new_ids if i is not None)):
                raise ValueError('Duplicated source identity in LDF objects: ' + key)
            lookup = {identity:i for i,identity in enumerate(old_ids)}
            rendered = []
            for identity, value in zip(new_ids,ns):
                old_index = lookup.get(identity)
                rendered.append(_merge_part(bs[old_index],value,ss[old_index]) if old_index is not None else value.text)
            for tag,b0,b1,n0,n1 in SequenceMatcher(None,old_ids,new_ids,autojunk=False).get_opcodes():
                common = min(b1-b0,n1-n0)
                for k in range(common):
                    item = ss[b0+k]
                    if rendered[n0+k] != item.text:
                        changes.append((item.start,item.end,rendered[n0+k]))
                for k in range(b0+common,b1):
                    item = ss[k]
                    changes.append((item.start,item.end,''))
                if n0+common<n1:
                    position=ss[b1].start if b1<len(ss) else ss[-1].end
                    changes.append((position,position,''.join(rendered[n0+common:n1])))
            continue
        matcher = SequenceMatcher(None, [p.text for p in bs], [p.text for p in ns], autojunk=False)
        for tag, b0, b1, n0, n1 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            common = min(b1-b0, n1-n0)
            for k in range(common):
                s = ss[b0+k]
                changes.append((s.start, s.end, _merge_part(bs[b0+k], ns[n0+k], s)))
            for k in range(b0+common, b1):
                s = ss[k]
                changes.append((s.start, s.end, ''))
            if n0+common < n1:
                where = ss[b1].start if b1 < len(ss) else ss[-1].end
                changes.append((where, where, ''.join(p.text for p in ns[n0+common:n1])))
    return _apply(source, changes)


def patch_source(before, after, source, old_script, new_script, orders=None):
    """Merge editor statements separately from the explicitly editable Script tab."""
    from .ldf_model import split_user_script_block
    if old_script != new_script:
        newline = '\r\n' if '\r\n' in before else '\n'
        new_script = new_script.replace('\r\n', '\n').replace('\r', '\n').replace('\n', newline)
        if new_script and not new_script.endswith(('\n', '\r')):
            new_script += newline
        marker = re.search(r'(?im)^[ \t]*;[ \t]*---[ \t]*USER SCRIPT[ \t]*---[^\r\n]*(?:\r?\n)?', source)
        if marker:
            rest = source[marker.end():]
            maps = re.search(r'(?im)^[ \t]*begin_maps\b', rest)
            end = marker.end()+maps.start() if maps else len(source)
            source = source[:marker.end()] + new_script + source[end:]
        else:
            script_parts = [p for p in parts(source) if p.key.startswith(('new_', 'modify_')) or p.key == 'include']
            changes = [(p.start, p.end, '') for p in script_parts]
            where = script_parts[0].start if script_parts else len(source)
            changes.append((where, where, new_script))
            source = _apply(source, changes)
    # These generated regions are reference output only; the source retains its own script.
    _, before = split_user_script_block(before)
    _, after = split_user_script_block(after)
    result = merge(before, after, source, orders)
    # Editor bookkeeping is stored in comments and is not an LDF statement.
    pattern = r'(?m)^; Studio (?:(?:building|special) ground|item key ground)[^\r\n]*(?:\r?\n)?'
    old_comments = re.findall(pattern, before)
    new_comments = re.findall(pattern, after)
    if old_comments != new_comments:
        result = re.sub(pattern, '', result)
        if new_comments:
            if result and not result.endswith(('\n','\r')): result += '\n'
            result += ''.join(new_comments)
    return result
