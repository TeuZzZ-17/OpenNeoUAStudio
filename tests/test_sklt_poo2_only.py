import struct
import tempfile
import unittest
from pathlib import Path

from sklt_parser import (
    parse_sklt_bytes,
    parse_sklt_file,
    save_sklt_with_poo2_points,
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    result = tag + struct.pack(">I", len(payload)) + payload
    return result + (b"\0" if len(payload) & 1 else b"")


def _form(form_type: bytes, children: bytes) -> bytes:
    return _chunk(b"FORM", form_type + children)


def _sklt_with_poo2_and_sen2(points, sensors) -> bytes:
    poo2 = b"".join(struct.pack(">fff", *point) for point in points)
    sen2 = b"".join(struct.pack(">fff", *point) for point in sensors)
    pol2 = struct.pack(">IHHHH", 1, 3, 0, 1, 2)
    children = (
        _chunk(b"NOTE", b"preserve-this-metadata")
        + _chunk(b"POO2", poo2)
        + _chunk(b"SEN2", sen2)
        + _chunk(b"POL2", pol2)
    )
    return _form(b"SKLT", children)


class SkltPoo2OnlyTests(unittest.TestCase):
    def _fixture(self):
        points = [
            (-2.0, -1.0, -3.0),
            (2.0, 1.0, 3.0),
            (0.0, 0.0, 0.0),
        ]
        sensors = [
            (-4.0, -2.0, -6.0),
            (4.0, 2.0, 6.0),
            (0.0, 0.0, 0.0),
        ]
        model = parse_sklt_bytes(
            _sklt_with_poo2_and_sen2(points, sensors), "POO2_ONLY.SKLT"
        )
        return model, points, sensors

    def test_legacy_three_argument_call_keeps_default_sen2_update(self):
        model, points, sensors = self._fixture()
        offset = (5.0, -7.0, 11.0)
        edited = [
            tuple(point[axis] + offset[axis] for axis in range(3))
            for point in points
        ]
        expected_sensors = [
            tuple(point[axis] + offset[axis] for axis in range(3))
            for point in sensors
        ]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "default.SKLT"
            save_sklt_with_poo2_points(model, edited, target)
            parsed = parse_sklt_file(target)

        self.assertEqual(parsed.points, edited)
        self.assertEqual(parsed.sensors, expected_sensors)

    def test_update_sensors_false_changes_only_poo2_payload(self):
        model, points, sensors = self._fixture()
        offset = (5.0, -7.0, 11.0)
        edited = [
            tuple(point[axis] + offset[axis] for axis in range(3))
            for point in points
        ]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "poo2-only.SKLT"
            save_sklt_with_poo2_points(
                model, edited, target, update_sensors=False
            )
            output = target.read_bytes()
            parsed = parse_sklt_file(target)

        self.assertEqual(parsed.points, edited)
        self.assertEqual(parsed.sensors, sensors)
        poo2_start = model.poo2_payload_offset
        self.assertIsNotNone(poo2_start)
        poo2_end = poo2_start + model.poo2_payload_size
        original = model.original_data
        self.assertEqual(output[:poo2_start], original[:poo2_start])
        self.assertEqual(output[poo2_end:], original[poo2_end:])


if __name__ == "__main__":
    unittest.main()
