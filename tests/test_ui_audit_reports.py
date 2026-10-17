"""Structural and behavioral verification for the viewer-facing audit reports UI.

The SPA has no JavaScript test harness, so tests follow the repository
convention in ``tests/test_ui_states.py``: structural markers in
``src/control_plane/ui/index.html`` plus Node-based evaluation of extracted
pure helpers when Node.js is available.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"
UI_COMPONENTS_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "components.css"


class AuditReportsNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_reports_view_is_visible_to_viewers_in_navigation(self):
        self.assertIn(
            '{ id: "reports", label: "Informes", icon: "reports", roles: ["admin", "operator", "viewer"] }',
            self.source,
        )
        icon = re.search(r"reports: '<svg[^>]*>.*?</svg>'", self.source, re.S)
        self.assertIsNotNone(icon, "the Informes nav item needs its own icon")

    def test_reports_view_has_topbar_title_and_renderer(self):
        self.assertIn('reports: "Informes",', self.source)
        self.assertIn("reports: renderReports,", self.source)


class AuditReportsRangeControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.presets = re.search(r"const REPORT_PRESETS = \[(.*?)\];", cls.source, re.S)
        cls.preset_range = re.search(
            r"function reportsPresetRange\(preset, today = reportsSchedulerToday\(\)\) \{.*?\n    \}",
            cls.source,
            re.S,
        )
        cls.scheduler_today = re.search(
            r"function reportsSchedulerToday\(now = new Date\(\)\) \{.*?\n    \}",
            cls.source,
            re.S,
        )

    def test_range_controls_offer_monthly_quarterly_and_custom_presets(self):
        self.assertIsNotNone(self.presets)
        for marker in (
            '{ id: "monthly", label: "Mes actual" }',
            '{ id: "quarterly", label: "Trimestre actual" }',
            '{ id: "custom", label: "Rango personalizado" }',
            '<select id="reportPreset"',
            '<input id="reportStartDate" type="date"',
            '<input id="reportEndDate" type="date"',
        ):
            self.assertIn(marker, self.source)

    def test_presets_and_custom_dates_use_scheduler_timezone_day_semantics(self):
        self.assertIsNotNone(self.scheduler_today)
        self.assertIn("state.schedulerTimezone", self.scheduler_today.group(0))
        self.assertIn('new Intl.DateTimeFormat("en-CA", {', self.scheduler_today.group(0))
        self.assertIn("timeZone,", self.scheduler_today.group(0))
        self.assertIn("formatToParts(now)", self.scheduler_today.group(0))
        self.assertIsNotNone(self.preset_range)
        preset_source = self.preset_range.group(0)
        # Monthly starts on the first day of the current scheduler-timezone month.
        self.assertIn('today.slice(0, 8) + "01"', preset_source)
        # Quarterly starts on the first day of the current quarter (Jan/Apr/Jul/Oct).
        self.assertIn("Math.floor((month - 1) / 3) * 3 + 1", preset_source)
        # Both presets end on the scheduler-timezone "today".
        self.assertIn("end_date: today", preset_source)


class AuditReportsPresetRangeBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def _run(self, script_body):
        preset_range = re.search(
            r"(function reportsPresetRange\(preset, today = reportsSchedulerToday\(\)\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        self.assertIsNotNone(preset_range)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = '"use strict";\n' + preset_range.group(1) + "\n" + script_body
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_monthly_preset_is_the_current_scheduler_timezone_month(self):
        output = self._run(
            'process.stdout.write(JSON.stringify(reportsPresetRange("monthly", "2026-05-17")));'
        )
        self.assertEqual(output, {"start_date": "2026-05-01", "end_date": "2026-05-17"})

    def test_quarterly_preset_is_the_current_calendar_quarter(self):
        output = self._run(
            'const q2 = reportsPresetRange("quarterly", "2026-05-17");'
            'const q1 = reportsPresetRange("quarterly", "2026-01-05");'
            'const q4 = reportsPresetRange("quarterly", "2026-12-31");'
            'process.stdout.write(JSON.stringify({ q2, q1, q4 }));'
        )
        self.assertEqual(output["q2"], {"start_date": "2026-04-01", "end_date": "2026-05-17"})
        self.assertEqual(output["q1"], {"start_date": "2026-01-01", "end_date": "2026-01-05"})
        self.assertEqual(output["q4"], {"start_date": "2026-10-01", "end_date": "2026-12-31"})

    def test_custom_and_unknown_presets_do_not_invent_a_range(self):
        output = self._run(
            'const custom = reportsPresetRange("custom", "2026-05-17");'
            'const bogus = reportsPresetRange("bogus", "2026-05-17");'
            'const malformed = reportsPresetRange("monthly", "not-a-date");'
            'const missing = reportsPresetRange("monthly", "");'
            'process.stdout.write(JSON.stringify({ custom, bogus, malformed, missing }));'
        )
        self.assertEqual(
            output,
            {"custom": None, "bogus": None, "malformed": None, "missing": None},
        )

    def test_month_preset_is_unavailable_without_scheduler_timezone(self):
        scheduler_today = re.search(
            r"(function reportsSchedulerToday\(now = new Date\(\)\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        preset_range = re.search(
            r"(function reportsPresetRange\(preset, today = reportsSchedulerToday\(\)\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        self.assertIsNotNone(scheduler_today)
        self.assertIsNotNone(preset_range)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = (
            '"use strict";\nconst state = { schedulerTimezone: null };\n'
            + scheduler_today.group(1)
            + "\n"
            + preset_range.group(1)
            + "\nprocess.stdout.write(JSON.stringify({ today: reportsSchedulerToday(), range: reportsPresetRange(\"monthly\") }));"
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"today": None, "range": None})


class AuditReportsSchedulerTodayBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_scheduler_today_uses_scheduler_timezone_and_degrades_gracefully(self):
        scheduler_today = re.search(
            r"(function reportsSchedulerToday\(now = new Date\(\)\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        self.assertIsNotNone(scheduler_today)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = (
            '"use strict";\n'
            "const state = { schedulerTimezone: 'America/Bogota' };\n"
            + scheduler_today.group(1)
            + "\n"
            + r'''
const inTz = reportsSchedulerToday(new Date(Date.UTC(2026, 4, 17, 0, 30))); // 2026-05-16 19:30 America/Bogota
state.schedulerTimezone = 'Europe/Madrid';
const madrid = reportsSchedulerToday(new Date(Date.UTC(2026, 4, 17, 0, 30))); // 2026-05-17 02:30 Europe/Madrid
state.schedulerTimezone = null;
const missing = reportsSchedulerToday(new Date(Date.UTC(2026, 0, 2, 12, 0)));
state.schedulerTimezone = 'Invalid/Timezone';
const invalidZone = reportsSchedulerToday(new Date(Date.UTC(2026, 0, 2, 12, 0)));
process.stdout.write(JSON.stringify({ inTz, madrid, missing, invalidZone }));
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["inTz"], "2026-05-16")
        self.assertEqual(output["madrid"], "2026-05-17")
        self.assertIsNone(output["missing"])
        self.assertIsNone(output["invalidZone"])


class AuditReportsPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.load = re.search(
            r"async function loadReportPreview\(\) \{.*?\n    \}",
            cls.source,
            re.S,
        )

    def test_preview_requests_the_report_api_with_exactly_two_date_bounds(self):
        self.assertIn("apiGet(`/api/v1/reports/backups?${query}`)", self.source)
        self.assertIn(
            'new URLSearchParams({ start_date: range.start_date, end_date: range.end_date })',
            self.source,
        )
        # The report route rejects every other query parameter, so the reports
        # view never appends extra parameters.
        reports_block = self.source[self.source.index("const REPORT_PRESETS"):]
        for extra in ("limit", "tz_offset", "sort"):
            self.assertNotIn(
                f'params.set("{extra}"', reports_block,
                "the report API accepts only start_date and end_date",
            )

    def test_preview_tracks_a_generation_to_discard_stale_and_off_view_responses(self):
        self.assertIsNotNone(self.load)
        load_source = self.load.group(0)
        for marker in (
            "reportLoadGeneration",
            'if (generation !== reportLoadGeneration || state.currentView !== "reports") return;',
            'if (error.message === "auth") return;',
        ):
            self.assertIn(marker, load_source)

    def test_invalid_range_clears_preview_and_old_success_or_error_cannot_overwrite_new_state(self):
        current_range = re.search(r"(function currentReportRange\(\) \{.*?\n    \})", self.source, re.S)
        valid_range = re.search(r"(function reportRangeIsValid\(range\) \{.*?\n    \})", self.source, re.S)
        self.assertIsNotNone(current_range)
        self.assertIsNotNone(valid_range)
        self.assertIsNotNone(self.load)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = (
            '"use strict";\n'
            "const state = { currentView: 'reports' };\n"
            "let reportRangeState = { preset: 'custom', start: '2026-03-01', end: '2026-03-31' };\n"
            "let reportLoadGeneration = 0;\n"
            "const requests = [];\n"
            "const region = { innerHTML: 'old report', setAttribute() {} };\n"
            "const content = { querySelector() { return region; } };\n"
            "const document = { getElementById() { return content; } };\n"
            "const escapeHtml = value => String(value ?? '');\n"
            "const renderReportData = (_content, report, range) => { region.innerHTML = `report:${range.start_date}:${report.marker}`; };\n"
            "const apiGet = () => new Promise((resolve, reject) => requests.push({ resolve, reject }));\n"
            + current_range.group(1)
            + "\n"
            + valid_range.group(1)
            + "\n"
            + self.load.group(0)
            + r'''
(async () => {
  const oldSuccess = loadReportPreview();
  reportRangeState.end = '';
  await loadReportPreview();
  const invalidNotice = region.innerHTML;
  requests[0].resolve({ marker: 'obsolete-success' });
  await oldSuccess;
  const afterOldSuccess = region.innerHTML;

  reportRangeState = { preset: 'custom', start: '2026-04-01', end: '2026-04-30' };
  const oldError = loadReportPreview();
  reportRangeState = { preset: 'custom', start: '2026-05-01', end: '2026-05-31' };
  const current = loadReportPreview();
  requests[2].resolve({ marker: 'current' });
  await current;
  const currentPreview = region.innerHTML;
  requests[1].reject(new Error('obsolete failure'));
  await oldError;
  process.stdout.write(JSON.stringify({ requests: requests.length, invalidNotice, afterOldSuccess, currentPreview, afterOldError: region.innerHTML }));
})();
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["requests"], 3)
        self.assertIn("rango", output["invalidNotice"].lower())
        self.assertIn("válido", output["invalidNotice"].lower())
        self.assertEqual(output["afterOldSuccess"], output["invalidNotice"])
        self.assertEqual(output["currentPreview"], "report:2026-05-01:current")
        self.assertEqual(output["afterOldError"], output["currentPreview"])

    def test_preview_shows_loading_state_and_escaped_failure_notice(self):
        self.assertIn('role="status">Generando informe...</div>', self.source)
        self.assertIn("No se pudo generar el informe.", self.source)
        load_source = self.load.group(0)
        self.assertIn("escapeHtml(error.message)", load_source)
        # The notice never injects server-provided error text unescaped.
        self.assertNotIn("`No se pudo generar el informe. ${error", load_source)

    def test_custom_range_validation_requires_both_calendar_dates_in_order(self):
        valid = re.search(r"function reportRangeIsValid\(range\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(valid)
        valid_source = valid.group(0)
        for marker in (
            "/^\\d{4}-\\d{2}-\\d{2}$/",
            'new Date(`${range.start_date}T00:00:00Z`)',
            'new Date(`${range.end_date}T00:00:00Z`)',
            "range.start_date <= range.end_date",
        ):
            self.assertIn(marker, valid_source)


class AuditReportsMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.row = re.search(
            r"function reportMatrixRowHtml\(row\) \{.*?\n    \}",
            cls.source,
            re.S,
        )

    def test_matrix_headers_cover_the_requested_audit_fields(self):
        for label in (
            "Servicio",
            "Servidor",
            "Periodicidad",
            "Retenci&oacute;n",
            ">Exitoso %<",
            ">Fallido %<",
            "Bytes procesados",
            "Bytes agregados al backend",
            "Ubicaci&oacute;n",
        ):
            self.assertIn(label, self.source)

    def test_matrix_includes_success_failure_percentages_per_target(self):
        self.assertIsNotNone(self.row)
        row_source = self.row.group(0)
        for marker in (
            "succeeded_percent", "failed_percent", "job_counts",
            "keep_last", "keep_hourly", "keep_daily", "keep_weekly",
            "keep_monthly", "keep_yearly", "prune",
        ):
            self.assertIn(marker, row_source)

    def test_matrix_reports_separate_processed_and_added_size_with_coverage(self):
        self.assertIsNotNone(self.row)
        row_source = self.row.group(0)
        for marker in (
            "bytes_processed",
            "bytes_added",
            "reported_jobs",
            "unavailable_jobs",
        ):
            self.assertIn(marker, row_source)
        # Percentages and sizes are never invented: missing values render as
        # "n/a" rather than zero.
        self.assertIn("n/a", row_source)

    def test_matrix_uses_storage_location_fields(self):
        row_source = self.row.group(0)
        for marker in ("backend_type", "storage_profile_name", "path_storage"):
            self.assertIn(marker, row_source)


class AuditReportsRowBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_row_renderer_escapes_untrusted_fields_and_marks_unavailable_sizes(self):
        row = re.search(
            r"(function reportMatrixRowHtml\(row\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        self.assertIsNotNone(row)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = (
            '"use strict";\n'
            + row.group(1)
            + "\n"
            + r'''
const escapeHtml = (str) => {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
};
const formatJobBytes = (value) => `${value} B`;
const sample = {
  service: 'db<script>alert(1)</script>',
  server: { name: "srv-01", host: '10.0.0.1"' },
  schedule: { cron_expression: "0 2 * * *", source: "target", timezone: "America/Bogota" },
  retention: { name: "30-days", keep_last: 30, keep_hourly: 24, keep_daily: 7, keep_weekly: 4, keep_monthly: 12, keep_yearly: 2, prune: true },
  job_counts: { succeeded: 3, failed: 1 },
  succeeded_percent: 75,
  failed_percent: 25,
  backup_size: {
    bytes_processed: 1234,
    bytes_added: 567,
    coverage: {
      bytes_processed: { reported_jobs: 4, unavailable_jobs: 0 },
      bytes_added: { reported_jobs: 3, unavailable_jobs: 1 },
    },
  },
  storage_location: { backend_type: "rclone", storage_profile_name: "s3-prod", path_storage: "s3:prod/vault" },
  target_id: "t-1",
};
const missing = {
  service: "svc",
  server: {},
  schedule: {},
  retention: {},
  job_counts: {},
  succeeded_percent: null,
  failed_percent: null,
  backup_size: {},
  storage_location: {},
  target_id: "t-2",
};
process.stdout.write(JSON.stringify({ html: reportMatrixRowHtml(sample), missing: reportMatrixRowHtml(missing) }));
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        # No untrusted markup survives; the escaped report fields are visible.
        self.assertNotIn("<script>", output["html"])
        self.assertIn("&lt;script&gt;", output["html"])
        self.assertIn("10.0.0.1&quot;", output["html"])
        self.assertIn("0 2 * * *", output["html"])
        self.assertIn("America/Bogota", output["html"])
        self.assertIn("30-days", output["html"])
        self.assertIn("por hora 24", output["html"])
        self.assertIn("por semana 4", output["html"])
        self.assertIn("por mes 12", output["html"])
        self.assertIn("por año 2", output["html"])
        self.assertIn("podar sí", output["html"])
        self.assertIn("75", output["html"])
        self.assertIn("25", output["html"])
        self.assertIn("1234 B", output["html"])
        self.assertIn("567 B", output["html"])
        self.assertIn("4/4", output["html"])
        self.assertIn("3/4", output["html"])
        self.assertIn("rclone", output["html"])
        self.assertIn("s3:prod/vault", output["html"])
        # Missing values are honest: n/a, never zero, and readable placeholders.
        self.assertIn("n/a", output["missing"])
        self.assertIn("—", output["missing"])
        self.assertNotIn("<script>", output["missing"])

    def test_matrix_rows_interpolate_through_escapeHtml_not_raw_row_fields(self):
        row = re.search(
            r"function reportMatrixRowHtml\(row\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        self.assertIsNotNone(row)
        row_source = row.group(0)
        # Every interpolation of report data goes through escapeHtml or a
        # number formatting helper; no raw `${row.` interpolation remains.
        self.assertNotRegex(row_source, r"\$\{row\.")
        self.assertIn("escapeHtml(", row_source)


class AuditReportsDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_downloads_use_only_the_four_authenticated_export_formats(self):
        self.assertIn('const REPORT_EXPORT_FORMATS = ["json", "csv", "xlsx", "pdf"];', self.source)
        formats = re.search(r"const REPORT_EXPORT_FORMATS = \[(.*?)\];", self.source, re.S)
        self.assertIsNotNone(formats)
        self.assertEqual([item.strip().strip('"') for item in formats.group(1).split(",")], ["json", "csv", "xlsx", "pdf"])
        # Anchor markup is generated for every format from the shared list.
        self.assertIn('data-report-download="${fmt}"', self.source)
        self.assertIn('Descargar ${labels[fmt]}</a>`', self.source)

    def test_download_links_hit_the_existing_attachment_endpoints(self):
        self.assertIn('`/api/v1/reports/backups/download/${format}?${params.toString()}`', self.source)
        url = re.search(r"function reportDownloadUrl\(range, format\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(url)
        url_source = url.group(0)
        self.assertIn('new URLSearchParams({ start_date: range.start_date, end_date: range.end_date })', url_source)
        self.assertNotIn("token", url_source)
        self.assertNotIn("apiPost", url_source)

    def test_report_download_anchors_are_native_attachment_downloads(self):
        self.assertIn('download="${fmt}"', self.source)
        # The SPA never opens the report downloads in a new tab; the
        # Content-Disposition of the endpoint is authoritative.
        self.assertNotIn('download="${fmt}" target="_blank"', self.source)
        self.assertIn("Descargar", self.source)

    def test_invalid_ranges_disable_downloads_and_never_render_endpoints(self):
        disabled = re.search(r"function reportDownloadButtonsHtml\(range\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(disabled)
        source = disabled.group(0)
        self.assertIn("reportRangeIsValid(range)", source)
        self.assertIn("reportDownloadUrl(range, fmt)", source)
        self.assertIn("disabled", source)


class AuditReportsLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.styles = UI_COMPONENTS_CSS_PATH.read_text(encoding="utf-8")

    def test_report_preview_has_section_spacing_and_a_readable_scrollable_matrix(self):
        self.assertIn('<div id="reportPreviewRegion" class="report-preview-region"', self.source)
        self.assertIn('<div class="table-wrap report-table-wrap">', self.source)
        self.assertIn('<table class="table report-table"', self.source)
        for marker in (
            ".report-preview-region {",
            "gap: 16px;",
            ".report-table-wrap .sheet {",
            "min-width: 1550px;",
            ".report-table {",
            "table-layout: fixed;",
            "overflow-wrap: anywhere;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.styles)

    def test_report_preview_omits_verbose_coverage_banner_but_keeps_metric_coverage(self):
        self.assertNotIn("report-coverage-note", self.source)
        self.assertNotIn(".report-coverage-note", self.styles)
        self.assertIn("coverageText(metrics.bytes_processed)", self.source)
        self.assertIn("coverageText(metrics.bytes_added)", self.source)


class AuditReportsDownloadUrlBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_download_url_is_a_same_origin_authenticated_endpoint_with_bounded_dates(self):
        url = re.search(
            r"(function reportDownloadUrl\(range, format\) \{.*?\n    \})",
            self.source,
            re.S,
        )
        self.assertIsNotNone(url)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = (
            '"use strict";\n'
            + url.group(1)
            + "\n"
            + r'''
const fmts = {};
for (const fmt of ["json", "csv", "xlsx", "pdf"]) {
  fmts[fmt] = reportDownloadUrl({ start_date: "2026-04-01", end_date: "2026-04-30" }, fmt);
}
process.stdout.write(JSON.stringify(fmts));
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        for fmt in ("json", "csv", "xlsx", "pdf"):
            self.assertEqual(
                output[fmt],
                f"/api/v1/reports/backups/download/{fmt}?start_date=2026-04-01&end_date=2026-04-30",
            )