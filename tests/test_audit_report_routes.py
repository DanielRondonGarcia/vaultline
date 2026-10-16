import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.control_plane.auth import ROLE_VIEWER
from src.control_plane.main import ControlPlaneRequestHandler


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
