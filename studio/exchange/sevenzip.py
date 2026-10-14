"""Minimal 7z archive writer built on the standard library only (no Qt).

It writes what the snapshot batch needs: a set of files in one solid block,
compressed with LZMA2, LZMA, BZip2, Deflate or stored. The result opens in 7-Zip
and every other 7z reader. Empty files are listed without data; folders are not
written (readers create them from the file paths).
"""

from __future__ import annotations

import bz2
import lzma
import struct
import zlib
from pathlib import Path
from typing import Callable, Iterable

SIGNATURE = b"7z\xbc\xaf\x27\x1c"
METHODS = ("lzma2", "lzma", "bzip2", "deflate", "store")
LEVELS = range(1, 10)
READ_CHUNK = 1 << 20

# Coder ids of the 7z format and the property bytes each coder needs.
_CODER_IDS = {
    "store": b"\x00", "lzma": b"\x03\x01\x01", "lzma2": b"\x21",
    "bzip2": b"\x04\x02\x02", "deflate": b"\x04\x01\x08",
}
# Dictionary sizes of the 7-Zip presets 1..9 (MiB), so level 9 matches "Ultra".
_DICT_MIB = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 16, 7: 32, 8: 64, 9: 64}
# 7z stores times as 100 ns ticks since 1601; Unix epoch offset in seconds.
_EPOCH_1601 = 11644473600

# Property ids of the header.
_END, _HEADER, _MAIN_STREAMS, _FILES = 0x00, 0x01, 0x04, 0x05
_PACK_INFO, _UNPACK_INFO, _SUB_STREAMS = 0x06, 0x07, 0x08
_SIZE, _CRC, _FOLDER, _CODERS_UNPACK_SIZE = 0x09, 0x0A, 0x0B, 0x0C
_NUM_UNPACK_STREAM, _EMPTY_STREAM, _EMPTY_FILE = 0x0D, 0x0E, 0x0F
_NAMES, _MTIME = 0x11, 0x14


def _number(value: int) -> bytes:
    """7z variable-length number: the leading 1-bits count the extra bytes."""

    if value < 0x80:
        return bytes([value])
    for extra in range(1, 9):
        if value < 1 << (7 * (extra + 1)):
            high = (0xFF << (8 - extra)) & 0xFF
            return bytes([high | (value >> (8 * extra))]) + \
                (value & ((1 << (8 * extra)) - 1)).to_bytes(extra, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _lzma2_dict_byte(dict_size: int) -> int:
    for code in range(41):
        if dict_size <= (2 | (code & 1)) << (code // 2 + 11):
            return code
    return 40


def _compressor(method: str, level: int):
    """Return (compress chunk, finish, coder properties) for one method."""

    level = max(1, min(9, int(level)))
    dict_size = _DICT_MIB[level] << 20
    if method == "store":
        return (lambda data: data), (lambda: b""), b""
    if method in ("lzma", "lzma2"):
        filter_id = lzma.FILTER_LZMA2 if method == "lzma2" else lzma.FILTER_LZMA1
        coder = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=[{
            "id": filter_id, "preset": level, "dict_size": dict_size}])
        if method == "lzma2":
            props = bytes([_lzma2_dict_byte(dict_size)])
        else:
            # lc=3, lp=0, pb=2 are the preset defaults: (pb * 5 + lp) * 9 + lc.
            props = bytes([(2 * 5 + 0) * 9 + 3]) + struct.pack("<I", dict_size)
        return coder.compress, coder.flush, props
    if method == "bzip2":
        coder = bz2.BZ2Compressor(level)
        return coder.compress, coder.flush, b""
    if method == "deflate":
        coder = zlib.compressobj(level, zlib.DEFLATED, -15)
        return coder.compress, coder.flush, b""
    raise ValueError(f"Unknown 7z method: {method}")


def _bit_vector(bits: list[bool]) -> bytes:
    """Bit flags packed most significant bit first."""

    data = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            data[index // 8] |= 0x80 >> (index % 8)
    return bytes(data)


def _digest_block(crcs: list[int]) -> bytes:
    return bytes([1]) + b"".join(struct.pack("<I", crc) for crc in crcs)


def write_7z(destination: str | Path, files: Iterable[tuple[Path, str]], *,
             method: str = "lzma2", level: int = 6,
             progress: Callable[[], None] | None = None) -> None:
    """Write ``files`` (path on disk, name inside the archive) to ``destination``.

    ``progress`` is called after every input chunk; it may raise to abort
    (the partial file is then left for the caller to delete).
    """

    if method not in METHODS:
        raise ValueError(f"Unknown 7z method: {method}")
    entries = [(Path(path), name.replace("\\", "/")) for path, name in files]
    if not entries:
        raise ValueError("A 7z archive needs at least one file.")
    compress, finish, props = _compressor(method, level)
    sizes: list[int] = []
    crcs: list[int] = []
    times: list[int] = []
    empty: list[bool] = []
    with open(destination, "wb") as out:
        out.write(b"\0" * 32)                    # start header, filled in last
        packed = 0
        for path, _name in entries:
            size = 0
            crc = 0
            with open(path, "rb") as source:
                while chunk := source.read(READ_CHUNK):
                    crc = zlib.crc32(chunk, crc)
                    size += len(chunk)
                    data = compress(chunk)
                    out.write(data)
                    packed += len(data)
                    if progress is not None:
                        progress()
            empty.append(size == 0)
            if size:
                sizes.append(size)
                crcs.append(crc & 0xFFFFFFFF)
            times.append((int(path.stat().st_mtime) + _EPOCH_1601) * 10_000_000)
        if not sizes:
            raise ValueError("A 7z archive needs at least one non-empty file.")
        tail = finish()
        out.write(tail)
        packed += len(tail)

        coder_id = _CODER_IDS[method]
        flag = len(coder_id) | (0x20 if props else 0)
        coder = bytes([flag]) + coder_id + (_number(len(props)) + props if props else b"")
        names = b"".join(name.encode("utf-16-le") + b"\0\0" for _p, name in entries)
        mtimes = b"".join(struct.pack("<Q", value) for value in times)
        empty_info = b""
        if any(empty):
            flags = _bit_vector(empty)
            empty_info = (bytes([_EMPTY_STREAM]) + _number(len(flags)) + flags
                          + bytes([_EMPTY_FILE])
                          + _number((empty.count(True) + 7) // 8)
                          + _bit_vector([True] * empty.count(True)))
        header = b"".join((
            bytes([_HEADER, _MAIN_STREAMS]),
            bytes([_PACK_INFO]) + _number(0) + _number(1)
            + bytes([_SIZE]) + _number(packed) + bytes([_END]),
            bytes([_UNPACK_INFO, _FOLDER]) + _number(1) + b"\0"
            + _number(1) + coder
            + bytes([_CODERS_UNPACK_SIZE]) + _number(sum(sizes)) + bytes([_END]),
            bytes([_SUB_STREAMS, _NUM_UNPACK_STREAM]) + _number(len(sizes))
            + bytes([_SIZE]) + b"".join(_number(size) for size in sizes[:-1])
            + bytes([_CRC]) + _digest_block(crcs) + bytes([_END]),
            bytes([_END]),                       # end of the streams info
            bytes([_FILES]) + _number(len(entries)),
            empty_info,
            bytes([_NAMES]) + _number(len(names) + 1) + b"\0" + names,
            bytes([_MTIME]) + _number(len(mtimes) + 2) + b"\1\0" + mtimes,
            bytes([_END, _END]),                 # end of files info and header
        ))
        out.write(header)
        start = struct.pack("<QQI", packed, len(header),
                            zlib.crc32(header) & 0xFFFFFFFF)
        out.seek(0)
        out.write(SIGNATURE + b"\0\x04"
                  + struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF) + start)
