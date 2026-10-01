from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

ISO_YAW = -45.0
ISO_PITCH = 35.26
CAMERA_K = 1.0 / 6000.0


@lru_cache(maxsize=16)
def _angle_trig(yaw, pitch):
    yaw, pitch = math.radians(yaw), math.radians(pitch)
    return (math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch))


@dataclass
class IsoCamera:
    yaw: float = ISO_YAW
    pitch: float = ISO_PITCH
    zoom: float = 0.12
    pan: tuple[float, float] = (0.0, 0.0)
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    width: int = 800
    height: int = 600
    k: float = CAMERA_K

    def copy(self) -> "IsoCamera":
        return IsoCamera(self.yaw, self.pitch, self.zoom, self.pan,
                         self.center, self.width, self.height, self.k)

    def _trig(self):
        return _angle_trig(self.yaw, self.pitch)

    def to_camera(self, point, relative: bool = False):
        """Punto mondo UA (y negativo = su) -> spazio camera di Studio."""
        cx, cy, cz = (0.0, 0.0, 0.0) if relative else self.center
        cyaw, syaw, cp, sp = self._trig()
        x = (point[0] - cx) * self.k
        y = -(point[1] - cy) * self.k
        z = (point[2] - cz) * self.k
        xz_x = x * cyaw + z * syaw
        xz_z = -x * syaw + z * cyaw
        return (xz_x, y * cp - xz_z * sp, y * sp + xz_z * cp)

    def to_screen(self, cam_point, origin=None):
        ox, oy = origin if origin is not None else (
            self.width / 2 + self.pan[0], self.height / 2 + self.pan[1])
        f = self.zoom / self.k
        return (ox + cam_point[0] * f, oy - cam_point[1] * f)

    def world_to_screen(self, point):
        cam = self.to_camera(point)
        return self.to_screen(cam), cam[2]

    def screen_to_ground(self, sx: float, sy: float, ground_y: float = 0.0):
        """Raggio schermo -> punto sul piano y=ground_y (ortografico)."""
        cyaw, syaw, cp, sp = self._trig()
        f = self.zoom / self.k
        xc = (sx - self.width / 2 - self.pan[0]) / f
        yc = -(sy - self.height / 2 - self.pan[1]) / f
        cy = -(ground_y - self.center[1]) * self.k
        if abs(sp) < 1e-6:
            return None
        xz_z = (cy * cp - yc) / sp
        x = xc * cyaw - xz_z * syaw
        z = xc * syaw + xz_z * cyaw
        return (x / self.k + self.center[0], ground_y, z / self.k + self.center[2])
