#!/usr/bin/env python3
"""Export BASE/KIDS scene metadata from a local Urban Assault/OpenNeoUA SET.BAS.

This tool is read-only for SET.BAS. It exports structural metadata and
references, not decoded asset payloads.

Ported verbatim from the BASet project (same author, GPL v3) as part of the
BASet/OpenNeoUA Studio capability merge; used by setbas_export.py for the optional
"raw BASE/KIDS chunks" developer dump and the JSON scene metadata export.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


ASCII_TAG = re.compile(rb"^[\x20-\x7e]{4}$")
TEXT_TAGS = {"CLID", "NAME", "NAM2"}
STRUCTURAL_FORM_TYPES = {
    "OBJT", "ROOT", "ADE ", "AREA", "BANI", "CIBO", "AMSH",
    "BASE", "SKLC", "ADES", "PTCL", "KIDS",
}
STRUCTURAL_LEAF_TAGS = {"STRC", "ATTS", "OLPL", "OTL2", "POL2", "POO2", "SEN2"}
RAW_LEAF_TAGS = {"CLID", "NAME", "NAM2", "STRC", "ATTS", "OLPL", "OTL2"}
ASSET_REF_RE = re.compile(r"\.(ilbm|ilb|sklt|skl|anm|vanm)$", re.IGNORECASE)


class ExportError(Exception):
    pass


@dataclass
class Chunk:
    tag: str
    offset: int
    size: int
    data_start: int
    data_end: int
    padded_end: int
    depth: int
    path: str
    form_type: Optional[str] = None


@dataclass
class ObjectNode:
    node_id: str
    parent_id: Optional[str]
    chunk_path: str
    offset: int
    size: int
    form_type: str
    class_id: str = ""
    name: str = ""
    nam2: str = ""
    names: List[str] = field(default_factory=list)
    nam2_values: List[str] = field(default_factory=list)
    child_ids: List[str] = field(default_factory=list)
    structural_chunks: List[Dict[str, object]] = field(default_factory=list)


def read_u32be(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def decode_tag(raw: bytes) -> str:
    if len(raw) != 4:
        return "????"
    if ASCII_TAG.match(raw):
        return raw.decode("ascii")
    return "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)


def hex_preview(data: bytes, start: int, end: int, limit: int = 16) -> str:
    return " ".join(f"{b:02X}" for b in data[start:min(end, start + limit)])


def chunk_bytes(data: bytes, chunk: Chunk) -> bytes:
    return data[chunk.offset:chunk.data_end]


def read_c_string(data: bytes, start: int, end: int, limit: int = 256) -> str:
    capped_end = min(end, start + limit)
    nul = data.find(b"\0", start, capped_end)
    if nul >= 0:
        capped_end = nul
    raw = data[start:capped_end]
    return raw.decode("latin-1", errors="replace").strip("\0")


def parse_chunk_at(data: bytes, offset: int, container_end: int, depth: int, path: str) -> Chunk:
    if offset + 8 > container_end:
        raise ExportError(f"truncated chunk header at 0x{offset:X}")

    tag = decode_tag(data[offset:offset + 4])
    size = read_u32be(data, offset + 4)
    data_start = offset + 8
    data_end = data_start + size
    padded_end = data_end + (size & 1)
    if data_end > container_end:
        raise ExportError(f"chunk {tag} at 0x{offset:X} extends past container end")
    if padded_end > len(data):
        raise ExportError(f"chunk {tag} at 0x{offset:X} padding extends past file end")

    form_type = None
    if tag == "FORM":
        if size < 4:
            raise ExportError(f"FORM at 0x{offset:X} is too small")
        form_type = decode_tag(data[data_start:data_start + 4])

    return Chunk(tag, offset, size, data_start, data_end, padded_end, depth, path, form_type)


def iter_chunks(data: bytes, start: int, end: int, depth: int, path: str) -> Iterable[Chunk]:
    offset = start
    index = 0
    while offset < end:
        if offset + 8 > end:
            raise ExportError(f"trailing bytes before container end at 0x{offset:X}")
        chunk = parse_chunk_at(data, offset, end, depth, f"{path}/{index}")
        yield chunk
        offset = chunk.padded_end
        index += 1


def load_setbas(path: Path) -> Tuple[bytes, Chunk]:
    data = path.read_bytes()
    if len(data) < 12:
        raise ExportError("file is too small to be an IFF FORM")
    root = parse_chunk_at(data, 0, len(data), 0, "root")
    if root.tag != "FORM":
        raise ExportError("SET.BAS root is not an IFF FORM")
    return data, root


def read_emrs_names(data: bytes, root: Chunk) -> Dict[str, List[str]]:
    names_by_class: Dict[str, List[str]] = defaultdict(list)

    def walk(start: int, end: int, depth: int, path: str) -> None:
        for chunk in iter_chunks(data, start, end, depth, path):
            if chunk.tag == "EMRS":
                class_name = read_c_string(data, chunk.data_start, chunk.data_end, 256)
                pos = chunk.data_start + len(class_name.encode("latin-1", errors="replace")) + 1
                resource = read_c_string(data, pos, chunk.data_end, 256) if pos < chunk.data_end else ""
                if class_name and resource:
                    names_by_class[class_name].append(resource)
            if chunk.tag == "FORM":
                walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path)

    walk(root.data_start + 4, root.data_end, 1, "root")
    return dict(names_by_class)


def classify_reference(text: str) -> Optional[str]:
    lowered = text.lower()
    if lowered.endswith((".ilbm", ".ilb")):
        return "texture"
    if lowered.endswith((".sklt", ".skl")) or "skeleton/" in lowered:
        return "skeleton"
    if lowered.endswith((".anm", ".vanm")):
        return "animation"
    return None


def asset_like(text: str) -> bool:
    return bool(ASSET_REF_RE.search(text)) or "Skeleton/" in text or "skeleton/" in text


def write_binary(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


class BaseKidsExporter:
    def __init__(self, data: bytes, root: Chunk) -> None:
        self.data = data
        self.root = root
        self.nodes: List[ObjectNode] = []
        self.kids_sections: List[Dict[str, object]] = []
        self.class_counts: Counter = Counter()
        self.name_counts: Counter = Counter()
        self.nam2_counts: Counter = Counter()
        self.texture_refs: Counter = Counter()
        self.skeleton_refs: Counter = Counter()
        self.animation_refs: Counter = Counter()
        self.references_by_node: Dict[str, Dict[str, List[str]]] = {}
        self.node_counter = 0

    def next_node_id(self) -> str:
        self.node_counter += 1
        return f"node_{self.node_counter:05d}"

    def export(self) -> Dict[str, object]:
        self.walk(self.root.data_start + 4, self.root.data_end, 1, "root", None, None)
        return {
            "root_form_type": self.root.form_type or "",
            "kids_sections": self.kids_sections,
            "nodes": [self.node_to_dict(node) for node in self.nodes],
        }

    def walk(
        self,
        start: int,
        end: int,
        depth: int,
        path: str,
        current_node: Optional[ObjectNode],
        parent_node_id: Optional[str],
    ) -> None:
        for chunk in iter_chunks(self.data, start, end, depth, path):
            if chunk.tag == "FORM" and chunk.form_type == "KIDS":
                self.kids_sections.append({
                    "offset": chunk.offset,
                    "size": chunk.size,
                    "path": chunk.path,
                    "parent_node_id": current_node.node_id if current_node else None,
                })
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_node, current_node.node_id if current_node else parent_node_id)
                continue

            if chunk.tag == "FORM" and chunk.form_type == "OBJT":
                node = ObjectNode(
                    node_id=self.next_node_id(),
                    parent_id=parent_node_id,
                    chunk_path=chunk.path,
                    offset=chunk.offset,
                    size=chunk.size,
                    form_type=chunk.form_type or "",
                )
                self.nodes.append(node)
                if parent_node_id:
                    parent = self.find_node(parent_node_id)
                    if parent is not None:
                        parent.child_ids.append(node.node_id)
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, node, node.node_id)
                self.finalize_node(node)
                continue

            if current_node is not None:
                self.capture_chunk(current_node, chunk)

            if chunk.tag == "FORM":
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_node, parent_node_id)

    def find_node(self, node_id: str) -> Optional[ObjectNode]:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def capture_chunk(self, node: ObjectNode, chunk: Chunk) -> None:
        if chunk.tag in TEXT_TAGS:
            text = read_c_string(self.data, chunk.data_start, chunk.data_end)
            if chunk.tag == "CLID":
                node.class_id = node.class_id or text
                self.class_counts[text] += 1
            elif chunk.tag == "NAME":
                node.names.append(text)
                self.name_counts[text] += 1
            elif chunk.tag == "NAM2":
                node.nam2_values.append(text)
                self.nam2_counts[text] += 1
            self.capture_reference(node, text)
            return

        if chunk.tag == "FORM":
            label = f"FORM {chunk.form_type}" if chunk.form_type else "FORM"
            if chunk.form_type in STRUCTURAL_FORM_TYPES:
                node.structural_chunks.append(self.chunk_info(chunk, label))
            return

        if chunk.tag in STRUCTURAL_LEAF_TAGS or chunk.size:
            node.structural_chunks.append(self.chunk_info(chunk, chunk.tag))

    def capture_reference(self, node: ObjectNode, text: str) -> None:
        ref_type = classify_reference(text)
        if ref_type is None:
            return

        by_node = self.references_by_node.setdefault(node.node_id, {"texture": [], "skeleton": [], "animation": []})
        if text not in by_node[ref_type]:
            by_node[ref_type].append(text)

        if ref_type == "texture":
            self.texture_refs[text] += 1
        elif ref_type == "skeleton":
            self.skeleton_refs[text] += 1
        elif ref_type == "animation":
            self.animation_refs[text] += 1

    def chunk_info(self, chunk: Chunk, label: str) -> Dict[str, object]:
        return {
            "label": label,
            "tag": chunk.tag,
            "form_type": chunk.form_type or "",
            "offset": chunk.offset,
            "size": chunk.size,
            "parent_path": chunk.path.rsplit("/", 1)[0] if "/" in chunk.path else "",
            "hex_preview": hex_preview(self.data, chunk.data_start, chunk.data_end),
        }

    def finalize_node(self, node: ObjectNode) -> None:
        if node.names:
            node.name = node.names[0]
        if node.nam2_values:
            node.nam2 = node.nam2_values[0]

    def node_to_dict(self, node: ObjectNode) -> Dict[str, object]:
        return {
            "id": node.node_id,
            "parent_id": node.parent_id,
            "chunk_path": node.chunk_path,
            "offset": node.offset,
            "size": node.size,
            "form_type": node.form_type,
            "class_id": node.class_id,
            "NAME": node.name,
            "NAM2": node.nam2,
            "names": node.names,
            "nam2_values": node.nam2_values,
            "child_ids": node.child_ids,
            "structural_chunks": node.structural_chunks,
        }

    def references(self) -> Dict[str, object]:
        return {
            "textures": dict(sorted(self.texture_refs.items())),
            "skeletons": dict(sorted(self.skeleton_refs.items())),
            "animations": dict(sorted(self.animation_refs.items())),
            "class_counts": dict(sorted(self.class_counts.items())),
            "name_counts": dict(sorted(self.name_counts.items())),
            "nam2_counts": dict(sorted(self.nam2_counts.items())),
            "references_by_node_id": self.references_by_node,
        }


class RawBaseKidsExporter:
    def __init__(
        self,
        data: bytes,
        root: Chunk,
        out_dir: Path,
        export_leaf_chunks: bool = True,
        write_manifest: bool = True,
    ) -> None:
        self.data = data
        self.root = root
        self.out_dir = out_dir
        self.export_leaf_chunks = export_leaf_chunks
        self.write_manifest = write_manifest
        self.entries: List[Dict[str, object]] = []
        self.kids_count = 0
        self.objt_count = 0
        self.node_count = 0
        self.leaf_counts: Counter = Counter()
        self.text_leaf_count = 0
        self.binary_leaf_count = 0
        self.warnings: List[str] = []

    def export(self) -> Dict[str, object]:
        self.prepare_output_dir()
        self.walk(self.root.data_start + 4, self.root.data_end, 1, "root", None, None, False)

        manifest = {
            "description": "Raw BASE/KIDS structural data extracted from SET.BAS",
            "leaf_chunk_files_enabled": self.export_leaf_chunks,
            "kids_forms_exported": self.kids_count,
            "objt_forms_exported": self.objt_count,
            "leaf_chunks_exported": dict(sorted(self.leaf_counts.items())),
            "text_leaf_chunks_exported": self.text_leaf_count,
            "binary_leaf_chunks_exported": self.binary_leaf_count,
            "warnings": self.warnings,
            "entries": self.entries,
        }
        manifest_path = self.out_dir / "base_kids_raw_manifest.json"
        if self.write_manifest:
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return {
            "raw_manifest_path": manifest_path if self.write_manifest else None,
            "kids_forms_exported": self.kids_count,
            "objt_forms_exported": self.objt_count,
            "leaf_chunks_exported": dict(sorted(self.leaf_counts.items())),
            "text_leaf_chunks_exported": self.text_leaf_count,
            "binary_leaf_chunks_exported": self.binary_leaf_count,
            "warning_count": len(self.warnings),
            "leaf_chunk_files_enabled": self.export_leaf_chunks,
        }

    def prepare_output_dir(self) -> None:
        """Clear BASet-generated BASE_KIDS raw output before writing a fresh export."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.out_dir / "base_kids_raw_manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()
        chunks_dir = self.out_dir / "chunks"
        if chunks_dir.exists():
            shutil.rmtree(chunks_dir)
        for child in self.out_dir.glob("kids_*"):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def walk(
        self,
        start: int,
        end: int,
        depth: int,
        path: str,
        current_kids_dir: Optional[Path],
        current_node_id: Optional[str],
        parent_is_kids: bool,
    ) -> None:
        for chunk in iter_chunks(self.data, start, end, depth, path):
            if chunk.tag == "FORM" and chunk.form_type == "KIDS":
                kids_index = self.kids_count
                kids_dir = self.out_dir / f"kids_{kids_index:04d}"
                out_file = kids_dir / f"kids_{kids_index:04d}_0x{chunk.offset:08X}.form"
                write_binary(out_file, chunk_bytes(self.data, chunk))
                self.kids_count += 1
                self.add_entry("kids_form", chunk, out_file, None, "")
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, kids_dir, current_node_id, True)
                continue

            next_node_id = current_node_id
            if chunk.tag == "FORM" and chunk.form_type == "OBJT":
                self.node_count += 1
                next_node_id = f"node_{self.node_count:05d}"
                if parent_is_kids and current_kids_dir is not None:
                    out_file = current_kids_dir / f"objt_{self.objt_count:06d}_0x{chunk.offset:08X}.form"
                    write_binary(out_file, chunk_bytes(self.data, chunk))
                    self.objt_count += 1
                    self.add_entry("objt_form", chunk, out_file, next_node_id, "")

            if self.export_leaf_chunks and current_kids_dir is not None and chunk.tag in RAW_LEAF_TAGS:
                self.export_leaf(chunk, next_node_id)

            if chunk.tag == "FORM":
                self.walk(chunk.data_start + 4, chunk.data_end, depth + 1, chunk.path, current_kids_dir, next_node_id, False)

    def export_leaf(self, chunk: Chunk, node_id: Optional[str]) -> None:
        tag_dir = self.out_dir / "chunks" / chunk.tag
        index = self.leaf_counts[chunk.tag]
        stem = f"{chunk.tag}_{index:06d}_0x{chunk.offset:08X}"
        bin_file = tag_dir / f"{stem}.bin"
        write_binary(bin_file, chunk_bytes(self.data, chunk))
        self.leaf_counts[chunk.tag] += 1
        self.binary_leaf_count += 1

        text_value = ""
        text_file: Optional[Path] = None
        if chunk.tag in TEXT_TAGS:
            text_value = read_c_string(self.data, chunk.data_start, chunk.data_end)
            text_file = tag_dir / f"{stem}.txt"
            write_text(text_file, text_value)
            self.text_leaf_count += 1

        self.add_entry("leaf_chunk", chunk, bin_file, node_id, text_value)
        if text_file is not None:
            self.entries[-1]["text_output_file"] = self.relative_output(text_file)

    def add_entry(self, kind: str, chunk: Chunk, out_file: Path, node_id: Optional[str], text_value: str) -> None:
        self.entries.append({
            "kind": kind,
            "tag": chunk.tag,
            "form_type": chunk.form_type or "",
            "offset": chunk.offset,
            "size": chunk.size,
            "parent_path": chunk.path.rsplit("/", 1)[0] if "/" in chunk.path else "",
            "chunk_path": chunk.path,
            "node_id": node_id or "",
            "text_value": text_value,
            "output_file": self.relative_output(out_file),
        })

    def relative_output(self, path: Path) -> str:
        return str(path.relative_to(self.out_dir)).replace("\\", "/")


def build_unresolved_lines(references: Dict[str, object], emrs_names_by_class: Dict[str, List[str]]) -> List[str]:
    emrs_full = set()
    emrs_base = set()
    for names in emrs_names_by_class.values():
        for name in names:
            normalized = name.replace("\\", "/").lower()
            emrs_full.add(normalized)
            emrs_base.add(normalized.rsplit("/", 1)[-1])

    lines: List[str] = []
    for group in ("textures", "skeletons", "animations"):
        refs = references[group]
        for ref, count in sorted(refs.items()):
            normalized = ref.replace("\\", "/").lower()
            base = normalized.rsplit("/", 1)[-1]
            if asset_like(ref) and normalized not in emrs_full and base not in emrs_base:
                lines.append(f"[{group}] {ref} (count {count})")
    return lines


def write_raw_base_kids(
    setbas: Path,
    out_dir: Path,
    export_leaf_chunks: bool = True,
    write_manifest: bool = True,
) -> Dict[str, object]:
    data, root = load_setbas(setbas)
    exporter = RawBaseKidsExporter(
        data,
        root,
        out_dir,
        export_leaf_chunks=export_leaf_chunks,
        write_manifest=write_manifest,
    )
    return exporter.export()


def write_outputs(setbas: Path, out_dir: Path) -> Dict[str, object]:
    data, root = load_setbas(setbas)
    exporter = BaseKidsExporter(data, root)
    scenegraph = exporter.export()
    references = exporter.references()
    emrs_names = read_emrs_names(data, root)
    unresolved = build_unresolved_lines(references, emrs_names)

    out_dir.mkdir(parents=True, exist_ok=True)
    scenegraph_path = out_dir / "scenegraph.json"
    references_path = out_dir / "references.json"
    unresolved_path = out_dir / "unresolved_refs.txt"

    scenegraph_path.write_text(json.dumps(scenegraph, indent=2), encoding="utf-8")
    references_path.write_text(json.dumps(references, indent=2), encoding="utf-8")
    if unresolved:
        unresolved_path.write_text("\n".join(unresolved) + "\n", encoding="utf-8")
    else:
        unresolved_path.write_text("<none>\n", encoding="utf-8")

    return {
        "scenegraph_path": scenegraph_path,
        "references_path": references_path,
        "unresolved_path": unresolved_path,
        "kids_count": len(scenegraph["kids_sections"]),
        "node_count": len(scenegraph["nodes"]),
        "top_class_counts": Counter(references["class_counts"]).most_common(10),
        "texture_ref_count": len(references["textures"]),
        "skeleton_ref_count": len(references["skeletons"]),
        "animation_ref_count": len(references["animations"]),
        "unresolved_count": len(unresolved),
    }


def print_summary(summary: Dict[str, object]) -> None:
    print(f"wrote {summary['scenegraph_path']}")
    print(f"wrote {summary['references_path']}")
    print(f"wrote {summary['unresolved_path']}")
    print("")
    print("Summary:")
    print(f"  KIDS sections found: {summary['kids_count']}")
    print(f"  OBJT nodes exported: {summary['node_count']}")
    print("  top CLID counts:")
    for class_name, count in summary["top_class_counts"]:
        print(f"    {class_name}: {count}")
    print(f"  texture references found: {summary['texture_ref_count']}")
    print(f"  skeleton references found: {summary['skeleton_ref_count']}")
    print(f"  animation references found: {summary['animation_ref_count']}")
    print(f"  unresolved references: {summary['unresolved_count']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export BASE/KIDS metadata from a local OpenNeoUA/Urban Assault SET.BAS.")
    parser.add_argument("setbas", metavar="path_to_SET.BAS", help="local SET.BAS file to parse read-only")
    parser.add_argument("--out", required=True, help="metadata output directory")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setbas = Path(args.setbas).resolve()
    out_dir = Path(args.out).resolve()

    if not setbas.is_file():
        print(f"error: SET.BAS not found: {setbas}", file=sys.stderr)
        return 2

    try:
        summary = write_outputs(setbas, out_dir)
    except (ExportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
