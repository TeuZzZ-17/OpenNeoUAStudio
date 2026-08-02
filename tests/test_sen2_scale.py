import struct
import tempfile
import unittest
from pathlib import Path

from sklt_parser import (
    parse_sklt_bytes,
    parse_sklt_file,
    save_sklt_with_poo2_points,
    sen2_points_for_poo2,
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes) -> bytes:
    return _chunk(b"FORM", form_type + children)


def _sklt_with_sen2(points, sensors) -> bytes:
    poo2 = b"".join(struct.pack(">fff", *point) for point in points)
    sen2 = b"".join(struct.pack(">fff", *point) for point in sensors)
    children = (
        _chunk(b"POO2", poo2)
        + _chunk(b"SEN2", sen2)
        + _chunk(b"POL2", b"")
    )
    return _form(b"SKLT", children)


class Sen2ScaleTests(unittest.TestCase):
    def test_whole_model_scale_and_move_are_applied_to_sen2(self):
        points = [
            (-2.0, -1.0, -3.0),
            (2.0, 1.0, 3.0),
            (0.0, 0.0, 0.0),
        ]
        sensors = [
            (-2.0, -1.0, -3.0),
            (2.0, -1.0, -3.0),
            (-2.0, 1.0, 3.0),
            (2.0, 1.0, 3.0),
        ]
        model = parse_sklt_bytes(
            _sklt_with_sen2(points, sensors), "SCALE.SKLT")
        edited = [
            (
                point[0] * 10.0 + 5.0,
                point[1] * 3.0 - 7.0,
                point[2] * 2.0 + 11.0,
            )
            for point in points
        ]
        expected = [
            (
                point[0] * 10.0 + 5.0,
                point[1] * 3.0 - 7.0,
                point[2] * 2.0 + 11.0,
            )
            for point in sensors
        ]

        self.assertEqual(sen2_points_for_poo2(model, edited), expected)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SCALE.SKLT"
            save_sklt_with_poo2_points(model, edited, path)
            parsed = parse_sklt_file(path)
        self.assertEqual(parsed.points, edited)
        self.assertEqual(parsed.sensors, expected)

    def test_selective_geometry_edit_preserves_original_sen2(self):
        points = [
            (-2.0, -1.0, -3.0),
            (2.0, 1.0, 3.0),
            (0.0, 0.0, 0.0),
        ]
        sensors = [
            (-2.0, -1.0, -3.0),
            (2.0, -1.0, -3.0),
            (-2.0, 1.0, 3.0),
            (2.0, 1.0, 3.0),
        ]
        model = parse_sklt_bytes(
            _sklt_with_sen2(points, sensors), "PARTIAL.SKLT")
        edited = list(points)
        edited[0] = (99.0, edited[0][1], edited[0][2])

        self.assertEqual(sen2_points_for_poo2(model, edited), sensors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PARTIAL.SKLT"
            save_sklt_with_poo2_points(model, edited, path)
            parsed = parse_sklt_file(path)
        self.assertEqual(parsed.sensors, sensors)


if __name__ == "__main__":
    unittest.main()
