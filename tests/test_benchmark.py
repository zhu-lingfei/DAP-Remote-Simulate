import hashlib
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from dapremote.benchmark import BenchmarkCancelled
from dapremote.client import Client, RemoteError
from dapremote.protocol import (BENCH_HEADER, HEADER, Frame, Kind, Op, ProtocolError, Status,
                               cobs_decode, cobs_encode, crc32c)
from dapremote.simulator import FaultPlan, SimulatorServer
from dapremote.transport import SocketTransport


class RateTransport:
    """Serial-setting model over actual TCP framing, with a mismatching high rate."""
    supports_baudrate = True

    def __init__(self, port):
        self.inner = SocketTransport(port=port)
        self.baudrate = 115200
        self.settings = []

    def set_baudrate(self, value):
        self.settings.append(value)
        if value == 460800:
            raise ValueError("driver rejects candidate")
        self.baudrate = value

    def write(self, data):
        if self.baudrate != 921600:
            self.inner.write(data)

    def read(self, size, timeout):
        return self.inner.read(size, timeout)

    def close(self):
        self.inner.close()


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = SimulatorServer(self.root / "sd", port=0).start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self.temp.cleanup()

    def client(self, transport=None):
        transport = transport or SocketTransport(port=self.server.server_address[1])
        client = Client(transport, timeout=0.15, retries=1)
        client.hello()
        self.clients.append(client)
        return client

    def test_memory_and_sd_benchmark_real_length_hash_and_no_published_file(self):
        client = self.client()
        for sink in ("memory", "sd"):
            with self.subTest(sink=sink):
                result = client.benchmark(size=16385, sink=sink)
                self.assertTrue(result["passed"])
                self.assertEqual(result["received_bytes"], 16385)
                self.assertEqual(result["received_sha256"], result["expected_sha256"])
                self.assertGreater(result["bytes_per_s"], 0)
                self.assertGreater(result["elapsed_us"], 0)
        self.assertEqual(client.list_files(), [])
        self.assertEqual(list((self.root / "sd" / ".dap").glob("bench-*.part")), [])

    def test_actual_local_file_is_sent_not_replaced_with_generated_data(self):
        source = self.root / "input.bin"
        data = bytes(range(256)) * 51 + b"end"
        source.write_bytes(data)
        result = self.client().benchmark(file_path=source)
        self.assertEqual(result["size"], len(data))
        self.assertEqual(result["received_sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(result["passed"])

    def test_payload_crc_is_disabled_but_header_is_protected(self):
        frame = Frame(Kind.BENCH_DATA, 1, 1, 0, Op.BENCH_DATA,
                      BENCH_HEADER.pack(2, 0) + b"abcd", flags=1)
        raw = bytearray(cobs_decode(frame.encode()[:-1]))
        self.assertEqual(struct.unpack("<I", raw[-4:])[0], crc32c(raw[:HEADER.size]))
        raw[-5] ^= 0x20  # payload changed; still structurally valid, header CRC unchanged
        decoded = Frame.decode(cobs_encode(raw))
        self.assertNotEqual(decoded.payload, frame.payload)
        raw[4] ^= 1
        with self.assertRaises(ProtocolError):
            Frame.decode(cobs_encode(raw))
        with self.assertRaises(ProtocolError):
            Frame(Kind.REQUEST, 0, 1, 2, Op.FS_DELETE, b"", flags=1).encode()

    def test_corrupt_drop_and_truncate_data_fail_without_retransmission(self):
        for action in ("corrupt", "drop", "truncate"):
            with self.subTest(action=action):
                self.server.faults = FaultPlan([(action, Op.BENCH_DATA, 2)])
                client = self.client()
                result = client.benchmark(size=12000)
                self.assertFalse(result["passed"])
                self.assertNotEqual(result["received_sha256"], result["expected_sha256"])
                self.assertEqual(client.retry_count, 0)
                self.assertEqual(self.server.faults.counts[Op.BENCH_DATA], 3)
                if action != "corrupt":
                    self.assertLess(result["received_bytes"], 12000)

    def test_final_control_reply_loss_retries_control_not_data(self):
        self.server.faults = FaultPlan([("drop", Op.BENCH_END, 1)])
        client = self.client()
        self.assertTrue(client.benchmark(size=9000)["passed"])
        self.assertEqual(client.retry_count, 1)
        self.assertEqual(self.server.faults.counts[Op.BENCH_DATA], 3)

    def test_cancel_releases_sd_temp_and_connection(self):
        client, cancel = self.client(), threading.Event()
        def progress(event):
            if event["event"] == "data":
                cancel.set()
        with self.assertRaises(BenchmarkCancelled):
            client.benchmark(size=32000, sink="sd", progress=progress, cancel_event=cancel)
        self.assertEqual(list((self.root / "sd" / ".dap").glob("bench-*.part")), [])
        self.assertTrue(client.benchmark(size=64)["passed"])

    def test_sd_fail_reports_error_and_does_not_promote_temp(self):
        client = self.client()
        first = True
        def progress(event):
            nonlocal first
            if first and event["event"] == "data":
                first = False
                client.call(Op.SIM_CONFIG, io_fail=True)
        result = client.benchmark(size=16384, sink="sd", progress=progress)
        self.assertFalse(result["passed"])
        self.assertIn("SD", result["error"])
        self.assertEqual(list((self.root / "sd" / ".dap").glob("bench-*.part")), [])

    def test_scan_applies_candidates_handles_unsupported_and_restores_rate(self):
        transport = RateTransport(self.server.server_address[1])
        client = self.client(transport)
        result = client.scan_baudrates([921600, 115200, 460800, 230400], rounds=2,
                                      size=8192, probe_timeout=0.05, settle_s=0)
        self.assertEqual(result["highest_passed_baudrate"], 230400)
        self.assertIn(result["recommended_baudrate"], (115200, 230400))
        self.assertTrue(result["restored"])
        self.assertFalse(result["simulated"])
        self.assertEqual(transport.baudrate, 115200)
        self.assertEqual(transport.settings, [115200, 230400, 460800, 921600, 115200])
        self.assertEqual([row["passed"] for row in result["results"]], [True, True, False, False])
        self.assertEqual((client.timeout, client.retries), (0.15, 1))

    def test_tcp_scan_is_labeled_simulated_and_has_no_physical_recommendation(self):
        result = self.client().scan_baudrates([115200, 230400], rounds=1, size=2048, settle_s=0)
        self.assertTrue(result["simulated"])
        self.assertIsNone(result["highest_passed_baudrate"])
        self.assertIsNone(result["recommended_baudrate"])
        self.assertTrue(all(row["passed"] and not row["baudrate_applied"] for row in result["results"]))

    def test_scan_cancel_restores_original_and_preserves_partial_results(self):
        transport = RateTransport(self.server.server_address[1])
        client, cancel = self.client(transport), threading.Event()
        def progress(event):
            if event["event"] == "round":
                cancel.set()
        result = client.scan_baudrates([115200, 230400], rounds=3, size=2048,
                                      settle_s=0, progress=progress, cancel_event=cancel)
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["restored"])
        self.assertEqual(transport.baudrate, 115200)
        self.assertEqual(result["results"][0]["rounds_passed"], 1)
        self.assertIsNone(result["highest_passed_baudrate"])

    def test_scan_reports_restore_failure(self):
        transport = RateTransport(self.server.server_address[1])
        original_set = transport.set_baudrate
        def set_rate(rate):
            if transport.settings:
                raise ValueError("restore failed")
            original_set(rate)
        transport.set_baudrate = set_rate
        result = self.client(transport).scan_baudrates([230400], rounds=1, size=64, settle_s=0)
        self.assertFalse(result["restored"])
        self.assertIn("restore failed", result["restore_error"])

    def test_callback_error_is_not_misreported_as_link_failure_and_restores(self):
        transport = RateTransport(self.server.server_address[1])
        client = self.client(transport)
        def progress(event):
            if event["event"] == "round":
                raise RuntimeError("UI callback failed")
        with self.assertRaisesRegex(RuntimeError, "UI callback failed"):
            client.scan_baudrates([230400], rounds=1, size=64, settle_s=0, progress=progress)
        self.assertEqual(transport.baudrate, 115200)
        self.assertEqual((client.timeout, client.retries), (0.15, 1))

    def test_invalid_rates_are_rejected_before_touching_serial_port(self):
        transport = RateTransport(self.server.server_address[1])
        client = self.client(transport)
        for rates in ([], [True], [115200, "bad"], [0], [4000001]):
            with self.assertRaises(ValueError):
                client.scan_baudrates(rates)
        self.assertEqual(transport.settings, [])

    def test_benchmark_is_exclusive_and_owned_by_session(self):
        first, second = self.client(), self.client()
        begin = first.call(Op.BENCH_BEGIN, size=3, sha256=hashlib.sha256(b"abc").digest(), sink="memory")
        with self.assertRaises(RemoteError) as caught:
            second.call(Op.BENCH_STATUS, bench_id=begin["bench_id"])
        self.assertEqual(caught.exception.status, Status.NOT_FOUND)
        with self.assertRaises(RemoteError) as caught:
            second.benchmark(size=3)
        self.assertEqual(caught.exception.status, Status.BUSY)
        first.call(Op.BENCH_ABORT, bench_id=begin["bench_id"])
        self.assertTrue(second.benchmark(size=3)["passed"])

    def test_disconnect_cleans_benchmark_for_new_connection(self):
        first = self.client()
        first.call(Op.BENCH_BEGIN, size=3, sha256=hashlib.sha256(b"abc").digest(), sink="sd")
        first.close()
        # Synchronize with handler finalization, no arbitrary timing assumption.
        import time
        end = time.monotonic() + 1
        while time.monotonic() < end:
            with self.server.device.lock:
                if self.server.device.benchmark.state != "ACTIVE":
                    break
            time.sleep(0.01)
        self.assertTrue(self.client().benchmark(size=3)["passed"])


if __name__ == "__main__":
    unittest.main()
