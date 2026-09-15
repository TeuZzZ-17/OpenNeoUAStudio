"""Shared ONUA3D wire constants and exact axis reflection; no Qt/native I/O."""
FORMAT = "OpenNeoUA ONUA3D"
VERSION = 1
MANIFEST = "onua3d_manifest.json"
# UA's downward Y becomes glTF's upward Y. Preserve numerical UA units;
# 1 UA unit is represented by 1 glTF metre, a bridge convention, not metrology.
AXES = (1.0, -1.0, 1.0)
UV_DIVISOR = 256.0
COORDINATES = {
    "ua_to_gltf": "(x, y, z) -> (x, -y, z)",
    "meters_per_ua_unit": 1.0,
    "physical_scale_known": False,
    "uv": "(u/256, v/256), top-left origin; no image flip",
    "winding": "UA fan (0,j,j-1), reversed after Y reflection: (0,j-1,j)",
    "transforms": "Studio independent object placement: T*S*R; identity owner hierarchy",
}



def convert_point(point):
    return tuple(float(value) * axis for value, axis in zip(point, AXES))

