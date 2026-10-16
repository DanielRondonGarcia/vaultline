import io
from types import SimpleNamespace
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.app.application.services.restore_service import RestoreService
from src.app.domain.models import RestoreConfig
from src.app.domain.restore_metadata import ResticMetadataError, ResticMetadataStreamParser, parse_restic_ls_long
from src.app.domain.restore_ownership import RestoreOwnershipPolicy
from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter


def line(mode="-rw-r--r--", uid=0, gid=0, size=12, path="/data/file"):
    return f"{mode} {uid} {gid} {size} 2026-01-02 03:04:05 {path}"


def header(snapshot="073a90db", variant="filtered-first"):
    if variant == "filtered-first":
        return f"snapshot {snapshot} of [/home/user/work.txt] filtered by [] at 2024-01-21 16:51:18.474558607 +0100 CET:"
    return f"snapshot {snapshot} of [/tmp/restic] at 2025-02-03 08:16:05.310764668 +0000 UTC filtered by []:"


class FakeProcess:
    def __init__(self, stdout, stderr=b"", returncode=0):
        self.stdout = stdout if hasattr(stdout, "readline") else io.BytesIO(stdout)
        self.stderr = stderr if hasattr(stderr, "read") else io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.returncode if self.waited else None

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def kill(self):
        self.returncode = -9


class LazyListing:
    def __init__(self, records, *, owner_for_record=None, path_padding=0):
        self.records = records
        self.owner_for_record = owner_for_record or (lambda _index: (1000, 1000))
        self.path_padding = path_padding
        self.index = -1
        self.bytes_read = 0

    def readline(self, size=-1):
        if self.index == -1:
            self.index = 0
            value = header() + "\n"
        elif self.index < self.records:
            index = self.index
            self.index += 1
            uid, gid = self.owner_for_record(index)
            value = line(uid=uid, gid=gid, path=f"/data/file-{index:05d}-{'x' * self.path_padding}") + "\n"
        else:
            return b""
        raw = value.encode("utf-8")
        if size >= 0 and len(raw) > size:
            raise AssertionError("test record exceeded the adapter's bounded readline size")
        self.bytes_read += len(raw)
        return raw

    def close(self):
        pass


class TrackingStream:
    def __init__(self, value):
        self.value = value
        self.offset = 0
        self.bytes_read = 0

    def read(self, size=-1):
        if size < 0:
            size = len(self.value) - self.offset
        chunk = self.value[self.offset:self.offset + size]
        self.offset += len(chunk)
        self.bytes_read += len(chunk)
        return chunk

    def close(self):
        pass


class RestoreMetadataTests(unittest.TestCase):
    def test_valid_records_mixed_owners_and_file_fields(self):
        evidence = parse_restic_ls_long(
            "\n".join((line(), line(uid=1000, gid=1000, path="/data/n8n"))), snapshot="snap"
        )
        self.assertEqual(
            (evidence.files[0].mode, evidence.files[1].uid, evidence.files[1].gid, evidence.files[1].size),
            ("-rw-r--r--", 1000, 1000, 12),
        )
        self.assertEqual(evidence.files[0].mtime.isoformat(), "2026-01-02T03:04:05")
        self.assertEqual(evidence.files[0].owner_label, "file_owner")
        self.assertEqual(evidence.ownership_classification, "mixed")

    def test_malformed_or_incomplete_output_fails_closed(self):
        valid_header = header()
        for output in (
            "",
            "not a restic record",
            line().replace(" 12 ", " "),
            valid_header,
            f"{valid_header}{' ' * 8192}\n{line()}",
            f"{valid_header}\nnot a restic record",
            f"{line()}\n{valid_header}\n{line()}",
            f"{valid_header}\n{valid_header}\n{line()}",
        ):
            with self.subTest(output=output), self.assertRaisesRegex(ResticMetadataError, "inspection"):
                parse_restic_ls_long(output)

    def test_documented_header_variants_precede_records(self):
        for snapshot, variant in (("073a90db", "filtered-first"), ("711b0bb6", "time-first")):
            with self.subTest(variant=variant):
                evidence = parse_restic_ls_long(
                    f"{header(snapshot, variant)}\n{line(path='/data/ni\u00f1o')}", snapshot=snapshot
                )
                self.assertEqual([item.path for item in evidence.files], ["/data/ni\u00f1o"])

    def test_restic_long_format_accepts_sticky_directory_mode(self):
        evidence = parse_restic_ls_long(line(mode="dtrwxrwxrwx", path="/tmp"))

        self.assertEqual(evidence.files[0].mode, "dtrwxrwxrwx")

    def test_header_snapshot_must_match_hex_snapshot_prefix(self):
        with self.assertRaisesRegex(ResticMetadataError, "does not match"):
            parse_restic_ls_long(f"{header()}\n{line()}", snapshot="deadbeef")
        self.assertTrue(parse_restic_ls_long(f"{header()}\n{line()}", snapshot="latest").success)

    def test_bounds_creator_labels_and_no_mapping(self):
        with self.assertRaisesRegex(ResticMetadataError, "bounded"):
            parse_restic_ls_long(line() * 2, max_output_bytes=40)
        self.assertTrue(parse_restic_ls_long(f"{header()}\n{line()}", max_records=1).success)
        with self.assertRaisesRegex(ResticMetadataError, "record bound"):
            parse_restic_ls_long(f"{header()}\n{line()}\n{line(path='/data/second')}", max_records=1)
        evidence = parse_restic_ls_long(line(uid=1000, gid=1000), creator=(0, 0))
        self.assertEqual(
            (evidence.creator_owner, evidence.backup_creator_label, evidence.inferred_mapping, evidence.restored_metadata_proven),
            ("0:0", "backup_creator", None, False),
        )

    def test_adapter_uses_argv_only_preserves_by_default_and_handles_failure(self):
        process = FakeProcess(line().encode("utf-8"), b"warning: non-root metadata was not applied")
        process_factory = Mock(return_value=process)
        result = ResticMetadataInspectorAdapter(process_factory=process_factory).inspect(
            "snap-1", SimpleNamespace(restic_repository="local:/repo", restic_password="secret")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.policy.mode, "preserve")
        args, kwargs = process_factory.call_args
        self.assertEqual(args[0], ["restic", "ls", "--long", "snap-1"])
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("bufsize", kwargs)
        self.assertEqual(kwargs["stdout"], -1)
        self.assertEqual(kwargs["stderr"], -1)
        self.assertEqual(kwargs["env"]["RESTIC_REPOSITORY"], "local:/repo")
        failed_process = FakeProcess(b"", b"private detail", returncode=1)
        failed = ResticMetadataInspectorAdapter(process_factory=Mock(return_value=failed_process)).inspect("snap-1")
        self.assertFalse(failed.success)
        self.assertEqual(failed.category, "restic_metadata_inspection_failed")
        self.assertIn("exit status 1", failed.detail)
        self.assertIn("private detail", failed.detail)
        self.assertTrue(failed_process.waited)

    def test_frozen_metadata_inspection_uses_bundled_restic_not_host_path(self):
        process_factory = Mock(return_value=FakeProcess(line().encode("utf-8")))
        executable_name = "restic.exe" if sys.platform == "win32" else "restic"
        rclone_name = "rclone.exe" if sys.platform == "win32" else "rclone"

        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / executable_name
            bundled.write_text("synthetic bundled executable", encoding="utf-8")
            bundled.chmod(0o755)
            bundled_rclone = Path(directory) / rclone_name
            bundled_rclone.write_text("synthetic bundled executable", encoding="utf-8")
            bundled_rclone.chmod(0o755)
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "_MEIPASS", directory, create=True
            ):
                result = ResticMetadataInspectorAdapter(process_factory=process_factory).inspect("snap-1")

        self.assertTrue(result.success)
        self.assertEqual(process_factory.call_args.args[0], [str(bundled), "ls", "--long", "snap-1"])

    def test_adapter_decodes_metadata_as_utf8_with_replacement(self):
        raw_output = (
            (header("35bbb727") + "\n" + line(path="/data/ni\u00f1o") + "\n").encode("utf-8")
            + line(path="/data/bad").encode("utf-8")
            + b"\x81\n"
        )
        result = ResticMetadataInspectorAdapter(
            process_factory=Mock(return_value=FakeProcess(raw_output))
        ).inspect("35bbb727")

        self.assertTrue(result.success)
        self.assertEqual([item.path for item in result.files], ["/data/ni\u00f1o", "/data/bad\ufffd"])

    def test_adapter_streams_large_listing_with_bounded_samples_and_exact_owner_counts(self):
        listing = LazyListing(
            3000,
            owner_for_record=lambda index: (0, 0) if index == 2999 else (1000, 1000),
            path_padding=100,
        )
        process = FakeProcess(listing)
        result = ResticMetadataInspectorAdapter(
            process_factory=Mock(return_value=process)
        ).inspect("073a90db", SimpleNamespace(snapshot_creator=(42, 43)))

        self.assertTrue(result.success)
        self.assertGreater(listing.bytes_read, 256 * 1024)
        self.assertEqual(result.total_files, 3000)
        self.assertEqual(len(result.files), 64)
        self.assertTrue(result.files_truncated)
        self.assertEqual(result.files[0].path, "/data/file-00000-" + "x" * 100)
        self.assertEqual(result.creator_owner, "42:43")
        self.assertEqual(result.owner_counts, (("0:0", 1), ("1000:1000", 2999)))
        self.assertEqual(result.owners, ("0:0", "1000:1000"))
        self.assertEqual(result.ownership_classification, "mixed")
        self.assertFalse(result.owners_truncated)
        self.assertTrue(process.waited)
        self.assertFalse(process.terminated)

    def test_valid_header_uses_header_limit_independently_of_record_limit(self):
        process = FakeProcess((header() + "\n" + line() + "\n").encode("utf-8"))
        result = ResticMetadataInspectorAdapter(
            process_factory=Mock(return_value=process),
            max_record_bytes=64,
            max_header_bytes=256,
        ).inspect("073a90db")

        self.assertTrue(result.success)
        self.assertEqual(result.total_files, 1)
        self.assertEqual(len(result.files), 1)
        self.assertTrue(process.waited)
        self.assertFalse(process.terminated)

    def test_owner_aggregate_overflow_never_claims_a_dominant_owner(self):
        parser = ResticMetadataStreamParser(max_distinct_owners=2)
        for uid, gid in ((10, 20), (30, 40), (50, 60), (50, 60)):
            parser.feed((line(uid=uid, gid=gid) + "\n").encode("utf-8"))

        evidence = parser.finish()

        self.assertEqual(evidence.total_files, 4)
        self.assertEqual(evidence.owner_counts, (("10:20", 1), ("30:40", 1)))
        self.assertTrue(evidence.owners_truncated)
        self.assertEqual(evidence.ownership_classification, "mixed")

    def test_streaming_output_byte_record_and_line_limits_fail_with_observed_caps(self):
        cases = (
            (
                "bytes",
                FakeProcess((line() + "\n").encode("utf-8")),
                {"max_output_bytes": 40},
                "observed at least",
                "configured limit 40 bytes",
            ),
            (
                "records",
                FakeProcess(LazyListing(3)),
                {"max_records": 2},
                "observed at least 3 records",
                "configured limit 2 records",
            ),
            (
                "record line",
                FakeProcess((line(path="/data/" + "x" * 100) + "\n").encode("utf-8")),
                {"max_record_bytes": 80},
                "record exceeded the line limit",
                "configured limit 80 bytes",
            ),
            (
                "header line",
                FakeProcess((header() + "\n" + line() + "\n").encode("utf-8")),
                {"max_header_bytes": 32},
                "header exceeded the line limit",
                "configured limit 32 bytes",
            ),
        )
        for name, process, limits, observed, configured in cases:
            with self.subTest(limit=name):
                result = ResticMetadataInspectorAdapter(
                    process_factory=Mock(return_value=process), **limits
                ).inspect("073a90db")

                self.assertFalse(result.success)
                self.assertIn(observed, result.detail)
                self.assertIn(configured, result.detail)
                self.assertTrue(process.terminated)
                self.assertTrue(process.waited)

    def test_stderr_is_drained_but_only_a_redacted_bounded_tail_is_reported(self):
        stderr = TrackingStream(b"x" * (32 * 1024) + b" token=private diagnostic tail")
        process = FakeProcess((line() + "\n").encode("utf-8"), stderr, returncode=7)
        result = ResticMetadataInspectorAdapter(
            process_factory=Mock(return_value=process), max_stderr_bytes=1024
        ).inspect("snap-1")

        self.assertFalse(result.success)
        self.assertEqual(result.category, "restic_metadata_inspection_failed")
        self.assertEqual(stderr.bytes_read, len(stderr.value))
        self.assertIn("exit status 7", result.detail)
        self.assertIn("diagnostic tail", result.detail)
        self.assertIn("token=<redacted>", result.detail)
        self.assertNotIn("private", result.detail)
        self.assertLessEqual(len(result.detail), 560)
        self.assertTrue(process.waited)
        self.assertFalse(process.terminated)

    def test_malformed_stream_reaps_child_and_restore_preflight_does_not_mutate_target(self):
        process = FakeProcess((line() + "\nnot a restic record\n").encode("utf-8"))
        inspector = ResticMetadataInspectorAdapter(process_factory=Mock(return_value=process))
        storage, container, strategy = Mock(), Mock(), Mock()

        with tempfile.TemporaryDirectory() as target:
            marker = Path(target) / "must-remain.txt"
            marker.write_text("preserve", encoding="utf-8")
            config = RestoreConfig(
                target,
                source="073a90db",
                dry_run=False,
                force_overwrite=True,
                backup_strategy="restic",
                layout="direct",
            )
            result = RestoreService(
                storage,
                container,
                strategy,
                config,
                metadata_inspector=inspector,
            ).execute_restore()

            self.assertFalse(result.success)
            self.assertEqual(result.category, "restic_metadata_inspection_failed")
            self.assertEqual(result.destructive_state, "none")
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        strategy.restore.assert_not_called()
        storage.download_restore_candidate.assert_not_called()
        container.stop_containers.assert_not_called()

    def test_unconfirmed_mapping_fails_before_restic(self):
        process_factory = Mock()
        result = ResticMetadataInspectorAdapter(process_factory=process_factory).inspect(
            "snap", policy=RestoreOwnershipPolicy(mode="map", mappings={"compose:app:data": "1000:1000"})
        )
        self.assertEqual(result.category, "confirmation_required")
        process_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
