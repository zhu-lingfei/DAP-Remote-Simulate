import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dapremote.client import Client, RemoteError
from dapremote.file_picker import local_entries, sd_entries
from dapremote.firmware import artifact_path, import_artifact
from dapremote.gui_flash import FlashCoordinator
from dapremote.protocol import Op, Status, encode_fields, decode_fields
from dapremote.simulator import FaultPlan, SimulatorServer
from tests.flm_fixture import flm_envelope


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.server = SimulatorServer(self.base / "sd", port=0).start()
        self.endpoint = "tcp://127.0.0.1:%d" % self.server.server_address[1]
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        self.local_hex = self.base / "固件.HEX"
        self.local_hex.write_bytes(Path("examples/blink.hex").read_bytes())
        self.local_flm = self.base / "芯片.FLM"
        self.local_flm.write_bytes(flm_envelope())
        self.cache = self.base / "cache"

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.temp.cleanup()

    def pair(self):
        firmware = import_artifact(self.client, self.local_hex, "hex", self.cache)
        algorithm = import_artifact(self.client, self.local_flm, "flm", self.cache)
        return firmware, algorithm

    def test_fixed_libraries_keep_names_and_separate_file_types(self):
        firmware, algorithm = self.pair()
        self.assertEqual(firmware["path"], "/HEX/固件.hex")
        self.assertEqual(algorithm["path"], "/FLM/芯片.flm")
        self.assertEqual(self.client.read_preview(firmware["path"])["data"], self.local_hex.read_bytes())
        self.assertEqual([e["name"] for e in self.client.list_files("/")], ["FLM", "HEX"])

    def test_collision_preserves_old_content_and_same_content_reuses(self):
        original = import_artifact(self.client, self.local_hex, "hex", self.cache)
        old_data = self.local_hex.read_bytes()
        self.local_hex.write_bytes(old_data + b"\n")
        newer = import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.assertNotEqual(original["path"], newer["path"])
        self.assertEqual(self.client.read_preview(original["path"])["data"], old_data)
        with patch.object(self.client, "upload", side_effect=AssertionError("must reuse")):
            same = import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.assertEqual(newer["path"], same["path"])

    def test_browser_filters_both_local_and_sd_and_forbids_escape(self):
        (self.base / "folder").mkdir()
        names = [entry["name"] for entry in local_entries(self.base, "flm")]
        self.assertIn("芯片.FLM", names)
        self.assertNotIn("固件.HEX", names)
        firmware, algorithm = self.pair()
        self.assertEqual(sd_entries([dict(firmware, is_dir=False), dict(algorithm, is_dir=False)], "hex")[0]["path"], firmware["path"])
        for path in ("/FLM/a.hex", "/HEX/../a.hex", "/HEX/a.flm", "/HEX/a\\b.hex", "/HEX2/a.hex"):
            with self.assertRaises(ValueError):
                artifact_path(path, "hex")

    def test_import_rejects_wrong_extension_and_empty_file(self):
        with self.assertRaises(ValueError):
            import_artifact(self.client, self.local_hex, "flm", self.cache)
        self.local_hex.write_bytes(b"")
        with self.assertRaises(ValueError):
            import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.assertFalse(self.server.device.jobs)

    def test_import_failure_resumes_in_fixed_directory(self):
        self.server.faults = FaultPlan([("disconnect", Op.FILE_DATA, 1)])
        with self.assertRaises(Exception):
            import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.client.close()
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        result = import_artifact(self.client, self.local_hex, "hex", self.cache)
        self.assertEqual(result["path"], "/HEX/固件.hex")

    def test_flm_fields_round_trip_without_changing_existing_tags(self):
        fields = dict(flm_path="/FLM/chip.flm", flm_version="a" * 64)
        self.assertEqual(decode_fields(encode_fields(fields)), fields)

    def test_coordinator_binds_persists_and_locks_selected_flm(self):
        firmware, algorithm = self.pair()
        coordinator = FlashCoordinator(self.base / "state.json")
        result = coordinator.start(self.client, self.endpoint, firmware["path"], expected_version=firmware["version"],
                                   flm_path=algorithm["path"], flm_version=algorithm["version"])
        current = coordinator.current(self.client, self.endpoint)
        self.assertEqual(current["flm_path"], algorithm["path"])
        self.assertEqual(current["flm_version"], algorithm["version"])
        self.assertEqual(FlashCoordinator(self.base / "state.json").current(self.client, self.endpoint)["flm_version"], algorithm["version"])
        with self.assertRaises(RemoteError) as error:
            self.client.delete(algorithm["path"], algorithm["version"])
        self.assertEqual(error.exception.status, Status.BUSY)
        self.assertEqual(self.client.wait_job(result["job"]["job_id"])["state"], "DONE")
        self.client.delete(algorithm["path"], algorithm["version"])

    def test_changed_flm_version_prevents_job_creation(self):
        firmware, algorithm = self.pair()
        with self.assertRaises(RemoteError) as error:
            self.client.start_flash(firmware["path"], "changed-algorithm", flm_path=algorithm["path"], flm_version="0" * 64)
        self.assertEqual(error.exception.status, Status.FILE_CHANGED)
        self.assertFalse(self.server.device.jobs)

    def test_non_elf_flm_is_rejected_at_device_check(self):
        self.local_flm.write_bytes(b"not an ELF" * 10)
        firmware, algorithm = self.pair()
        with self.assertRaises(RemoteError):
            self.client.check_hex(firmware["path"], flm_path=algorithm["path"], flm_version=algorithm["version"])
        self.assertFalse(self.server.device.jobs)

    def test_old_peer_cannot_silently_ignore_flm_selection(self):
        firmware, algorithm = self.pair()
        call = self.client.call
        def legacy(op, **fields):
            return dict(features="files,hex,jobs") if op == Op.GET_CAPS else call(op, **fields)
        with patch.object(self.client, "call", side_effect=legacy):
            with self.assertRaises(ValueError):
                self.client.start_flash(firmware["path"], "legacy", flm_path=algorithm["path"])
        self.assertFalse(self.server.device.jobs)

    def test_changed_flm_with_same_operation_token_conflicts(self):
        firmware, algorithm = self.pair()
        job = self.client.start_flash(firmware["path"], "once", flm_path=algorithm["path"], flm_version=algorithm["version"])
        with self.assertRaises(RemoteError) as error:
            self.client.start_flash(firmware["path"], "once", flm_path=algorithm["path"], flm_version="0" * 64)
        self.assertEqual(error.exception.status, Status.REQUEST_CONFLICT)
        self.assertEqual(len(self.server.device.jobs), 1)

    def test_lost_start_recovers_same_pair_without_new_job(self):
        firmware, algorithm = self.pair()
        coordinator = FlashCoordinator(self.base / "state.json")
        self.server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
        with self.assertRaises(Exception):
            coordinator.start(self.client, self.endpoint, firmware["path"], flm_path=algorithm["path"], flm_version=algorithm["version"])
        self.client.close()
        self.client = Client.open(self.endpoint, timeout=0.08, retries=1)
        recovered = coordinator.start(self.client, self.endpoint, "/unused.hex")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(len(self.server.device.jobs), 1)
