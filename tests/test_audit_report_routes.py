import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.control_plane.auth import ROLE_VIEWER
from src.control_plane.main import ControlPlaneRequestHandler


def _sample_download_report() -> dict:
    return {
        "report": "backup_audit",
        "generated_at": "2026-03-09T00:00:00Z",
        "period": {
            "start_date": "2026-03-08",
            "end_date": "2026-03-08",
            "timezone": "America/New_York",
        },
        "availability": {
            "backup_size": {
                "available": True,
                "basis": "Restic summary total_bytes_processed and data_added captured per backup.run job",
            },
            "storage_location": {"available": True, "basis": "path_storage and storage profile"},
        },
        "summary": {
            "target_count": 1,
            "backup_job_count": 0,
            "completed_job_count": 0,
            "succeeded_count": 0,
            "failed_count": 0,
            "other_status_counts": {},
            "unmapped_target_job_count": 0,
            "succeeded_percent": None,
            "failed_percent": None,
            "backup_size": {
                "bytes_processed": None,
                "bytes_added": None,
                "coverage": {
                    "bytes_processed": {"reported_jobs": 0, "unavailable_jobs": 0},
                    "bytes_added": {"reported_jobs": 0, "unavailable_jobs": 0},
                },
            },
        },
        "matrix": [
            {
                "target_id": "target-a",
                "service": "service-a",
                "server": {"id": "worker-a", "name": "worker-a", "host": "server-a"},
                "runtime_type": "docker",
                "backup_strategy": "restic",
                "backup_mode": "cold",
                "enabled": True,
                "resources": {"compose_project": None, "volumes": [], "filesystem_paths": [], "pvc_names": []},
                "schedule": {"cron_expression": None, "source": "manual", "timezone": "America/New_York"},
                "retention": None,
                "job_counts": {"total": 0, "completed": 0, "succeeded": 0, "failed": 0, "other": 0, "other_statuses": {}},
                "succeeded_percent": None,
                "failed_percent": None,
                "backup_size": {
                    "bytes_processed": None,
                    "bytes_added": None,
                    "coverage": {
                        "bytes_processed": {"reported_jobs": 0, "unavailable_jobs": 0},
                        "bytes_added": {"reported_jobs": 0, "unavailable_jobs": 0},
                    },
                },
                "storage_location": {"backend_type": None, "storage_profile_name": None, "path_storage": None},
            }
        ],
    }


class BackupAuditReportRouteTests(unittest.TestCase):
    @staticmethod
    def _handler(path, *, authorized=True):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        handler.headers = {}
        handler._require_auth = Mock(return_value=authorized)
        service = Mock()
        service.backup_audit_report.return_value = {"report": "backup_audit"}
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=SimpleNamespace(timezone_name="America/New_York"))
        handler._write_json = Mock()
        return handler, service

    def test_report_route_requires_viewer_role_before_accessing_data(self):
        handler, _ = self._handler(
            "/api/v1/reports/backups?start_date=2026-03-08&end_date=2026-03-08",
            authorized=False,
        )

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, head_only=False, api_mode=True)
        handler._control_plane_service.assert_not_called()
        handler._write_json.assert_not_called()

    def test_report_route_returns_no_store_json_for_viewer(self):
        handler, service = self._handler(
            "/api/v1/reports/backups?start_date=2026-03-08&end_date=2026-03-09"
        )

        handler._handle_get_request(head_only=False)

        service.backup_audit_report.assert_called_once_with(
            start_date="2026-03-08",
            end_date="2026-03-09",
            timezone_name="America/New_York",
        )
        handler._write_json.assert_called_once_with(
            200,
            {"report": "backup_audit"},
            head_only=False,
            extra_headers={"Cache-Control": "no-store"},
        )

    def test_report_route_rejects_missing_date_before_loading_report(self):
        handler, _ = self._handler("/api/v1/reports/backups?start_date=2026-03-08")

        handler._handle_get_request(head_only=False)

        handler._control_plane_service.assert_not_called()
        handler._write_json.assert_called_once_with(
            400,
            {"error": "end_date is required exactly once"},
            head_only=False,
        )

    def test_report_route_rejects_unsupported_query_parameters(self):
        handler, _ = self._handler(
            "/api/v1/reports/backups?start_date=2026-03-08&end_date=2026-03-08&unexpected=true"
        )

        handler._handle_get_request(head_only=False)

        handler._control_plane_service.assert_not_called()
        handler._write_json.assert_called_once_with(
            400,
            {"error": "unsupported report query parameter: unexpected"},
            head_only=False,
        )

    def test_report_route_maps_reversed_date_range_to_bad_request(self):
        handler, service = self._handler(
            "/api/v1/reports/backups?start_date=2026-03-09&end_date=2026-03-08"
        )
        service.backup_audit_report.side_effect = ValueError("start_date must be on or before end_date")

        handler._handle_get_request(head_only=False)

        handler._write_json.assert_called_once_with(
            400,
            {"error": "start_date must be on or before end_date"},
            head_only=False,
        )


class BackupAuditDownloadRouteTests(unittest.TestCase):
    @staticmethod
    def _download_handler(path, *, authorized=True):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        handler.headers = {}
        handler._require_auth = Mock(return_value=authorized)
        service = Mock()
        service.backup_audit_report.return_value = _sample_download_report()
        handler._control_plane_service = Mock(return_value=service)
        handler._scheduler = Mock(return_value=SimpleNamespace(timezone_name="America/New_York"))
        handler._write_json = Mock()
        handler._write_download = Mock()
        return handler, service

    def test_download_route_requires_viewer_role_before_loading_report(self):
        handler, service = self._download_handler(
            "/api/v1/reports/backups/download/csv?start_date=2026-03-08&end_date=2026-03-08",
            authorized=False,
        )

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_VIEWER, head_only=False, api_mode=True)
        service.backup_audit_report.assert_not_called()
        handler._write_download.assert_not_called()
        handler._write_json.assert_not_called()

    def test_download_route_rejects_unsupported_format_before_loading_report(self):
        handler, service = self._download_handler(
            "/api/v1/reports/backups/download/xml?start_date=2026-03-08&end_date=2026-03-08"
        )

        handler._handle_get_request(head_only=False)

        service.backup_audit_report.assert_not_called()
        handler._write_download.assert_not_called()
        handler._write_json.assert_called_once()
        status, payload = handler._write_json.call_args.args[0], handler._write_json.call_args.args[1]
        self.assertEqual(status, 400)
        self.assertIn("unsupported report format", payload["error"])

    def test_download_route_rejects_missing_date_before_loading_report(self):
        handler, service = self._download_handler(
            "/api/v1/reports/backups/download/json?start_date=2026-03-08"
        )

        handler._handle_get_request(head_only=False)

        service.backup_audit_report.assert_not_called()
        handler._write_download.assert_not_called()
        handler._write_json.assert_called_once_with(
            400,
            {"error": "end_date is required exactly once"},
            head_only=False,
        )

    def test_download_route_maps_reversed_date_range_to_bad_request(self):
        handler, service = self._download_handler(
            "/api/v1/reports/backups/download/csv?start_date=2026-03-09&end_date=2026-03-08"
        )
        service.backup_audit_report.side_effect = ValueError("start_date must be on or before end_date")

        handler._handle_get_request(head_only=False)

        handler._write_download.assert_not_called()
        handler._write_json.assert_called_once_with(
            400,
            {"error": "start_date must be on or before end_date"},
            head_only=False,
        )

    def test_download_route_renders_each_format_with_correct_content_type_and_attachment(self):
        expected = {
            "json": "application/json",
            "csv": "text/csv; charset=utf-8",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf",
        }
        for fmt, content_type in expected.items():
            with self.subTest(fmt=fmt):
                handler, service = self._download_handler(
                    f"/api/v1/reports/backups/download/{fmt}?start_date=2026-03-08&end_date=2026-03-08"
                )

                handler._handle_get_request(head_only=False)

                service.backup_audit_report.assert_called_once_with(
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    timezone_name="America/New_York",
                )
                handler._write_download.assert_called_once()
                call = handler._write_download.call_args
                self.assertEqual(call.args[0], 200)
                self.assertEqual(call.args[1], content_type)
                self.assertTrue(call.args[2].startswith("backup_audit_2026-03-08_2026-03-08."))
                self.assertTrue(call.args[2].endswith(f".{fmt}"))
                body = call.args[3]
                self.assertIsInstance(body, bytes)
                self.assertGreater(len(body), 0)
                if fmt == "json":
                    import json as _json
                    self.assertEqual(_json.loads(body.decode("utf-8")), _sample_download_report())
                elif fmt == "csv":
                    self.assertIn(b"target_id", body)
                elif fmt == "xlsx":
                    self.assertEqual(body[:2], b"PK")
                elif fmt == "pdf":
                    self.assertTrue(body.startswith(b"%PDF-"))


class BackupAuditDownloadHelperTests(unittest.TestCase):
    def test_write_download_sets_attachment_headers_and_writes_body(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()

        handler._write_download(200, "application/pdf", "backup_audit_2026-03-08_2026-03-08.pdf", b"%PDF-body", head_only=False)

        handler.send_response.assert_called_once_with(200)
        headers = {call.args[0]: call.args[1] for call in handler.send_header.call_args_list}
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="backup_audit_2026-03-08_2026-03-08.pdf"')
        self.assertEqual(headers["Content-Length"], "9")
        self.assertEqual(headers["Cache-Control"], "no-store")
        handler.end_headers.assert_called_once()
        handler.wfile.write.assert_called_once_with(b"%PDF-body")

    def test_write_download_skips_body_for_head_only(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()

        handler._write_download(200, "text/csv; charset=utf-8", "x.csv", b"data", head_only=True)

        handler.end_headers.assert_called_once()
        handler.wfile.write.assert_not_called()
