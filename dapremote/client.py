"""Synchronous API. Calls are serialized; use a worker thread in GUI programs."""
import hashlib
import contextlib
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from .protocol import (Decoder, FILE_HEADER, Frame, Kind, Op, ProtocolError, Status,
                       encode_fields, parse_response)
from .transport import connect


class RemoteError(RuntimeError):
    def __init__(self, status, fields):
        self.status, self.fields = status, fields
        super().__init__("%s: %s" % (status.name, fields.get("message", fields.get("error", ""))))


class RpcTimeout(TimeoutError):
    """Result may be unknown: query transfer/job state before replaying side effects."""


class ResumeError(ValueError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            digest.update(block)
    return digest.digest()


def _save_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


class Client:
    def __init__(self, transport, timeout=1.0, retries=5):
        if timeout <= 0 or not 0 <= retries <= 20:
            raise ValueError("timeout must be positive, retries must be 0..20")
        self.transport, self.timeout, self.retries = transport, timeout, retries
        self.decoder = Decoder()
        self.session = 0
        self._request = secrets.randbelow(2**30) + 1
        self.lock = threading.RLock()
        self.retry_count = 0
        self.info = {}

    @classmethod
    def open(cls, endpoint="tcp://127.0.0.1:8765", baudrate=115200, **options):
        client = cls(connect(endpoint, baudrate), **options)
        try:
            client.hello()
        except Exception:
            client.close()
            raise
        return client

    def close(self):
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def hello(self):
        if self.session:
            raise ValueError("HELLO already completed; create a new client for reconnect")
        self.info = self.call(Op.HELLO, protocol=1)
        return self.info

    def call(self, opcode, **fields):
        """Execute one typed TLV request. RemoteError exposes status and fields."""
        return self._rpc(opcode, encode_fields(fields))

    def _rpc(self, opcode, payload):
        with self.lock:
            if opcode != Op.HELLO and not self.session:
                raise ValueError("HELLO required")
            self._request += 1
            if self._request >= 2**32:
                raise ConnectionError("request counter exhausted; reconnect")
            request = self._request
            channel = 1 if opcode in (Op.FILE_DATA, Op.FILE_READ) else 3 if 0x0700 <= opcode <= 0x07FF else 0
            frame = Frame(Kind.REQUEST, channel, self.session, request, opcode, payload)
            encoded = frame.encode()
            for attempt in range(self.retries + 1):
                if attempt:
                    self.retry_count += 1
                self.transport.write(encoded)
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    data = self.transport.read(4096, deadline - time.monotonic())
                    for response in self.decoder.feed(data):
                        if (response.kind != Kind.RESPONSE or response.request != request or
                                response.opcode != opcode or response.channel != channel):
                            continue
                        if opcode != Op.HELLO and response.session != self.session:
                            continue
                        try:
                            status, fields = parse_response(response.payload)
                        except (ProtocolError, ValueError):
                            continue
                        if status not in (Status.OK, Status.ACCEPTED):
                            raise RemoteError(status, fields)
                        if opcode == Op.HELLO:
                            if not response.session or fields.get("protocol") != 1:
                                raise ProtocolError("invalid HELLO response")
                            self.session = response.session
                        return fields
            raise RpcTimeout("%s request %d timed out after %d attempts; query state before replay" %
                             (Op(opcode).name, request, self.retries + 1))

    def list_files(self, path="/"):
        cursor, version, result = 0, "", []
        while True:
            response = self.call(Op.FS_LIST, path=path, cursor=cursor, version=version, limit=8)
            result.extend(response["entries"])
            cursor, version = response["next_cursor"], response["version"]
            if not cursor:
                return result

    def stat(self, path):
        return self.call(Op.FS_STAT, path=path)

    def mkdir(self, path):
        return self.call(Op.FS_MKDIR, path=path)

    def delete(self, path, version):
        return self.call(Op.FS_DELETE, path=path, version=version)

    def rename(self, path, new_path, version):
        return self.call(Op.FS_RENAME, path=path, new_path=new_path, version=version)

    def write_block(self, transfer_id, offset, data):
        """Send <=512 raw bytes. Retries reuse the identical request and offset."""
        if not 1 <= len(data) <= 512:
            raise ValueError("block must contain 1..512 bytes")
        return self._rpc(Op.FILE_DATA, FILE_HEADER.pack(transfer_id, offset, len(data)) + data)

    def read_preview(self, remote_path, max_bytes=16384):
        """Read a bounded prefix, releasing the transfer before returning.

        Complete files are checked against SHA-256. Partial previews are explicitly
        marked unverified; ordinary frame CRC and device version checks still apply.
        No local file or download sidecar is created.
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024:
            raise ValueError("preview size must be 1..262144 bytes")
        with self.lock:
            info = self.stat(remote_path)
            if info["is_dir"]:
                raise ValueError("不能预览文件夹，请选择 HEX 文件。")
            transfer_id, finalized = None, False
            try:
                transfer = self.call(Op.FILE_BEGIN, direction="download", path=remote_path, version=info["version"])
                transfer_id = transfer["transfer_id"]
                if transfer["size"] != info["size"] or transfer["sha256"].hex() != info["version"]:
                    raise ProtocolError("preview transfer metadata mismatch")
                limit, data = min(info["size"], max_bytes), bytearray()
                while len(data) < limit:
                    offset, length = len(data), min(512, limit - len(data))
                    block = self.call(Op.FILE_READ, transfer_id=transfer_id, offset=offset, length=length)
                    payload = block["data"]
                    if (block["offset"] != offset or not 1 <= len(payload) <= length or
                            block["next_offset"] != offset + len(payload)):
                        raise ProtocolError("invalid preview block")
                    data.extend(payload)
                complete = len(data) == info["size"]
                if complete:
                    if hashlib.sha256(data).hexdigest() != info["version"]:
                        raise ProtocolError("HEX 预览文件完整性校验失败。")
                    self.call(Op.FILE_END, transfer_id=transfer_id)
                else:
                    self.call(Op.FILE_ABORT, transfer_id=transfer_id)
                finalized = True
                return dict(path=remote_path, size=info["size"], version=info["version"],
                            data=bytes(data), truncated=not complete, verified=complete)
            finally:
                if transfer_id is not None and not finalized:
                    with contextlib.suppress(Exception):
                        self.call(Op.FILE_ABORT, transfer_id=transfer_id)

    def upload(self, local_path, remote_path, overwrite=False, resume_file=None, progress=None):
        """Upload and commit; optional sidecar allows resume with a new Client."""
        local = Path(local_path)
        size, digest = local.stat().st_size, sha256_file(local)
        state_path = Path(resume_file) if resume_file else None
        if state_path and state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            expected = dict(direction="upload", path=remote_path, size=size,
                            sha256=digest.hex(), overwrite=overwrite,
                            boot_id=self.info["boot_id"], device_id=self.info["device_id"])
            if any(state.get(key) != value for key, value in expected.items()):
                raise ResumeError("upload metadata/device boot changed; abort old transfer or remove stale sidecar explicitly")
            transfer = self.call(Op.FILE_STATUS, transfer_id=state["transfer_id"])
        else:
            transfer = self.call(Op.FILE_BEGIN, direction="upload", path=remote_path,
                                 size=size, sha256=digest, overwrite=overwrite)
            state = dict(direction="upload", transfer_id=transfer["transfer_id"], path=remote_path,
                         size=size, sha256=digest.hex(), overwrite=overwrite,
                         boot_id=self.info["boot_id"], device_id=self.info["device_id"])
            if state_path:
                _save_json(state_path, state)
        transfer_id = transfer["transfer_id"]
        if transfer["state"] not in ("ACTIVE", "COMMITTED"):
            raise ResumeError("transfer is " + transfer["state"])
        if transfer["size"] != size or transfer["sha256"] != digest:
            raise ResumeError("device transfer metadata mismatch")
        offset = transfer["next_offset"]
        if not 0 <= offset <= size:
            raise ProtocolError("device returned invalid offset")
        with open(local, "rb") as source:
            source.seek(offset)
            if progress:
                progress(offset, size)
            while offset < size:
                block = source.read(min(512, size - offset))
                if not block:
                    raise ResumeError("local source changed during upload")
                response = self.write_block(transfer_id, offset, block)
                expected_offset = offset + len(block)
                if response["next_offset"] != expected_offset:
                    raise ProtocolError("unexpected upload ACK offset")
                offset = expected_offset
                if progress:
                    progress(offset, size)
        result = self.call(Op.FILE_END, transfer_id=transfer_id)
        if result["state"] != "COMMITTED":
            raise ProtocolError("file did not commit")
        if state_path:
            state_path.unlink(missing_ok=True)
        return dict(path=remote_path, size=size, version=digest.hex(), transfer_id=transfer_id)

    def download(self, remote_path, local_path, overwrite=False, progress=None):
        """Retrieve to .part, validate SHA-256, then replace destination.

        .dap-download.json and .part are intentionally retained on failure.
        """
        local = Path(local_path)
        if local.exists() and not overwrite:
            raise FileExistsError(str(local))
        part = local.with_name(local.name + ".part")
        sidecar = local.with_name(local.name + ".dap-download.json")
        info = self.stat(remote_path)
        if info["is_dir"]:
            raise ValueError("cannot download a directory")
        expected = dict(direction="download", path=remote_path, size=info["size"],
                        version=info["version"], boot_id=self.info["boot_id"],
                        device_id=self.info["device_id"])
        if sidecar.exists():
            state = json.loads(sidecar.read_text(encoding="utf-8"))
            if any(state.get(key) != value for key, value in expected.items()):
                raise ResumeError("download source/device boot changed; remove stale sidecar and .part explicitly")
            transfer = self.call(Op.FILE_STATUS, transfer_id=state["transfer_id"])
            if transfer["state"] not in ("ACTIVE", "COMMITTED"):
                raise ResumeError("download transfer is " + transfer["state"])
            offset = state["offset"]
            if not part.exists() or part.stat().st_size < offset or not 0 <= offset <= info["size"]:
                raise ResumeError("invalid partial file length")
            with open(part, "r+b") as output:
                output.truncate(offset)
            if sha256_file(part).hex() != state["prefix_hash"]:
                raise ResumeError("partial file hash mismatch")
        else:
            if part.exists():
                raise FileExistsError("unowned partial file exists: " + str(part))
            transfer = self.call(Op.FILE_BEGIN, direction="download", path=remote_path,
                                 version=info["version"])
            offset = 0
            state = dict(expected, transfer_id=transfer["transfer_id"], offset=0,
                         prefix_hash=hashlib.sha256(b"").hexdigest())
            part.touch(exist_ok=False)
            _save_json(sidecar, state)
        digest = hashlib.sha256()
        with open(part, "rb") as source:
            for block in iter(lambda: source.read(65536), b""):
                digest.update(block)
        with open(part, "r+b") as output:
            output.seek(offset)
            if progress:
                progress(offset, info["size"])
            while offset < info["size"]:
                response = self.call(Op.FILE_READ, transfer_id=state["transfer_id"], offset=offset, length=512)
                block = response["data"]
                if (response["offset"] != offset or not block or len(block) > min(512, info["size"] - offset)
                        or response["next_offset"] != offset + len(block)):
                    raise ProtocolError("invalid download block")
                if output.write(block) != len(block):
                    raise OSError("short local write")
                output.flush()
                os.fsync(output.fileno())
                digest.update(block)
                offset += len(block)
                state.update(offset=offset, prefix_hash=digest.hexdigest())
                _save_json(sidecar, state)
                if progress:
                    progress(offset, info["size"])
        if digest.hexdigest() != info["version"]:
            raise ResumeError("download SHA-256 mismatch; destination not replaced")
        # The final RPC may have completed before a lost response; retry/end is idempotent.
        self.call(Op.FILE_END, transfer_id=state["transfer_id"])
        if local.exists() and not overwrite:
            raise FileExistsError(str(local))
        os.replace(part, local)
        sidecar.unlink(missing_ok=True)
        return dict(path=str(local), size=offset, version=digest.hexdigest())

    def _flm_fields(self, flm_path=None, flm_version=None):
        if flm_path is None and flm_version is None:
            return {}
        if not flm_path:
            raise ValueError("请选择 FLM 烧录算法。")
        if Path(flm_path).suffix.lower() != ".flm":
            raise ValueError("烧录算法仅支持 .FLM 文件，不支持 FLX。")
        # Unknown optional TLVs may be ignored by older peers, so negotiate first.
        features = self.call(Op.GET_CAPS).get("features", "").split(";")[0].split(",")
        if "flm-select-v1" not in features:
            raise ValueError("设备未声明 FLM 选择能力，请更新设备协议实现。")
        flm_version = flm_version or self.stat(flm_path)["version"]
        return dict(flm_path=flm_path, flm_version=flm_version)

    def check_hex(self, remote_path, version=None, flm_path=None, flm_version=None):
        version = version or self.stat(remote_path)["version"]
        return self.call(Op.FLASH_CHECK, path=remote_path, version=version,
                         **self._flm_fields(flm_path, flm_version))

    def start_flash(self, remote_path, operation_token, version=None, run_after=False, target_id=None,
                    flm_path=None, flm_version=None):
        """Caller must retain token BEFORE sending; reuse it unchanged on reconnect."""
        if not operation_token:
            raise ValueError("operation_token is required")
        version = version or self.stat(remote_path)["version"]
        target_id = target_id or self.call(Op.TARGET_INFO)["target_id"]
        return self.call(Op.FLASH_START, path=remote_path, version=version,
                         operation_token=operation_token, run_after=run_after,
                         target_id=target_id, **self._flm_fields(flm_path, flm_version))

    def job_status(self, job_id=None, operation_token=None):
        if not job_id and not operation_token:
            raise ValueError("job_id or operation_token required")
        return self.call(Op.JOB_STATUS, **({"job_id": job_id} if job_id else {"operation_token": operation_token}))

    def wait_job(self, job_id, timeout=30.0, progress=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.job_status(job_id=job_id)
            if progress:
                progress(job)
            if job["state"] in ("DONE", "FAILED", "CANCELLED", "INTERRUPTED"):
                return job
            time.sleep(0.1)
        raise TimeoutError("job still running; query it later")

    def start_rtt(self, destinations=1, sd_path=None, rate=1024, sd_max_bytes=1024 * 1024):
        fields = dict(destinations=destinations, rate=rate, sd_max_bytes=sd_max_bytes)
        if sd_path:
            fields["sd_path"] = sd_path
        return self.call(Op.RTT_START, **fields)

    def read_rtt(self, stream_id, length=512):
        return self.call(Op.RTT_READ, stream_id=stream_id, length=length)

    def stop_rtt(self, stream_id):
        return self.call(Op.RTT_STOP, stream_id=stream_id)

    def benchmark(self, size=65536, file_path=None, sink="memory", seed=1,
                  progress=None, cancel_event=None):
        """Stream without data ACK/retry/CRC; verify the whole received object."""
        from .benchmark import run_benchmark
        return run_benchmark(self, size=size, file_path=file_path, sink=sink, seed=seed,
                             progress=progress, cancel_event=cancel_event)

    def scan_baudrates(self, baudrates=(115200, 230400, 460800, 921600), rounds=3,
                       size=65536, file_path=None, sink="memory", probe_timeout=0.5,
                       settle_s=0.1, progress=None, cancel_event=None):
        """Scan local port settings and restore the original rate in a finally block."""
        from .benchmark import scan_baudrates
        return scan_baudrates(self, baudrates, rounds=rounds, size=size, file_path=file_path,
                              sink=sink, probe_timeout=probe_timeout, settle_s=settle_s,
                              progress=progress, cancel_event=cancel_event)
