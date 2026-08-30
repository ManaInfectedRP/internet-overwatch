"""Start-with-the-system support (plan section 84, phase 9).

Windows uses the per-user Run key, which needs no elevation and is trivial to
remove. Linux uses an XDG autostart desktop entry and macOS a LaunchAgent.
Every function reports success rather than raising, because failing to register
autostart must never stop the app from running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.config.defaults import APP_ID, APP_NAME
from app.utils.logger import get_logger
from app.utils.platform import IS_LINUX, IS_MACOS, IS_WINDOWS

log = get_logger("utils.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ENTRY_NAME = "InternetOverwatch"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return getattr(sys, "frozen", False)


def launch_command(minimised: bool = False) -> str:
    """Command line that starts the app the same way it is running now."""
    if is_frozen():
        command = f'"{sys.executable}"'
    else:
        entry = Path(sys.argv[0]).resolve()
        if entry.suffix == ".py":
            command = f'"{sys.executable}" "{entry}"'
        else:
            # Started via `python -m app.main`.
            command = f'"{sys.executable}" -m app.main'
    if minimised:
        command += " --minimised"
    return command


# ---------------------------------------------------------------- Windows ---


def _windows_set(enabled: bool) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, ENTRY_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.warning("Could not update the autostart registry entry: %s", exc)
        return False


def _windows_get() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, ENTRY_NAME)
        return True
    except (FileNotFoundError, OSError):
        return False


# ------------------------------------------------------------------ Linux ---


def _linux_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / f"{APP_ID}.desktop"


def _linux_set(enabled: bool) -> bool:
    path = _linux_path()
    try:
        if not enabled:
            path.unlink(missing_ok=True)
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={launch_command()}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        log.warning("Could not write the autostart entry: %s", exc)
        return False


# ------------------------------------------------------------------ macOS ---


def _macos_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.{APP_ID}.plist"


def _macos_set(enabled: bool) -> bool:
    path = _macos_path()
    try:
        if not enabled:
            path.unlink(missing_ok=True)
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        arguments = "".join(
            f"        <string>{part}</string>\n"
            for part in launch_command().replace('"', "").split()
        )
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>Label</key>\n"
            f"    <string>com.{APP_ID}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{arguments}"
            "    </array>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "</dict>\n"
            "</plist>\n",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        log.warning("Could not write the LaunchAgent: %s", exc)
        return False


# ----------------------------------------------------------------- public ---


def supported() -> bool:
    return IS_WINDOWS or IS_LINUX or IS_MACOS


def is_enabled() -> bool:
    """Whether the app is currently registered to start with the system."""
    try:
        if IS_WINDOWS:
            return _windows_get()
        if IS_LINUX:
            return _linux_path().exists()
        if IS_MACOS:
            return _macos_path().exists()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Autostart check failed: %s", exc)
    return False


def set_enabled(enabled: bool) -> bool:
    """Register or unregister autostart. Returns whether it succeeded."""
    try:
        if IS_WINDOWS:
            result = _windows_set(enabled)
        elif IS_LINUX:
            result = _linux_set(enabled)
        elif IS_MACOS:
            result = _macos_set(enabled)
        else:  # pragma: no cover - unsupported platform
            return False
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not change the autostart setting: %s", exc)
        return False
    if result:
        log.info("Autostart %s", "enabled" if enabled else "disabled")
    return result
