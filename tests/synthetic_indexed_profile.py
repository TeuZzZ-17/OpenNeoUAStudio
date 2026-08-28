"""Small valid retail-indexed profile used by write-path tests."""

from __future__ import annotations

from pathlib import Path
import struct

from asset_resolver import ResolvedFile
from ilbm_parser import parse_pal_file


def _chunk(tag: bytes, payload: bytes) -> bytes:
    encoded = tag + struct.pack(">I", len(payload)) + payload
    return encoded + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes) -> bytes:
    return _chunk(b"FORM", form_type + children)


def _vbmp(pixels: bytes) -> bytes:
    return _form(
        b"VBMP",
        _chunk(b"HEAD", struct.pack(">HHH", 256, 256, 0))
        + _chunk(b"BODY", pixels),
    )


def _shader_pixels() -> bytes:
    data = bytearray(bytes(range(256)) * 256)
    for shade in range(256):
        data[shade * 256] = 0
    data[6] = 0
    data[255 * 256:] = bytes(256)
    return bytes(data)


def _tracy_pixels() -> bytes:
    data = bytearray()
    for background in range(256):
        data.extend(bytes((background,)) * 256)
    for source in (8, 10, 11, 12, 14, 15):
        for background in range(256):
            data[background * 256 + source] = source
    for background in range(256):
        data[background * 256] = 0 if background == 13 else background
    for source in range(256):
        data[13 * 256 + source] = source
    for (background, source), output in {
        (31, 220): 212,
        (220, 31): 200,
        (13, 255): 255,
        (255, 13): 177,
        (156, 185): 54,
        (159, 185): 106,
        (0, 156): 223,
        (0, 159): 255,
        (223, 159): 207,
        (255, 156): 191,
    }.items():
        data[background * 256 + source] = output
    return bytes(data)


def attach_synthetic_indexed_profile(family, root: str | Path) -> None:
    """Attach one coherent on-disk profile without changing asset sources."""

    profile_root = Path(root)
    palette_dir = profile_root / "PALETTE"
    remap_dir = profile_root / "REMAP"
    palette_dir.mkdir(parents=True, exist_ok=True)
    remap_dir.mkdir(parents=True, exist_ok=True)
    palette_path = palette_dir / "NORMAL.PAL"
    shader_path = remap_dir / "SHADERMP.ILB"
    tracy_path = remap_dir / "TRACYRMP.ILB"
    palette_bytes = bytes(
        channel for index in range(256)
        for channel in (index, index, index))
    palette_path.write_bytes(_form(b"ILBM", _chunk(b"CMAP", palette_bytes)))
    shader_path.write_bytes(_vbmp(_shader_pixels()))
    tracy_path.write_bytes(_vbmp(_tracy_pixels()))

    family.external_palette = parse_pal_file(palette_path)
    family.external_palette_path = palette_path
    family.external_palette_ref = ResolvedFile(
        "PALETTE/NORMAL.PAL", status="found", path=palette_path,
        source="loose", resolution_rule="synthetic test profile",
        source_root=profile_root,
    )
    family.indexed_profile_refs = {
        "shader": ResolvedFile(
            "REMAP/SHADERMP.ILB", status="found", path=shader_path,
            source="loose", resolution_rule="synthetic test profile",
            source_root=profile_root,
        ),
        "tracy": ResolvedFile(
            "REMAP/TRACYRMP.ILB", status="found", path=tracy_path,
            source="loose", resolution_rule="synthetic test profile",
            source_root=profile_root,
        ),
    }
    resolved_root = str(profile_root.resolve())
    if resolved_root not in family.search_roots:
        family.search_roots.append(resolved_root)
