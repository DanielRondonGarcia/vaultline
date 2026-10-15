from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.app.domain.restore_metadata import ResticMetadataError, parse_restic_ls_long
from src.app.domain.restore_ownership import RestoreOwnershipPolicy
from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter


def line(mode="-rw-r--r--", uid=0, gid=0, size=12, path="/data/file"):
    return f"{mode} {uid} {gid} {size} 2026-01-02 03:04:05 {path}"


def header(snapshot="073a90db", variant="filtered-first"):
    if variant == "filtered-first":
        return f"snapshot {snapshot} of [/home/user/work.txt] filtered by [] at 2024-01-21 16:51:18.474558607 +0100 CET:"
    return f"snapshot {snapshot} of [/tmp/restic] at 2025-02-03 08:16:05.310764668 +0000 UTC filtered by []:"


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
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=line(),
                stderr="warning: non-root metadata was not applied",
            )
        )
        result = ResticMetadataInspectorAdapter(runner=runner).inspect(
            "snap-1", SimpleNamespace(restic_repository="local:/repo", restic_password="secret")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.policy.mode, "preserve")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ["restic", "ls", "--long", "snap-1"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["RESTIC_REPOSITORY"], "local:/repo")
        runner.return_value = SimpleNamespace(returncode=1, stdout="", stderr="private detail")
        failed = ResticMetadataInspectorAdapter(runner=runner).inspect("snap-1")
        self.assertFalse(failed.success)
        self.assertEqual(failed.category, "restic_metadata_inspection_failed")

    def test_adapter_decodes_metadata_as_utf8_with_replacement(self):
        raw_output = (
            (header("35bbb727") + "\n" + line(path="/data/ni\u00f1o") + "\n").encode("utf-8")
            + line(path="/data/bad").encode("utf-8")
            + b"\x81\n"
        )
        captured = {}

        def runner(command, **kwargs):
            captured.update(kwargs)
            if kwargs.get("encoding") is None:
                raw_output.decode("cp1252")
            return SimpleNamespace(
                returncode=0,
                stdout=raw_output.decode(kwargs["encoding"], errors=kwargs["errors"]),
                stderr="",
            )

        result = ResticMetadataInspectorAdapter(runner=runner).inspect("35bbb727")

        self.assertTrue(result.success)
        self.assertEqual([item.path for item in result.files], ["/data/ni\u00f1o", "/data/bad\ufffd"])
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["errors"], "replace")

    def test_unconfirmed_mapping_fails_before_restic(self):
        runner = Mock()
        result = ResticMetadataInspectorAdapter(runner=runner).inspect(
            "snap", policy=RestoreOwnershipPolicy(mode="map", mappings={"compose:app:data": "1000:1000"})
        )
        self.assertEqual(result.category, "confirmation_required")
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
