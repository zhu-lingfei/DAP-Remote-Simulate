import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dapremote.client import Client, RemoteError
from dapremote.hex_preview import preview_lines
from dapremote.protocol import Op, ProtocolError, Status
from dapremote.simulator import FaultPlan, SimulatorServer


class HexPreviewTextTests(unittest.TestCase):
    def test_render_line_count_is_bounded_for_newline_heavy_files(self):
        self.assertEqual(len(preview_lines(b"\n" * 20000)), 4000)

    def test_real_hex_records_are_numbered_and_classified(self):
        rows = preview_lines(Path("examples/blink.hex").read_bytes())
        self.assertEqual([row.kind for row in rows], ["address", "data", "eof"])
        self.assertEqual([row.number for row in rows], [1, 2, 3])
        self.assertTrue(rows[1].text.startswith(":10000000"))

    def test_incomplete_truncated_tail_is_not_reported_as_bad_checksum(self):
        raw = b":020000040800F2\n:1000"
        self.assertEqual(preview_lines(raw, truncated=True)[-1].kind, "partial")
        self.assertEqual(preview_lines(raw, truncated=False)[-1].kind, "error")

    def test_malformed_checksum_and_binary_controls_are_visible(self):
        rows = preview_lines(b":020000040800F3\n\x00\xff\n")
        self.assertEqual([row.kind for row in rows], ["error", "error"])
        self.assertEqual(rows[-1].text, "\\x00\\xff")


class PreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = SimulatorServer(Path(self.temp.name) / "sd", port=0).start()
        self.client = Client.open("tcp://127.0.0.1:%d" % self.server.server_address[1], timeout=0.08, retries=1)
        self.data = Path("examples/blink.hex").read_bytes()
        self.client.upload("examples/blink.hex", "/blink.hex")

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.temp.cleanup()

    def assert_released(self):
        self.assertFalse(any(transfer["state"] == "ACTIVE" for transfer in self.server.device.transfers.values()))

    def test_full_file_preview_verifies_hash_and_releases_transaction(self):
        result = self.client.read_preview("/blink.hex")
        self.assertEqual(result["data"], self.data)
        self.assertTrue(result["verified"])
        self.assertFalse(result["truncated"])
        self.assert_released()

    def test_partial_preview_is_bounded_and_releases_source_lock(self):
        source = Path(self.temp.name) / "large.hex"
        source.write_bytes(self.data * 100)
        self.client.upload(source, "/large.hex")
        result = self.client.read_preview("/large.hex", max_bytes=1024)
        self.assertEqual(result["data"], (self.data * 100)[:1024])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["verified"])
        self.assert_released()
        # Preview must not prevent later flashing or file operations.
        self.client.delete("/large.hex", result["version"])

    def test_bad_crc_is_retried_during_preview(self):
        self.server.faults = FaultPlan([("corrupt", Op.FILE_READ, 1)])
        result = self.client.read_preview("/blink.hex")
        self.assertEqual(result["data"], self.data)
        self.assertEqual(self.client.retry_count, 1)
        self.assert_released()

    def test_read_failure_releases_transfer_without_masking_original_error(self):
        original = self.client.call
        def call(opcode, **fields):
            if opcode == Op.FILE_READ:
                original(Op.SIM_CONFIG, io_fail=True)
            return original(opcode, **fields)
        with patch.object(self.client, "call", side_effect=call):
            with self.assertRaises(RemoteError) as caught:
                self.client.read_preview("/blink.hex")
        self.assertEqual(caught.exception.status, Status.IO_ERROR)
        self.assert_released()

    def test_whole_file_hash_detects_inconsistent_device_data(self):
        original = self.client.call
        def call(opcode, **fields):
            result = original(opcode, **fields)
            if opcode == Op.FILE_READ:
                result["data"] = bytes([result["data"][0] ^ 1]) + result["data"][1:]
            return result
        with patch.object(self.client, "call", side_effect=call):
            with self.assertRaises(ProtocolError):
                self.client.read_preview("/blink.hex")
        self.assert_released()

    def test_invalid_preview_size_is_rejected_before_creating_transaction(self):
        count = len(self.server.device.transfers)
        for limit in (0, -1, True, 262145):
            with self.assertRaises(ValueError):
                self.client.read_preview("/blink.hex", max_bytes=limit)
        self.assertEqual(len(self.server.device.transfers), count)


if __name__ == "__main__":
    unittest.main()
