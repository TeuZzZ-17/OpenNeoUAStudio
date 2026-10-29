"""Image and animation writers shared by Snapshot Studio exports."""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QIODevice, QPoint, QRect, QSaveFile, Qt
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QImage, QImageWriter, QPainter)

from studio.exchange.portable import backup_existing_file


# Contact sheet: (label drawn in the cell, canonical view preset), row by row.
CONTACT_SHEET_VIEWS = (
    ("iso", "Isometric Front Right"),
    ("iso_left", "Isometric Front Left"),
    ("iso_back", "Isometric Back Right"),
    ("iso_back_left", "Isometric Back Left"),
    ("left", "Left"),
    ("right", "Right"),
    ("top", "Top"),
    ("bottom", "Bottom"),
)
# 4 columns fit the 8 views with no empty cell: the isometric views on the
# first row, then left, right, top, bottom.
CONTACT_SHEET_COLUMNS = 4
# Used when no Snapshot background is picked: the label text must stay readable,
# which a transparent sheet would not guarantee.
CONTACT_SHEET_BACKGROUND = QColor("#1d2027")
CONTACT_SHEET_LABEL_COLOR = QColor("#c8cbd2")

FORMAT_EXTENSIONS = {
    "png": "png",
    "jpg": "jpg",
    "webp": "webp",
    "gif": "gif",
    "webp_anim": "webp",
}
ANIMATED_FORMATS = frozenset(("gif", "webp_anim"))
ANIMATION_MEMORY_BUDGET_BYTES = 512 * 1024 * 1024


class AnimationExportCancelled(RuntimeError):
    """Raised when an animation export is cancelled between frames."""


def output_extension(format_id: str) -> str:
    try:
        return FORMAT_EXTENSIONS[format_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported snapshot format: {format_id}") from exc


def is_animated_format(format_id: str) -> bool:
    return format_id in ANIMATED_FORMATS


def pillow_supports_static_webp() -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    return Image.registered_extensions().get(".webp") == "WEBP"


def pillow_supports_animated_webp() -> bool:
    try:
        from io import BytesIO

        from PIL import Image, features
    except ImportError:
        return False
    try:
        if not features.check("webp"):
            return False
        first = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
        second = Image.new("RGBA", (1, 1), (0, 0, 255, 255))
        encoded = BytesIO()
        first.save(
            encoded, format="WEBP", save_all=True,
            append_images=[second], duration=[100, 100], loop=0)
        encoded.seek(0)
        with Image.open(encoded) as decoded:
            return bool(getattr(decoded, "is_animated", False)) \
                and getattr(decoded, "n_frames", 1) > 1
    except (OSError, ValueError, KeyError, AttributeError):
        return False


def _to_pillow_rgba(image: QImage):
    from PIL import Image

    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width = rgba.width()
    height = rgba.height()
    stride = rgba.bytesPerLine()
    pixels = bytes(rgba.constBits())
    if stride != width * 4:
        return Image.frombytes(
            "RGBA", (width, height), pixels, "raw", "RGBA", stride, 1)
    return Image.frombytes("RGBA", (width, height), pixels)


def write_static_image_atomic(
        image: QImage, path: Path, format_id: str,
        quality: int = 95) -> None:
    """Write one still image without exposing a partially written target."""

    if image.isNull():
        raise ValueError("Snapshot renderer returned a null image")
    if format_id not in ("png", "jpg", "webp"):
        raise ValueError(f"Unsupported still image format: {format_id}")

    path.parent.mkdir(parents=True, exist_ok=True)
    if format_id == "webp" and pillow_supports_static_webp():
        frame = _to_pillow_rgba(image)
        _save_pillow_atomic(
            frame, path, "WEBP", quality=max(1, min(100, int(quality))))
        return

    backup_existing_file(path)
    target = QSaveFile(str(path))
    if not target.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(target.errorString() or f"Could not open {path}")
    image_format = {
        "jpg": b"jpeg",
        "webp": b"webp",
        "png": b"png",
    }[format_id]
    writer = QImageWriter(target, image_format)
    if format_id != "png":
        writer.setQuality(max(1, min(100, int(quality))))
    if not writer.write(image):
        error = writer.errorString() or f"Could not write {path}"
        target.cancelWriting()
        raise OSError(error)
    del writer
    if not target.commit():
        raise OSError(target.errorString() or f"Could not commit {path}")


def compose_contact_sheet(
        cells: list[tuple[str, QImage]], background: QColor,
        columns: int = CONTACT_SHEET_COLUMNS) -> QImage:
    """Tile labelled views into one image, filling the grid row by row.

    Every cell takes the size of the first image, so the caller renders all
    views at the same size."""

    if not cells or any(image.isNull() for _label, image in cells):
        raise ValueError("Contact sheet needs at least one rendered view")
    cell_width = cells[0][1].width()
    cell_height = cells[0][1].height()
    rows = -(-len(cells) // columns)
    sheet = QImage(
        cell_width * columns, cell_height * rows,
        QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(background)
    painter = QPainter(sheet)
    try:
        font = QFont(QGuiApplication.font())
        font.setPixelSize(max(10, cell_height // 32))
        painter.setFont(font)
        painter.setPen(CONTACT_SHEET_LABEL_COLOR)
        margin = max(4, cell_height // 64)
        for index, (label, image) in enumerate(cells):
            left = (index % columns) * cell_width
            top = (index // columns) * cell_height
            painter.drawImage(QPoint(left, top), image)
            painter.drawText(
                QRect(left + margin, top + margin,
                      cell_width - 2 * margin, cell_height - 2 * margin),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                label)
    finally:
        painter.end()
    return sheet


def animation_frame_count(
        animation_duration_ms: float, fps: int, speed: float,
        duration_seconds: float | None = None) -> int:
    """Return the frame count for one viewport cycle or a fixed duration."""

    fps = int(fps)
    speed = float(speed)
    if not 1 <= fps <= 60:
        raise ValueError("Animation FPS must be between 1 and 60")
    if not math.isfinite(speed) or not 0.05 <= speed <= 8.0:
        raise ValueError("Animation speed must be between 0.05 and 8")
    if duration_seconds is None:
        cycle_ms = float(animation_duration_ms)
        if not math.isfinite(cycle_ms) or cycle_ms <= 0:
            raise ValueError("The selected model has no timed animation cycle")
        output_seconds = cycle_ms / (1000.0 * speed)
    else:
        output_seconds = float(duration_seconds)
        if not math.isfinite(output_seconds) or output_seconds <= 0:
            raise ValueError("Animation duration must be greater than zero")
    return max(1, math.ceil(output_seconds * fps))


def _frame_durations(
        fps: int, count: int, gif: bool,
        total_duration_seconds: float) -> list[int]:
    unit_ms = 10 if gif else 1
    total_units = max(1, round(total_duration_seconds * 1000 / unit_ms))
    durations = []
    for index in range(count):
        start = round(index * 1000.0 / fps / unit_ms)
        end = min(
            total_units,
            round((index + 1) * 1000.0 / fps / unit_ms),
        )
        durations.append(max(1, end - start) * unit_ms)
    return durations


def _gif_palette_frame(frame):
    from PIL import Image

    rgba = frame.convert("RGBA")
    palette_frame = rgba.convert("RGB").quantize(
        colors=255, method=Image.Quantize.MEDIANCUT)
    palette = list(palette_frame.getpalette() or [0] * 768)
    palette[255 * 3:256 * 3] = [0, 0, 0]
    palette_frame.putpalette(palette)
    transparent_pixels = rgba.getchannel("A").point(
        lambda alpha: 255 if alpha < 128 else 0)
    palette_frame.paste(255, mask=transparent_pixels)
    palette_frame.info["transparency"] = 255
    return palette_frame


def _save_pillow_atomic(image, path: Path, image_format: str, **options) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_existing_file(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".part", dir=path.parent)
    os.close(descriptor)
    try:
        image.save(temporary, format=image_format, **options)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export_animation_atomic(
        viewport, path: Path, format_id: str, target_size,
        background, include_guides: bool, fps: int, speed: float,
        quality: int = 95, duration_seconds: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
        memory_budget_bytes: int = ANIMATION_MEMORY_BUDGET_BYTES) -> int:
    """Render from animation time zero and atomically save GIF or WebP."""

    if format_id not in ANIMATED_FORMATS:
        raise ValueError(f"Unsupported animation format: {format_id}")
    if format_id == "webp_anim" and not pillow_supports_animated_webp():
        raise RuntimeError("This Pillow installation cannot write animated WebP")
    if not viewport.has_animation:
        raise ValueError("The selected model has no animation to export")

    width = int(target_size.width())
    height = int(target_size.height())
    if width <= 0 or height <= 0:
        raise ValueError("Animation image dimensions must be greater than zero")
    fps = int(fps)
    cycle_duration_ms = float(viewport.animation_duration_ms)
    frame_count = animation_frame_count(
        viewport.animation_duration_ms, fps, speed, duration_seconds)
    output_duration_seconds = (
        float(duration_seconds)
        if duration_seconds is not None
        else cycle_duration_ms / (1000.0 * float(speed)))
    # Reserve room for the frame pixels and Pillow's encoder copies.
    bytes_per_pixel = 3 if format_id == "gif" else 8
    estimated_bytes = width * height * frame_count * bytes_per_pixel
    if estimated_bytes > memory_budget_bytes:
        estimated_mib = estimated_bytes / (1024 * 1024)
        budget_mib = memory_budget_bytes / (1024 * 1024)
        raise MemoryError(
            f"This animation needs about {estimated_mib:.0f} MiB of frame "
            f"memory; the export limit is {budget_mib:.0f} MiB. Reduce the "
            "image size, FPS, or duration.")

    from PIL import Image

    frames = []
    for frame_index in range(frame_count):
        if cancelled is not None and cancelled():
            raise AnimationExportCancelled("Animation export cancelled")
        time_ms = frame_index * 1000.0 / fps * float(speed)
        viewport.seek_animation(time_ms)
        rendered = viewport.render_snapshot(
            target_size, background, include_guides=include_guides)
        if rendered.isNull():
            raise RuntimeError("Snapshot renderer returned a null animation frame")
        frame = _to_pillow_rgba(rendered)
        frames.append(_gif_palette_frame(frame)
                      if format_id == "gif" else frame)
        if progress is not None:
            progress(frame_index + 1, frame_count)
        if cancelled is not None and cancelled():
            raise AnimationExportCancelled("Animation export cancelled")

    if not frames:
        raise RuntimeError("Animation export produced no frames")
    durations = _frame_durations(
        fps, frame_count, format_id == "gif", output_duration_seconds)
    if format_id == "gif":
        options = {
            "save_all": True,
            "append_images": frames[1:],
            "duration": durations,
            "loop": 0,
            "disposal": 2,
            "transparency": 255,
            "optimize": False,
        }
        _save_pillow_atomic(frames[0], path, "GIF", **options)
    else:
        options = {
            "save_all": True,
            "append_images": frames[1:],
            "duration": durations,
            "loop": 0,
            "quality": max(1, min(100, int(quality))),
            "method": 4,
        }
        _save_pillow_atomic(frames[0], path, "WEBP", **options)
    return frame_count
