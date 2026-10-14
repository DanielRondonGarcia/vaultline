"""Windows SCM integration tests with pywin32 replaced by in-memory fakes.

No test here touches the real Service Control Manager; every pywin32 module the
worker imports lazily is injected into ``sys.modules`` so the suite runs on
Linux CI and on unelevated Windows hosts alike.
"""

import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import Mock, patch


class FakeWin32Error(Exception):
    """Mirror of ``pywintypes.error``: ``(winerror, funcname, strerror)``."""

    def __init__(self, winerror, funcname="ScmCall", strerror="scm failure"):
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror
        self.funcname = funcname
        self.strerror = strerror


class FakeServiceFramework:
    def __init__(self, args):
        self.args = args
        self.reported_statuses = []
        self.reported_status_details = []

    def ReportServiceStatus(self, status, *_args, **_kwargs):
        self.reported_statuses.append(status)
        self.reported_status_details.append((status, _kwargs))


def build_fake_pywin32():
    pywintypes = types.ModuleType("pywintypes")
    pywintypes.error = FakeWin32Error

    win32service = types.ModuleType("win32service")
    win32service.error = FakeWin32Error
    win32service.SERVICE_STOPPED = 1
    win32service.SERVICE_START_PENDING = 2
    win32service.SERVICE_STOP_PENDING = 3
    win32service.SERVICE_RUNNING = 4
    win32service.SERVICE_AUTO_START = 2
    win32service.SERVICE_DEMAND_START = 3
    win32service.SC_MANAGER_CONNECT = 1
    win32service.SERVICE_QUERY_CONFIG = 1
    win32service.SERVICE_CHANGE_CONFIG = 2
    win32service.SERVICE_START = 16
    win32service.SERVICE_CONFIG_FAILURE_ACTIONS = 2
    win32service.SC_ACTION_NONE = 0
    win32service.SC_ACTION_RESTART = 1
    win32service.OpenSCManager = Mock(return_value="hscm")
    win32service.OpenService = Mock(return_value="hs")
    win32service.CloseServiceHandle = Mock()
    win32service.ChangeServiceConfig2 = Mock()
    # (serviceType, startType, errorControl, binaryPath, ...)
    win32service.QueryServiceConfig = Mock(return_value=(16, 2, 1, '"C:\\x.exe" service run'))

    win32serviceutil = types.ModuleType("win32serviceutil")
    win32serviceutil.ServiceFramework = FakeServiceFramework
    win32serviceutil.InstallService = Mock()
    win32serviceutil.ChangeServiceConfig = Mock()
    win32serviceutil.RemoveService = Mock()
    win32serviceutil.StartService = Mock()
    win32serviceutil.StopService = Mock()
    # (serviceType, currentState, controlsAccepted, ...)
    win32serviceutil.QueryServiceStatus = Mock(return_value=(16, 4, 1, 0, 0, 0, 0))
    win32serviceutil.WaitForServiceStatus = Mock()

    win32event = types.ModuleType("win32event")
    win32event.CreateEvent = Mock(return_value="stop-handle")
    win32event.SetEvent = Mock()

    servicemanager = types.ModuleType("servicemanager")
    servicemanager.Initialize = Mock()
    servicemanager.PrepareToHostSingle = Mock()
    servicemanager.StartServiceCtrlDispatcher = Mock()
    servicemanager.LogInfoMsg = Mock()
    servicemanager.LogErrorMsg = Mock()

    return {
        "pywintypes": pywintypes,
        "win32service": win32service,
        "win32serviceutil": win32serviceutil,
        "win32event": win32event,
        "servicemanager": servicemanager,
    }


class FakePywin32TestCase(unittest.TestCase):
    def setUp(self):
        self.fakes = build_fake_pywin32()
        patcher = patch.dict(sys.modules, self.fakes)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def win32serviceutil(self):
        return self.fakes["win32serviceutil"]


class WindowsServiceModuleImportTests(unittest.TestCase):
    def test_module_imports_without_pywin32_present(self):
        import importlib

        import src.worker_agent as package

        hidden = {name: None for name in ("pywintypes", "win32service", "win32serviceutil", "win32event", "servicemanager")}
        original = sys.modules.get("src.worker_agent.windows_service")
        # patch.dict restores sys.modules (including the popped module) on exit;
        # the package attribute set by the fresh import is restored by hand below.
        with patch.dict(sys.modules, hidden):
            sys.modules.pop("src.worker_agent.windows_service", None)
            windows_service = importlib.import_module("src.worker_agent.windows_service")
        if original is not None:
            package.windows_service = original

        self.assertEqual(windows_service.SERVICE_NAME, "VaultlineWorker")
        self.assertEqual(windows_service.DISPLAY_NAME, "Vaultline Worker")
        self.assertIn("vaultline-worker enroll --control-plane-url <url>", windows_service.DESCRIPTION)


class ServiceCommandLineTests(unittest.TestCase):
    def test_frozen_executable_registers_service_run(self):
        from src.worker_agent import windows_service

        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\Program Files\Vaultline\vaultline-worker.exe"
        ):
            command = windows_service.service_command_line()
            registered = windows_service.registered_command_line()

        self.assertEqual(command, [r"C:\Program Files\Vaultline\vaultline-worker.exe", "service", "run"])
        self.assertEqual(registered, '"C:\\Program Files\\Vaultline\\vaultline-worker.exe" service run')

    def test_source_checkout_registers_module_invocation(self):
        from src.worker_agent import windows_service

        with patch.object(sys, "executable", r"C:\Python311\python.exe"):
            if hasattr(sys, "frozen"):
                with patch.object(sys, "frozen", False):
                    command = windows_service.service_command_line()
            else:
                command = windows_service.service_command_line()

        self.assertEqual(command, [r"C:\Python311\python.exe", "-m", "src.worker_agent.cli", "service", "run"])


class RootLoggerIsolationMixin:
    """Snapshot root logger handlers so logging tests leave no handler behind."""

    def _isolate_root_logger(self):
        root = logging.getLogger()
        snapshot = list(root.handlers)

        def restore():
            for handler in list(root.handlers):
                if handler not in snapshot:
                    root.removeHandler(handler)
                    handler.close()
            for handler in snapshot:
                if handler not in root.handlers:
                    root.addHandler(handler)

        self.addCleanup(restore)
        return root


class ServiceLoggingTests(RootLoggerIsolationMixin, unittest.TestCase):
    def setUp(self):
        self.root = self._isolate_root_logger()
        self.state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state_dir, ignore_errors=True))

    def _rotating_handlers(self):
        return [handler for handler in self.root.handlers if isinstance(handler, RotatingFileHandler)]

    def test_attaches_rotating_file_handler_under_state_dir(self):
        from src.worker_agent import windows_service

        log_path = windows_service.configure_service_logging(self.state_dir)

        self.assertEqual(log_path, self.state_dir / "logs" / "worker.log")
        self.assertTrue(log_path.parent.is_dir())
        handlers = self._rotating_handlers()
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        # FileHandler stores os.path.abspath(); resolve() would also expand 8.3 names on Windows.
        self.assertEqual(handler.baseFilename, os.path.abspath(str(log_path)))
        self.assertEqual(handler.maxBytes, 5 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)
        self.assertEqual(handler.encoding, "utf-8")
        self.assertEqual(handler.formatter._fmt, "%(asctime)s [%(levelname)s] %(message)s")

        logging.getLogger("worker-test").warning("written through the service handler")
        handler.flush()
        self.assertIn("[WARNING] written through the service handler", log_path.read_text(encoding="utf-8"))

    def test_repeated_calls_do_not_duplicate_handlers(self):
        from src.worker_agent import windows_service

        first = windows_service.configure_service_logging(self.state_dir)
        with patch.object(sys, "stderr", None):
            second = windows_service.configure_service_logging(self.state_dir)

        self.assertEqual(first, second)
        self.assertEqual(len(self._rotating_handlers()), 1)

    def test_removes_stream_handlers_without_a_stream(self):
        from src.worker_agent import windows_service

        dead = logging.StreamHandler()
        dead.stream = None
        alive = logging.StreamHandler(sys.stdout)
        self.root.addHandler(dead)
        self.root.addHandler(alive)

        windows_service.configure_service_logging(self.state_dir)

        self.assertNotIn(dead, self.root.handlers)
        self.assertIn(alive, self.root.handlers)

    def test_without_stderr_drops_console_handlers_but_keeps_file_handlers(self):
        from src.worker_agent import windows_service

        console = logging.StreamHandler(sys.stdout)
        self.root.addHandler(console)
        with patch.object(sys, "stderr", None):
            windows_service.configure_service_logging(self.state_dir)

        self.assertNotIn(console, self.root.handlers)
        self.assertEqual(len(self._rotating_handlers()), 1)

    def test_default_state_dir_comes_from_state_paths(self):
        from src.worker_agent import windows_service

        with patch.dict(os.environ, {"WORKER_STATE_DIR": str(self.state_dir)}):
            log_path = windows_service.configure_service_logging()

        self.assertEqual(log_path, self.state_dir / "logs" / "worker.log")

    def test_unwritable_log_dir_raises_os_error_without_touching_handlers(self):
        from src.worker_agent import windows_service

        blocker = self.state_dir / "logs"
        blocker.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(OSError):
            windows_service.configure_service_logging(self.state_dir)

        self.assertEqual(self._rotating_handlers(), [])


class ServiceClassTests(RootLoggerIsolationMixin, FakePywin32TestCase):
    def setUp(self):
        super().setUp()
        self._isolate_root_logger()
        self.log_path = Path(tempfile.gettempdir()) / "vaultline-service-tests" / "logs" / "worker.log"
        logging_patch = patch("src.worker_agent.windows_service.configure_service_logging", return_value=self.log_path)
        self.configure_logging = logging_patch.start()
        self.addCleanup(logging_patch.stop)

    def test_svc_do_run_configures_file_logging_before_main_and_announces_path(self):
        from src.worker_agent import windows_service

        order = []
        self.configure_logging.side_effect = lambda: order.append("logging") or self.log_path
        with patch("src.worker_agent.main.reset_stop"), patch(
            "src.worker_agent.main.main", side_effect=lambda: order.append("main")
        ):
            windows_service.build_service_class()(["VaultlineWorker"]).SvcDoRun()

        self.assertEqual(order, ["logging", "main"])
        messages = [call.args[0] for call in self.fakes["servicemanager"].LogInfoMsg.call_args_list]
        self.assertTrue(any(str(self.log_path) in message for message in messages), messages)

    def test_svc_do_run_survives_logging_setup_failure(self):
        from src.worker_agent import windows_service

        self.configure_logging.side_effect = PermissionError(13, "Access is denied", "C:\\ProgramData\\Vaultline\\worker\\logs")
        with patch("src.worker_agent.main.reset_stop"), patch("src.worker_agent.main.main") as main:
            windows_service.build_service_class()(["VaultlineWorker"]).SvcDoRun()

        main.assert_called_once_with()
        error_message = self.fakes["servicemanager"].LogErrorMsg.call_args.args[0]
        self.assertIn("PermissionError", error_message)
        self.assertIn("Access is denied", error_message)

    def test_service_class_declares_scm_identity(self):
        from src.worker_agent import windows_service

        cls = windows_service.build_service_class()

        self.assertEqual(cls.__name__, "VaultlineWorkerService")
        self.assertTrue(issubclass(cls, FakeServiceFramework))
        self.assertEqual(cls._svc_name_, "VaultlineWorker")
        self.assertEqual(cls._svc_display_name_, "Vaultline Worker")
        self.assertEqual(cls._svc_description_, windows_service.DESCRIPTION)

    def test_svc_do_run_hosts_worker_main_in_native_continuous_mode(self):
        from src.worker_agent import windows_service

        order = []
        with patch.dict(os.environ, {"WORKER_RUNTIME": "docker", "WORKER_RUN_ONCE": "true"}, clear=True), patch(
            "src.worker_agent.main.reset_stop", side_effect=lambda: order.append("reset_stop")
        ), patch("src.worker_agent.main.main", side_effect=lambda: order.append("main")):
            service = windows_service.build_service_class()(["VaultlineWorker"])
            service.SvcDoRun()
            self.assertEqual(os.environ["WORKER_RUNTIME"], "native")
            self.assertEqual(os.environ["WORKER_RUN_ONCE"], "false")

        self.assertEqual(order, ["reset_stop", "main"])
        self.assertIn(4, service.reported_statuses)
        self.fakes["servicemanager"].LogInfoMsg.assert_called()
        self.fakes["servicemanager"].LogErrorMsg.assert_not_called()

    def test_svc_do_run_logs_worker_failure_instead_of_raising(self):
        from src.worker_agent import windows_service

        with patch("src.worker_agent.main.reset_stop"), patch(
            "src.worker_agent.main.main", side_effect=RuntimeError("worker startup failed after 5 attempts")
        ):
            service = windows_service.build_service_class()(["VaultlineWorker"])
            service.SvcDoRun()

        message = self.fakes["servicemanager"].LogErrorMsg.call_args.args[0]
        self.assertIn("RuntimeError", message)
        self.assertIn("worker startup failed after 5 attempts", message)
        self.assertEqual(service.reported_status_details[-1], (1, {"win32ExitCode": 1066, "svcExitCode": 1}))

    def test_svc_do_run_does_not_report_failure_status_when_stop_was_requested(self):
        from src.worker_agent import windows_service

        service = windows_service.build_service_class()(["VaultlineWorker"])
        service.stop_requested = True
        with patch("src.worker_agent.main.reset_stop"), patch(
            "src.worker_agent.main.main", side_effect=RuntimeError("worker stopped")
        ):
            service.SvcDoRun()

        self.assertNotIn(1, service.reported_statuses)

    def test_svc_stop_reports_bounded_pending_status_and_starts_watchdog(self):
        from src.worker_agent import windows_service

        with patch("src.worker_agent.main.request_stop"), patch(
            "src.worker_agent.windows_service.threading.Thread"
        ) as thread:
            service = windows_service.build_service_class()(["VaultlineWorker"])
            service.SvcStop()

        status, details = service.reported_status_details[0]
        self.assertEqual(status, 3)
        self.assertEqual(details, {"waitHint": 120000, "checkPoint": 1})
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        self.assertEqual(windows_service.STOP_WAIT_SECONDS, 120)

    def test_stop_completes_gracefully_without_disabling_recovery_or_forcing_exit(self):
        from src.worker_agent import windows_service

        service = windows_service.build_service_class()(["VaultlineWorker"])
        service.worker_done.set()
        real_thread = threading.Thread
        threads = []

        def start_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch("src.worker_agent.windows_service.threading.Thread", side_effect=start_thread), patch(
            "src.worker_agent.windows_service._suppress_service_failure_restarts"
        ) as suppress, patch("src.worker_agent.windows_service._terminate_service_process") as force_exit:
            service.SvcStop()
            threads[0].join(timeout=1)

        self.assertFalse(threads[0].is_alive())
        suppress.assert_not_called()
        force_exit.assert_not_called()
        self.assertEqual(service.reported_statuses, [3])

    def test_stop_timeout_suppresses_recovery_before_forced_process_exit(self):
        from src.worker_agent import windows_service

        service = windows_service.build_service_class()(["VaultlineWorker"])
        real_thread = threading.Thread
        threads = []
        ordering = []

        def start_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        def force_exit():
            ordering.append(("exit", 0))
            service.worker_done.set()

        with patch("src.worker_agent.windows_service.STOP_WAIT_SECONDS", 0.02), patch(
            "src.worker_agent.windows_service.STOP_WATCHDOG_INTERVAL_SECONDS", 0.005
        ), patch("src.worker_agent.windows_service.threading.Thread", side_effect=start_thread), patch(
            "src.worker_agent.windows_service._suppress_service_failure_restarts",
            side_effect=lambda: ordering.append("suppress-recovery"),
        ), patch(
            "src.worker_agent.windows_service._terminate_service_process",
            side_effect=force_exit,
        ):
            service.SvcStop()
            threads[0].join(timeout=1)

        self.assertFalse(threads[0].is_alive())
        self.assertEqual(ordering, ["suppress-recovery", ("exit", 0)])
        self.assertNotIn(1, service.reported_statuses, "the watchdog must not report STOPPED while alive")
        warning = self.fakes["servicemanager"].LogErrorMsg.call_args.args[0]
        self.assertIn("may be interrupted", warning)

    def test_service_start_restores_msi_recovery_policy_before_worker_main(self):
        from src.worker_agent import windows_service

        calls = []
        with patch("src.worker_agent.windows_service._restore_service_failure_restarts", side_effect=lambda: calls.append("recovery")), patch(
            "src.worker_agent.main.reset_stop"
        ), patch("src.worker_agent.main.main", side_effect=lambda: calls.append("main")):
            windows_service.build_service_class()(["VaultlineWorker"]).SvcDoRun()

        self.assertEqual(calls, ["recovery", "main"])

    def test_recovery_suppression_and_restoration_use_scm_failure_actions_api(self):
        from src.worker_agent import windows_service

        win32service = self.fakes["win32service"]
        windows_service._suppress_service_failure_restarts()
        win32service.OpenService.assert_called_with(
            "hscm",
            "VaultlineWorker",
            win32service.SERVICE_CHANGE_CONFIG | win32service.SERVICE_START,
        )
        suppressed = win32service.ChangeServiceConfig2.call_args.args[2]
        self.assertEqual(suppressed["Actions"], ((win32service.SC_ACTION_NONE, 0),))
        self.assertEqual(suppressed["ResetPeriod"], 0)

        windows_service._restore_service_failure_restarts()
        restored = win32service.ChangeServiceConfig2.call_args.args[2]
        self.assertEqual(
            restored["Actions"],
            ((win32service.SC_ACTION_RESTART, 10000),) * 3,
        )
        self.assertEqual(restored["ResetPeriod"], 86400)
        self.assertEqual(win32service.ChangeServiceConfig2.call_count, 2)
        self.assertEqual(win32service.CloseServiceHandle.call_count, 4)

    def test_svc_stop_requests_worker_stop_and_signals_event(self):
        from src.worker_agent import windows_service

        with patch("src.worker_agent.main.request_stop") as request_stop:
            service = windows_service.build_service_class()(["VaultlineWorker"])
            service.SvcStop()

        request_stop.assert_called_once_with()
        self.assertEqual(service.reported_statuses, [3])
        self.fakes["win32event"].SetEvent.assert_called_once_with("stop-handle")

    def test_run_as_service_hosts_single_service_through_dispatcher(self):
        from src.worker_agent import windows_service

        servicemanager = self.fakes["servicemanager"]
        windows_service.run_as_service()

        servicemanager.Initialize.assert_called_once_with()
        hosted = servicemanager.PrepareToHostSingle.call_args.args[0]
        self.assertEqual(hosted._svc_name_, "VaultlineWorker")
        servicemanager.StartServiceCtrlDispatcher.assert_called_once_with()

    def test_run_as_service_from_console_raises_service_error_1063(self):
        from src.worker_agent import windows_service

        self.fakes["servicemanager"].StartServiceCtrlDispatcher.side_effect = FakeWin32Error(
            1063, "StartServiceCtrlDispatcher", "The service process could not connect to the service controller."
        )

        with self.assertRaises(windows_service.ServiceError) as raised:
            windows_service.run_as_service()

        self.assertEqual(raised.exception.winerror, 1063)


class InstallServiceTests(FakePywin32TestCase):
    def test_install_registers_auto_start_local_system_service_with_frozen_exe(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(1060)
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "executable", r"C:\Program Files\Vaultline\vaultline-worker.exe"
        ):
            result = windows_service.install_service()

        self.win32serviceutil.InstallService.assert_called_once_with(
            None,
            "VaultlineWorker",
            "Vaultline Worker",
            startType=2,
            exeName=r"C:\Program Files\Vaultline\vaultline-worker.exe",
            exeArgs="service run",
            description=windows_service.DESCRIPTION,
        )
        self.win32serviceutil.ChangeServiceConfig.assert_not_called()
        self.assertEqual(result["action"], "installed")
        self.assertEqual(result["command_line"], '"C:\\Program Files\\Vaultline\\vaultline-worker.exe" service run')

    def test_install_from_source_checkout_registers_module_arguments(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(1060)
        with patch.object(sys, "executable", r"C:\Python311\python.exe"), patch.object(sys, "frozen", False, create=True):
            windows_service.install_service()

        kwargs = self.win32serviceutil.InstallService.call_args.kwargs
        self.assertEqual(kwargs["exeName"], r"C:\Python311\python.exe")
        self.assertEqual(kwargs["exeArgs"], "-m src.worker_agent.cli service run")

    def test_install_updates_existing_service_instead_of_failing(self):
        from src.worker_agent import windows_service

        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", r"C:\new\vaultline-worker.exe"):
            result = windows_service.install_service()

        self.win32serviceutil.InstallService.assert_not_called()
        self.win32serviceutil.ChangeServiceConfig.assert_called_once_with(
            None,
            "VaultlineWorker",
            startType=2,
            exeName=r"C:\new\vaultline-worker.exe",
            displayName="Vaultline Worker",
            exeArgs="service run",
            description=windows_service.DESCRIPTION,
        )
        self.assertEqual(result["action"], "updated")

    def test_install_translates_access_denied_into_service_error(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(1060)
        self.win32serviceutil.InstallService.side_effect = FakeWin32Error(5, "CreateService", "Access is denied.")

        with self.assertRaises(windows_service.ServiceError) as raised:
            windows_service.install_service()

        self.assertEqual(raised.exception.winerror, 5)
        self.assertTrue(raised.exception.access_denied)
        self.assertIn("Access is denied.", str(raised.exception))


class LifecycleTests(FakePywin32TestCase):
    def test_uninstall_stops_running_service_before_removal(self):
        from src.worker_agent import windows_service

        result = windows_service.uninstall_service()

        self.win32serviceutil.StopService.assert_called_once_with("VaultlineWorker")
        self.win32serviceutil.WaitForServiceStatus.assert_called_once_with("VaultlineWorker", 1, windows_service.STOP_WAIT_SECONDS)
        self.win32serviceutil.RemoveService.assert_called_once_with("VaultlineWorker")
        self.assertEqual(result["action"], "removed")

    def test_uninstall_of_stopped_service_skips_stop(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.return_value = (16, 1, 1, 0, 0, 0, 0)
        windows_service.uninstall_service()

        self.win32serviceutil.StopService.assert_not_called()
        self.win32serviceutil.RemoveService.assert_called_once_with("VaultlineWorker")

    def test_uninstall_when_not_installed_is_a_no_op(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(1060)
        result = windows_service.uninstall_service()

        self.win32serviceutil.RemoveService.assert_not_called()
        self.assertEqual(result["action"], "not_installed")

    def test_start_waits_for_running_and_tolerates_already_running(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.StartService.side_effect = FakeWin32Error(1056)
        result = windows_service.start_service()

        self.win32serviceutil.WaitForServiceStatus.assert_called_once_with("VaultlineWorker", 4, windows_service.START_WAIT_SECONDS)
        self.assertEqual(result["state"], "running")

    def test_stop_tolerates_not_active_and_waits_for_stopped(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.StopService.side_effect = FakeWin32Error(1062)
        self.win32serviceutil.QueryServiceStatus.return_value = (16, 1, 1, 0, 0, 0, 0)
        result = windows_service.stop_service()

        self.win32serviceutil.WaitForServiceStatus.assert_called_once_with("VaultlineWorker", 1, windows_service.STOP_WAIT_SECONDS)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(windows_service.STOP_WAIT_SECONDS, 120)

    def test_stop_timeout_surfaces_as_service_error(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.WaitForServiceStatus.side_effect = FakeWin32Error(1053, "QueryServiceStatus", "timeout")

        with self.assertRaises(windows_service.ServiceError) as raised:
            windows_service.stop_service()

        self.assertEqual(raised.exception.winerror, 1053)


class ServiceStatusTests(FakePywin32TestCase):
    STATE_DIR = r"C:\ProgramData\Vaultline\worker"

    def setUp(self):
        super().setUp()
        env_patch = patch.dict(os.environ, {"WORKER_STATE_DIR": self.STATE_DIR})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_status_reports_running_auto_start_service_with_log_file(self):
        from src.worker_agent import windows_service

        status = windows_service.service_status()

        self.assertEqual(
            status,
            {
                "name": "VaultlineWorker",
                "installed": True,
                "state": "running",
                "start_type": "auto",
                "log_file": str(Path(self.STATE_DIR) / "logs" / "worker.log"),
            },
        )
        self.fakes["win32service"].CloseServiceHandle.assert_called()

    def test_status_reports_not_installed_without_raising(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(1060)
        status = windows_service.service_status()

        self.assertEqual(
            status,
            {"name": "VaultlineWorker", "installed": False, "state": "not_installed", "start_type": None},
        )

    def test_status_maps_pending_states_and_manual_start(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.return_value = (16, 2, 0, 0, 0, 0, 0)
        self.fakes["win32service"].QueryServiceConfig.return_value = (16, 3, 1, "x")
        status = windows_service.service_status()

        self.assertEqual(status["state"], "start_pending")
        self.assertEqual(status["start_type"], "manual")

    def test_status_propagates_unexpected_errors(self):
        from src.worker_agent import windows_service

        self.win32serviceutil.QueryServiceStatus.side_effect = FakeWin32Error(5, "OpenSCManager", "Access is denied.")

        with self.assertRaises(windows_service.ServiceError):
            windows_service.service_status()


if __name__ == "__main__":
    unittest.main()
