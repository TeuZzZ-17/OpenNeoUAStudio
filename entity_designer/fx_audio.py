"""Sound playback of the Destruction FX preview.

The engine plays every ``snd_*`` sample at (file sample rate + pitch) with a
volume of 0..127. A WAV copy with that sample rate is written once to a
temporary folder, so the pitch needs no resampling code and the sound system
of Qt does the rest. The player only follows the preview clock: it starts
the sounds the clock passes, stops looping ones when their state ends, and
stops everything on seek, pause or mute.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

try:  # numpy is already required by the renderers.
    import numpy as _np
except ImportError:  # pragma: no cover - optional
    _np = None

from entity_designer.destruction_fx import SoundEvent

MAX_VOLUME = 127
MAX_SAMPLE_RATE = 192000
# Below this rate the audio devices play nothing audible (slow motion makes
# very low rates), so those sounds are stretched to STANDARD_RATE instead.
MIN_SAMPLE_RATE = 8000
STANDARD_RATE = 44100


def read_wav(path) -> tuple[int, int, bytes] | None:
    """(sample rate, channels, 16-bit PCM bytes) of a WAV file, else None.

    Handles the 8/16/24/32-bit PCM and 32/64-bit float files of the game.
    """

    if _np is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt = pcm = None
    position = 12
    while position + 8 <= len(data):
        name = data[position:position + 4]
        size = struct.unpack_from("<I", data, position + 4)[0]
        body = data[position + 8:position + 8 + size]
        if name == b"fmt ":
            fmt = body
        elif name == b"data":
            pcm = body
        position += 8 + size + (size & 1)
    if fmt is None or pcm is None or len(fmt) < 16:
        return None
    tag, channels, rate, _bytes_rate, _align, bits = struct.unpack_from("<HHIIHH", fmt)
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if channels < 1 or rate < 1:
        return None
    samples = _decode(tag, bits, pcm)
    if samples is None:
        return None
    return rate, channels, samples.tobytes()


def _decode(tag: int, bits: int, pcm: bytes):
    if tag == 1:
        if bits == 8:
            return ((_np.frombuffer(pcm, dtype=_np.uint8).astype(_np.int16) - 128) << 8)
        if bits == 16:
            return _np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype="<i2")
        if bits == 24:
            raw = _np.frombuffer(pcm[:len(pcm) // 3 * 3], dtype=_np.uint8).reshape(-1, 3)
            value = (raw[:, 0].astype(_np.int32) | (raw[:, 1].astype(_np.int32) << 8)
                     | (raw[:, 2].astype(_np.int32) << 16))
            value = _np.where(value >= 1 << 23, value - (1 << 24), value)
            return (value >> 8).astype(_np.int16)
        if bits == 32:
            return (_np.frombuffer(pcm[:len(pcm) // 4 * 4], dtype="<i4") >> 16).astype(_np.int16)
    if tag == 3 and bits in (32, 64):
        dtype = "<f4" if bits == 32 else "<f8"
        size = bits // 8
        values = _np.frombuffer(pcm[:len(pcm) // size * size], dtype=dtype)
        return (_np.clip(values, -1.0, 1.0) * 32767.0).astype(_np.int16)
    return None


def pcm_wav(rate: int, channels: int, pcm: bytes) -> bytes:
    """A canonical 16-bit PCM WAV file."""

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1,
        channels, rate, rate * channels * 2, channels * 2, 16, b"data", len(pcm))
    return header + pcm


def stretch_pcm(rate: int, channels: int, pcm: bytes) -> tuple[int, bytes]:
    """The same sound at STANDARD_RATE, as long as it lasts at ``rate``.

    Linear interpolation: the sound keeps its low pitch of the slow rate.
    """

    frames = _np.frombuffer(pcm[:len(pcm) // (2 * channels) * 2 * channels],
                            dtype="<i2").reshape(-1, channels)
    count = max(1, int(round(len(frames) * STANDARD_RATE / rate)))
    positions = _np.linspace(0, max(0, len(frames) - 1), count)
    columns = [_np.interp(positions, _np.arange(len(frames)), frames[:, c])
               for c in range(channels)]
    stretched = _np.stack(columns, axis=1).round().astype("<i2")
    return STANDARD_RATE, stretched.tobytes()


def sound_volume(volume: int) -> float:
    """Engine volume (clamped to 0..127) as a 0..1 gain."""

    return max(0, min(MAX_VOLUME, volume)) / MAX_VOLUME


class _QtVoice:
    """One QSoundEffect; starts as soon as its file is ready."""

    def __init__(self) -> None:
        from PySide6.QtMultimedia import QSoundEffect

        self._effect = QSoundEffect()
        self._pending = False
        self._effect.statusChanged.connect(self._status_changed)

    def play(self, path: Path, gain: float, loop: bool) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QSoundEffect

        effect = self._effect
        effect.setVolume(gain)
        effect.setLoopCount(QSoundEffect.Loop.Infinite.value if loop else 1)
        self._pending = True
        effect.setSource(QUrl.fromLocalFile(str(path)))
        self._status_changed()

    def _status_changed(self) -> None:
        from PySide6.QtMultimedia import QSoundEffect

        if self._pending and self._effect.status() == QSoundEffect.Status.Ready:
            self._pending = False
            self._effect.play()

    def stop(self) -> None:
        self._pending = False
        self._effect.stop()

    def finished(self) -> bool:
        return not self._pending and not self._effect.isPlaying()


def _qt_voice():
    try:
        from PySide6.QtMultimedia import QMediaDevices

        if not QMediaDevices.audioOutputs():
            return None
        return _QtVoice()
    except (ImportError, RuntimeError):  # pragma: no cover - no audio stack
        return None


class FxSoundPlayer:
    """Plays the sounds of a preview while its clock runs."""

    def __init__(self, resolver=None, voice_factory=None) -> None:
        # resolver(name) -> Path | None finds the sample in the game data.
        self.resolver = resolver
        self.muted = False
        # audio.volume of OpenNeoUA.ini (0..127), the master gain of every sample.
        self.master_volume = MAX_VOLUME
        # Preview speed: the sounds are played at (sample rate + pitch) times
        # this factor, so slow motion lowers them like a slowed tape.
        self.speed = 1.0
        self.missing: set[str] = set()
        self._voice_factory = voice_factory or _qt_voice
        self._folder = None
        self._files: dict[tuple, Path | None] = {}
        self._active: list[tuple[SoundEvent, object]] = []

    # -- files -----------------------------------------------------------------------

    def _file_for(self, event: SoundEvent) -> Path | None:
        """WAV copy of the sample at its (sample rate + pitch) for this play."""

        name = event.spec.sample
        source = self.resolver(name) if self.resolver is not None else None
        if source is None:
            self.missing.add(name)
            return None
        try:
            stat = Path(source).stat()
            version = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            version = None
        # The version keeps a re-saved sample from playing from the old copy.
        key = (str(source), event.pitch, round(self.speed, 4), version)
        if key in self._files:
            return self._files[key]
        wav = read_wav(source)
        result = None
        if wav is not None:
            rate, channels, pcm = wav
            if self._folder is None:
                self._folder = tempfile.TemporaryDirectory(prefix="studio_fx_sound_")
            result = Path(self._folder.name) / f"{len(self._files)}.wav"
            target = max(1, min(MAX_SAMPLE_RATE,
                                int(round((rate + event.pitch) * self.speed))))
            if target < MIN_SAMPLE_RATE and pcm:
                target, pcm = stretch_pcm(target, channels, pcm)
            result.write_bytes(pcm_wav(target, channels, pcm))
        else:
            self.missing.add(name)
        self._files[key] = result
        return result

    # -- playback --------------------------------------------------------------------

    def _start(self, event: SoundEvent) -> None:
        # Palette-only events have nothing to play.
        if self.muted or not event.spec.sample:
            return
        path = self._file_for(event)
        if path is None:
            return
        self._active = [entry for entry in self._active
                        if entry[0].loops or not entry[1].finished()]
        voice = self._voice_factory()
        if voice is None:
            return
        gain = sound_volume(event.spec.volume) * sound_volume(self.master_volume)
        voice.play(path, gain, event.loops)
        self._active.append((event, voice))

    def step(self, events, previous: float, now: float) -> None:
        """Advance from ``previous`` to ``now`` (ms): start and end sounds."""

        for event in events:
            if previous < event.start <= now:
                self._start(event)
        for entry in list(self._active):
            event = entry[0]
            if event.loops and event.end <= now:
                entry[1].stop()
                self._active.remove(entry)

    def resume(self, events, now: float) -> None:
        """Start the loops that are in progress at ``now`` (after a pause)."""

        running = {id(entry[0]) for entry in self._active}
        for event in events:
            if event.loops and event.start <= now < event.end \
                    and id(event) not in running:
                self._start(event)

    def stop_all(self) -> None:
        for _event, voice in self._active:
            voice.stop()
        self._active.clear()

    def set_speed(self, speed: float) -> None:
        """Change the playback speed; sounds already playing are stopped."""

        speed = float(speed) if speed and speed > 0 else 1.0
        if abs(speed - self.speed) > 1e-9:
            self.speed = speed
            self.stop_all()

    def set_muted(self, muted: bool) -> None:
        self.muted = bool(muted)
        if self.muted:
            self.stop_all()

    def close(self) -> None:
        self.stop_all()
        if self._folder is not None:
            self._folder.cleanup()
            self._folder = None
        self._files.clear()
