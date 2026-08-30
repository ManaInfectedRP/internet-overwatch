"""Application entry point.

Boots logging, settings and storage, runs first-run setup when needed, then
hands control to Qt. A `--simulate` flag starts the app in synthetic-data mode,
and `--headless` runs monitoring with no GUI for quick command-line checks.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from app import __version__
from app.config.defaults import APP_ID, APP_NAME
from app.config.settings import Settings, get_settings, set_settings
from app.core.simulator import SCENARIOS
from app.storage.database import get_database
from app.storage.repository import Repository
from app.utils.assets import ensure_icon
from app.utils.logger import get_logger, setup_logging
from app.utils.platform import IS_WINDOWS

log = get_logger("main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="internet-overwatch",
        description=f"{APP_NAME} - network monitoring and lag diagnostics",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--simulate", metavar="SCENARIO", choices=SCENARIOS,
                        help="start in synthetic test mode: " + ", ".join(SCENARIOS))
    parser.add_argument("--headless", action="store_true",
                        help="run monitoring without a GUI and print a summary")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="headless run length in seconds (default: 30)")
    parser.add_argument("--minimised", "--minimized", dest="minimised",
                        action="store_true",
                        help="start hidden in the system tray (used by autostart)")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--reset-settings", action="store_true",
                        help="restore default settings before starting")
    return parser.parse_args(argv)


def _configure_console() -> None:
    """Make stdout/stderr UTF-8 so status symbols never crash a Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - already fine
            pass


def bootstrap(args: argparse.Namespace) -> tuple[Settings, Repository]:
    """Shared start-up for GUI and headless modes."""
    setup_logging("DEBUG" if args.debug else "INFO")
    log.info("%s %s starting", APP_NAME, __version__)

    settings = Settings.load()
    if args.reset_settings:
        settings = Settings()
        settings.save()
    if args.debug:
        settings.advanced.debug_logging = True
    setup_logging(settings.advanced.log_level)
    set_settings(settings)

    database = get_database(settings.storage.resolved_database_path())
    repository = Repository(database)
    repository.close_orphan_sessions()
    repository.apply_retention(settings.storage.retention_days)
    return settings, repository


def run_headless(args: argparse.Namespace) -> int:
    """Monitor from the command line and print a report (no Qt required)."""
    _configure_console()
    settings, repository = bootstrap(args)

    from app.core.monitor import Monitor
    from app.network.gateway import detect_gateway
    from app.services.report_service import build_session_report, context_from_monitor

    gateway = detect_gateway()
    targets = repository.ensure_default_targets(gateway.address)
    enabled = [t for t in targets if t.enabled]
    if not enabled:
        print("No enabled targets to monitor. Add one first.")
        return 1

    monitor = Monitor(repository, settings)
    monitor.on_incident.append(
        lambda incident: print(
            f"  ! {incident.severity.label} incident on {incident.target_name}: "
            f"{incident.peak_latency_ms:.0f} ms peak"
        )
    )

    stop = {"requested": False}

    def handle_signal(_sig, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, handle_signal)

    print(f"Monitoring {len(enabled)} target(s) for {args.duration:.0f}s. Ctrl+C to stop.")
    monitor.start(enabled)
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline and not stop["requested"]:
            time.sleep(0.25)
    finally:
        monitor.stop()

    print()
    print(build_session_report(context_from_monitor(monitor)))
    repository.flush()
    return 0


def run_gui(args: argparse.Namespace) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog

    # High-DPI rounding policy must be set before the QApplication exists.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    settings, repository = bootstrap(args)

    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    application.setApplicationDisplayName(APP_NAME)
    application.setApplicationVersion(__version__)
    application.setOrganizationName(APP_NAME)
    application.setDesktopFileName(APP_ID)

    if IS_WINDOWS:
        # Without an explicit AppUserModelID Windows groups the window under
        # the Python interpreter and shows its icon in the taskbar.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # pragma: no cover - cosmetic only
            pass

    ensure_icon()
    from app.ui.main_window import MainWindow, apply_theme
    from app.utils.assets import app_icon

    application.setWindowIcon(app_icon())
    apply_theme(application, settings.appearance.scale)

    from app.services.monitoring_service import MonitoringService

    service = MonitoringService(repository, settings)

    # First run: detect the network and let the user choose what to monitor.
    if not settings.first_run_completed and not repository.list_targets():
        from app.ui.pages.first_run import FirstRunDialog

        wizard = FirstRunDialog()
        wizard.setWindowIcon(app_icon())
        if wizard.exec() == QDialog.DialogCode.Accepted:
            for target in wizard.selected_targets():
                repository.add_target(target)
        settings.first_run_completed = True
        settings.save()

    if not repository.list_targets():
        service.ensure_targets()

    window = MainWindow(service)
    if (args.minimised or settings.monitoring.start_minimised) and window.tray is not None:
        # Only hide when there is a tray icon to restore from, otherwise the
        # app would be running with no way to reach it.
        window.hide()
        window.tray.showMessage(
            APP_NAME, "Monitoring in the background. Click the tray icon to open.",
            window.tray.MessageIcon.Information, 4000,
        )
    else:
        window.show()

    if args.simulate:
        service.start_simulation(args.simulate)
        window.refresh_targets()
    elif settings.monitoring.auto_start_monitoring:
        if repository.list_targets(enabled_only=True):
            service.start()
        window._update_controls()

    # Let Ctrl+C in a terminal close the app rather than being swallowed by Qt.
    signal.signal(signal.SIGINT, lambda *_: application.quit())

    exit_code = application.exec()
    service.shutdown()
    log.info("%s exited with code %s", APP_NAME, exit_code)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.headless:
        return run_headless(args)
    try:
        return run_gui(args)
    except Exception as exc:  # pragma: no cover - top-level safety net
        log.critical("Fatal error: %s", exc, exc_info=True)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, f"{APP_NAME} - fatal error",
                    f"{APP_NAME} could not continue:\n\n{exc}",
                )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
