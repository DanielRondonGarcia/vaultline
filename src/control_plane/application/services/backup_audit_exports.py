"""Authenticated download renderers for the backup audit report.

These functions turn the already-sanitized report dictionary produced by
``build_backup_audit_report`` into downloadable bytes for JSON, CSV, Excel
(.xlsx), and PDF. They never touch persisted data, secrets, the filesystem, or
the job history; they only serialize the report they are given, so the T2
no-secrets invariant is preserved by construction.

Backup size is intentionally surfaced as ``None``/unavailable: the persisted
``backup.run`` job ``result_summary`` records only a fractional progress value
and a sanitized ``storage_context``, not an absolute byte total, and
``SnapshotRecord.summary`` is replaced wholesale per target with no reliable
date-bound tie to the report period. Aggregating either would be inference, so
the report leaves size unavailable and documents the gap instead.

The storage location is derived only from safe, non-credential-bearing
fields: ``BackupTargetRecord.path_storage`` (a relative POSIX sub-path) and the
linked ``StorageProfileRecord.backend_type``/``name``. Repository URLs, secret
IDs, runtime environment, and secret values are never exported.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Callable, Dict, Tuple

BACKUP_AUDIT_MATRIX_COLUMNS: Tuple[str, ...] = (
    "target_id",
    "service",
    "server_name",
    "server_host",
    "runtime_type",
    "backup_strategy",
    "backup_mode",
    "enabled",
    "schedule_cron",
    "schedule_source",
    "schedule_timezone",
    "retention_name",
    "keep_last",
    "keep_hourly",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "keep_yearly",
    "prune",
    "total",
    "completed",
    "succeeded",
    "failed",
    "other",
    "succeeded_percent",
    "failed_percent",
    "backup_size_bytes",
    "storage_backend_type",
    "storage_profile_name",
    "path_storage",
)

_CONTENT_TYPES: Dict[str, str] = {
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def _spreadsheet_safe_cell(value: Any) -> Any:
    """Prevent untrusted text cells from being evaluated as spreadsheet formulas."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _flatten_target(row: Dict[str, Any]) -> list:
    retention = row.get("retention") or {}
    schedule = row.get("schedule") or {}
    server = row.get("server") or {}
    counts = row.get("job_counts") or {}
    location = row.get("storage_location") or {}
    return [
        row.get("target_id"),
        row.get("service"),
        server.get("name"),
        server.get("host"),
        row.get("runtime_type"),
        row.get("backup_strategy"),
        row.get("backup_mode"),
        row.get("enabled"),
        schedule.get("cron_expression"),
        schedule.get("source"),
        schedule.get("timezone"),
        retention.get("name"),
        retention.get("keep_last"),
        retention.get("keep_hourly"),
        retention.get("keep_daily"),
        retention.get("keep_weekly"),
        retention.get("keep_monthly"),
        retention.get("keep_yearly"),
        retention.get("prune"),
        counts.get("total"),
        counts.get("completed"),
        counts.get("succeeded"),
        counts.get("failed"),
        counts.get("other"),
        row.get("succeeded_percent"),
        row.get("failed_percent"),
        row.get("backup_size"),
        location.get("backend_type"),
        location.get("storage_profile_name"),
        location.get("path_storage"),
    ]


def export_backup_audit_json(report: Dict[str, Any]) -> bytes:
    return json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")


def export_backup_audit_csv(report: Dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(BACKUP_AUDIT_MATRIX_COLUMNS)
    for row in report.get("matrix") or []:
        writer.writerow([_spreadsheet_safe_cell(value) for value in _flatten_target(row)])
    return buffer.getvalue().encode("utf-8")


def export_backup_audit_xlsx(report: Dict[str, Any]) -> bytes:
    from openpyxl import Workbook

    period = report.get("period") or {}
    availability = report.get("availability") or {}
    size_availability = availability.get("backup_size") or {}
    location_availability = availability.get("storage_location") or {}

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Backup Audit"
    sheet.append(["Backup Audit Report"])
    sheet.append([f"Generated: {report.get('generated_at', '')}"])
    sheet.append(
        [
            f"Period: {period.get('start_date', '')} to {period.get('end_date', '')} "
            f"({period.get('timezone', '')})"
        ]
    )
    if size_availability.get("available"):
        sheet.append(["Backup size: available"])
    else:
        sheet.append(
            [f"Backup size: unavailable - {size_availability.get('reason', '')}"]
        )
    sheet.append(
        [
            "Storage location: "
            + (location_availability.get("basis", "") if location_availability.get("available") else "unavailable")
        ]
    )
    sheet.append([])
    sheet.append(list(BACKUP_AUDIT_MATRIX_COLUMNS))
    for row in report.get("matrix") or []:
        sheet.append([_spreadsheet_safe_cell(value) for value in _flatten_target(row)])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_escape(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("latin-1", "replace")


def _pdf_text_lines(report: Dict[str, Any]) -> list[str]:
    period = report.get("period") or {}
    summary = report.get("summary") or {}
    availability = report.get("availability") or {}
    matrix = report.get("matrix") or []

    def pct(value: Any) -> str:
        return f"{value}%" if value is not None else "n/a"

    lines: list[str] = []
    lines.append("Backup Audit Report")
    lines.append(f"Generated: {report.get('generated_at', '')}")
    lines.append(
        f"Period: {period.get('start_date', '')} to {period.get('end_date', '')} "
        f"({period.get('timezone', '')})"
    )
    lines.append(
        f"Targets: {summary.get('target_count', '')}  "
        f"Backup jobs: {summary.get('backup_job_count', '')}  "
        f"Succeeded: {pct(summary.get('succeeded_percent'))}  "
        f"Failed: {pct(summary.get('failed_percent'))}"
    )
    size_availability = availability.get("backup_size") or {}
    if size_availability.get("available"):
        lines.append("Backup size: available")
    else:
        lines.append(
            f"Backup size: unavailable - "
            f"{size_availability.get('reason', 'no trustworthy historical size source')}"
        )
    location_availability = availability.get("storage_location") or {}
    lines.append(
        "Storage location: "
        + (location_availability.get("basis", "") if location_availability.get("available") else "unavailable")
    )
    lines.append("")
    lines.append("Per-target matrix:")
    for row in matrix:
        server = row.get("server") or {}
        schedule = row.get("schedule") or {}
        location = row.get("storage_location") or {}
        cron = schedule.get("cron_expression") or "manual"
        location_text = location.get("backend_type") or "n/a"
        if location.get("storage_profile_name"):
            location_text += f" [{location.get('storage_profile_name')}]"
        if location.get("path_storage"):
            location_text += f" {location.get('path_storage')}"
        lines.append(f"- {row.get('service', '')} (host: {server.get('host', '')})")
        lines.append(
            f"  strategy={row.get('backup_strategy', '')} mode={row.get('backup_mode', '')} cron={cron}"
        )
        lines.append(
            f"  succeeded={pct(row.get('succeeded_percent'))} failed={pct(row.get('failed_percent'))} "
            f"size=unavailable location={location_text}"
        )
    return lines


def _paginate_lines(lines: list[str]) -> list[list[Tuple[str, float]]]:
    pages: list[list[Tuple[str, float]]] = []
    current: list[Tuple[str, float]] = []
    y = 800.0
    for line in lines:
        if y < 60.0 and current:
            pages.append(current)
            current = []
            y = 800.0
        current.append((line, y))
        y -= 14.0
    if current:
        pages.append(current)
    if not pages:
        pages.append([("Backup Audit Report", 800.0)])
    return pages


def _content_stream(page_lines: list[Tuple[str, float]]) -> bytes:
    parts = []
    for text, y in page_lines:
        parts.append(f"BT /F1 10 Tf 1 0 0 1 50 {y:.1f} Tm (".encode("latin-1"))
        parts.append(_pdf_escape(text))
        parts.append(b") Tj ET\n")
    return b"".join(parts)


def export_backup_audit_pdf(report: Dict[str, Any]) -> bytes:
    pages = _paginate_lines(_pdf_text_lines(report))
    page_count = len(pages)
    page_obj_nums = [4 + 2 * index for index in range(page_count)]
    content_obj_nums = [5 + 2 * index for index in range(page_count)]
    kids = " ".join(f"{num} 0 R" for num in page_obj_nums)

    body = bytearray()
    body += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: Dict[int, int] = {}

    def emit(obj_num: int, data: bytes) -> None:
        offsets[obj_num] = len(body)
        body.extend(f"{obj_num} 0 obj\n".encode("ascii"))
        body.extend(data)
        body.extend(b"\nendobj\n")

    emit(1, b"<</Type /Catalog /Pages 2 0 R>>")
    emit(2, f"<</Type /Pages /Kids [{kids}] /Count {page_count}>>".encode("ascii"))
    emit(3, b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>")
    for index in range(page_count):
        emit(
            page_obj_nums[index],
            (
                f"<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources <</Font <</F1 3 0 R>>>> /Contents {content_obj_nums[index]} 0 R>>"
            ).encode("ascii"),
        )
    for index in range(page_count):
        stream = _content_stream(pages[index])
        emit(
            content_obj_nums[index],
            b"<</Length " + str(len(stream)).encode("ascii") + b">>\nstream\n" + stream + b"\nendstream",
        )

    xref_offset = len(body)
    total_objects = 3 + 2 * page_count
    body.extend(b"xref\n")
    body.extend(f"0 {total_objects + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for obj_num in range(1, total_objects + 1):
        body.extend(f"{offsets[obj_num]:010d} 00000 n \n".encode("ascii"))
    body.extend(b"trailer\n")
    body.extend(f"<</Size {total_objects + 1} /Root 1 0 R>>\n".encode("ascii"))
    body.extend(b"startxref\n")
    body.extend(f"{xref_offset}\n".encode("ascii"))
    body.extend(b"%%EOF")
    return bytes(body)


_EXPORTERS: Dict[str, Callable[[Dict[str, Any]], bytes]] = {
    "json": export_backup_audit_json,
    "csv": export_backup_audit_csv,
    "xlsx": export_backup_audit_xlsx,
    "pdf": export_backup_audit_pdf,
}


def render_backup_audit_export(fmt: str, report: Dict[str, Any]) -> Tuple[str, str, bytes]:
    """Render ``report`` to ``(content_type, filename, body)`` for ``fmt``.

    ``fmt`` must be one of json, csv, xlsx, pdf; any other value raises
    ``ValueError`` so the route maps it to a 400 response.
    """
    normalized = (fmt or "").lower()
    if normalized not in _EXPORTERS:
        raise ValueError(f"unsupported report format: {normalized}")
    period = report.get("period") or {}
    start = str(period.get("start_date") or "")
    end = str(period.get("end_date") or "")
    stamp = f"{start}_{end}" if (start or end) else "report"
    filename = f"backup_audit_{stamp}.{normalized}"
    return _CONTENT_TYPES[normalized], filename, _EXPORTERS[normalized](report)