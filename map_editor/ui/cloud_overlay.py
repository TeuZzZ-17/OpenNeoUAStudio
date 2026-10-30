"""Animated preview of the level's Cloud profile (cloud_id) drawn over the map view."""
import math
import time

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPolygonF

from ..core.clouds import CloudSimulation, load_cloud_mesh, map_particle_count, spin_matrices
from ..core.ldf_model import DEFAULT_HGT, SECTOR_SIZE

FRAME_MS = 40
# Faces drawn per frame; particles beyond the budget, or too small to show faces, become dots.
# Dots keep the real projected size of the mesh, so the preview never inflates the game's look.
FACE_BUDGET = 4000
MIN_FACE_PIXELS = 3.0


class CloudOverlay:
    def __init__(self, window):
        self.window = window
        window.view.cloud_overlay = self
        self._simulation = None
        self._mesh = None
        self._mesh_key = None
        self._state = None
        self._last = None
        self._brushes = {}
        # The view only repaints on its own events, so a timer keeps the animation moving.
        self._timer = QTimer(window)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def profile(self):
        view = self.window.view
        panel = getattr(self.window, 'level_panel', None)
        if view.doc is None or panel is None:
            return None
        return panel.cloud_profiles.get(int(view.doc.lvl_info.get('cloud_id', -1)))

    def _tick(self):
        view = self.window.view
        if self.profile() is not None and view.isVisible() and not view.clear_view:
            view.update()

    def _prepare(self, profile):
        panel = self.window.level_panel
        doc = self.window.view.doc
        key = (str(panel.catalog.installation.data), profile.mesh_3ds)
        if key != self._mesh_key:
            self._mesh = load_cloud_mesh(panel.catalog.installation.data, profile.mesh_3ds)
            self._mesh_key = key
            self._simulation = None
        if self._mesh is None:
            return False
        # The whole map footprint (borders included); it follows map resizes.
        box = (0.0, doc.mw * SECTOR_SIZE, -doc.mh * SECTOR_SIZE, 0.0)
        ground = float(-(self.window.view.terrain.cells.mean() - DEFAULT_HGT) * 100.0)
        count = map_particle_count(profile, (box[1] - box[0]) * (box[3] - box[2]))
        state = (profile, box, count)
        if self._simulation is None or self._state != state:
            self._simulation = CloudSimulation(profile, box, ground, count)
            self._state = state
            self._last = None
        return True

    @staticmethod
    def _project(camera, points):
        """Vectorised IsoCamera.world_to_screen for an (..., 3) array: screen (..., 2) and camera depth."""
        cyaw, syaw, cp, sp = camera._trig()
        x = (points[..., 0] - camera.center[0]) * camera.k
        y = -(points[..., 1] - camera.center[1]) * camera.k
        z = (points[..., 2] - camera.center[2]) * camera.k
        px = x * cyaw + z * syaw
        pz = -x * syaw + z * cyaw
        cx, cy, cz = px, y * cp - pz * sp, y * sp + pz * cp + (4 if camera.perspective else 0)
        ox, oy = camera.width / 2 + camera.pan[0], camera.height / 2 + camera.pan[1]
        if camera.perspective:
            f = camera.height / (2 * math.tan(math.radians(camera.fov) / 2)) / np.maximum(1e-9, 4 - cz)
        else:
            f = camera.zoom / camera.k
        return np.stack([ox - cx * f, oy - cy * f], -1), cz

    def _brush(self, rgb, shade, alpha):
        key = (rgb, round(shade * 8), round(alpha * 16))
        brush = self._brushes.get(key)
        if brush is None:
            level = key[1] / 8
            brush = QBrush(QColor(*(min(255, int(c * level)) for c in rgb), min(255, key[2] * 16)))
            self._brushes[key] = brush
        return brush

    def draw(self, painter, camera=None, terrain=None):
        view = self.window.view
        profile = self.profile()
        if profile is None or not self._prepare(profile):
            return
        camera = camera or view.camera
        now = time.monotonic()
        dt_ms = 0.0 if self._last is None else (now - self._last) * 1000.0
        self._last = now
        sim, mesh = self._simulation, self._mesh
        sim.step(dt_ms)

        rotation = spin_matrices(profile.spin, sim.age)
        local = np.einsum('vj,nij->nvi', mesh.vertices * np.asarray(profile.scale), rotation)
        world = sim.pos[:, None, :] + local
        screen, depth = self._project(camera, world)
        alpha = sim.alpha()
        face_points = screen[:, mesh.faces]
        center = screen.mean(axis=1)
        extent = (screen.max(axis=1) - screen.min(axis=1)).max(axis=1)
        width, height = camera.width, camera.height
        rgb = tuple(int(c * 255) for c in profile.tint[:3])

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        faces_left = FACE_BUDGET
        count = len(mesh.faces)
        for i in range(len(alpha)):
            a = float(alpha[i])
            cx, cy = center[i]
            if a <= 0.01 or (camera.perspective and depth[i].max() >= 4):
                continue
            margin = extent[i] + 4
            if cx < -margin or cy < -margin or cx > width + margin or cy > height + margin:
                continue
            if extent[i] >= MIN_FACE_PIXELS and faces_left >= count:
                faces_left -= count
                for f in range(count):
                    painter.setBrush(self._brush(rgb, 1.0, a))
                    painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in face_points[i, f].tolist()]))
            else:
                painter.setBrush(self._brush(rgb, 1.0, a))
                radius = extent[i] / 2
                painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.restore()
