"""Run a complete headless workflow: python -m examples.api_demo."""
import json
import tempfile
import time
import uuid
from pathlib import Path

from dapremote.client import Client
from dapremote.simulator import FaultPlan, SimulatorServer
from dapremote.protocol import Op


def main():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        faults = FaultPlan([("drop", Op.FILE_DATA, 1)], fragment_size=7)
        with SimulatorServer(base / "sd", port=0, faults=faults) as server:
            endpoint = "tcp://127.0.0.1:%d" % server.server_address[1]
            with Client.open(endpoint, timeout=0.2) as dap:
                uploaded = dap.upload(Path(__file__).with_name("blink.hex"), "/blink.hex")
                checked = dap.check_hex("/blink.hex", uploaded["version"])
                job = dap.start_flash("/blink.hex", uuid.uuid4().hex, version=uploaded["version"])
                result = dap.wait_job(job["job_id"])
                dap.download("/blink.hex", base / "retrieved.hex")
                stream = dap.start_rtt(3, "/demo.log")
                time.sleep(0.1)
                batch = dap.read_rtt(stream["stream_id"])
                dap.stop_rtt(stream["stream_id"])
                print(json.dumps(dict(hex=checked, job=result, retries=dap.retry_count,
                                      rtt_bytes=len(batch["data"]), sd_bytes=batch["sd_bytes"]),
                                 ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
