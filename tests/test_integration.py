import hashlib
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from dapremote.client import Client, RemoteError, ResumeError, RpcTimeout
from dapremote.protocol import (Decoder, FILE_HEADER, Frame, Kind, Op, Status,
                                cobs_decode, cobs_encode, encode_fields, parse_response)
from dapremote.simulator import Device, FaultPlan, SimulatorServer
from dapremote.transport import SocketTransport


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.server = SimulatorServer(self.base / "sd", port=0).start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self.temp.cleanup()

    def client(self, **options):
        client = Client(SocketTransport(port=self.server.server_address[1]), timeout=0.15, **options)
        client.hello()
        self.clients.append(client)
        return client

    def source(self, data, name="input.bin"):
        path = self.base / name
        path.write_bytes(data)
        return path

    def assert_status(self, status, callback):
        with self.assertRaises(RemoteError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)

    def test_upload_download_binary_empty_and_boundary_sizes(self):
        client = self.client()
        for size in (0, 1, 511, 512, 513, 8193):
            with self.subTest(size=size):
                data = bytes(i % 256 for i in range(size))
                remote = "/file%d.bin" % size
                result = client.upload(self.source(data), remote)
                self.assertEqual(result["version"], hashlib.sha256(data).hexdigest())
                target = self.base / ("output%d.bin" % size)
                client.download(remote, target)
                self.assertEqual(target.read_bytes(), data)
        self.assertEqual(len(client.list_files()), 6)

    def test_lost_corrupt_truncated_responses_and_fragmentation(self):
        self.server.faults = FaultPlan([("drop", Op.FILE_DATA, 1), ("corrupt", Op.FILE_DATA, 3),
                                       ("truncate", Op.FILE_READ, 1)], fragment_size=1)
        client = self.client()
        data = bytes(range(256)) * 9
        client.upload(self.source(data), "/test.bin")
        target = self.base / "result.bin"
        client.download("/test.bin", target)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(client.retry_count, 3)
        self.assertGreaterEqual(client.decoder.errors, 2)

    def test_hello_lost_response_reuses_session(self):
        self.server.faults = FaultPlan([("drop", Op.HELLO, 1)])
        client = self.client()
        self.assertEqual(client.retry_count, 1)
        self.assertEqual(client.call(Op.PING)["boot_id"], client.info["boot_id"])

    def test_upload_resume_after_execute_then_disconnect(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_DATA, 2)])
        data = bytes(range(256)) * 11
        source, sidecar = self.source(data), self.base / "resume.json"
        first = self.client()
        with self.assertRaises(ConnectionError):
            first.upload(source, "/resume.bin", resume_file=sidecar)
        self.assertTrue(sidecar.exists())
        second = self.client()
        progress = []
        second.upload(source, "/resume.bin", resume_file=sidecar,
                      progress=lambda current, total: progress.append(current))
        self.assertEqual(progress[0], 1024)
        self.assertFalse(sidecar.exists())
        self.assertEqual((self.server.device.root / "resume.bin").read_bytes(), data)

    def test_download_resume_after_response_disconnect(self):
        client = self.client()
        data = bytes(range(256)) * 7
        client.upload(self.source(data), "/resume.bin")
        self.server.faults = FaultPlan([("disconnect", Op.FILE_READ, 2)])
        local = self.base / "retrieved.bin"
        with self.assertRaises(ConnectionError):
            client.download("/resume.bin", local)
        self.assertFalse(local.exists())
        second = self.client()
        second.download("/resume.bin", local)
        self.assertEqual(local.read_bytes(), data)
        self.assertFalse(local.with_name(local.name + ".part").exists())

    def test_completed_upload_lost_commit_response_can_recover(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_END, 1)])
        source, state = self.source(b"hello"), self.base / "state.json"
        with self.assertRaises(ConnectionError):
            self.client().upload(source, "/done.bin", resume_file=state)
        self.client().upload(source, "/done.bin", resume_file=state)
        self.assertEqual((self.server.device.root / "done.bin").read_bytes(), b"hello")

    def test_upload_resume_rejects_changed_local_file(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_DATA, 1)])
        source, sidecar = self.source(b"A" * 1024), self.base / "resume.json"
        with self.assertRaises(ConnectionError):
            self.client().upload(source, "/x.bin", resume_file=sidecar)
        source.write_bytes(b"B" * 1024)
        with self.assertRaises(ResumeError):
            self.client().upload(source, "/x.bin", resume_file=sidecar)

    def test_status_recovers_handle_when_begin_response_lost(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_BEGIN, 1)])
        source = self.source(b"start")
        with self.assertRaises(ConnectionError):
            self.client().upload(source, "/lost-begin.bin")
        second = self.client()
        status = second.call(Op.DEV_STATUS)
        self.assertEqual(len(status["transfers"]), 1)
        transfer = status["transfers"][0]
        self.assertEqual(transfer["path"], "/lost-begin.bin")
        self.assertEqual(transfer["sha256"], hashlib.sha256(b"start").digest())
        second.call(Op.FILE_ABORT, transfer_id=transfer["transfer_id"])
        second.upload(source, "/lost-begin.bin")

    def test_download_detects_modified_partial_file(self):
        client = self.client()
        client.upload(self.source(b"A" * 1600), "/partial.bin")
        self.server.faults = FaultPlan([("disconnect", Op.FILE_READ, 2)])
        local = self.base / "out.bin"
        with self.assertRaises(ConnectionError):
            client.download("/partial.bin", local)
        local.with_name("out.bin.part").write_bytes(b"B" * 512)
        with self.assertRaises(ResumeError):
            self.client().download("/partial.bin", local)
        self.assertFalse(local.exists())

    def test_evicted_request_is_never_executed_again(self):
        client = self.client()
        first_request = client._request + 1
        client.mkdir("/once")
        for _ in range(257):
            client.call(Op.PING)
        # Recreate the exact old request after its cached response was evicted.
        old = Frame(1, 0, client.session, first_request, Op.FS_MKDIR, encode_fields(dict(path="/once")))
        client.transport.write(old.encode())
        frames, decoder = [], Decoder()
        while not frames:
            frames = decoder.feed(client.transport.read(4096, 0.2))
        self.assertEqual(parse_response(frames[0].payload)[0], Status.RESULT_UNKNOWN)

    def test_unicode_long_names_respect_listing_byte_budget(self):
        client = self.client()
        for i in range(12):
            (self.server.device.root / (("文" * 60) + str(i))).write_bytes(b"data")
        self.assertEqual(len(client.list_files()), 12)

    def test_duplicate_block_conflict_offset_and_bad_final_hash(self):
        client = self.client()
        transfer = client.call(Op.FILE_BEGIN, direction="upload", path="/bad.bin", size=4,
                               sha256=hashlib.sha256(b"abcd").digest())
        tid = transfer["transfer_id"]
        client.write_block(tid, 0, b"ab")
        self.assertEqual(client.write_block(tid, 0, b"ab")["next_offset"], 2)
        self.assert_status(Status.REQUEST_CONFLICT, lambda: client.write_block(tid, 0, b"zz"))
        self.assert_status(Status.OFFSET_MISMATCH, lambda: client.write_block(tid, 3, b"d"))
        client.write_block(tid, 2, b"xx")
        self.assert_status(Status.HASH_MISMATCH, lambda: client.call(Op.FILE_END, transfer_id=tid))
        self.assertFalse((self.server.device.root / "bad.bin").exists())

    def test_no_space_io_failure_sd_missing_and_old_file_preserved(self):
        client = self.client()
        source = self.source(b"old")
        client.upload(source, "/keep.bin")
        client.call(Op.SIM_CONFIG, capacity=4)
        self.assert_status(Status.NO_SPACE, lambda: client.upload(self.source(b"new"), "/keep.bin", overwrite=True))
        client.call(Op.SIM_CONFIG, capacity=4096, io_fail=True)
        self.assert_status(Status.IO_ERROR, lambda: client.list_files())
        client.call(Op.SIM_CONFIG, io_fail=False, sd_ready=False)
        self.assert_status(Status.SD_NOT_READY, lambda: client.list_files())
        self.assertEqual((self.server.device.root / "keep.bin").read_bytes(), b"old")

    def test_path_traversal_internal_paths_and_empty_directory(self):
        client = self.client()
        for path in ("/../escape", "/.dap/jobs.json", "/.DAP/x", "C:/x", "/a\\b", "/./x"):
            self.assert_status(Status.INVALID_ARGUMENT, lambda p=path: client.mkdir(p))
        client.mkdir("/中文")
        client.delete("/中文", "")
        self.assertEqual(client.list_files(), [])

    def test_paginated_listing_and_versioned_mutation(self):
        client = self.client()
        for i in range(20):
            (self.server.device.root / ("%02d.bin" % i)).write_bytes(bytes([i]))
        entries = client.list_files()
        self.assertEqual(len(entries), 20)
        page = client.call(Op.FS_LIST, path="/", limit=2)
        (self.server.device.root / "new.bin").write_bytes(b"new")
        self.assert_status(Status.FILE_CHANGED, lambda: client.call(Op.FS_LIST, path="/", cursor=page["next_cursor"], version=page["version"]))
        entry = entries[0]
        self.assert_status(Status.FILE_CHANGED, lambda: client.delete(entry["path"], "stale"))
        client.rename(entry["path"], "/renamed.bin", entry["version"])
        client.delete("/renamed.bin", entry["version"])

    def hex_on_sd(self, client):
        client.upload(Path("examples/blink.hex"), "/blink.hex")

    def test_flash_real_bytes_disconnect_and_operation_dedup(self):
        client = self.client()
        self.hex_on_sd(client)
        self.server.device.flash[4096] = 0x42
        self.server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
        version = client.stat("/blink.hex")["version"]
        with self.assertRaises(ConnectionError):
            client.start_flash("/blink.hex", "stable-token", version)
        second = self.client()
        job = second.job_status(operation_token="stable-token")
        duplicate = second.start_flash("/blink.hex", "stable-token", version)
        self.assertEqual(job["job_id"], duplicate["job_id"])
        result = second.wait_job(job["job_id"])
        self.assertEqual(result["state"], "DONE")
        self.assertEqual(bytes(self.server.device.flash[:16]), bytes(range(1, 17)))
        self.assertEqual(self.server.device.flash[4096], 0x42)
        self.assertEqual(len(self.server.device.jobs), 1)
        self.assert_status(Status.REQUEST_CONFLICT, lambda: second.start_flash("/blink.hex", "stable-token", version, True))

    def test_low_battery_busy_file_cancel_and_verify_failure(self):
        client = self.client()
        self.hex_on_sd(client)
        client.call(Op.SIM_CONFIG, battery_percent=5)
        self.assert_status(Status.POWER_INSUFFICIENT, lambda: client.start_flash("/blink.hex", "low"))
        client.call(Op.SIM_CONFIG, external_power=True)
        job = client.start_flash("/blink.hex", "cancel")
        version = client.stat("/blink.hex")["version"]
        self.assert_status(Status.BUSY, lambda: client.delete("/blink.hex", version))
        result = client.call(Op.JOB_CANCEL, job_id=job["job_id"])
        self.assertEqual(result["state"], "CANCELLED")
        client.call(Op.SIM_CONFIG, verify_fail=True)
        failed = client.wait_job(client.start_flash("/blink.hex", "verify")["job_id"])
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(failed["error"], "VERIFY_FAILED")

    def test_persistent_operation_journal_and_interrupted_reboot(self):
        client = self.client()
        self.hex_on_sd(client)
        result = client.start_flash("/blink.hex", "restart")
        self.server.stopping.set()  # freeze background tick before loading a second device
        self.server._threads_started[1].join(timeout=1)
        device = Device(self.server.device.root)
        status, result = device.handle(Op.JOB_STATUS, dict(operation_token="restart"))
        self.assertEqual(status, Status.OK)
        self.assertEqual(result["state"], "INTERRUPTED")

    def test_rtt_all_destinations_binary_logging_and_target_exclusion(self):
        client = self.client()
        self.hex_on_sd(client)
        for dest in (1, 2, 3):
            with self.subTest(dest=dest):
                result = client.start_rtt(dest, "/rtt%d.log" % dest, rate=10000)
                stream_id = result["stream_id"]
                time.sleep(0.08)
                batch = client.read_rtt(stream_id)
                if dest & 1:
                    self.assertIn(b"\x00\xff", batch["data"])
                else:
                    self.assertEqual(batch["data"], b"")
                if dest & 2:
                    raw = (self.server.device.root / ("rtt%d.log" % dest)).read_bytes()
                    self.assertEqual(raw[:4], b"DRT1")
                    self.assertGreater(batch["sd_bytes"], 4)
                self.assert_status(Status.BUSY, lambda: client.start_flash("/blink.hex", "rtt-busy"))
                client.stop_rtt(stream_id)

    def test_rtt_backpressure_sd_failure_and_disconnect_continue(self):
        first = self.client()
        stream_id = first.call(Op.RTT_START, destinations=3, sd_path="/small.log", rate=50000,
                               rtt_capacity=512, sd_max_bytes=1)["stream_id"]
        first.close()
        time.sleep(0.1)
        second = self.client()
        stats = second.call(Op.RTT_STATS, stream_id=stream_id)
        self.assertGreater(stats["dropped_bytes"], 0)
        self.assertTrue(stats["sd_error"])
        self.assertTrue(second.read_rtt(stream_id)["data"])
        second.stop_rtt(stream_id)

    def test_rtt_and_file_transfer_can_run_together(self):
        client = self.client()
        stream = client.start_rtt(3, "/concurrent.log", rate=10000)
        data = bytes(range(256)) * 32
        client.upload(self.source(data), "/concurrent.bin")
        time.sleep(0.04)
        batch = client.read_rtt(stream["stream_id"])
        self.assertTrue(batch["data"])
        client.stop_rtt(stream["stream_id"])
        self.assertEqual((self.server.device.root / "concurrent.bin").read_bytes(), data)

    def test_timeout_is_bounded_and_partial_not_published(self):
        self.server.faults = FaultPlan([("drop", Op.FILE_DATA, 1), ("drop", Op.FILE_DATA, 2)])
        client = self.client(retries=1)
        with self.assertRaises(RpcTimeout):
            client.upload(self.source(b"abc"), "/timeout.bin")
        self.assertFalse((self.server.device.root / "timeout.bin").exists())
        self.assertEqual(client.retry_count, 1)

    def test_corrupt_request_and_same_id_conflict_and_old_session(self):
        client = self.client()
        transport = client.transport
        request = client._request + 1
        frame = Frame(Kind.REQUEST, 0, client.session, request, Op.FS_MKDIR, encode_fields(dict(path="/safe")))
        raw = bytearray(cobs_decode(frame.encode()[:-1]))
        raw[-1] ^= 1
        transport.write(cobs_encode(raw) + b"\0")
        self.assertEqual(transport.read(4096, 0.05), b"")
        self.assertFalse((self.server.device.root / "safe").exists())
        transport.write(frame.encode())
        decoder = Decoder()

        def receive():
            end = time.monotonic() + 1
            while time.monotonic() < end:
                frames = decoder.feed(transport.read(4096, 0.1))
                if frames:
                    return parse_response(frames[0].payload)[0]
            self.fail("missing response")

        self.assertEqual(receive(), Status.OK)
        conflict = Frame(1, 0, client.session, request, Op.FS_MKDIR, encode_fields(dict(path="/other")))
        transport.write(conflict.encode())
        self.assertEqual(receive(), Status.REQUEST_CONFLICT)
        transport.write(Frame(1, 0, client.session ^ 1, request + 1, Op.PING).encode())
        self.assertEqual(receive(), Status.SESSION_INVALID)
        self.assertFalse((self.server.device.root / "other").exists())


if __name__ == "__main__":
    unittest.main()
