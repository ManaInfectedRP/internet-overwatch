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


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than from source."""
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path | None:
    """PyInstaller's temporary extraction directory, if we are inside one.

    Anything written here is destroyed when the process exits, so it must never
    be used for the database, settings or logs.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def app_root() -> Path:
    """Directory the application is installed in.

    From source this is the repository root. From a frozen build it is the
    folder holding the .exe - not the temporary extraction directory, which
    `__file__` would otherwise point into.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _is_writable(directory: Path) -> bool:
    """Whether we can actually create files in `directory`."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _platform_data_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "InternetOverwatch"


def user_data_dir() -> Path:
    """Writable directory for the database, config and logs.

    Prefers a `data/` folder beside the application (portable mode) and falls
    back to the per-user application-data directory when that is read-only,
    which is the normal case for an install under Program Files.
    """
    portable = app_root() / "data"
    bundle = bundle_dir()
    # Guard against the portable path resolving inside the bundle's temporary
    # extraction directory, which is wiped when the process exits.
    inside_bundle = bundle is not None and (
        portable == bundle or bundle in portable.parents
    )
    if not inside_bundle and _is_writable(portable):
        return portable

    target = _platform_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def log_dir() -> Path:
    """Directory for application logs, alongside the data directory."""
    data = user_data_dir()
    # In portable mode data/ sits under the app root, so logs/ becomes its
    # sibling. Otherwise keep logs inside the per-user data directory rather
    # than scattering them into its parent.
    candidate = app_root() / "logs" if data.parent == app_root() else data / "logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:  # pragma: no cover - defensive
        fallback = _platform_data_dir() / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


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
