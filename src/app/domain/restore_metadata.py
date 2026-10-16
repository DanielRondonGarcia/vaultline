"""Bounded, read-only Restic ownership evidence."""

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Optional

from src.app.domain.restore_ownership import RestoreOwnershipPolicy

MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_RECORDS = 1_000_000
MAX_HEADER_BYTES = 8 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_FILE_SAMPLES = 64
MAX_DISTINCT_OWNERS = 4096
_RESTIC_MODE = r"[-?bcCdDlLpPs](?:[rwxXsStT-]{9}|[tT-][rwxXsStT-]{9})"
_RECORD = re.compile(
    r"^(?P<mode>" + _RESTIC_MODE + r")\s+(?P<uid>\d+)\s+(?P<gid>\d+)\s+(?P<size>\d+)\s+"
    r"(?P<mtime>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<path>.+)$"
)
_HEADER_TIMESTAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,9})? [+-](?:[01]\d|2[0-3])[0-5]\d (?:[A-Za-z][A-Za-z0-9+-]{0,9}|[+-]\d{1,2})"
_RESTIC_LS_HEADER = re.compile(
    r"^snapshot (?P<snapshot>[0-9a-fA-F]{8,64}) of \[[^\]\r\n]{0,4096}\] "
    r"(?:filtered by \[[^\]\r\n]{0,4096}\] at (?P<filtered_at>" + _HEADER_TIMESTAMP + r")|"
    r"at (?P<listed_at>" + _HEADER_TIMESTAMP + r")"
    r"(?: by (?P<identity>(?:[^@\r\n]*@)+[^@\r\n]*))? filtered by \[[^\]\r\n]{0,4096}\])\:$"
)


class ResticMetadataError(ValueError):
    def __init__(self, message: str, category: str = "restic_metadata_inspection_failed"):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class ResticFileMetadata:
    path: str
    mode: str
    uid: int
    gid: int
    size: int
    mtime: datetime

    @property
    def owner(self) -> str: return f"{self.uid}:{self.gid}"
    @property
    def owner_label(self) -> str: return "file_owner"


@dataclass(frozen=True)
class ResticMetadataEvidence:
    snapshot: str = ""
    files: tuple[ResticFileMetadata, ...] = ()
    creator_uid: Optional[int] = None
    creator_gid: Optional[int] = None
    policy: RestoreOwnershipPolicy = field(default_factory=RestoreOwnershipPolicy)
    category: str = "ok"
    detail: Optional[str] = None
    total_files: int = 0
    owner_counts: tuple[tuple[str, int], ...] = ()
    owners_truncated: bool = False
    files_truncated: bool = False

    @property
    def success(self) -> bool: return self.category == "ok"
    @property
    def owners(self) -> tuple[str, ...]:
        if self.owner_counts:
            return tuple(owner for owner, _count in self.owner_counts)
        return tuple(sorted({item.owner for item in self.files}))
    @property
    def ownership_classification(self) -> str:
        if not self.total_files and not self.files:
            return "empty"
        return "dominant" if len(self.owners) == 1 and not self.owners_truncated else "mixed"
    @property
    def creator_owner(self) -> Optional[str]:
        return None if self.creator_uid is None or self.creator_gid is None else f"{self.creator_uid}:{self.creator_gid}"
    @property
    def backup_creator_label(self) -> str: return "backup_creator"
    @property
    def inferred_mapping(self) -> None: return None
    @property
    def restored_metadata_proven(self) -> bool: return False


def _policy(value: Any) -> RestoreOwnershipPolicy:
    try:
        result = value if isinstance(value, RestoreOwnershipPolicy) else RestoreOwnershipPolicy.from_dict(value or {})
        return result.require_confirmation()
    except ValueError as exc:
        raise ResticMetadataError(str(exc), getattr(exc, "category", "confirmation_required")) from exc


class ResticMetadataStreamParser:
    """Incrementally validate Restic's plain-text ls --long output."""

    def __init__(
        self,
        *,
        snapshot: str = "",
        creator: Optional[tuple[int, int]] = None,
        policy: Any = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        max_records: int = MAX_RECORDS,
        max_header_bytes: int = MAX_HEADER_BYTES,
        max_record_bytes: int = MAX_RECORD_BYTES,
        max_file_samples: int = MAX_FILE_SAMPLES,
        max_distinct_owners: int = MAX_DISTINCT_OWNERS,
        decode_errors: str = "replace",
    ):
        self.snapshot = snapshot
        self.creator = creator
        self.policy = policy
        self.max_output_bytes = max_output_bytes
        self.max_records = max_records
        self.max_header_bytes = max_header_bytes
        self.max_record_bytes = max_record_bytes
        self.max_file_samples = max_file_samples
        self.max_distinct_owners = max_distinct_owners
        self.decode_errors = decode_errors
        self.output_bytes = 0
        self.total_files = 0
        self.header_checked = False
        self.files: list[ResticFileMetadata] = []
        self.owner_counts: dict[str, int] = {}
        self.owners_truncated = False

    def feed(self, raw_line: str | bytes) -> None:
        if isinstance(raw_line, bytes):
            raw = raw_line
        elif isinstance(raw_line, str):
            try:
                raw = raw_line.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ResticMetadataError("Restic metadata output is not valid UTF-8") from exc
        else:
            raise ResticMetadataError("Restic metadata output is incomplete")

        observed_bytes = self.output_bytes + len(raw)
        if observed_bytes > self.max_output_bytes:
            raise ResticMetadataError(
                "Restic metadata inspection output exceeded the bounded byte limit "
                f"(observed at least {observed_bytes} bytes; configured limit {self.max_output_bytes} bytes)"
            )
        self.output_bytes = observed_bytes

        content = raw.rstrip(b"\r\n")
        try:
            line = content.decode("utf-8", errors=self.decode_errors)
        except UnicodeDecodeError as exc:
            raise ResticMetadataError("Restic metadata output is not valid UTF-8") from exc
        if not line.strip():
            return

        if line.startswith("snapshot "):
            if self.header_checked:
                raise ResticMetadataError("Restic metadata inspection output contains a malformed header")
            if len(content) > self.max_header_bytes:
                raise ResticMetadataError(
                    "Restic metadata inspection header exceeded the line limit "
                    f"(observed {len(content)} bytes; configured limit {self.max_header_bytes} bytes)"
                )
            header = _RESTIC_LS_HEADER.fullmatch(line)
            if header is None:
                raise ResticMetadataError("Restic metadata inspection output contains a malformed header")
            self.header_checked = True
            self._validate_header(header)
            return
        if not self.header_checked:
            self.header_checked = True

        if len(content) > self.max_record_bytes:
            raise ResticMetadataError(
                "Restic metadata record exceeded the line limit "
                f"(observed {len(content)} bytes; configured limit {self.max_record_bytes} bytes)"
            )
        if self.total_files >= self.max_records:
            raise ResticMetadataError(
                "Restic metadata inspection output exceeded the record bound "
                f"(observed at least {self.total_files + 1} records; configured limit {self.max_records} records)"
            )

        match = _RECORD.fullmatch(line)
        if match is None:
            raise ResticMetadataError(
                f"Restic metadata inspection output contains a malformed record at record {self.total_files + 1}"
            )
        try:
            values = match.groupdict()
            record = ResticFileMetadata(
                values["path"], values["mode"], int(values["uid"]), int(values["gid"]),
                int(values["size"]), datetime.fromisoformat(values["mtime"]),
            )
        except (TypeError, ValueError) as exc:
            raise ResticMetadataError(
                f"Restic metadata inspection output contains an invalid record at record {self.total_files + 1}"
            ) from exc

        self.total_files += 1
        if len(self.files) < self.max_file_samples:
            self.files.append(record)
        if record.owner in self.owner_counts:
            self.owner_counts[record.owner] += 1
        elif len(self.owner_counts) < self.max_distinct_owners:
            self.owner_counts[record.owner] = 1
        else:
            self.owners_truncated = True

    def _validate_header(self, header: re.Match[str]) -> None:
        timestamp = header.group("filtered_at") or header.group("listed_at")
        try:
            datetime.fromisoformat(timestamp[:19])
        except ValueError as exc:
            raise ResticMetadataError("Restic metadata inspection output contains a malformed header") from exc
        requested_snapshot = self.snapshot.lower()
        header_snapshot = header.group("snapshot").lower()
        if re.fullmatch(r"[0-9a-f]{8,64}", requested_snapshot) and not (
            requested_snapshot.startswith(header_snapshot) or header_snapshot.startswith(requested_snapshot)
        ):
            raise ResticMetadataError("Restic metadata header does not match the requested snapshot")

    def finish(self) -> ResticMetadataEvidence:
        if not self.total_files:
            raise ResticMetadataError("Restic metadata inspection output is incomplete or over the record bound")
        creator_uid, creator_gid = self.creator or (None, None)
        return ResticMetadataEvidence(
            snapshot=self.snapshot,
            files=tuple(self.files),
            creator_uid=creator_uid,
            creator_gid=creator_gid,
            policy=_policy(self.policy),
            total_files=self.total_files,
            owner_counts=tuple(sorted(self.owner_counts.items())),
            owners_truncated=self.owners_truncated,
            files_truncated=self.total_files > len(self.files),
        )


def parse_restic_ls_long(
    output: str | bytes, *, snapshot: str = "", creator: Optional[tuple[int, int]] = None,
    policy: Any = None, max_output_bytes: int = MAX_OUTPUT_BYTES, max_records: int = MAX_RECORDS,
    max_header_bytes: int = MAX_HEADER_BYTES, max_record_bytes: int = MAX_RECORD_BYTES,
) -> ResticMetadataEvidence:
    if isinstance(output, bytes):
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResticMetadataError("Restic metadata output is not valid UTF-8") from exc
    elif isinstance(output, str):
        text = output
    else:
        raise ResticMetadataError("Restic metadata output is incomplete")

    parser = ResticMetadataStreamParser(
        snapshot=snapshot,
        creator=creator,
        policy=policy,
        max_output_bytes=max_output_bytes,
        max_records=max_records,
        max_header_bytes=max_header_bytes,
        max_record_bytes=max_record_bytes,
        decode_errors="strict",
    )
    for line in text.splitlines(keepends=True):
        parser.feed(line)
    return parser.finish()
