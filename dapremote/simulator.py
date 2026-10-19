"""Loopback-only reference device. This is a simulator, not an authenticated server."""
import hashlib
import json
import os
import secrets
import socket
import socketserver
import struct
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .benchmark import BenchmarkReceiver

from .intelhex import HexError, parse_hex
from .protocol import (Decoder, FILE_HEADER, Frame, Kind, Op, ProtocolError, Status,
                       cobs_decode, cobs_encode, decode_fields, response_payload)

TERMINAL = {"DONE", "FAILED", "CANCELLED", "INTERRUPTED"}


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            digest.update(block)
    return digest.digest()


class DeviceError(Exception):
    def __init__(self, status, message, **fields):
        self.status, self.message, self.fields = status, message, fields


class Device:
    def __init__(self, root, capacity=32 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.internal = self.root / ".dap"
        self.internal.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.boot_id = secrets.randbits(64) or 1
        self.media_epoch = self.boot_id
        self.capacity = capacity
        self.sd_ready, self.io_fail, self.verify_fail = True, False, False
        self.battery_mv, self.battery_percent = 4000, 80
        self.charging, self.external_power = False, False
        self.base_address, self.flash_size = 0x08000000, 256 * 1024
        self.flash = bytearray(b"\xff" * self.flash_size)
        self.transfers, self.jobs, self.streams = {}, {}, {}
        self.benchmark = None
        self.started = time.monotonic()
        state = self.internal / "jobs.json"
        if state.exists():
            # Fail closed on damaged journal; do not silently lose deduplication records.
            self.jobs = json.loads(state.read_text(encoding="utf-8"))
            for job in self.jobs.values():
                if job["state"] not in TERMINAL:
                    job.update(state="INTERRUPTED", phase="INTERRUPTED", error="device restarted")
            self._save_jobs()
        target = self.internal / "target.bin"
        if target.exists():
            data = target.read_bytes()
            if len(data) != self.flash_size:
                raise ValueError("invalid simulated target size")
            self.flash[:] = data
        # v0.1 resumes across links, not across process restarts.
        for path in self.internal.glob("upload-*.part"):
            path.unlink()
        for path in self.internal.glob("bench-*.part"):
            path.unlink()

    def _save_jobs(self):
        safe = {key: {k: v for k, v in job.items() if k not in ("image", "tick")}
                for key, job in self.jobs.items()}
        temp = self.internal / "jobs.tmp"
        with open(temp, "w", encoding="utf-8") as output:
            json.dump(safe, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, self.internal / "jobs.json")

    def _path(self, name, allow_root=False):
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240:
            raise DeviceError(Status.INVALID_ARGUMENT, "path must be 1..240 UTF-8 bytes")
        if "\\" in name or ":" in name or "\0" in name:
            raise DeviceError(Status.INVALID_ARGUMENT, "use slash-separated device paths")
        parts = name.strip("/").split("/")
        if any(part in ("..", ".") or part.lower() == ".dap" for part in parts):
            raise DeviceError(Status.INVALID_ARGUMENT, "reserved or traversing path")
        path = (self.root / name.lstrip("/")).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise DeviceError(Status.INVALID_ARGUMENT, "path escapes SD root")
        if path == self.internal or self.internal in path.parents:
            raise DeviceError(Status.INVALID_ARGUMENT, "internal path")
        if path == self.root and not allow_root:
            raise DeviceError(Status.INVALID_ARGUMENT, "root cannot be modified")
        return path

    def _sd(self):
        if not self.sd_ready:
            raise DeviceError(Status.SD_NOT_READY, "SD card is not ready")
        if self.io_fail:
            raise DeviceError(Status.IO_ERROR, "injected SD I/O failure")

    def _used(self):
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                if self.internal not in path.parents or path.name.startswith(("upload-", "bench-")):
                    total += path.stat().st_size
        return total

    def _busy(self, path):
        for transfer in self.transfers.values():
            if transfer["state"] == "ACTIVE" and transfer["path"] == str(path):
                return True
        for job in self.jobs.values():
            if job["state"] not in TERMINAL and str(path) in (job["path"], job.get("flm_path")):
                return True
        return any(stream["active"] and stream["destinations"] & 2 and
                   stream["sd_path_abs"] == str(path) for stream in self.streams.values())

    def _assert_unlocked(self, path, subtree=False):
        candidates = [path]
        if subtree and path.is_dir():
            candidates.extend(path.rglob("*"))
        if any(self._busy(candidate) for candidate in candidates):
            raise DeviceError(Status.BUSY, "file is in use")

    def _stat(self, path):
        if not path.exists():
            raise DeviceError(Status.NOT_FOUND, "path not found")
        return dict(name=path.name, path="/" + path.relative_to(self.root).as_posix(),
                    is_dir=path.is_dir(), size=0 if path.is_dir() else path.stat().st_size,
                    version="" if path.is_dir() else digest_file(path).hex())

    def _version(self, path, expected):
        info = self._stat(path)
        if info["is_dir"]:
            raise DeviceError(Status.INVALID_ARGUMENT, "regular file required")
        if not expected or info["version"] != expected:
            raise DeviceError(Status.FILE_CHANGED, "file version does not match")
        return info

    def _power(self):
        return dict(battery_mv=self.battery_mv, battery_percent=self.battery_percent,
                    charging=self.charging, external_power=self.external_power,
                    can_flash=self.external_power or self.battery_percent >= 20)

    def _transfer(self, transfer_id):
        try:
            return self.transfers[transfer_id]
        except KeyError:
            raise DeviceError(Status.NOT_FOUND, "transfer expired or device restarted")

    def _transfer_info(self, transfer_id, transfer):
        return dict(transfer_id=transfer_id, state=transfer["state"],
                    next_offset=transfer["offset"], checkpoint_offset=0,
                    size=transfer["size"], sha256=bytes.fromhex(transfer["sha256"]),
                    block_size=512, direction=transfer["direction"])

    def _job_info(self, job):
        keys = ("job_id", "operation_token", "state", "phase", "completed_bytes",
                "total_bytes", "error", "run_after")
        return {key: job[key] for key in keys}

    def handle(self, opcode, fields, data=None, session=0):
        with self.lock:
            try:
                return self._dispatch(Op(opcode), fields, data, session)
            except DeviceError as exc:
                return exc.status, dict(message=exc.message, **exc.fields)
            except HexError as exc:
                return Status.INVALID_HEX, dict(message=str(exc))
            except FileNotFoundError:
                return Status.NOT_FOUND, dict(message="path not found")
            except FileExistsError:
                return Status.ALREADY_EXISTS, dict(message="path already exists")
            except (KeyError, ValueError, TypeError, struct.error) as exc:
                return Status.INVALID_ARGUMENT, dict(message=str(exc)[:240])
            except OSError as exc:
                return Status.IO_ERROR, dict(message=str(exc)[:240])

    def _dispatch(self, op, f, data, session=0):
        if op in (Op.BENCH_BEGIN, Op.BENCH_END, Op.BENCH_STATUS, Op.BENCH_ABORT):
            return self._bench_control(op, f, session)
        if (self.benchmark is not None and self.benchmark.state == "ACTIVE"
                and op in (Op.FILE_BEGIN, Op.FLASH_START)):
            raise DeviceError(Status.BUSY, "benchmark active")
        if op == Op.PING:
            return Status.OK, dict(boot_id=self.boot_id)
        if op == Op.GET_CAPS:
            return Status.OK, dict(protocol=1, max_payload=4096, block_size=512,
                                   features="files,hex,jobs,rtt-pull,rtt-sd,power,sim-config,bench-raw-v1,flm-select-v1;no-auth;no-power-resume")
        if op == Op.DEV_INFO:
            return Status.OK, dict(device_id="DAP-SIM-001", boot_id=self.boot_id,
                                   protocol=1, target_id="SIM-CORTEX-M")
        if op == Op.POWER_STATUS:
            return Status.OK, self._power()
        if op == Op.DEV_STATUS:
            return Status.OK, dict(sd_ready=self.sd_ready, boot_id=self.boot_id,
                                   media_epoch=self.media_epoch, **self._power(),
                                   transfers=[dict(self._transfer_info(tid, t),
                                                   path="/" + Path(t["path"]).relative_to(self.root).as_posix())
                                              for tid, t in self.transfers.items() if t["state"] == "ACTIVE"],
                                   streams=[dict(stream_id=sid, destinations=s["destinations"], sd_path=s["sd_path"])
                                            for sid, s in self.streams.items() if s["active"]])
        if op == Op.SIM_CONFIG:
            allowed = {"battery_mv", "battery_percent", "charging", "external_power",
                       "sd_ready", "io_fail", "verify_fail", "capacity"}
            if not set(f) <= allowed or not f:
                raise DeviceError(Status.INVALID_ARGUMENT, "unsupported simulator setting")
            if "battery_percent" in f and f["battery_percent"] > 100:
                raise DeviceError(Status.INVALID_ARGUMENT, "battery percent must be <=100")
            if "sd_ready" in f and f["sd_ready"] != self.sd_ready:
                if any(t["state"] == "ACTIVE" for t in self.transfers.values()):
                    raise DeviceError(Status.BUSY, "abort active transfers before changing media")
                self.media_epoch += 1
            for key, value in f.items():
                setattr(self, key, value)
            return Status.OK, {}
        if op == Op.TARGET_INFO:
            return Status.OK, dict(target_id="SIM-CORTEX-M", base_address=self.base_address,
                                   flash_size=self.flash_size)
        if op in (Op.JOB_STATUS, Op.JOB_CANCEL):
            job = self.jobs.get(f.get("job_id"))
            if job is None and f.get("operation_token"):
                job = next((j for j in self.jobs.values()
                            if j["operation_token"] == f["operation_token"]), None)
            if job is None:
                raise DeviceError(Status.RESULT_UNKNOWN, "no retained operation record")
            if op == Op.JOB_CANCEL and job["state"] not in TERMINAL:
                job.update(state="CANCELLED", phase="CANCELLED", error="cancelled at simulator checkpoint")
                self._save_jobs()
            return Status.OK, self._job_info(job)
        if op == Op.JOB_LIST:
            cursor, limit = f.get("cursor", 0), min(f.get("limit", 8), 8)
            if not limit:
                raise DeviceError(Status.INVALID_ARGUMENT, "limit must be positive")
            jobs = list(self.jobs.values())
            from .protocol import encode_fields
            page, used = [], 4
            for job in jobs[cursor:cursor + limit]:
                length = 4 + len(encode_fields(self._job_info(job)))
                if used + length > 3400:
                    break
                page.append(job)
                used += length
            return Status.OK, dict(jobs=[self._job_info(j) for j in page],
                                   next_cursor=cursor + len(page) if cursor + len(page) < len(jobs) else 0)
        if op in (Op.RTT_START, Op.RTT_STOP, Op.RTT_READ, Op.RTT_STATS, Op.RTT_WRITE):
            return self._rtt(op, f)
        if op == Op.FILE_STATUS:
            transfer = self._transfer(f["transfer_id"])
            transfer["last_active"] = time.monotonic()
            return Status.OK, self._transfer_info(f["transfer_id"], transfer)
        if op == Op.FILE_ABORT:
            transfer = self._transfer(f["transfer_id"])
            if transfer["state"] == "COMMITTED":
                raise DeviceError(Status.BUSY, "already committed")
            transfer["state"] = "ABORTED"
            if transfer["direction"] == "upload":
                Path(transfer["temp"]).unlink(missing_ok=True)
            return Status.OK, {}
        self._sd()
        if op == Op.FS_INFO:
            return Status.OK, dict(capacity=self.capacity, free=max(0, self.capacity - self._used()),
                                   media_epoch=self.media_epoch)
        if op == Op.FS_LIST:
            path = self._path(f.get("path", "/"), allow_root=True)
            if not path.is_dir():
                raise DeviceError(Status.NOT_FOUND, "directory not found")
            paths = sorted((p for p in path.iterdir() if p.name.lower() != ".dap" and not p.is_symlink()),
                           key=lambda p: p.name)
            entries = [self._stat(p) for p in paths]
            version = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
            cursor, limit = f.get("cursor", 0), min(f.get("limit", 8), 8)
            if not limit or cursor > len(entries):
                raise DeviceError(Status.INVALID_ARGUMENT, "invalid pagination")
            if cursor and f.get("version") != version:
                raise DeviceError(Status.FILE_CHANGED, "directory changed; restart listing")
            # Variable path lengths require a byte budget as well as an entry limit.
            from .protocol import encode_fields
            page, used = [], 4
            for item in entries[cursor:cursor + limit]:
                length = 4 + len(encode_fields(item))
                if used + length > 3400:
                    break
                page.append(item)
                used += length
            return Status.OK, dict(entries=page, version=version,
                                   next_cursor=cursor + len(page) if cursor + len(page) < len(entries) else 0)
        if op in (Op.FS_STAT, Op.FS_HASH):
            path = self._path(f["path"])
            info = self._stat(path)
            if op == Op.FS_HASH:
                self._version(path, f["version"])
                info["sha256"] = digest_file(path)
            return Status.OK, info
        if op == Op.FS_MKDIR:
            self._path(f["path"]).mkdir()
            return Status.OK, {}
        if op in (Op.FS_DELETE, Op.FS_RENAME):
            path = self._path(f["path"])
            self._assert_unlocked(path, subtree=True)
            if path.is_file():
                self._version(path, f["version"])
            if op == Op.FS_DELETE:
                path.rmdir() if path.is_dir() else path.unlink()
            else:
                target = self._path(f["new_path"])
                self._assert_unlocked(target)
                if target.exists():
                    raise DeviceError(Status.ALREADY_EXISTS, "destination exists")
                path.rename(target)
            return Status.OK, {}
        if op == Op.FILE_BEGIN:
            return self._begin(f)
        if op == Op.FILE_DATA:
            return self._write(data)
        if op == Op.FILE_READ:
            transfer = self._transfer(f["transfer_id"])
            if transfer["direction"] != "download" or transfer["state"] != "ACTIVE":
                raise DeviceError(Status.INVALID_ARGUMENT, "not an active download")
            path = Path(transfer["path"])
            self._version(path, transfer["version"])
            offset, length = f["offset"], f.get("length", 512)
            if not 1 <= length <= 512 or offset > transfer["size"]:
                raise DeviceError(Status.INVALID_ARGUMENT, "invalid read range")
            with open(path, "rb") as source:
                source.seek(offset)
                block = source.read(length)
            transfer["offset"] = max(transfer["offset"], offset + len(block))
            transfer["last_active"] = time.monotonic()
            return Status.OK, dict(offset=offset, data=block, next_offset=offset + len(block))
        if op == Op.FILE_END:
            return self._end(f)
        if op in (Op.FLASH_CHECK, Op.FLASH_START):
            return self._flash(op, f)
        return Status.UNSUPPORTED, dict(message="opcode not implemented")

    def _begin(self, f):
        direction = f["direction"]
        if direction not in ("upload", "download"):
            raise DeviceError(Status.INVALID_ARGUMENT, "direction must be upload/download")
        path = self._path(f["path"])
        if any(t["state"] == "ACTIVE" for t in self.transfers.values()):
            raise DeviceError(Status.BUSY, "one active transfer supported")
        if len(self.transfers) >= 256:
            raise DeviceError(Status.BUSY, "transfer history full; restart simulator")
        self._assert_unlocked(path)
        transfer_id = secrets.randbits(64) or 1
        transfer = dict(direction=direction, path=str(path), state="ACTIVE", offset=0,
                        last_active=time.monotonic(), overwrite=f.get("overwrite", False))
        if direction == "upload":
            size, sha = f["size"], f["sha256"]
            if len(sha) != 32:
                raise DeviceError(Status.INVALID_ARGUMENT, "SHA-256 must be 32 bytes")
            if path.exists() and (path.is_dir() or not transfer["overwrite"]):
                raise DeviceError(Status.ALREADY_EXISTS, "destination exists")
            if not path.parent.is_dir():
                raise DeviceError(Status.NOT_FOUND, "parent directory missing")
            if size > max(0, self.capacity - self._used()):
                raise DeviceError(Status.NO_SPACE, "insufficient space for temporary file")
            temp = self.internal / ("upload-%016x.part" % transfer_id)
            temp.touch(exist_ok=False)
            transfer.update(temp=str(temp), size=size, sha256=sha.hex(),
                            original_version=digest_file(path).hex() if path.exists() else None)
        else:
            info = self._version(path, f["version"])
            transfer.update(size=info["size"], sha256=info["version"], version=info["version"])
        self.transfers[transfer_id] = transfer
        return Status.OK, self._transfer_info(transfer_id, transfer)

    def _write(self, data):
        if data is None or len(data) < FILE_HEADER.size:
            raise DeviceError(Status.INVALID_ARGUMENT, "missing file block header")
        transfer_id, offset, size = FILE_HEADER.unpack_from(data)
        block = data[FILE_HEADER.size:]
        transfer = self._transfer(transfer_id)
        if transfer["direction"] != "upload" or transfer["state"] != "ACTIVE":
            raise DeviceError(Status.INVALID_ARGUMENT, "not an active upload")
        if not 1 <= size <= 512 or len(block) != size or offset + size > transfer["size"]:
            raise DeviceError(Status.INVALID_ARGUMENT, "invalid block length/range")
        if offset > transfer["offset"] or offset < transfer["offset"] < offset + size:
            raise DeviceError(Status.OFFSET_MISMATCH, "unexpected offset", next_offset=transfer["offset"])
        with open(transfer["temp"], "r+b") as output:
            output.seek(offset)
            if offset < transfer["offset"]:
                if output.read(size) != block:
                    raise DeviceError(Status.REQUEST_CONFLICT, "duplicate block differs")
            else:
                if size > max(0, self.capacity - self._used()):
                    raise DeviceError(Status.NO_SPACE, "SD full")
                if output.write(block) != size:
                    raise DeviceError(Status.IO_ERROR, "short write")
                output.flush()
                transfer["offset"] += size
        transfer["last_active"] = time.monotonic()
        return Status.OK, dict(next_offset=transfer["offset"], checkpoint_offset=0)

    def _end(self, f):
        transfer_id = f["transfer_id"]
        transfer = self._transfer(transfer_id)
        if transfer["state"] == "COMMITTED":
            return Status.OK, self._transfer_info(transfer_id, transfer)
        if transfer["state"] != "ACTIVE":
            raise DeviceError(Status.INVALID_ARGUMENT, "transfer is not active")
        path = Path(transfer["path"])
        if transfer["direction"] == "upload":
            if transfer["offset"] != transfer["size"]:
                raise DeviceError(Status.OFFSET_MISMATCH, "file incomplete", next_offset=transfer["offset"])
            temp = Path(transfer["temp"])
            with open(temp, "r+b") as output:
                output.flush()
                os.fsync(output.fileno())
            if digest_file(temp).hex() != transfer["sha256"]:
                raise DeviceError(Status.HASH_MISMATCH, "whole-file SHA-256 mismatch")
            current = digest_file(path).hex() if path.is_file() else None
            if current != transfer["original_version"] or path.is_dir():
                raise DeviceError(Status.FILE_CHANGED, "destination changed during upload")
            os.replace(temp, path)
        else:
            self._version(path, transfer["version"])
        transfer["state"] = "COMMITTED"
        return Status.OK, self._transfer_info(transfer_id, transfer)

    def _flash(self, op, f):
        token = f.get("operation_token", "")
        fingerprint = hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest()
        if op == Op.FLASH_START:
            if not 1 <= len(token) <= 80:
                raise DeviceError(Status.INVALID_ARGUMENT, "operation token required, max 80 characters")
            for job in self.jobs.values():
                if job["operation_token"] == token:
                    if job["fingerprint"] != fingerprint:
                        raise DeviceError(Status.REQUEST_CONFLICT, "operation token reused with different parameters")
                    return Status.ACCEPTED, self._job_info(job)
        flm = None
        if "flm_path" in f or "flm_version" in f:
            if not f.get("flm_path") or not f.get("flm_version"):
                raise DeviceError(Status.INVALID_ARGUMENT, "FLM path and version are required together")
            flm = self._path(f["flm_path"])
            self._version(flm, f["flm_version"])
            self._assert_unlocked(flm)
            if flm.suffix.lower() != ".flm":
                raise DeviceError(Status.UNSUPPORTED, "烧录算法仅支持 .FLM 文件，不支持 FLX")
            if not 52 <= flm.stat().st_size <= 4 * 1024 * 1024:
                raise DeviceError(Status.INVALID_ARGUMENT, "FLM 文件大小必须为 52 字节至 4 MiB")
            with flm.open("rb") as source:
                header = source.read(52)
            # Envelope check only. The simulator never executes an uploaded FLM.
            if (header[:7] != b"\x7fELF\x01\x01\x01" or struct.unpack_from("<H", header, 18)[0] != 40
                    or struct.unpack_from("<H", header, 16)[0] not in (1, 2)
                    or struct.unpack_from("<H", header, 40)[0] != 52):
                raise DeviceError(Status.INVALID_ARGUMENT, "FLM is not a supported ELF32 little-endian ARM envelope")
        path = self._path(f["path"])
        self._version(path, f["version"])
        self._assert_unlocked(path)
        if f.get("target_id", "SIM-CORTEX-M") != "SIM-CORTEX-M":
            raise DeviceError(Status.TARGET_MISMATCH, "unsupported simulated target")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise DeviceError(Status.INVALID_HEX, "simulator HEX input limited to 4 MiB")
        image = parse_hex(path.read_bytes(), self.base_address, self.flash_size)
        if op == Op.FLASH_CHECK:
            return Status.OK, dict(total_bytes=len(image), min_address=min(image), max_address=max(image),
                                   target_id="SIM-CORTEX-M")
        if not self._power()["can_flash"]:
            raise DeviceError(Status.POWER_INSUFFICIENT, "simulator requires >=20 percent or external power")
        if any(j["state"] not in TERMINAL for j in self.jobs.values()):
            raise DeviceError(Status.BUSY, "target already in use")
        if any(s["active"] for s in self.streams.values()):
            raise DeviceError(Status.BUSY, "stop RTT before flashing")
        if len(self.jobs) >= 128:
            raise DeviceError(Status.BUSY, "retained job journal full; archive manually before reuse")
        job_id = uuid.uuid4().hex
        job = dict(job_id=job_id, operation_token=token, fingerprint=fingerprint,
                   path=str(path), state="QUEUED", phase="QUEUED", completed_bytes=0,
                   total_bytes=len(image), error="", run_after=f.get("run_after", False),
                   image=sorted(image.items()), tick=time.monotonic())
        if flm is not None:
            job.update(flm_path=str(flm), flm_version=f["flm_version"])
        self.jobs[job_id] = job
        try:
            self._save_jobs()
        except OSError:
            del self.jobs[job_id]
            raise
        return Status.ACCEPTED, self._job_info(job)

    def _rtt(self, op, f):
        if op == Op.RTT_START:
            if any(j["state"] not in TERMINAL for j in self.jobs.values()):
                raise DeviceError(Status.BUSY, "target in use")
            if any(s["active"] for s in self.streams.values()) or len(self.streams) >= 32:
                raise DeviceError(Status.BUSY, "RTT stream limit reached")
            destinations = f["destinations"]
            rate, cap = f.get("rate", 1024), f.get("rtt_capacity", 8192)
            if destinations not in (1, 2, 3) or not 1 <= rate <= 100000 or not 512 <= cap <= 1024 * 1024:
                raise DeviceError(Status.INVALID_ARGUMENT, "invalid RTT destination/rate/capacity")
            if f.get("channel_id", 0) != 0:
                raise DeviceError(Status.UNSUPPORTED, "simulator supports RTT channel 0 only")
            stream_id = secrets.randbits(64) or 1
            sd_path = f.get("sd_path", "/rtt-%016x.log" % stream_id)
            path = self._path(sd_path) if destinations & 2 else None
            if path:
                self._sd()
                self._assert_unlocked(path)
                with open(path, "xb"):
                    pass
            stream = dict(active=True, destinations=destinations, rate=rate, capacity=cap,
                          queue=bytearray(), generated=0, sequence=0, dropped_bytes=0,
                          sd_bytes=0, gui_bytes=0, sd_error="", last=time.monotonic(),
                          sd_path_abs=str(path) if path else "", sd_path=sd_path if path else "",
                          sd_max_bytes=f.get("sd_max_bytes", 1024 * 1024), input_data=b"")
            self.streams[stream_id] = stream
            return Status.OK, dict(stream_id=stream_id, sd_path=stream["sd_path"],
                                   destinations=destinations, disconnect_policy="CONTINUE_BOUNDED")
        try:
            stream = self.streams[f["stream_id"]]
        except KeyError:
            raise DeviceError(Status.NOT_FOUND, "RTT stream not found")
        if op == Op.RTT_STOP:
            stream["active"] = False
        if op == Op.RTT_WRITE:
            if not stream["active"]:
                raise DeviceError(Status.INVALID_ARGUMENT, "RTT stopped")
            data = f["data"]
            if len(data) > 512:
                raise DeviceError(Status.INVALID_ARGUMENT, "RTT input limited to 512 bytes")
            # Simulated target consumes input immediately. Keep only bounded last input.
            stream["input_data"] = data
            return Status.OK, dict(written_bytes=len(data))
        result = dict(stream_id=f["stream_id"], state="ACTIVE" if stream["active"] else "STOPPED",
                      dropped_bytes=stream["dropped_bytes"], sd_error=stream["sd_error"],
                      sd_bytes=stream["sd_bytes"], gui_bytes=stream["gui_bytes"],
                      destinations=stream["destinations"], sd_path=stream["sd_path"])
        if op == Op.RTT_READ:
            length = f.get("length", 512)
            if not 1 <= length <= 1024:
                raise DeviceError(Status.INVALID_ARGUMENT, "RTT read limited to 1..1024 bytes")
            data = bytes(stream["queue"][:length])
            del stream["queue"][:length]
            result.update(data=data, sequence=stream["sequence"], channel_id=0,
                          timestamp_ms=int((time.monotonic() - self.started) * 1000))
            if data:
                stream["sequence"] += 1
                stream["gui_bytes"] += len(data)
        return Status.OK, result

    def tick(self):
        with self.lock:
            now = time.monotonic()
            if (self.benchmark is not None and self.benchmark.state == "ACTIVE" and
                    time.perf_counter_ns() - self.benchmark.last_active > 10_000_000_000):
                self.benchmark.finish(state="EXPIRED")
            for transfer in self.transfers.values():
                if transfer["state"] == "ACTIVE" and now - transfer["last_active"] > 600:
                    transfer["state"] = "EXPIRED"
                    if transfer["direction"] == "upload":
                        Path(transfer["temp"]).unlink(missing_ok=True)
            for job in self.jobs.values():
                if job["state"] in TERMINAL or now - job["tick"] < 0.08:
                    continue
                job["tick"] = now
                phase = job["phase"]
                if phase == "QUEUED":
                    job.update(state="RUNNING", phase="CONNECTING")
                elif phase == "CONNECTING":
                    job["phase"] = "ERASING"
                elif phase == "ERASING":
                    # Erase only touched 1 KiB simulated sectors, not the whole chip.
                    sectors = {(address - self.base_address) // 1024 for address, _ in job["image"]}
                    for sector in sectors:
                        offset = sector * 1024
                        self.flash[offset:offset + 1024] = b"\xff" * 1024
                    self._persist_flash()
                    job["phase"] = "PROGRAMMING"
                elif phase == "PROGRAMMING":
                    start = job["completed_bytes"]
                    for address, byte in job["image"][start:start + 256]:
                        self.flash[address - self.base_address] = byte
                    job["completed_bytes"] = min(start + 256, job["total_bytes"])
                    self._persist_flash()
                    if job["completed_bytes"] == job["total_bytes"]:
                        job["phase"] = "VERIFYING"
                elif phase == "VERIFYING":
                    failed = self.verify_fail or any(self.flash[a - self.base_address] != b for a, b in job["image"])
                    if failed:
                        job.update(state="FAILED", phase="FAILED", error="VERIFY_FAILED")
                    elif job["run_after"]:
                        job["phase"] = "RESETTING"
                    else:
                        job.update(state="DONE", phase="DONE")
                else:
                    job.update(state="DONE", phase="DONE")
                self._save_jobs()
            pattern = b"SIM RTT: target running\n\x00\xff"
            for stream in self.streams.values():
                if not stream["active"]:
                    continue
                count = min(4096, int((now - stream["last"]) * stream["rate"]))
                if not count:
                    continue
                stream["last"] += count / stream["rate"]
                start = stream["generated"]
                data = bytes(pattern[(start + i) % len(pattern)] for i in range(count))
                stream["generated"] += count
                if stream["destinations"] & 1:
                    room = stream["capacity"] - len(stream["queue"])
                    stream["queue"].extend(data[:room])
                    stream["dropped_bytes"] += max(0, count - room)
                if stream["destinations"] & 2 and not stream["sd_error"]:
                    try:
                        self._sd()
                        timestamp = int((now - self.started) * 1000)
                        # File header DRT1; records: time:u64, channel:u16, size:u16, raw data.
                        new_file = stream["sd_bytes"] == 0
                        record = (b"DRT1" if new_file else b"") + struct.pack("<QHH", timestamp, 0, len(data)) + data
                        if stream["sd_bytes"] + len(record) > stream["sd_max_bytes"] or len(record) > max(0, self.capacity - self._used()):
                            raise DeviceError(Status.NO_SPACE, "RTT SD limit reached")
                        with open(stream["sd_path_abs"], "ab") as output:
                            output.write(record)
                        stream["sd_bytes"] += len(record)
                    except (DeviceError, OSError) as exc:
                        stream["sd_error"] = exc.message if isinstance(exc, DeviceError) else str(exc)

    def _persist_flash(self):
        with open(self.internal / "target.bin", "wb") as output:
            output.write(self.flash)
            output.flush()
            os.fsync(output.fileno())

    def _bench_control(self, op, fields, session):
        if op == Op.BENCH_BEGIN:
            if (self.benchmark is not None and self.benchmark.state == "ACTIVE") or any(
                    t["state"] == "ACTIVE" for t in self.transfers.values()) or any(
                    j["state"] not in TERMINAL for j in self.jobs.values()):
                raise DeviceError(Status.BUSY, "file transfer, target job or benchmark active")
            sink = fields.get("sink", "memory")
            if sink == "sd":
                self._sd()
                if fields["size"] > max(0, self.capacity - self._used()):
                    raise DeviceError(Status.NO_SPACE, "insufficient benchmark SD space")
            temp = self.internal / ("bench-%s.part" % uuid.uuid4().hex)
            def check_write(size):
                self._sd()
                if size > max(0, self.capacity - self._used()):
                    raise OSError("benchmark SD full")
            self.benchmark = BenchmarkReceiver(session, fields["size"], fields["sha256"], sink,
                                                temp if sink == "sd" else None, check_write)
            return Status.OK, self.benchmark.result()
        bench = self.benchmark
        if bench is None or bench.owner != session or bench.bench_id != fields["bench_id"]:
            raise DeviceError(Status.NOT_FOUND, "benchmark not found in this session")
        if op == Op.BENCH_END:
            return Status.OK, bench.finish(fields.get("framing_errors", 0))
        if op == Op.BENCH_ABORT:
            return Status.OK, bench.finish(state="CANCELLED")
        return Status.OK, bench.result()

    def benchmark_data(self, session, payload):
        with self.lock:
            if self.benchmark is not None and self.benchmark.owner == session:
                self.benchmark.feed(payload)

    def close_benchmark(self, session):
        with self.lock:
            if self.benchmark is not None and self.benchmark.owner == session:
                self.benchmark.finish(state="CANCELLED")


class FaultPlan:
    """One-shot response faults: (action, opcode, occurrence). Counts include retries."""
    def __init__(self, rules=(), fragment_size=0, delay=0.0):
        self.rules = list(rules)
        self.counts = {}
        self.lock = threading.Lock()
        self.fragment_size, self.delay = fragment_size, delay

    def action(self, opcode):
        with self.lock:
            self.counts[opcode] = self.counts.get(opcode, 0) + 1
            for action, op, occurrence in self.rules:
                if opcode == op and self.counts[opcode] == occurrence:
                    return action
        return None


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.session = 0
        try:
            self._handle_connection()
        finally:
            self.server.device.close_benchmark(self.session)

    def _handle_connection(self):
        self.request.settimeout(0.2)
        decoder, cache, session = Decoder(), OrderedDict(), 0
        highest_request = 0
        benchmark_start_errors = 0
        hello = None
        while not self.server.stopping.is_set():
            try:
                data = self.request.recv(4096)
                if not data:
                    return
            except socket.timeout:
                continue
            except OSError:
                return
            for frame in decoder.feed(data):
                if frame.kind == Kind.BENCH_DATA:
                    if not session or frame.session != session:
                        continue
                    action = self.server.faults.action(Op.BENCH_DATA)
                    if action == "disconnect":
                        return
                    if action == "drop":
                        continue
                    payload = frame.payload
                    if action == "corrupt" and len(payload) > 16:
                        payload = payload[:-1] + bytes([payload[-1] ^ 0x80])
                    if action == "truncate":
                        payload = payload[:max(16, len(payload) // 2)]
                    self.server.device.benchmark_data(session, payload)
                    continue
                if frame.kind != Kind.REQUEST or not frame.request:
                    continue
                signature = (frame.channel, frame.opcode, frame.payload)
                response_session = session
                try:
                    if frame.opcode == Op.HELLO:
                        if frame.session != 0 or frame.channel != 0:
                            status, fields = Status.SESSION_INVALID, dict(message="HELLO requires session/channel zero")
                        elif hello is not None and hello != (frame.request, signature):
                            status, fields = Status.SESSION_INVALID, dict(message="one HELLO per connection")
                        else:
                            fields_in = decode_fields(frame.payload)
                            if fields_in.get("protocol") != 1:
                                status, fields = Status.UNSUPPORTED, dict(message="protocol 1 required")
                            else:
                                session = session or secrets.randbits(64) or 1
                                self.session = session
                                hello = (frame.request, signature)
                                status, fields = Status.OK, dict(boot_id=self.server.device.boot_id,
                                                                  protocol=1, device_id="DAP-SIM-001")
                        response_session = session
                        payload = response_payload(status, **fields)
                    elif not session or frame.session != session:
                        payload = response_payload(Status.SESSION_INVALID, message="invalid session")
                    elif frame.request in cache:
                        previous, payload = cache[frame.request]
                        if previous != signature:
                            payload = response_payload(Status.REQUEST_CONFLICT, message="request ID reused")
                    elif frame.request <= highest_request:
                        payload = response_payload(Status.RESULT_UNKNOWN, message="request older than response cache; query state")
                    else:
                        expected_channel = 1 if frame.opcode in (Op.FILE_DATA, Op.FILE_READ) else 3 if 0x0700 <= frame.opcode <= 0x07FF else 0
                        if frame.channel != expected_channel:
                            status, fields = Status.INVALID_ARGUMENT, dict(message="wrong channel")
                        else:
                            try:
                                Op(frame.opcode)
                            except ValueError:
                                status, fields = Status.UNSUPPORTED, dict(message="unknown opcode")
                            else:
                                fields_in = {} if frame.opcode == Op.FILE_DATA else decode_fields(frame.payload)
                                if frame.opcode == Op.BENCH_END:
                                    fields_in["framing_errors"] = decoder.errors - benchmark_start_errors
                                status, fields = self.server.device.handle(frame.opcode, fields_in, frame.payload, session=session)
                                if frame.opcode == Op.BENCH_BEGIN and status == Status.OK:
                                    benchmark_start_errors = decoder.errors
                        payload = response_payload(status, **fields)
                        cache[frame.request] = (signature, payload)
                        highest_request = frame.request
                        if len(cache) > 256:
                            cache.popitem(last=False)
                except (ValueError, UnicodeError, struct.error) as exc:
                    payload = response_payload(Status.INVALID_ARGUMENT, message=str(exc)[:240])
                response = Frame(Kind.RESPONSE, frame.channel, response_session,
                                 frame.request, frame.opcode, payload).encode()
                action = self.server.faults.action(frame.opcode)
                if self.server.trace:
                    with self.server.trace_lock:
                        with open(self.server.trace, "a", encoding="utf-8") as output:
                            output.write(json.dumps(dict(time=time.time(), request=frame.request,
                                                         opcode=frame.opcode, fault=action,
                                                         status=int.from_bytes(payload[:2], "little"))) + "\n")
                if action == "drop":
                    continue
                if action == "disconnect":
                    return
                if action == "corrupt":
                    raw = bytearray(cobs_decode(response[:-1]))
                    raw[-1] ^= 0x80
                    response = cobs_encode(raw) + b"\0"
                if action == "truncate":
                    response = response[:len(response) // 2] + b"\0"
                if self.server.faults.delay:
                    time.sleep(self.server.faults.delay)
                try:
                    size = self.server.faults.fragment_size or len(response)
                    for offset in range(0, len(response), size):
                        self.request.sendall(response[offset:offset + size])
                except OSError:
                    return


class SimulatorServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, root, host="127.0.0.1", port=8765, faults=None, trace=None):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("v0.1 has no authentication: loopback binding only")
        self.device = Device(root)
        self.faults = faults or FaultPlan()
        self.trace, self.trace_lock = trace, threading.Lock()
        self.stopping = threading.Event()
        super().__init__((host, port), _Handler)
        self._threads_started = []

    def start(self):
        worker = threading.Thread(target=self.serve_forever, kwargs=dict(poll_interval=0.05), daemon=True)
        ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._threads_started = [worker, ticker]
        worker.start()
        ticker.start()
        return self

    def _tick_loop(self):
        while not self.stopping.wait(0.02):
            try:
                self.device.tick()
            except OSError as exc:
                with self.device.lock:
                    for job in self.device.jobs.values():
                        if job["state"] not in TERMINAL:
                            job.update(state="FAILED", phase="FAILED", error="simulator storage error: " + str(exc))

    def close(self):
        self.stopping.set()
        if self._threads_started:
            self.shutdown()
        self.server_close()
        for worker in self._threads_started:
            worker.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
