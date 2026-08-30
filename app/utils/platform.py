"""Platform detection and platform-specific helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


def platform_name() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    if IS_LINUX:
        return "Linux"
    return sys.platform


def no_window_kwargs() -> dict:
    """Subprocess kwargs that suppress console windows on Windows."""
    if not IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def run_command(
    args: list[str],
    timeout: float = 10.0,
    encoding: str | None = None,
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr).

    Never raises for a non-zero exit code; a missing binary or timeout is
    reported as returncode -1 / -2 with the reason in stderr.
    """
    if encoding is None:
        encoding = "utf-8"
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        return -1, "", f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"command timed out after {timeout}s: {' '.join(args)}"
    except OSError as exc:  # pragma: no cover - defensive
        return -3, "", str(exc)

    out = proc.stdout.decode(encoding, errors="replace")
    err = proc.stderr.decode(encoding, errors="replace")
    return proc.returncode, out, err


def has_command(name: str) -> bool:
    return shutil.which(name) is not None


def app_root() -> Path:
    """Root directory of the application source tree."""
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    """Writable directory for the database, config and logs.

    Uses a directory next to the source tree when writable (portable mode),
    otherwise falls back to the per-user application data directory.
    """
    portable = app_root() / "data"
    try:
        portable.mkdir(parents=True, exist_ok=True)
        probe = portable / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return portable
    except OSError:
        pass

    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / "InternetOverwatch"
    target.mkdir(parents=True, exist_ok=True)
    return target


def log_dir() -> Path:
    d = user_data_dir().parent / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:  # pragma: no cover - defensive
        d = user_data_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def open_in_file_manager(path: Path) -> bool:
    """Reveal a file or folder in the OS file manager. Returns success."""
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False
