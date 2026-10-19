import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dapremote.client import Client, RemoteError
from dapremote.firmware import local_preview, prepare_local_firmware
from dapremote.gui_flash import FlashCoordinator
from dapremote.protocol import Op
from dapremote.simulator import FaultPlan, SimulatorServer


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.server = SimulatorServer(self.base / "sd", port=0).start()
        self.endpoint = "tcp://127.0.0.1:%d" % self.server.server_address[1]
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        self.local = self.base / "中文固件.hex"
        self.local.write_bytes(Path("examples/blink.hex").read_bytes())
        self.cache = self.base / "cache"
        self.coordinator = FlashCoordinator(self.base / "flash.json")

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.temp.cleanup()

    def test_local_preview_is_bounded_and_works_without_connection(self):
        result = local_preview(self.local, 16)
        self.assertEqual(result["data"], self.local.read_bytes()[:16])
        self.assertTrue(result["truncated"])
        with self.assertRaises(ValueError):
            local_preview(self.local, 0)

    def test_upload_check_flash_one_call_and_content_reused(self):
        first = self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertEqual(self.client.wait_job(first["job"]["job_id"])["state"], "DONE")
        self.coordinator.query(self.client, self.endpoint)
        with patch.object(self.client, "upload", side_effect=AssertionError("already cached")):
            second = self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertNotEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertFalse(list(self.base.glob("*.upload.json")))

    def test_snapshot_survives_source_change_during_upload(self):
        original_data = self.local.read_bytes()
        upload = self.client.upload
        def mutate(*args, **kwargs):
            self.local.write_bytes(b"edited during upload")
            return upload(*args, **kwargs)
        with patch.object(self.client, "upload", side_effect=mutate):
            result = prepare_local_firmware(self.client, self.local, self.cache)
        self.assertEqual(result["version"], hashlib.sha256(original_data).hexdigest())
        self.assertEqual(self.client.read_preview(result["path"])["data"], original_data)

    def test_invalid_hex_never_creates_flash_job(self):
        self.local.write_bytes(b"invalid HEX")
        with self.assertRaises(RemoteError):
            self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertFalse(self.server.device.jobs)

    def test_upload_failure_never_creates_job_and_can_resume(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_DATA, 1)])
        with self.assertRaises(Exception):
            self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertFalse(self.server.device.jobs)
        self.client.close()
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        result = self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertEqual(self.client.wait_job(result["job"]["job_id"])["state"], "DONE")

    def test_unresolved_flash_is_queried_before_accessing_pc_file(self):
        self.server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
        with self.assertRaises(Exception):
            self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.client.close()
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        self.local.unlink()
        result = self.coordinator.start_local(self.client, self.endpoint, self.local, self.cache)
        self.assertTrue(result["recovered"])
        self.assertEqual(len(self.server.device.jobs), 1)

    def test_version_change_before_start_stops_flash(self):
        self.client.upload(self.local, "/selected.hex")
        with self.assertRaises(ValueError):
            self.coordinator.start(self.client, self.endpoint, "/selected.hex", expected_version="0" * 64)
        self.assertFalse(self.server.device.jobs)

    def test_existing_cache_collision_is_not_overwritten(self):
        digest = hashlib.sha256(self.local.read_bytes()).hexdigest()
        self.client.upload(self.local, "/pc-" + digest + ".hex")
        with patch.object(self.client, "stat", return_value=dict(version="0" * 64, is_dir=False)):
            with self.assertRaises(ValueError):
                prepare_local_firmware(self.client, self.local, self.cache)
        self.assertFalse(self.server.device.jobs)
