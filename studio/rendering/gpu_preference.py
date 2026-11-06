"""Ask Windows to run Studio on the graphics card, not on the built-in chip.

The engine does this by exporting two names from OpenNeoUA.exe, which the
NVIDIA and AMD drivers read when the process starts. Studio cannot: it runs
under Python, and those names only count when they are in the program itself
(tested: a library loaded into the process is ignored).

Windows offers the same result for any program through the per-application
graphics preference, the one the Settings page writes. It lives in the current
user's own registry, needs no administrator, and the user can change it back at
any time in Settings, System, Display, Graphics.

Studio writes it once, for its own executable, and only when:
  - it is running as the built program (never for python.exe, because that
    would decide for every other Python program on the computer);
  - the chip it got is a built-in one;
  - Windows has no preference for that program yet, so a choice the user has
    already made is never overwritten.

The preference is read when a program starts, so it takes effect the next time
Studio is opened.
"""

from __future__ import annotations

import sys

# What the Settings page stores for "High performance".
REGISTRY_PATH = r"Software\Microsoft\DirectX\UserGpuPreferences"
HIGH_PERFORMANCE = "GpuPreference=2;"

APPLIED = "applied"            # Written now; it works from the next start.
ALREADY_SET = "already_set"    # Windows already had a choice for this program.
NOT_FROZEN = "not_frozen"      # Running from the sources, under python.exe.
UNSUPPORTED = "unsupported"    # Not Windows, or the registry refused.


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


def prefer_graphics_card(*, on_integrated: bool) -> str:
    """Ask Windows for the graphics card for this program. Returns what happened.

    `on_integrated` says whether the chip Studio is drawing with right now is a
    built-in one. Nothing is written when it is already on a graphics card.
    """

    if sys.platform != "win32":
        return UNSUPPORTED
    if not on_integrated:
        return ALREADY_SET
    if not running_as_program():
        return NOT_FROZEN

    path = program_path()
    if read_preference(path) is not None:
        return ALREADY_SET
    return APPLIED if write_preference(path) else UNSUPPORTED


def message(result: str, chip: str = "") -> str:
    """One short line for the log, or an empty string when there is nothing to say."""

    chip_name = chip or "the chip built into the processor"
    if result == APPLIED:
        return (f"Studio is drawing with {chip_name}. Windows has now been asked to use "
                f"the graphics card for {program_path()}: close Studio and open it again "
                "to get it.")
    if result == NOT_FROZEN:
        return (f"Studio is drawing with {chip_name}. Running from the sources, so the "
                "choice is not made for you: in Windows Settings, System, Display, "
                "Graphics, add python.exe and pick High performance.")
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
