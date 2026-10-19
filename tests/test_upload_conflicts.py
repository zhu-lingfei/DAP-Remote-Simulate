from pathlib import Path
from unittest.mock import patch
import unittest

from tests.test_artifacts import ArtifactTests as _Fixture
from dapremote.firmware import UploadConflict, import_artifact, upload_regular_file


class UploadConflictTests(unittest.TestCase):
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    def test_regular_new_file_uploads_without_consent(self):
        source = self.base / "notes.txt"
        source.write_bytes(b"hello")
        result = upload_regular_file(self.client, source, "/notes.txt")
        self.assertEqual(result["path"], "/notes.txt")
        self.assertEqual(self.client.read_preview("/notes.txt")["data"], b"hello")

    def test_regular_conflict_cancel_preserves_and_confirm_overwrites(self):
        source = self.base / "notes.txt"
        source.write_bytes(b"old")
        upload_regular_file(self.client, source, "/notes.txt")
        source.write_bytes(b"new")
        with self.assertRaises(UploadConflict) as error:
            upload_regular_file(self.client, source, "/notes.txt")
        self.assertEqual(self.client.read_preview("/notes.txt")["data"], b"old")
        upload_regular_file(self.client, source, "/notes.txt", overwrite_version=error.exception.version)
        self.assertEqual(self.client.read_preview("/notes.txt")["data"], b"new")

    def test_identical_file_is_reused_without_overwrite_request(self):
        source = self.base / "notes.txt"
        source.write_bytes(b"same")
        upload_regular_file(self.client, source, "/notes.txt")
        with patch.object(self.client, "upload", side_effect=AssertionError("must reuse")):
            result = upload_regular_file(self.client, source, "/notes.txt")
        self.assertEqual(result["size"], 4)

    def test_library_ask_preserves_name_and_overwrites_only_approved_version(self):
        result = import_artifact(self.client, self.local_hex, "hex", self.cache, collision="ask")
        self.local_hex.write_bytes(self.local_hex.read_bytes() + b"\n")
        with self.assertRaises(UploadConflict) as error:
            import_artifact(self.client, self.local_hex, "hex", self.cache, collision="ask")
        changed = import_artifact(self.client, self.local_hex, "hex", self.cache, collision="ask", overwrite_version=error.exception.version)
        self.assertEqual(changed["path"], result["path"])
        self.assertNotEqual(changed["version"], result["version"])
        self.assertEqual(len(self.client.list_files("/HEX")), 1)

    def test_stale_consent_does_not_replace_a_changed_destination(self):
        result = import_artifact(self.client, self.local_hex, "hex", self.cache, collision="ask")
        self.local_hex.write_bytes(b"second")
        self.client.upload(self.local_hex, result["path"], overwrite=True)
        self.local_hex.write_bytes(b"third")
        with self.assertRaises(UploadConflict):
            import_artifact(self.client, self.local_hex, "hex", self.cache, collision="ask", overwrite_version=result["version"])
        self.assertEqual(self.client.read_preview(result["path"])["data"], b"second")


del _Fixture
