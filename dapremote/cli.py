"""python -m dapremote --help"""
import argparse
import json
import time
import uuid
from pathlib import Path

from .client import Client
from .protocol import Op
from .simulator import FaultPlan, SimulatorServer


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2,
                     default=lambda v: v.hex() if isinstance(v, bytes) else str(v)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="DAP Remote v0.2 - protocol client and simulator")
    parser.add_argument("--endpoint", default="tcp://127.0.0.1:8765", help="TCP URL or serial COM port")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=1.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sim = sub.add_parser("sim", help="run loopback device simulator")
    sim.add_argument("--root", default="sim_sd")
    sim.add_argument("--port", type=int, default=8765)
    sim.add_argument("--fault", action="append", default=[], metavar="ACTION:OPCODE:OCCURRENCE",
                     help="e.g. drop:FILE_DATA:1 or corrupt:FILE_READ:2")
    sim.add_argument("--fragment-size", type=int, default=0)
    sim.add_argument("--delay-ms", type=int, default=0)
    sim.add_argument("--trace", help="metadata-only JSONL trace")
    sub.add_parser("info")
    ls = sub.add_parser("ls")
    ls.add_argument("path", nargs="?", default="/")
    for command in ("stat", "mkdir", "delete", "check"):
        item = sub.add_parser(command)
        item.add_argument("path")
    rename = sub.add_parser("rename")
    rename.add_argument("path")
    rename.add_argument("new_path")
    put = sub.add_parser("upload")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("--overwrite", action="store_true")
    put.add_argument("--resume-file")
    get = sub.add_parser("download")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("--overwrite", action="store_true")
    flash = sub.add_parser("flash")
    flash.add_argument("path")
    flash.add_argument("--token", required=True, help="retain and reuse for the SAME flash operation")
    flash.add_argument("--version", help="expected file SHA-256; required to replay after file changes")
    flash.add_argument("--run", action="store_true")
    flash.add_argument("--wait", action="store_true")
    job = sub.add_parser("job")
    group = job.add_mutually_exclusive_group(required=True)
    group.add_argument("--id")
    group.add_argument("--token")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    status = sub.add_parser("transfer")
    status.add_argument("transfer_id", type=int)
    abort = sub.add_parser("abort")
    abort.add_argument("transfer_id", type=int)
    rtt = sub.add_parser("rtt")
    rtt.add_argument("--dest", choices=("gui", "sd", "both"), default="gui")
    rtt.add_argument("--sd-path")
    rtt.add_argument("--rate", type=int, default=1024)
    rtt.add_argument("--seconds", type=float, default=5)
    rtt.add_argument("--sd-max-bytes", type=int, default=1024 * 1024)
    config = sub.add_parser("sim-config")
    config.add_argument("settings", help='JSON object, e.g. {"battery_percent":10}')
    for name in ("bench", "baud-scan"):
        bench = sub.add_parser(name, help="unacknowledged stream with final SHA-256 verification")
        bench.add_argument("--size-kib", type=int, default=64)
        bench.add_argument("--file", help="optional 1 byte..16 MiB local test file")
        bench.add_argument("--sink", choices=("memory", "sd"), default="memory")
        if name == "baud-scan":
            bench.add_argument("--rates", default="115200,230400,460800,921600")
            bench.add_argument("--rounds", type=int, default=3)
            bench.add_argument("--probe-timeout", type=float, default=0.5)
    sub.add_parser("gui", help="Tkinter test GUI (standard-library frontend)")
    args = parser.parse_args(argv)
    if args.command == "sim":
        rules = []
        for value in args.fault:
            action, name, occurrence = value.split(":")
            if action not in ("drop", "corrupt", "truncate", "disconnect") or int(occurrence) < 1:
                parser.error("invalid fault rule")
            rules.append((action, Op[name], int(occurrence)))
        if args.fragment_size < 0 or not 0 <= args.delay_ms <= 5000:
            parser.error("fragment size must be >=0; delay must be 0..5000 ms")
        faults = FaultPlan(rules, args.fragment_size, args.delay_ms / 1000)
        with SimulatorServer(args.root, port=args.port, faults=faults, trace=args.trace) as server:
            print("DAP simulator: tcp://127.0.0.1:%d | SD: %s" % (server.server_address[1], server.device.root), flush=True)
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
        return 0
    if args.command == "gui":
        from .gui import main as gui_main
        return gui_main(args.endpoint, args.baudrate)
    try:
        with Client.open(args.endpoint, args.baudrate, timeout=args.timeout) as client:
            command = args.command
            if command == "info":
                result = dict(device=client.call(Op.DEV_INFO), capabilities=client.call(Op.GET_CAPS),
                              status=client.call(Op.DEV_STATUS), sd=client.call(Op.FS_INFO))
            elif command == "ls":
                result = client.list_files(args.path)
            elif command == "stat":
                result = client.stat(args.path)
            elif command == "mkdir":
                result = client.mkdir(args.path)
            elif command == "delete":
                result = client.delete(args.path, client.stat(args.path)["version"])
            elif command == "rename":
                result = client.rename(args.path, args.new_path, client.stat(args.path)["version"])
            elif command == "upload":
                state = args.resume_file or str(Path(args.local).with_name(Path(args.local).name + ".dap-upload.json"))
                result = client.upload(args.local, args.remote, args.overwrite, state)
            elif command == "download":
                result = client.download(args.remote, args.local, args.overwrite)
            elif command == "check":
                result = client.check_hex(args.path)
            elif command == "flash":
                result = client.start_flash(args.path, args.token, args.version, args.run)
                _print(result)
                if not args.wait:
                    return 0
                result = client.wait_job(result["job_id"])
            elif command == "job":
                result = client.job_status(job_id=args.id, operation_token=args.token)
            elif command == "cancel":
                result = client.call(Op.JOB_CANCEL, job_id=args.job_id)
            elif command == "transfer":
                result = client.call(Op.FILE_STATUS, transfer_id=args.transfer_id)
            elif command == "abort":
                result = client.call(Op.FILE_ABORT, transfer_id=args.transfer_id)
            elif command == "sim-config":
                result = client.call(Op.SIM_CONFIG, **json.loads(args.settings))
            elif command == "bench":
                result = client.benchmark(size=args.size_kib * 1024, file_path=args.file, sink=args.sink)
                _print(result)
                return 0 if result["passed"] else 1
            elif command == "baud-scan":
                rates = [int(value.strip()) for value in args.rates.split(",")]
                result = client.scan_baudrates(rates, rounds=args.rounds, size=args.size_kib * 1024,
                                               file_path=args.file, sink=args.sink, probe_timeout=args.probe_timeout)
                _print(result)
                return 0 if result["restored"] and any(row["passed"] for row in result["results"]) else 1
            elif command == "rtt":
                stream = client.start_rtt({"gui": 1, "sd": 2, "both": 3}[args.dest], args.sd_path,
                                          args.rate, args.sd_max_bytes)
                _print(stream)
                end = time.monotonic() + args.seconds
                try:
                    while time.monotonic() < end:
                        if args.dest != "sd":
                            batch = client.read_rtt(stream["stream_id"])
                            print(repr(batch["data"]))
                        time.sleep(0.1)
                finally:
                    result = client.stop_rtt(stream["stream_id"])
            else:
                parser.error("unknown command")
            _print(result)
            return 1 if isinstance(result, dict) and result.get("state") in ("FAILED", "INTERRUPTED", "CANCELLED") else 0
    except (OSError, ValueError, RuntimeError) as exc:
        import sys
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
