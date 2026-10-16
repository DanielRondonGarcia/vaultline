import csv
import io
import json
import unittest

from openpyxl import load_workbook

from src.control_plane.application.services.backup_audit_exports import (
    BACKUP_AUDIT_MATRIX_COLUMNS,
    export_backup_audit_csv,
    export_backup_audit_json,
    export_backup_audit_pdf,
    export_backup_audit_xlsx,
    render_backup_audit_export,
)


def _sample_report() -> dict:
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
                "complete": False,
                "basis": (
                    "Restic JSON summary total_bytes_processed (source bytes processed) "
                    "and data_added (backend-reported stored bytes) captured per "
                    "backup.run job; each metric is unavailable independently when a "
                    "job has no exact Restic summary or uses a non-Restic strategy."
                ),
                "metrics": {
                    "bytes_processed": {
                        "source_field": "total_bytes_processed",
                        "label": "source bytes processed",
                        "available": True,
                        "complete": False,
                        "reported_jobs": 1,
                        "unavailable_jobs": 1,
                    },
                    "bytes_added": {
                        "source_field": "data_added",
                        "label": "backend stored bytes",
                        "available": True,
                        "complete": False,
                        "reported_jobs": 1,
                        "unavailable_jobs": 1,
                    },
                },
            },
            "storage_location": {
                "available": True,
                "basis": "target.path_storage and linked storage profile backend_type/name",
            },
        },
        "summary": {
            "target_count": 1,
            "backup_job_count": 2,
            "completed_job_count": 2,
            "succeeded_count": 1,
            "failed_count": 1,
            "other_status_counts": {},
            "unmapped_target_job_count": 0,
            "succeeded_percent": 50.0,
            "failed_percent": 50.0,
            "backup_size": {
                "bytes_processed": 2048,
                "bytes_added": 1024,
                "coverage": {
                    "bytes_processed": {"reported_jobs": 1, "unavailable_jobs": 1},
                    "bytes_added": {"reported_jobs": 1, "unavailable_jobs": 1},
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
                "resources": {
                    "compose_project": "project-a",
                    "volumes": ["database:/var/lib/database"],
                    "filesystem_paths": [],
                    "pvc_names": [],
                },
                "schedule": {
                    "cron_expression": "0 2 * * *",
                    "source": "target",
                    "timezone": "America/New_York",
                },
                "retention": {
                    "id": "retention-a",
                    "name": "daily-seven",
                    "keep_last": None,
                    "keep_hourly": None,
                    "keep_daily": 7,
                    "keep_weekly": None,
                    "keep_monthly": None,
                    "keep_yearly": None,
                    "prune": True,
                },
                "job_counts": {
                    "total": 2,
                    "completed": 2,
                    "succeeded": 1,
                    "failed": 1,
                    "other": 0,
                    "other_statuses": {},
                },
                "succeeded_percent": 50.0,
                "failed_percent": 50.0,
                "backup_size": {
                    "bytes_processed": 2048,
                    "bytes_added": 1024,
                    "coverage": {
                        "bytes_processed": {"reported_jobs": 1, "unavailable_jobs": 1},
                        "bytes_added": {"reported_jobs": 1, "unavailable_jobs": 1},
                    },
                },
                "storage_location": {
                    "backend_type": "s3",
                    "storage_profile_name": "prod-s3",
                    "path_storage": "svc/backups",
                },
            }
        ],
    }


class BackupAuditJsonExportTests(unittest.TestCase):
    def test_json_export_roundtrips_the_full_report(self):
        body = export_backup_audit_json(_sample_report())
        self.assertIsInstance(body, bytes)
        self.assertEqual(json.loads(body.decode("utf-8")), _sample_report())


class BackupAuditCsvExportTests(unittest.TestCase):
    def test_csv_has_header_and_one_row_per_target_with_safe_fields_and_size_coverage(self):
        report = _sample_report()
        text = export_backup_audit_csv(report).decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], list(BACKUP_AUDIT_MATRIX_COLUMNS))
        self.assertEqual(len(rows), 1 + len(report["matrix"]))
        data_row = rows[1]
        self.assertIn("service-a", data_row)
        self.assertIn("server-a", data_row)
        self.assertIn("prod-s3", data_row)
        self.assertIn("svc/backups", data_row)
        # both size metrics and their coverage counts are exposed as columns
        self.assertIn("2048", data_row)
        self.assertIn("1024", data_row)
        columns = dict(zip(BACKUP_AUDIT_MATRIX_COLUMNS, data_row))
        self.assertEqual(columns["backup_bytes_processed"], "2048")
        self.assertEqual(columns["backup_bytes_added"], "1024")
        self.assertEqual(columns["backup_bytes_processed_reported_jobs"], "1")
        self.assertEqual(columns["backup_bytes_processed_unavailable_jobs"], "1")
        self.assertEqual(columns["backup_bytes_added_reported_jobs"], "1")
        self.assertEqual(columns["backup_bytes_added_unavailable_jobs"], "1")


class BackupAuditXlsxExportTests(unittest.TestCase):
    def test_xlsx_is_valid_workbook_with_header_and_data_row(self):
        report = _sample_report()
        body = export_backup_audit_xlsx(report)
        self.assertIsInstance(body, bytes)
        self.assertEqual(body[:2], b"PK")  # zip/openxml container
        wb = load_workbook(io.BytesIO(body))
        ws = wb.active
        self.assertEqual(ws.title, "Backup Audit")

        header_row = None
        for row in ws.iter_rows(values_only=True):
            if row and row[0] == BACKUP_AUDIT_MATRIX_COLUMNS[0]:
                header_row = row
                break
        self.assertIsNotNone(header_row)
        self.assertEqual(header_row, BACKUP_AUDIT_MATRIX_COLUMNS)

        summary_lines = [row[0] for row in ws.iter_rows(values_only=True) if row and isinstance(row[0], str)]
        self.assertIn("Source bytes processed: partial; total=2048 bytes; reported for 1/2 jobs", summary_lines)
        self.assertIn("Backend stored bytes: partial; total=1024 bytes; reported for 1/2 jobs", summary_lines)

        data_rows = [
            row
            for row in ws.iter_rows(values_only=True)
            if row and row[0] == "target-a"
        ]
        self.assertEqual(len(data_rows), 1)
        columns = dict(zip(BACKUP_AUDIT_MATRIX_COLUMNS, data_rows[0]))
        self.assertEqual(columns["service"], "service-a")
        self.assertEqual(columns["server_host"], "server-a")
        self.assertEqual(columns["storage_backend_type"], "s3")
        self.assertEqual(columns["storage_profile_name"], "prod-s3")
        self.assertEqual(columns["path_storage"], "svc/backups")
        self.assertEqual(columns["backup_bytes_processed"], 2048)
        self.assertEqual(columns["backup_bytes_added"], 1024)
        self.assertEqual(columns["backup_bytes_processed_reported_jobs"], 1)
        self.assertEqual(columns["backup_bytes_added_unavailable_jobs"], 1)


class BackupAuditPdfExportTests(unittest.TestCase):
    def test_pdf_is_valid_pdf_containing_report_text(self):
        report = _sample_report()
        body = export_backup_audit_pdf(report)
        self.assertIsInstance(body, bytes)
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertTrue(body.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Catalog", body)
        self.assertIn(b"/Type /Pages", body)
        self.assertIn(b"(Backup Audit Report)", body)
        self.assertIn(b"service-a", body)
        self.assertIn(b"server-a", body)
        # both size metrics are communicated in the PDF text
        self.assertIn(b"backup size: partial", body.lower())
        self.assertIn(b"Source bytes processed: 2048 bytes; reported for 1/2 jobs", body)
        self.assertIn(b"Backend stored bytes: 1024 bytes; reported for 1/2 jobs", body)
        self.assertIn(b"processed=2048 \\(1/2 jobs\\)", body)
        self.assertIn(b"added=1024 \\(1/2 jobs\\)", body)


class BackupAuditRenderDispatchTests(unittest.TestCase):
    def test_render_returns_content_type_and_dated_filename_for_each_format(self):
        report = _sample_report()
        expected = {
            "json": "application/json",
            "csv": "text/csv; charset=utf-8",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf",
        }
        for fmt, content_type in expected.items():
            with self.subTest(fmt=fmt):
                rendered_type, filename, body = render_backup_audit_export(fmt, report)
                self.assertEqual(rendered_type, content_type)
                self.assertTrue(filename.startswith("backup_audit_2026-03-08_2026-03-08."))
                self.assertTrue(filename.endswith(f".{fmt}"))
                self.assertIsInstance(body, bytes)
                self.assertGreater(len(body), 0)

    def test_render_rejects_unsupported_format(self):
        with self.assertRaisesRegex(ValueError, "unsupported report format"):
            render_backup_audit_export("xml", _sample_report())


class BackupAuditSpreadsheetInjectionTests(unittest.TestCase):
    def test_csv_export_neutralizes_formula_like_text(self):
        import csv

        report = _sample_report()
        report["matrix"][0]["service"] = "=1+1"
        rows = list(csv.reader(io.StringIO(export_backup_audit_csv(report).decode("utf-8"))))
        service_index = BACKUP_AUDIT_MATRIX_COLUMNS.index("service")
        self.assertEqual(rows[1][service_index], "'=1+1")

    def test_xlsx_export_stores_formula_like_text_as_a_literal(self):
        report = _sample_report()
        report["matrix"][0]["service"] = "=1+1"
        workbook = load_workbook(io.BytesIO(export_backup_audit_xlsx(report)), data_only=False)
        sheet = workbook.active
        header_row = next(
            row_number
            for row_number in range(1, sheet.max_row + 1)
            if sheet.cell(row_number, 1).value == BACKUP_AUDIT_MATRIX_COLUMNS[0]
        )
        service_column = BACKUP_AUDIT_MATRIX_COLUMNS.index("service") + 1
        service_cell = sheet.cell(header_row + 1, service_column)
        self.assertEqual(service_cell.value, "'=1+1")
        self.assertNotEqual(service_cell.data_type, "f")


if __name__ == "__main__":
    unittest.main()