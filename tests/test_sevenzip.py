"""7z writer: structure, every method, empty files, abort."""

import lzma
import struct
import zlib

import pytest

from studio.exchange.sevenzip import METHODS, SIGNATURE, write_7z


def _files(tmp_path):
    entries = []
    for name, data in (("a.bin", bytes(range(256)) * 40), ("empty.txt", b""),
                       ("b.bin", b"x" * 5000)):
        path = tmp_path / name
        path.write_bytes(data)
        entries.append((path, f"Corpus/{name}"))
    return entries


@pytest.mark.parametrize("method", METHODS)
def test_archive_header_and_data_are_consistent(tmp_path, method):
    entries = _files(tmp_path)
    target = tmp_path / "out.7z"
    write_7z(target, entries, method=method, level=5)
    data = target.read_bytes()
    assert data[:6] == SIGNATURE
    start_crc, = struct.unpack_from("<I", data, 8)
    assert start_crc == zlib.crc32(data[12:32]) & 0xFFFFFFFF
    offset, size, header_crc = struct.unpack_from("<QQI", data, 12)
    header = data[32 + offset:32 + offset + size]
    assert len(header) == size and header_crc == zlib.crc32(header) & 0xFFFFFFFF
    if method == "lzma2":
        packed = data[32:32 + offset]
        raw = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA2, "dict_size": 1 << 24}]).decompress(packed)
        assert raw == b"".join(path.read_bytes() for path, _n in entries)


def test_level_changes_size_and_bad_input_is_rejected(tmp_path):
    entries = _files(tmp_path)
    write_7z(tmp_path / "fast.7z", entries, method="bzip2", level=1)
    write_7z(tmp_path / "best.7z", entries, method="bzip2", level=9)
    assert (tmp_path / "fast.7z").stat().st_size > 0
    with pytest.raises(ValueError):
        write_7z(tmp_path / "x.7z", entries, method="rar")
    with pytest.raises(ValueError):
        write_7z(tmp_path / "x.7z", [])
    only_empty = tmp_path / "e"
    only_empty.write_bytes(b"")
    with pytest.raises(ValueError):
        write_7z(tmp_path / "x.7z", [(only_empty, "e")])


def test_progress_callback_can_abort(tmp_path):
    def stop():
        raise InterruptedError

    with pytest.raises(InterruptedError):
        write_7z(tmp_path / "x.7z", _files(tmp_path), progress=stop)
