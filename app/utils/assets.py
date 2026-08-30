"""Bundled asset lookup (logo, icons, fonts).

Paths resolve both from the source tree and from a PyInstaller one-file bundle,
where data files are unpacked into `sys._MEIPASS`.
"""

from __future__ import annotations

import struct
import sys
from functools import lru_cache
from pathlib import Path

from app.utils.logger import get_logger
from app.utils.platform import app_root

log = get_logger("utils.assets")

LOGO_FILENAME = "app_logo.png"
ICON_FILENAME = "app_icon.ico"


def assets_dir() -> Path:
    """Directory holding bundled assets, wherever the app is running from."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "assets"
        if candidate.exists():
            return candidate
    return app_root() / "assets"


def asset_path(*parts: str) -> Path:
    return assets_dir().joinpath(*parts)


def logo_path() -> Path | None:
    path = asset_path(LOGO_FILENAME)
    return path if path.exists() else None


def icon_path() -> Path | None:
    path = asset_path("icons", ICON_FILENAME)
    return path if path.exists() else None


@lru_cache(maxsize=8)
def _load_pixmap(path_str: str, size: int):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    pixmap = QPixmap(path_str)
    if pixmap.isNull():
        return None
    if size:
        pixmap = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap


def logo_pixmap(size: int = 64):
    """Scaled logo pixmap, or None when the asset is missing."""
    path = logo_path()
    if path is None:
        return None
    return _load_pixmap(str(path), size)


def app_icon():
    """QIcon for the window and taskbar, built from whichever asset exists."""
    from PySide6.QtGui import QIcon

    ico = icon_path()
    if ico is not None:
        return QIcon(str(ico))
    logo = logo_path()
    if logo is not None:
        return QIcon(str(logo))
    return QIcon()


# ---------------------------------------------------------------------------
# ICO generation
# ---------------------------------------------------------------------------

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_ico(source: Path | None = None, destination: Path | None = None,
              sizes: tuple[int, ...] = ICO_SIZES) -> Path | None:
    """Write a multi-resolution .ico from the PNG logo.

    Windows needs an .ico for the executable and taskbar. Rather than adding an
    image library, the container is assembled directly: an ICO is a small
    header followed by embedded PNG payloads, which modern Windows accepts.
    """
    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QImage

    source = source or logo_path()
    if source is None or not Path(source).exists():
        log.warning("No logo asset to build an icon from")
        return None
    destination = destination or asset_path("icons", ICON_FILENAME)

    image = QImage(str(source))
    if image.isNull():
        log.warning("Could not read logo image %s", source)
        return None

    payloads: list[tuple[int, bytes]] = []
    for size in sizes:
        scaled = image.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # The QByteArray must outlive the QBuffer that writes into it, so keep
        # a Python reference rather than passing a temporary.
        storage = QByteArray()
        buffer = QBuffer(storage)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        saved = scaled.save(buffer, "PNG")
        buffer.close()
        if not saved:
            log.warning("Could not encode %spx icon frame", size)
            continue
        payloads.append((size, bytes(storage.data())))

    if not payloads:
        return None

    header = struct.pack("<HHH", 0, 1, len(payloads))  # reserved, type=icon, count
    offset = len(header) + 16 * len(payloads)
    entries = bytearray()
    body = bytearray()
    for size, data in payloads:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # width (0 means 256)
            0 if size >= 256 else size,  # height
            0,      # palette size
            0,      # reserved
            1,      # colour planes
            32,     # bits per pixel
            len(data),
            offset,
        )
        body += data
        offset += len(data)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(header + bytes(entries) + bytes(body))
    except OSError as exc:
        log.error("Could not write icon %s: %s", destination, exc)
        return None
    log.info("Icon written to %s (%s sizes)", destination, len(payloads))
    return destination


def ensure_icon() -> Path | None:
    """Build the .ico on first run if the logo is present and it is missing."""
    existing = icon_path()
    if existing is not None:
        return existing
    if logo_path() is None:
        return None
    return build_ico()


if __name__ == "__main__":  # pragma: no cover - build helper
    # `python -m app.utils.assets` regenerates the .ico from the PNG logo.
    import sys

    from PySide6.QtWidgets import QApplication

    QApplication(sys.argv)
    result = build_ico()
    print(f"Icon written to {result}" if result else "No logo asset found")
