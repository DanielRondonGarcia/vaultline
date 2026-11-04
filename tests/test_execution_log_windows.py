"""Focused tests for the extracted Windows handle/ctypes deletion primitives.

``_WindowsDeletion`` and its lazy ``kernel32`` glue live now in the sibling
``execution_log_windows`` module so the retention component stays reviewable
in slices; the primitives are the exact ones the contained Windows deletion
uses: a decision is bound to the object an inspected handle names, never to
a path a concurrent ancestor replacement could redirect.

Synthetic project-local fixtures (``tempfile`` directories) exercise every
primitive against real Windows objects; on other platforms everything is
deterministically skipped except the fail-closed lazy loader (no Windows
API, no deletion — never an error).
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_WIN32 = sys.platform == "win32"

try:
    from src.worker_agent.infrastructure.security.execution_log_windows import (
        _WindowsDeletion,
        _windows_api,
    )
except ModuleNotFoundError:  # pragma: no cover - direct execution from tests/
    from execution_log_windows import _WindowsDeletion, _windows_api  # type: ignore[no-redef]


class _FixtureTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-windows-"))
        self._file = self._temporary / "synthetic.log"
        self._file.write_text("EVIDENCE", encoding="utf-8")
        stale = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()
        os.utime(self._file, (stale, stale))
        self._directory = self._temporary / "synthetic-directory"
        self._directory.mkdir()

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)


class _WindowsApiTests(_FixtureTestCase):
    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_lazy_loader_is_a_cached_successfully_built_singleton(self):
        first = _windows_api()
        self.assertIsNotNone(first)
        self.assertIs(first, _windows_api())

    @unittest.skipIf(_WIN32, "fail-closed loader outcome is the other platform's only behavior")
    def test_lazy_loader_fails_closed_without_a_windows_api(self):
        self.assertIsNone(_windows_api())

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_constants_match_the_win32_values_the_primitives_depend_on(self):
        self.assertEqual(_WindowsDeletion.DELETE_ACCESS, 0x00010000)
        self.assertEqual(_WindowsDeletion.FILE_SHARE_ALL, 0x00000007)
        self.assertEqual(_WindowsDeletion.OPEN_EXISTING, 3)
        self.assertEqual(_WindowsDeletion.FILE_FLAG_BACKUP_SEMANTICS, 0x02000000)
        self.assertEqual(_WindowsDeletion.FILE_FLAG_OPEN_REPARSE_POINT, 0x00200000)
        self.assertEqual(_WindowsDeletion.FILE_ATTRIBUTE_DIRECTORY, 0x10)
        self.assertEqual(_WindowsDeletion.FILE_ATTRIBUTE_REPARSE_POINT, 0x400)
        self.assertEqual(_WindowsDeletion.FILE_DISPOSITION_INFO_CLASS, 4)


class _WindowsHandleTests(_FixtureTestCase):
    def setUp(self):
        super().setUp()
        if _WIN32:
            self._api = _windows_api()

    def _open_file(self):
        return self._api.open_object(str(self._file), directory=False)

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_attributes_reports_real_attributes_and_none_for_missing_paths(self):
        self.assertIsInstance(self._api.attributes(str(self._file)), int)
        self.assertIsNone(self._api.attributes(str(self._temporary / "does-not-exist.log")))

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_open_object_and_open_query_hand_out_usable_handles_or_none(self):
        file_handle = self._open_file()
        self.assertIsNotNone(file_handle, "an existing managed log must open by handle")
        self._api.close(file_handle)
        directory_handle = self._api.open_query(str(self._directory))
        self.assertIsNotNone(directory_handle, "a directory must open query-only")
        self._api.close(directory_handle)
        self.assertIsNone(self._api.open_object(str(self._temporary / "absent.log"), directory=False))
        self.assertIsNone(self._api.open_query(str(self._temporary / "absent-directory")))

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_handle_information_exposes_the_full_object_identity(self):
        handle = self._open_file()
        try:
            information = self._api.handle_information(handle)
            self.assertIsNotNone(information)
            self.assertFalse(information.dwFileAttributes & _WindowsDeletion.FILE_ATTRIBUTE_DIRECTORY)
            identity = os.lstat(self._file)
            self.assertEqual(self._api.index(information), identity.st_ino)
            self.assertEqual(
                self._api.volume(information),
                identity.st_dev & 0xFFFFFFFF,  # handle serial is the 32-bit domain
            )
            self.assertEqual(self._api.creation_ns(information), identity.st_ctime_ns)
            self.assertEqual(
                information.nFileSizeHigh << 32 | information.nFileSizeLow, identity.st_size
            )
        finally:
            self._api.close(handle)

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_final_path_resolves_the_opened_object_from_its_handle(self):
        for opened, target in (
            (self._api.open_object(str(self._file), directory=False), self._file),
            (self._api.open_query(str(self._directory)), self._directory),
        ):
            with self.subTest(target=target):
                self.assertIsNotNone(opened)
                try:
                    final = self._api.final_path(opened)
                finally:
                    self._api.close(opened)
                self.assertIsNotNone(final)
                self.assertEqual(
                    Path(final).name.casefold(),
                    target.name.casefold(),
                    f"final path {final} does not name {target}",
                )

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_is_stale_uses_the_last_write_stamp_from_the_handle(self):
        handle = self._open_file()
        try:
            information = self._api.handle_information(handle)
            self.assertTrue(
                self._api.is_stale(information, datetime.now(timezone.utc).timestamp()),
                "a 35-day-old stamp is older than a now cutoff",
            )
            fresh = self._temporary / "fresh.log"
            fresh.write_text("fresh", encoding="utf-8")
            fresh_handle = self._api.open_object(str(fresh), directory=False)
            try:
                self.assertFalse(
                    self._api.is_stale(
                        self._api.handle_information(fresh_handle),
                        (datetime.now(timezone.utc) - timedelta(days=35)).timestamp(),
                    )
                )
            finally:
                self._api.close(fresh_handle)
        finally:
            self._api.close(handle)

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_mark_for_delete_removes_the_object_through_its_handle(self):
        handle = self._open_file()
        self.assertTrue(self._api.mark_for_delete(handle))
        self._api.close(handle)
        self.assertFalse(self._file.exists(), "the scheduled deletion must complete on close")

    @unittest.skipUnless(_WIN32, "Windows API glue is Windows specific")
    def test_a_reparse_point_leaf_is_opened_and_inspected_never_followed(self):
        import _winapi

        outside = self._temporary / "outside"
        outside.mkdir()
        (outside / "payload.txt").write_text("payload", encoding="utf-8")
        link_path = self._temporary / "link.log"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(outside.resolve()), str(link_path))
        self.assertNotEqual(
            self._api.attributes(str(link_path)) & _WindowsDeletion.FILE_ATTRIBUTE_REPARSE_POINT,
            0,
            "the link's own attributes must expose the reparse tag",
        )
        handle = self._api.open_object(str(link_path), directory=True)
        self.assertIsNotNone(handle)
        # A non-directory open (as the file-delete flow performs after its own
        # attribute check rejects this case first) must fail closed, never
        # traverse: the link is a directory reparse point.
        self.assertIsNone(self._api.open_object(str(link_path), directory=False))
        try:
            information = self._api.handle_information(handle)
            self.assertIsNotNone(information)
            self.assertNotEqual(
                information.dwFileAttributes & _WindowsDeletion.FILE_ATTRIBUTE_REPARSE_POINT,
                0,
                "the handle must refer to the link itself, never its target",
            )
        finally:
            self._api.close(handle)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()