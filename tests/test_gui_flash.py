import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dapremote.client import Client, RemoteError, RpcTimeout
from dapremote.gui_flash import FlashCoordinator, PendingFlashError
from dapremote.protocol import Op, Status
from dapremote.simulator import FaultPlan, SimulatorServer


class GuiFlashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = SimulatorServer(self.root / "sd", port=0).start()
        self.endpoint = "tcp://127.0.0.1:%d" % self.server.server_address[1]
        self.clients = []
        self.client = self.open()
        self.client.upload("examples/blink.hex", "/blink.hex")
        self.state = self.root / "gui-flash.json"
        self.coordinator = FlashCoordinator(self.state)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.close()
        self.temp.cleanup()

    def open(self):
        client = Client.open(self.endpoint, timeout=0.07, retries=1)
        self.clients.append(client)
        return client

    def start(self):
        return self.coordinator.start(self.client, self.endpoint, "/blink.hex")

    def test_two_explicit_clicks_after_completion_create_two_real_jobs(self):
        first = self.start()
        final = self.client.wait_job(first["job"]["job_id"])
        self.coordinator.observe(self.client, self.endpoint, final)
        second = self.start()
        self.assertFalse(first["recovered"])
        self.assertFalse(second["recovered"])
        self.assertNotEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertNotEqual(first["job"]["operation_token"], second["job"]["operation_token"])
        self.assertEqual(len(self.server.device.jobs), 2)

    def test_second_click_while_active_queries_the_existing_job(self):
        first = self.start()
        second = self.start()
        self.assertTrue(second["recovered"])
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertEqual(len(self.server.device.jobs), 1)

    def test_internal_record_is_saved_before_the_flash_request_is_sent(self):
        original = self.client.start_flash
        def start(path, token, **options):
            records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
            self.assertEqual(records[0]["operation_token"], token)
            self.assertEqual(records[0]["state"], "REQUESTING")
            return original(path, token, **options)
        with patch.object(self.client, "start_flash", side_effect=start):
            self.start()

    def test_gui_restart_and_reconnect_recover_after_start_reply_is_lost(self):
        self.server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
        with self.assertRaises(ConnectionError):
            self.start()
        self.assertEqual(self.coordinator.current(self.client, self.endpoint)["state"], "UNKNOWN")
        reloaded = FlashCoordinator(self.state)
        second = self.open()
        recovered = reloaded.start(second, self.endpoint, "/blink.hex")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(len(self.server.device.jobs), 1)
        self.assertEqual(recovered["job"]["operation_token"], self.coordinator.current(second, self.endpoint)["operation_token"])

    def test_response_timeout_does_not_create_a_second_physical_job(self):
        self.server.faults = FaultPlan([("drop", Op.FLASH_START, 1), ("drop", Op.FLASH_START, 2)])
        with self.assertRaises(RpcTimeout):
            self.start()
        recovered = self.start()
        self.assertTrue(recovered["recovered"])
        self.assertEqual(len(self.server.device.jobs), 1)

    def test_unknown_result_never_turns_into_a_new_flash_request(self):
        self.server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
        with self.assertRaises(ConnectionError):
            self.start()
        second = self.open()
        with patch.object(second, "job_status", side_effect=RemoteError(Status.RESULT_UNKNOWN, {})), patch.object(second, "start_flash") as start:
            with self.assertRaises(PendingFlashError):
                self.coordinator.start(second, self.endpoint, "/blink.hex")
            start.assert_not_called()

    def test_rejected_request_can_be_corrected_and_clicked_again(self):
        self.client.call(Op.SIM_CONFIG, battery_percent=5)
        with self.assertRaises(RemoteError):
            self.start()
        record = self.coordinator.current(self.client, self.endpoint)
        self.assertEqual(record["state"], "REJECTED")
        self.client.call(Op.SIM_CONFIG, external_power=True)
        accepted = self.start()
        self.assertFalse(accepted["recovered"])
        self.assertNotEqual(record["operation_token"], accepted["job"]["operation_token"])
        self.assertEqual(len(self.server.device.jobs), 1)

    def test_preflight_failure_does_not_create_a_pending_operation(self):
        with self.assertRaises(RemoteError):
            self.coordinator.start(self.client, self.endpoint, "/missing.hex")
        self.assertIsNone(self.coordinator.current(self.client, self.endpoint))
        self.assertEqual(len(self.server.device.jobs), 0)

    def test_unwritable_recovery_path_stops_before_sending_flash(self):
        parent = self.root / "not-a-directory"
        parent.write_text("file", encoding="utf-8")
        coordinator = FlashCoordinator(parent / "state.json")
        with self.assertRaises(PendingFlashError):
            coordinator.start(self.client, self.endpoint, "/blink.hex")
        self.assertEqual(len(self.server.device.jobs), 0)
        self.assertIsNone(coordinator.current(self.client, self.endpoint))

    def test_corrupt_recovery_record_fails_closed_without_destroying_it(self):
        self.state.write_text("{broken", encoding="utf-8")
        coordinator = FlashCoordinator(self.state)
        with self.assertRaises(PendingFlashError):
            coordinator.start(self.client, self.endpoint, "/blink.hex")
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(len(self.server.device.jobs), 0)


if __name__ == "__main__":
    unittest.main()
