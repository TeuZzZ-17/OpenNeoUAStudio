"""Ask Windows to run Studio on the graphics card, not on the built-in chip.

The engine does this by exporting two names from OpenNeoUA.exe, which the
NVIDIA and AMD drivers read when the process starts. Studio cannot: it runs
under Python, and those names only count when they are in the program itself
(tested: a library loaded into the process is ignored).

Windows offers the same result for any program through the per-application
graphics preference, the one the Settings page writes. It lives in the current
user's own registry, needs no administrator, and the user can change it back at
any time in Settings, System, Display, Graphics.

The preference belongs to one exact executable path, so every way of starting
Studio needs its own: the built program, a copy in dist, or python.exe when it
runs from the sources. Studio writes it for whatever is running it, only when:
  - the chip it got is a built-in one and the computer really has a card;
  - nobody chose "Power saving" for that program: that choice is respected.

The preference is read when a program starts, so Studio then starts itself
again, once, to land on the card straight away (see `restart_on_card`).
"""

from __future__ import annotations

import sys
import os
import re
import subprocess
from pathlib import Path

# What the Settings page stores for "High performance".
REGISTRY_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"
HIGH_PERFORMANCE = "GpuPreference=2;"
POWER_SAVING = "GpuPreference=1;"

APPLIED = "applied"            # Written now; it works from the next start.
ALREADY_SET = "already_set"    # Windows already had a choice for this program.
NO_CARD = "no_card"            # This computer has only the built-in chip.
UNSUPPORTED = "unsupported"    # Not Windows, or the registry refused.

# Set on the restarted copy, so a card that stays out of reach cannot start a loop.
RESTART_GUARD = "OPENNEOUA_GPU_RESTARTED"
_GPU_FIELD = re.compile(r"GpuPreference=(\d+);?")


def running_as_program() -> bool:
    """True when Studio runs as the built executable, not under python.exe."""

    return bool(getattr(sys, "frozen", False))


def program_path() -> str:
    """Full path of the executable Windows decides the graphics chip for."""

    return sys.executable or ""


def read_preference(path: str):
    """The preference Windows holds for that program, or None if it has none."""

    if sys.platform != "win32" or not path:
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, path)
            return value
    except OSError:
        return None


def chosen_preference(value) -> int:
    """The GpuPreference number in a stored value; 0 means Windows decides.

    Settings keeps other options in the same value, for example
    "SwapEffectUpgradeEnable=1;GpuPreference=2;".
    """

    match = _GPU_FIELD.search(value or "")
    return int(match[1]) if match else 0


def with_high_performance(value) -> str:
    """Ask for the card, keeping any other option stored in the same value."""

    value = value or ""
    if _GPU_FIELD.search(value):
        return _GPU_FIELD.sub(HIGH_PERFORMANCE, value)
    return value + HIGH_PERFORMANCE


def write_preference(path: str, value: str = HIGH_PERFORMANCE) -> bool:
    """Store the preference for that program, and check that it stuck.

    Writing can report success and leave nothing behind, so the value is read
    back: only a preference that is really there is reported as written.
    """

    if sys.platform != "win32" or not path:
        return False
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, path, 0, winreg.REG_SZ, value)
    except OSError:
        return False

    return read_preference(path) == value


def prefer_graphics_card(*, on_integrated: bool, has_card: bool = True) -> str:
    """Ask Windows for the graphics card for this program. Returns what happened.

    `on_integrated` says whether the chip Studio is drawing with right now is a
    built-in one, `has_card` whether the computer has anything better. Nothing
    is written when it is already on a graphics card.
    """

    if sys.platform != "win32":
        return UNSUPPORTED
    if not on_integrated:
        return ALREADY_SET
    if not has_card:
        return NO_CARD

    path = program_path()
    stored = read_preference(path)
    if chosen_preference(stored):
        # Power saving was somebody's choice; High performance is already asked.
        return ALREADY_SET
    return APPLIED if write_preference(path, with_high_performance(stored)) else UNSUPPORTED


def restart_on_card() -> bool:
    """Start a new copy of Studio, which Windows puts on the card. True if it started.

    The built program unpacks itself into a temporary folder that disappears
    with this process, so the copy must unpack its own.
    """

    if os.environ.get(RESTART_GUARD):
        return False
    command = ([sys.executable, *sys.argv[1:]] if running_as_program()
               else [sys.executable, *sys.orig_argv[1:]])
    environment = {**os.environ, RESTART_GUARD: "1",
                   "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    try:
        subprocess.Popen(command, env=environment, close_fds=True)
    except OSError:
        return False
    return True


def message(result: str, chip: str = "") -> str:
    """One short line for the log, or an empty string when there is nothing to say."""

    chip_name = chip or "the chip built into the processor"
    if result == APPLIED:
        return (f"Studio was drawing with {chip_name}. Windows has now been asked to use "
                f"the graphics card for {program_path()}: Studio restarts to get it.")
    if result == UNSUPPORTED:
        return (f"Studio is drawing with {chip_name}, and the graphics card could not be "
                "chosen for you. On Windows do it in Settings, System, Display, Graphics; "
                "on Linux start Studio with DRI_PRIME=1 (AMD/Intel) or prime-run (NVIDIA).")
    return ""


# ----------------------------------------------------------------------------
# Linux
#
# There is no equivalent of the Windows per-application setting, but the choice
# is made inside the process: the NVIDIA library looks at two variables when
# the first context is created, which is what the `prime-run` command sets.
# Studio can set them itself, before Qt builds any context. Same rules as the
# engine does in System::Init.
#
# Mesa (AMD and Intel) uses DRI_PRIME instead, and which card its number picks
# depends on the computer, so that one is left to the person.
# ----------------------------------------------------------------------------

NVIDIA_OFFLOAD = {
    "__NV_PRIME_RENDER_OFFLOAD": "1",
    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
}

_asked_nvidia = False


def nvidia_driver_loaded() -> bool:
    """True when the NVIDIA driver is running on this computer."""

    import os

    return os.path.exists("/proc/driver/nvidia/version") or os.path.exists("/dev/nvidia0")


def ask_nvidia_offload() -> bool:
    """Ask the NVIDIA library for the graphics card. True when anything changed.

    Never touches a choice already made by whoever started Studio, and does
    nothing when the driver is not there.
    """

    global _asked_nvidia
    import os

    if not sys.platform.startswith("linux") or not nvidia_driver_loaded():
        return False
    if all(name in os.environ for name in NVIDIA_OFFLOAD):
        return False

    for name, value in NVIDIA_OFFLOAD.items():
        os.environ.setdefault(name, value)
    _asked_nvidia = True
    return True


def stop_asking_nvidia_offload() -> bool:
    """Undo the request, for when no context can be made with it."""

    global _asked_nvidia
    import os

    if not _asked_nvidia:
        return False
    for name in NVIDIA_OFFLOAD:
        os.environ.pop(name, None)
    _asked_nvidia = False
    return True


def asked_nvidia_offload() -> bool:
    return _asked_nvidia


def linux_graphics_devices(drm_root=Path('/sys/class/drm')):
    """List PCI display devices exposed by DRM, without inventing GPU names."""
    if not sys.platform.startswith('linux'):
        return []
    devices = []
    seen = set()
    for card in sorted(Path(drm_root).glob('card[0-9]*')):
        if not re.fullmatch(r'card\d+', card.name):
            continue
        try:
            device = (card / 'device').resolve(strict=True)
            bus = device.name
            if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', bus):
                continue
            vendor = (device / 'vendor').read_text().strip().lower()
        except OSError:
            continue
        if bus in seen:
            continue
        seen.add(bus)
        devices.append((bus, vendor))
    return devices


def apply_linux_device_preference(value):
    """Apply an explicit saved PCI choice before Qt creates its first context."""
    if not sys.platform.startswith('linux') or not value:
        return False
    parts = str(value).split('|')
    if len(parts) != 2:
        return False
    bus, vendor = parts
    if (bus, vendor) not in linux_graphics_devices():
        return False
    if vendor == '0x10de':
        os.environ.update(NVIDIA_OFFLOAD)
        os.environ.pop('__NV_PRIME_RENDER_OFFLOAD_PROVIDER', None)
        os.environ.pop('DRI_PRIME', None)
    else:
        for name in NVIDIA_OFFLOAD:
            os.environ.pop(name, None)
        os.environ.pop('__NV_PRIME_RENDER_OFFLOAD_PROVIDER', None)
        os.environ['DRI_PRIME'] = 'pci-' + bus.replace(':', '_').replace('.', '_')
    return True


def windows_graphics_devices():
    """Names of the display adapters Windows knows, without duplicates.

    A card with no screen attached is listed too, which is the usual state of
    the NVIDIA chip in a laptop. This asks the system directly, so it is
    instant and safe to call while a menu opens.
    """

    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class DisplayDevice(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    device = DisplayDevice()
    device.cb = ctypes.sizeof(device)
    names = []
    index = 0
    while ctypes.windll.user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
        if device.DeviceString and device.DeviceString not in names:
            names.append(device.DeviceString)
        index += 1
    return names


def configure_before_qt():
    """Restore the user's Linux GPU selection before any OpenGL context exists."""
    if not sys.platform.startswith('linux'):
        return
    from PySide6.QtCore import QSettings
    saved = QSettings('OpenNeoUA', 'Studio').value('gpu/linux_device', '')
    if not apply_linux_device_preference(saved):
        ask_nvidia_offload()
