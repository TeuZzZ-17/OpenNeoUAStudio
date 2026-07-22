"""Read-only parser for Urban Assault .base/.bas prefab files.

The chunk layout implemented here is CONFIRMED: it mirrors the OpenUA runtime
loaders line by line (UA_source: base.cpp, ade.cpp, area.cpp, amesh.cpp,
ilbm.cpp, bmpAnm.cpp, embed.cpp, nucleas.cpp) and was verified byte for byte
against original 1996 developer assets (BP_FLAK1.base and others).

File layout (CONFIRMED):

    FORM MC2
      FORM OBJT                     <- object wrapper
        CLID  "base.class\\0"       <- class dispatch string
        FORM BASE                   <- payload form, one per class
          FORM ROOT   (NAME)        <- optional object name
          STRC        62 bytes      <- transform (see BaseTransform)
          FORM OBJT   before STRC   -> embedded resources (embed.class)
          FORM OBJT   after STRC    -> skeleton object (sklt.class)
          FORM ADES   (FORM OBJT*)  -> amesh.class / area.class materials
          FORM KIDS   (FORM OBJT*)  -> nested child base.class objects

    amesh.class payload:
    FORM AMSH
      FORM AREA
        FORM ADE (FORM ROOT, STRC 10B: s16 ver, s8 pad, s8 flags,
                  s16 pointID, s16 polyID, s16 pad)
        STRC 10B: s16 ver, u16 flags, u16 polflags, u8 pad,
                  u8 colorVal, u8 tracyVal, u8 shadeVal
        FORM OBJT -> texture (ilbm.class FORM CIBO / bmpanim.class FORM BANI)
      ATTS: N * 6 bytes: s16 polyID, u8 colorVal, u8 shadeVal,
            u8 tracyVal, u8 pad          (N = chunk size / 6)
      OLPL: per ATTS entry: s16 count, count * (u8 u, u8 v);
            u,v are texture-space pixels normalised by /256 at runtime.
            The i-th OLPL group maps onto skeleton polygon ATTS[i].polyID and
            its j-th UV pair pairs with the polygon's j-th vertex.

    ilbm.class payload (embedded in .base):
    FORM CIBO
      NAM2  resource file name ("MTL.ILBM\\0")
      OTL2  optional default outline UVs: N * (u8 u, u8 v)

    bmpanim.class payload:
    FORM BANI
      STRC  s16 version, s16 nameOffset, s16 animType, then chars;
            resource name starts at byte (nameOffset - 6) of the char block.
            animType: 0 = loop, 1 = ping-pong.

IMPORTANT: the chunk ID "ATTS" is reused by particle.class (FORM PTCL) with a
completely different payload (emitter parameters).  The meaning of ATTS
depends on the containing class; this parser only decodes amesh ATTS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import struct

from iff_reader import IffChunk, IffTree, read_cstring, read_iff_bytes, read_iff_file

CONFIRMED = "CONFIRMED"
STRONG_HYPOTHESIS = "STRONG HYPOTHESIS"
WEAK_HYPOTHESIS = "WEAK HYPOTHESIS"
UNKNOWN = "UNKNOWN"


class BaseParseError(Exception):
    pass


# --- decoded structures -----------------------------------------------------


@dataclass
class BaseTransform:
    """FORM BASE > STRC payload (62 bytes, CONFIRMED, base.cpp ReadIFFTagSTRC)."""

    version: int = 0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    saved_move: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    euler: tuple[int, int, int] = (0, 0, 0)
    saved_rotation: tuple[int, int, int] = (0, 0, 0)
    flags: int = 0
    vis_limit: int = 4096
    ambient_light: int = 255


@dataclass
class AttsEntry:
    """One amesh ATTS record (6 bytes, CONFIRMED, amesh.cpp LoadingFromIFF)."""

    poly_id: int
    color_val: int
    shade_val: int
    tracy_val: int
    pad: int


@dataclass
class TextureRef:
    """Texture object found inside FORM AREA (ilbm.class or bmpanim.class)."""

    class_id: str = ""
    kind: str = ""          # "ilbm" | "bmpanim" | class_id fallback
    name: str = ""          # resource file name (NAM2 / BANI STRC name)
    outline_uvs: list[tuple[int, int]] = field(default_factory=list)  # CIBO OTL2
    anim_type: int | None = None  # BANI only: 0 loop, 1 ping-pong
    # Absolute c-string span in the source BASE.  The conservative texture
    # writer only replaces names that fit in this existing allocation.
    name_payload_offset: int = -1
    name_capacity: int = 0


@dataclass
class AmeshBlock:
    """One ADES entry: amesh.class material block (or area.class single poly)."""

    class_id: str = ""
    ade_flags: int = 0
    ade_point_id: int = 0
    ade_poly_id: int = 0
    area_flags: int = 0
    polflags: int = 0
    color_val: int = 0
    tracy_val: int = 0
    shade_val: int = 0
    texture: TextureRef | None = None
    tracy_texture: TextureRef | None = None
    atts: list[AttsEntry] = field(default_factory=list)
    olpl: list[list[tuple[int, int]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # File offsets of this block's ATTS/OLPL chunks (headers), -1 when absent.
    # Used by the safe mapping editor to append entries in place.
    atts_chunk_offset: int = -1
    atts_chunk_size: int = 0
    olpl_chunk_offset: int = -1
    olpl_chunk_size: int = 0

    # AREA_POL_FLAG_* bits (CONFIRMED, area.h)
    @property
    def map_mode(self) -> str:
        bits = self.polflags & 0x6
        return {0x0: "none", 0x2: "linear", 0x6: "depth"}.get(bits, "linear?")

    @property
    def textured(self) -> bool:
        return bool(self.polflags & 0x8)

    @property
    def shade_mode(self) -> str:
        bits = self.polflags & 0x30
        return {0x0: "none", 0x10: "flat", 0x20: "line", 0x30: "gradient"}[bits]

    @property
    def tracy_mode(self) -> str:
        bits = self.polflags & 0xC0
        return {0x0: "none", 0x40: "clear", 0x80: "flat", 0xC0: "mapped"}[bits]

    @property
    def tracy_light(self) -> bool:
        return bool(self.polflags & 0x100)

    @property
    def depth_fade(self) -> bool:
        return bool(self.area_flags & 0x1)  # AREA_FLAG_DPTHFADE

    def describe_polflags(self) -> str:
        parts = [f"map={self.map_mode}"]
        parts.append("textured" if self.textured else "untextured")
        parts.append(f"shade={self.shade_mode}")
        parts.append(f"tracy={self.tracy_mode}")
        if self.tracy_light:
            parts.append("tracy-light")
        if self.depth_fade:
            parts.append("depth-fade")
        return ", ".join(parts)


@dataclass
class EmbeddedResource:
    """One EMRS record inside FORM EMBD (CONFIRMED, embed.cpp)."""

    class_id: str = ""
    resource_name: str = ""
    payload_tag: str = ""
    payload_form_type: str = ""
    payload_offset: int = 0
    payload_size: int = 0  # full payload chunk bytes, including header/pad


@dataclass
class BaseObject:
    """One base.class object (root or KIDS child)."""

    name: str = ""
    transform: BaseTransform | None = None
    skeleton_class: str = ""
    skeleton_name: str = ""
    ades: list[AmeshBlock] = field(default_factory=list)
    kids: list["BaseObject"] = field(default_factory=list)
    embedded: list[EmbeddedResource] = field(default_factory=list)
    unknown_chunks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Source FORM OBJT location, used to export one KIDS object as a standalone
    # loose BASE without serializing or rewriting unrelated archive objects.
    source_objt_offset: int = -1
    source_objt_size: int = 0

    def iter_tree(self):
        yield self
        for kid in self.kids:
            yield from kid.iter_tree()


@dataclass
class BaseAsset:
    """Parsed .base file."""

    source_path: str = ""
    tree: IffTree | None = None
    root: BaseObject | None = None
    warnings: list[str] = field(default_factory=list)

    def all_objects(self) -> list[BaseObject]:
        return list(self.root.iter_tree()) if self.root else []

    def referenced_skeletons(self) -> list[str]:
        return [o.skeleton_name for o in self.all_objects() if o.skeleton_name]

    def referenced_textures(self) -> list[str]:
        names = []
        for obj in self.all_objects():
            for block in obj.ades:
                for tex in (block.texture, block.tracy_texture):
                    if tex and tex.name and tex.name not in names:
                        names.append(tex.name)
        return names

    def referenced_animations(self) -> list[str]:
        names = []
        for obj in self.all_objects():
            for block in obj.ades:
                for tex in (block.texture, block.tracy_texture):
                    if tex and tex.kind == "bmpanim" and tex.name and tex.name not in names:
                        names.append(tex.name)
        return names


# --- low-level chunk decoding ------------------------------------------------


def _read_clid(data: bytes, objt: IffChunk) -> str:
    for child in objt.children:
        if child.tag == "CLID":
            text, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            return text
    return ""


def _payload_form(objt: IffChunk) -> IffChunk | None:
    for child in objt.children:
        if child.tag == "FORM":
            return child
    return None


def _read_root_name(data: bytes, form: IffChunk) -> str:
    root = None
    for child in form.children:
        if child.is_form("ROOT"):
            root = child
            break
    if root is None:
        return ""
    for child in root.children:
        if child.tag == "NAME":
            text, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            return text
    return ""


def _parse_base_strc(data: bytes, chunk: IffChunk, warnings: list[str]) -> BaseTransform:
    if chunk.available_size < 62:
        warnings.append(
            f"BASE STRC at 0x{chunk.offset:X} has {chunk.available_size} "
            "available bytes; expected 62."
        )
        return BaseTransform()
    p = chunk.payload_offset
    tf = BaseTransform()
    tf.version = struct.unpack_from(">h", data, p)[0]
    tf.position = struct.unpack_from(">fff", data, p + 2)
    tf.saved_move = struct.unpack_from(">fff", data, p + 14)
    tf.scale = struct.unpack_from(">fff", data, p + 26)
    tf.euler = struct.unpack_from(">hhh", data, p + 38)
    tf.saved_rotation = struct.unpack_from(">hhh", data, p + 44)
    tf.flags = struct.unpack_from(">h", data, p + 50)[0]
    tf.vis_limit, tf.ambient_light = struct.unpack_from(">ii", data, p + 54)
    return tf


def _parse_cibo(data: bytes, form: IffChunk, class_id: str) -> TextureRef:
    tex = TextureRef(class_id=class_id, kind="ilbm")
    for child in form.children:
        if child.tag == "NAM2":
            tex.name, _ = read_cstring(
                data, child.payload_offset, child.available_payload_end
            )
            tex.name_payload_offset = child.payload_offset
            tex.name_capacity = child.available_size
        elif child.tag == "OTL2":
            payload = child.payload(data)
            usable = len(payload) - (len(payload) % 2)
            tex.outline_uvs = [
                (payload[i], payload[i + 1]) for i in range(0, usable, 2)
            ]
    return tex


def _parse_bani(data: bytes, form: IffChunk, class_id: str,
                warnings: list[str]) -> TextureRef:
    tex = TextureRef(class_id=class_id, kind="bmpanim")
    for child in form.children:
        if child.tag == "STRC":
            p = child.payload_offset
            if child.available_size < 7:
                warnings.append(
                    f"BANI STRC at 0x{child.offset:X} is too small "
                    f"({child.available_size} available bytes)."
                )
                continue
            version, name_offset, anim_type = struct.unpack_from(">hhh", data, p)
            tex.anim_type = anim_type
            # Runtime: name starts at buf[nameOffset - 6] where buf follows the
            # three int16 fields (bmpAnm.cpp LoadingFromIFF, CONFIRMED).
            name_start = p + 6 + max(0, name_offset - 6)
            if version >= 1 and name_start < child.available_payload_end:
                tex.name, _ = read_cstring(
                    data, name_start, child.available_payload_end
                )
                tex.name_payload_offset = name_start
                tex.name_capacity = child.available_payload_end - name_start
    return tex


def _parse_texture_objt(data: bytes, objt: IffChunk,
                        warnings: list[str]) -> TextureRef | None:
    class_id = _read_clid(data, objt)
    form = _payload_form(objt)
    if form is None:
        warnings.append(f"Texture OBJT at 0x{objt.offset:X} has no payload FORM.")
        return None
    if form.form_type == "CIBO":
        return _parse_cibo(data, form, class_id)
    if form.form_type == "BANI":
        return _parse_bani(data, form, class_id, warnings)
    warnings.append(
        f"Texture OBJT class {class_id!r} uses unhandled FORM "
        f"{form.form_type!r} at 0x{form.offset:X}."
    )
    return TextureRef(class_id=class_id, kind=class_id or "unknown")


def _parse_area(data: bytes, area: IffChunk, block: AmeshBlock) -> None:
    for child in area.children:
        if child.is_form("ADE"):
            for sub in child.children:
                if sub.tag == "STRC" and sub.available_size >= 10:
                    p = sub.payload_offset
                    version, _nu, flags, point, poly, _nu2 = struct.unpack_from(
                        ">hbbhhh", data, p
                    )
                    if version >= 1:
                        block.ade_flags = flags
                        block.ade_point_id = point
                        block.ade_poly_id = poly
        elif child.tag == "STRC":
            if child.available_size < 10:
                block.warnings.append(
                    f"AREA STRC at 0x{child.offset:X} has "
                    f"{child.available_size} available bytes; expected 10."
                )
                continue
            p = child.payload_offset
            version, flags, polflags, _un, clr, trc, shd = struct.unpack_from(
                ">hHHBBBB", data, p
            )
            if version >= 1:
                block.area_flags = flags
                block.polflags = polflags
                block.color_val = clr
                block.tracy_val = trc
                block.shade_val = shd
        elif child.is_form("OBJT"):
            tex = _parse_texture_objt(data, child, block.warnings)
            if tex is None:
                continue
            # Runtime assignment (area.cpp area_func5__sub1, CONFIRMED):
            # TEXBIT -> diffuse texture; TRACYMAPPED -> transparency map;
            # both -> first OBJT is diffuse, second is the tracy map.
            textured = bool(block.polflags & 0x8)
            tracy_mapped = (block.polflags & 0xC0) == 0xC0
            if textured and (not tracy_mapped or block.texture is None):
                block.texture = tex
            elif tracy_mapped:
                block.tracy_texture = tex
            else:
                block.texture = tex


def _parse_atts(data: bytes, chunk: IffChunk, block: AmeshBlock) -> None:
    block.atts_chunk_offset = chunk.offset
    block.atts_chunk_size = chunk.size
    count = chunk.available_size // 6
    if chunk.available_size % 6:
        block.warnings.append(
            f"ATTS available size {chunk.available_size} is not a multiple of 6; "
            "trailing bytes ignored."
        )
    p = chunk.payload_offset
    for i in range(count):
        poly_id, color, shade, tracy, pad = struct.unpack_from(">hBBBB", data, p + i * 6)
        block.atts.append(AttsEntry(poly_id, color, shade, tracy, pad))


def _parse_olpl(data: bytes, chunk: IffChunk, block: AmeshBlock) -> None:
    block.olpl_chunk_offset = chunk.offset
    block.olpl_chunk_size = chunk.size
    p = chunk.payload_offset
    end = chunk.available_payload_end
    expected = len(block.atts)
    while p + 2 <= end and (expected == 0 or len(block.olpl) < expected):
        count = struct.unpack_from(">h", data, p)[0]
        p += 2
        if count < 0 or p + count * 2 > end:
            block.warnings.append(
                f"OLPL group {len(block.olpl)} is truncated or has an invalid "
                f"count ({count}); remaining data skipped."
            )
            return
        block.olpl.append(
            [(data[p + 2 * i], data[p + 2 * i + 1]) for i in range(count)]
        )
        p += count * 2
    leftover = end - p
    if leftover > 1:
        block.warnings.append(f"OLPL has {leftover} unused trailing byte(s).")


def _parse_amesh(data: bytes, form: IffChunk, class_id: str) -> AmeshBlock:
    block = AmeshBlock(class_id=class_id)
    for child in form.children:
        if child.is_form("AREA"):
            _parse_area(data, child, block)
        elif child.tag == "ATTS":
            _parse_atts(data, child, block)
        elif child.tag == "OLPL":
            _parse_olpl(data, child, block)
        else:
            block.warnings.append(
                f"Unhandled chunk {child.display_name} inside AMSH at 0x{child.offset:X}."
            )
    if block.olpl and len(block.olpl) != len(block.atts):
        block.warnings.append(
            f"OLPL group count ({len(block.olpl)}) differs from ATTS entry "
            f"count ({len(block.atts)})."
        )
    return block


def _parse_area_only(data: bytes, form: IffChunk, class_id: str) -> AmeshBlock:
    """area.class ADES entry: FORM AREA without AMSH wrapper (one polygon)."""

    block = AmeshBlock(class_id=class_id)
    _parse_area(data, form, block)
    # area.class renders exactly one polygon: _polyID from the ADE STRC, with
    # UVs taken from the texture's OTL2 outline (area.cpp GenMesh, CONFIRMED).
    block.atts.append(
        AttsEntry(block.ade_poly_id, block.color_val, block.shade_val,
                  block.tracy_val, 0)
    )
    if block.texture and block.texture.outline_uvs:
        block.olpl.append(list(block.texture.outline_uvs))
    return block


def _parse_embed(data: bytes, form: IffChunk, obj: BaseObject) -> None:
    for child_index, child in enumerate(form.children):
        if child.tag != "EMRS":
            continue
        class_id, pos = read_cstring(
            data, child.payload_offset, child.available_payload_end
        )
        res_name, pos = read_cstring(data, pos, child.available_payload_end)
        record = EmbeddedResource(class_id=class_id, resource_name=res_name)
        # Payload is either inline after the strings or the next sibling chunk.
        while pos < child.available_payload_end and data[pos] == 0:
            pos += 1
        if pos + 8 <= child.available_payload_end:
            record.payload_tag = data[pos:pos + 4].decode("latin-1", "replace")
            declared = struct.unpack_from(">I", data, pos + 4)[0]
            record.payload_size = 8 + declared + (declared & 1)
            record.payload_offset = pos
            if record.payload_tag == "FORM":
                record.payload_form_type = data[pos + 8:pos + 12].decode(
                    "latin-1", "replace"
                )
        elif child_index + 1 < len(form.children):
            # embed.cpp stores the normal payload as the EMRS record's next
            # sibling.  It is not part of the EMRS chunk itself.
            payload = form.children[child_index + 1]
            if payload.tag != "EMRS":
                record.payload_tag = payload.tag
                record.payload_form_type = payload.form_type
                record.payload_offset = payload.offset
                record.payload_size = 8 + payload.size + (payload.size & 1)
        obj.embedded.append(record)


def _parse_ades(data: bytes, form: IffChunk, obj: BaseObject) -> None:
    for child in form.children:
        if not child.is_form("OBJT"):
            continue
        class_id = _read_clid(data, child)
        payload = _payload_form(child)
        if payload is None:
            obj.warnings.append(
                f"ADES OBJT at 0x{child.offset:X} ({class_id!r}) has no payload FORM."
            )
            continue
        if payload.form_type == "AMSH":
            obj.ades.append(_parse_amesh(data, payload, class_id))
        elif payload.form_type == "AREA":
            obj.ades.append(_parse_area_only(data, payload, class_id))
        elif payload.form_type == "PTCL":
            block = AmeshBlock(class_id=class_id)
            block.warnings.append(
                "particle.class block: its ATTS chunk stores emitter parameters "
                "(not polygon attributes) and is not decoded here."
            )
            obj.ades.append(block)
        else:
            obj.warnings.append(
                f"ADES entry with unhandled payload FORM {payload.form_type!r} "
                f"(class {class_id!r}) at 0x{payload.offset:X}."
            )


def _parse_base_object(data: bytes, objt: IffChunk, warnings: list[str]) -> BaseObject:
    obj = BaseObject()
    obj.source_objt_offset = objt.offset
    obj.source_objt_size = objt.size
    class_id = _read_clid(data, objt)
    if class_id and class_id.lower() != "base.class":
        obj.warnings.append(f"OBJT declares class {class_id!r}; expected base.class.")

    base_form = _payload_form(objt)
    if base_form is None or base_form.form_type != "BASE":
        obj.warnings.append("OBJT has no FORM BASE payload.")
        return obj

    obj.name = _read_root_name(data, base_form)
    strc_seen = False

    for child in base_form.children:
        if child.is_form("ROOT"):
            continue  # name already read
        if child.tag == "STRC":
            obj.transform = _parse_base_strc(data, child, obj.warnings)
            strc_seen = True
        elif child.is_form("OBJT"):
            # Runtime rule (base.cpp LoadingFromIFF, CONFIRMED): an OBJT before
            # STRC is the embedded-resources object, after STRC it is the
            # skeleton object.
            inner_class = _read_clid(data, child)
            inner_form = _payload_form(child)
            if not strc_seen:
                if inner_form is not None and inner_form.form_type == "EMBD":
                    _parse_embed(data, inner_form, obj)
                else:
                    obj.warnings.append(
                        f"Pre-STRC OBJT (class {inner_class!r}) with unexpected "
                        f"payload at 0x{child.offset:X}."
                    )
            else:
                obj.skeleton_class = inner_class
                if inner_form is not None and inner_form.form_type == "SKLC":
                    for sub in inner_form.children:
                        if sub.tag == "NAME":
                            obj.skeleton_name, _ = read_cstring(
                                data, sub.payload_offset,
                                sub.available_payload_end
                            )
                else:
                    obj.warnings.append(
                        f"Skeleton OBJT (class {inner_class!r}) without FORM SKLC "
                        f"at 0x{child.offset:X}."
                    )
        elif child.is_form("ADES"):
            _parse_ades(data, child, obj)
        elif child.is_form("KIDS"):
            for kid_objt in child.children:
                if kid_objt.is_form("OBJT"):
                    obj.kids.append(_parse_base_object(data, kid_objt, warnings))
        else:
            obj.unknown_chunks.append(
                f"{child.display_name} at 0x{child.offset:X} ({child.size} bytes)"
            )
    return obj


# --- entry points -------------------------------------------------------------


def parse_base_bytes(data: bytes, source_name: str = "<memory>") -> BaseAsset:
    asset = BaseAsset(source_path=source_name)
    asset.tree = read_iff_bytes(data, source_name)
    asset.warnings.extend(asset.tree.warnings)

    mc2 = asset.tree.find_first("FORM", "MC2")
    if mc2 is None:
        asset.warnings.append("No FORM MC2 container found; not a .base file?")
        return asset

    root_objt = None
    for child in mc2.children:
        if child.is_form("OBJT"):
            root_objt = child
            break
    if root_objt is None:
        asset.warnings.append("FORM MC2 contains no FORM OBJT.")
        return asset

    asset.root = _parse_base_object(data, root_objt, asset.warnings)
    for obj in asset.root.iter_tree():
        asset.warnings.extend(obj.warnings)
        for block in obj.ades:
            asset.warnings.extend(block.warnings)
    return asset


def parse_base_file(path: str | Path) -> BaseAsset:
    file_path = Path(path)
    tree = read_iff_file(file_path)
    asset = parse_base_bytes(tree.data, file_path.name)
    asset.source_path = str(file_path)
    return asset


if __name__ == "__main__":
    import argparse

    cli = argparse.ArgumentParser(description="Inspect a .base file (read-only).")
    cli.add_argument("file")
    args = cli.parse_args()

    parsed = parse_base_file(args.file)
    print(f"== {parsed.source_path} ==")
    if parsed.tree:
        print(parsed.tree.dump_text())
    for obj in parsed.all_objects():
        print(f"\nObject name={obj.name!r} skeleton={obj.skeleton_name!r}")
        if obj.transform:
            t = obj.transform
            print(f"  pos={t.position} scale={t.scale} euler={t.euler} "
                  f"visLimit={t.vis_limit} ambient={t.ambient_light}")
        for i, block in enumerate(obj.ades):
            tex = block.texture.name if block.texture else "<none>"
            print(f"  ADES[{i}] {block.class_id}: texture={tex} "
                  f"ATTS={len(block.atts)} OLPL={len(block.olpl)} "
                  f"[{block.describe_polflags()}]")
        for res in obj.embedded:
            print(f"  EMRS {res.class_id} {res.resource_name} "
                  f"({res.payload_tag} {res.payload_form_type})")
    for warning in parsed.warnings:
        print(f"WARNING: {warning}")
