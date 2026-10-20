"""Detail levels of the wireframe generator (light module: no Qt, no numpy)."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DetailLevel:
    """How much the generator simplifies. Level 0 is the raw, original behaviour."""

    label: str
    # False: a see-through texture hides the edges behind it (original behaviour).
    see_through_open: bool
    # Join, straighten and clean the final lines (see wireframe_cleanup).
    tidy: bool
    # Two faces meeting at less than this angle are one flat surface: no line between them.
    soft_edge_degrees: float
    # Texture contours are simplified by this fraction of the model size (0 = ``simplification`` only).
    contour_fraction: float
    # Contours come from each see-through face (sprites keep a simple outline) instead of one mask.
    per_shape: bool
    # Smooth the contour mask and drop its small teeth.
    smooth: bool
    # A corner of a contour must add at least this many tolerance-squares of area.
    tooth_area: float

    @property
    def soft_edge_cos(self) -> float:
        return math.cos(math.radians(self.soft_edge_degrees)) if self.soft_edge_degrees > 0 else 2.0


DETAIL_LEVELS = (
    DetailLevel("Maximum", False, False, 0.0, 0.0, False, False, 0.0),
    DetailLevel("High", True, True, 0.0, 0.006, False, False, 0.0),
    DetailLevel("Medium", True, True, 12.0, 0.015, False, True, 1.5),
    DetailLevel("Simple", True, True, 12.0, 0.03, True, True, 3.0),
    DetailLevel("Minimal", True, True, 20.0, 0.05, True, True, 6.0),
)
DEFAULT_DETAIL = 3


def detail_settings(position: float) -> DetailLevel:
    """Settings for a position between levels (0..4): numbers blend, switches follow the nearest level."""

    top = len(DETAIL_LEVELS)-1
    if not isinstance(position, (int, float)) or isinstance(position, bool) \
            or not math.isfinite(position) or not 0 <= position <= top:
        raise ValueError(f"Detail must be a number between 0 and {top}.")
    low = min(int(math.floor(position)), top)
    high = min(low+1, top)
    t = position-low
    a, b = DETAIL_LEVELS[low], DETAIL_LEVELS[high]
    nearest = DETAIL_LEVELS[min(int(math.floor(position+0.5)), top)]

    def blend(x: float, y: float) -> float:
        return x+(y-x)*t

    return DetailLevel(
        nearest.label, nearest.see_through_open, nearest.tidy,
        blend(a.soft_edge_degrees, b.soft_edge_degrees),
        blend(a.contour_fraction, b.contour_fraction),
        nearest.per_shape, nearest.smooth,
        blend(a.tooth_area, b.tooth_area))
