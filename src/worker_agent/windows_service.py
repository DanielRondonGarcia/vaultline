"""Windows Service Control Manager (SCM) integration for the native worker.

The frozen ``vaultline-worker.exe`` hosts the service itself: the registered
command line is ``"<exe>" service run`` and :func:`run_as_service` connects the
process to the SCM dispatcher (no ``pythonservice.exe`` is involved).

pywin32 is imported lazily inside the functions below so this module stays
importable on Linux, where pywin32 is not installed (tests, Debian build), and
so tests can inject fake modules through ``sys.modules``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, TypeVar

from src.worker_agent import main as worker_main
from src.worker_agent.state_paths import resolve_state_dir

SERVICE_NAME = "VaultlineWorker"
DISPLAY_NAME = "Vaultline Worker"
DESCRIPTION = "Vaultline native backup worker (enroll with: vaultline-worker enroll --control-plane-url <url>)"

START_WAIT_SECONDS = 30
STOP_WAIT_SECONDS = 180
STOP_INITIAL_WAIT_HINT_MS = 120000
STOP_WAIT_HINT_MS = 30000
STOP_WATCHDOG_INTERVAL_SECONDS = 10

# A service process has no console: sys.stderr is None and the StreamHandler
# installed by logging.basicConfig in worker_agent.main drops every record.
# The service therefore logs to a rotating file under the state directory.
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "worker.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Win32 error codes surfaced by pywin32 as ``pywintypes.error.winerror``.
ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_REQUEST_TIMEOUT = 1053
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

# SERVICE_STATUS.dwCurrentState and start-type values from winsvc.h. They are
# part of the Win32 ABI, so they can be mapped without importing pywin32.
_SERVICE_STOPPED = 1
_SERVICE_RUNNING = 4
_STATE_NAMES = {
    1: "stopped",
    2: "start_pending",
    3: "stop_pending",
    4: "running",
    5: "continue_pending",
    6: "pause_pending",
    7: "paused",
}
_START_TYPE_NAMES = {0: "boot", 1: "system", 2: "auto", 3: "manual", 4: "disabled"}

_T = TypeVar("_T")


class ServiceError(RuntimeError):
    """A Service Control Manager operation failed (wraps ``pywintypes.error``)."""

    def __init__(self, message: str, winerror: int | None = None):
        super().__init__(message)
        self.winerror = winerror

    @property
    def access_denied(self) -> bool:
        return self.winerror == ERROR_ACCESS_DENIED

    @property
    def not_installed(self) -> bool:
        return self.winerror == ERROR_SERVICE_DOES_NOT_EXIST


def _guarded(operation: Callable[[], _T]) -> _T:
    """Run a pywin32 call and translate its error type into :class:`ServiceError`.

    ``pywintypes.error`` cannot be named at import time on non-Windows hosts, so
    it is detected structurally through its ``winerror`` attribute.
    """
    try:
        return operation()
    except ServiceError:
        raise
    except Exception as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror is None:
            raise
        detail = getattr(exc, "strerror", None) or str(exc)
        raise ServiceError(f"{detail} (win32 error {winerror})", winerror) from exc


# --- logging ----------------------------------------------------------------


def service_log_file(state_dir: Path | str | None = None) -> Path:
    base = Path(state_dir) if state_dir is not None else resolve_state_dir()
    return base / LOG_DIR_NAME / LOG_FILE_NAME


def _is_console_handler(handler: logging.Handler) -> bool:
    # FileHandler (and RotatingFileHandler) subclass StreamHandler but own
    # their stream; only handlers bound to a console stream are "console".
    return isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)


def configure_service_logging(state_dir: Path | str | None = None) -> Path:
    """Route root logging to ``<state_dir>/logs/worker.log`` and return that path.

    Idempotent: a handler already bound to the same file is reused. Console
    handlers whose stream is gone (or every console handler when ``sys.stderr``
    is ``None``, i.e. under the SCM) are removed so records are not lost to a
    dead stream. Raises ``OSError`` when the log directory or file cannot be
    created; callers decide how to degrade.
    """
    log_file = service_log_file(state_dir)
    root = logging.getLogger()
    target = os.path.abspath(str(log_file))
    existing = next(
        (
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler) and handler.baseFilename == target
        ),
        None,
    )
    if existing is None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_file),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)

    for candidate in list(root.handlers):
        if _is_console_handler(candidate) and (candidate.stream is None or sys.stderr is None):
            root.removeHandler(candidate)
    return log_file


# --- command line -----------------------------------------------------------


def service_command_line() -> list[str]:
    """Executable and arguments the SCM should run to host the worker.

    The source-checkout form is for developers only: ``-m`` resolution needs the
    repository root as the working directory, which the SCM does not provide.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "service", "run"]
    return [sys.executable, "-m", "src.worker_agent.cli", "service", "run"]


def _install_arguments() -> tuple[str, str]:
    """Split the command line the way ``win32serviceutil`` expects it.

    ``InstallService``/``ChangeServiceConfig`` wrap ``exeName`` in double quotes
    themselves, so the executable is passed bare and only the arguments are
    pre-quoted.
    """
    command = service_command_line()
    return command[0], subprocess.list2cmdline(command[1:])


def registered_command_line() -> str:
    """Exact ``ImagePath`` the SCM stores after :func:`install_service`."""
    exe_name, exe_args = _install_arguments()
    return f'"{exe_name}" {exe_args}'


# --- pywin32 boundary -------------------------------------------------------
# Every pywin32 call goes through one of these small functions so tests can
# patch them (or inject fake modules) without touching the real SCM.


def _scm_install(exe_name: str, exe_args: str) -> None:
    import win32service
    import win32serviceutil

    # pythonClassString=None skips the PythonClass registry key used by
    # pythonservice.exe; the frozen executable hosts the service itself.
    win32serviceutil.InstallService(
        None,
        SERVICE_NAME,
        DISPLAY_NAME,
        startType=win32service.SERVICE_AUTO_START,
        exeName=exe_name,
        exeArgs=exe_args,
        description=DESCRIPTION,
    )


def _scm_change_config(exe_name: str, exe_args: str) -> None:
    import win32service
    import win32serviceutil

    win32serviceutil.ChangeServiceConfig(
        None,
        SERVICE_NAME,
        startType=win32service.SERVICE_AUTO_START,
        exeName=exe_name,
        displayName=DISPLAY_NAME,
        exeArgs=exe_args,
        description=DESCRIPTION,
    )


def _scm_remove() -> None:
    import win32serviceutil

    win32serviceutil.RemoveService(SERVICE_NAME)


def _scm_start() -> None:
    import win32serviceutil

    win32serviceutil.StartService(SERVICE_NAME)


def _scm_stop() -> None:
    import win32serviceutil

    win32serviceutil.StopService(SERVICE_NAME)


def _scm_wait_for_state(state: int, seconds: int) -> None:
    import win32serviceutil

    win32serviceutil.WaitForServiceStatus(SERVICE_NAME, state, seconds)


def _scm_query_state() -> int:
    import win32serviceutil

    return int(win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1])


def _scm_query_start_type() -> int:
    import win32service

    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_CONFIG)
        try:
            return int(win32service.QueryServiceConfig(handle)[1])
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(manager)


# --- lifecycle --------------------------------------------------------------


def _current_state() -> int | None:
    """Return the SCM state code, or ``None`` when the service is not installed."""
    try:
        return _guarded(_scm_query_state)
    except ServiceError as exc:
        if exc.not_installed:
            return None
        raise


def is_installed() -> bool:
    return _current_state() is not None


def install_service() -> dict[str, Any]:
    """Register (or update) the auto-start LocalSystem service; idempotent."""
    exe_name, exe_args = _install_arguments()
    if is_installed():
        _guarded(lambda: _scm_change_config(exe_name, exe_args))
        action = "updated"
    else:
        _guarded(lambda: _scm_install(exe_name, exe_args))
        action = "installed"
    return {"name": SERVICE_NAME, "action": action, "command_line": f'"{exe_name}" {exe_args}'}


def start_service() -> dict[str, Any]:
    try:
        _guarded(_scm_start)
    except ServiceError as exc:
        if exc.winerror != ERROR_SERVICE_ALREADY_RUNNING:
            raise
    _guarded(lambda: _scm_wait_for_state(_SERVICE_RUNNING, START_WAIT_SECONDS))
    return service_status()


def stop_service() -> dict[str, Any]:
    try:
        _guarded(_scm_stop)
    except ServiceError as exc:
        if exc.winerror != ERROR_SERVICE_NOT_ACTIVE:
            raise
    _guarded(lambda: _scm_wait_for_state(_SERVICE_STOPPED, STOP_WAIT_SECONDS))
    return service_status()


def uninstall_service() -> dict[str, Any]:
    state = _current_state()
    if state is None:
        return {"name": SERVICE_NAME, "action": "not_installed"}
    if state != _SERVICE_STOPPED:
        stop_service()
    _guarded(_scm_remove)
    return {"name": SERVICE_NAME, "action": "removed"}


def service_status() -> dict[str, Any]:
    """Describe the service without raising when it is not installed."""
    state = _current_state()
    if state is None:
        return {"name": SERVICE_NAME, "installed": False, "state": "not_installed", "start_type": None}
    start_type = _guarded(_scm_query_start_type)
    return {
        "name": SERVICE_NAME,
        "installed": True,
        "state": _STATE_NAMES.get(state, f"unknown_{state}"),
        "start_type": _START_TYPE_NAMES.get(start_type, f"unknown_{start_type}"),
        "log_file": str(service_log_file()),
    }


# --- service host -----------------------------------------------------------


def build_service_class() -> type:
    """Create the ``ServiceFramework`` subclass; deferred so pywin32 loads lazily."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class VaultlineWorkerService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.worker_done = threading.Event()
            self.stop_requested = False

        def SvcStop(self):
            self.stop_requested = True
            self.ReportServiceStatus(
                win32service.SERVICE_STOP_PENDING,
                waitHint=STOP_INITIAL_WAIT_HINT_MS,
                checkPoint=1,
            )
            # The worker main loop checks this between iterations; a job in
            # flight finishes its current loop iteration before main() returns.
            worker_main.request_stop()
            win32event.SetEvent(self.stop_event)

            def report_progress():
                checkpoint = 1
                while not self.worker_done.wait(STOP_WATCHDOG_INTERVAL_SECONDS):
                    checkpoint += 1
                    self.ReportServiceStatus(
                        win32service.SERVICE_STOP_PENDING,
                        waitHint=STOP_WAIT_HINT_MS,
                        checkPoint=checkpoint,
                    )

            threading.Thread(target=report_progress, daemon=True, name="worker-stop-watchdog").start()

        def SvcDoRun(self):
            servicemanager.LogInfoMsg(f"{DISPLAY_NAME} starting")
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            # Logging must never take the service down; without the file the
            # worker still runs and the Event Log records where it should have gone.
            try:
                log_file = configure_service_logging()
            except Exception as exc:  # noqa: BLE001
                servicemanager.LogErrorMsg(
                    f"{DISPLAY_NAME} could not open its log file ({exc.__class__.__name__}): {exc}; "
                    "continuing without file logging"
                )
            else:
                servicemanager.LogInfoMsg(f"{DISPLAY_NAME} logging to {log_file}")
            os.environ["WORKER_RUNTIME"] = "native"
            os.environ["WORKER_RUN_ONCE"] = "false"
            os.environ["WORKER_SERVICE_MODE"] = "true"
            worker_main.reset_stop()
            try:
                worker_main.main()
            except Exception as exc:  # noqa: BLE001 - the SCM host must not see a traceback
                servicemanager.LogErrorMsg(
                    f"{DISPLAY_NAME} stopped after an unhandled error ({exc.__class__.__name__}): {exc}"
                )
                if not self.stop_requested:
                    self.ReportServiceStatus(
                        win32service.SERVICE_STOPPED,
                        win32ExitCode=getattr(win32service, "ERROR_SERVICE_SPECIFIC_ERROR", 1066),
                        svcExitCode=1,
                    )
            else:
                servicemanager.LogInfoMsg(f"{DISPLAY_NAME} stopped")
            finally:
                self.worker_done.set()

    return VaultlineWorkerService


def run_as_service() -> None:
    """Entry point of ``vaultline-worker.exe service run`` when launched by the SCM.

    Blocks until the service stops. From an interactive console the dispatcher
    cannot connect (win32 error 1063) and a :class:`ServiceError` is raised.
    """
    import servicemanager

    service_class = build_service_class()

    def _dispatch() -> None:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(service_class)
        servicemanager.StartServiceCtrlDispatcher()

    _guarded(_dispatch)
