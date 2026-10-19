"""Automatic operation IDs for one-click flashing, never displayed by the GUI."""
import json
import os
import threading
import uuid
from pathlib import Path

from .client import RemoteError
from .protocol import Op, Status

TERMINAL = {"DONE", "FAILED", "CANCELLED", "INTERRUPTED", "REJECTED"}
STATES = TERMINAL | {"REQUESTING", "UNKNOWN", "QUEUED", "RUNNING"}
# These responses explicitly reject the request before creating a flash job.
REJECTED = {Status.INVALID_ARGUMENT, Status.UNSUPPORTED, Status.NOT_FOUND,
            Status.BUSY, Status.NO_SPACE, Status.SD_NOT_READY, Status.FILE_CHANGED,
            Status.INVALID_HEX, Status.TARGET_MISMATCH, Status.POWER_INSUFFICIENT}


class PendingFlashError(RuntimeError):
    """An earlier operation is unresolved; do not create another physical operation."""


class FlashCoordinator:
    def __init__(self, state_path=None):
        self.path = Path(state_path) if state_path else None
        self.records = {}
        self.lock = threading.RLock()
        self.load_error = self.last_save_error = ""
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if raw["version"] != 1 or not isinstance(raw["records"], list) or len(raw["records"]) > 128:
                    raise ValueError("unsupported recovery file")
                for record in raw["records"]:
                    if (not all(isinstance(record[k], str) and record[k] for k in
                                ("endpoint", "device_id", "operation_token", "path", "version", "target_id", "state"))
                            or record["state"] not in STATES
                            or len(record["operation_token"]) != 32
                            or any(c not in "0123456789abcdef" for c in record["operation_token"])
                            or len(record["version"]) != 64):
                        raise ValueError("invalid recovery record")
                    key = (record["endpoint"], record["device_id"])
                    if key in self.records:
                        raise ValueError("duplicate device recovery record")
                    self.records[key] = record
            except (OSError, ValueError, KeyError, TypeError):
                self.load_error = "本地烧录恢复记录无法读取。为避免重复烧录，已暂停新任务；请先备份并检查恢复记录。"

    def _key(self, client, endpoint):
        return endpoint.strip(), client.info["device_id"]

    def current(self, client, endpoint):
        if self.load_error:
            raise PendingFlashError(self.load_error)
        with self.lock:
            record = self.records.get(self._key(client, endpoint))
            return dict(record) if record else None

    def _save(self, required=False):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            with open(temp, "w", encoding="utf-8") as output:
                json.dump(dict(version=1, records=list(self.records.values())), output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.path)
            self.last_save_error = ""
        except OSError as exc:
            self.last_save_error = str(exc)
            if required:
                raise PendingFlashError("无法保存烧录恢复记录，尚未向设备发起烧录。请检查项目目录的写入权限和磁盘空间。") from exc

    def observe(self, client, endpoint, job, persist=True):
        with self.lock:
            record = self.records.get(self._key(client, endpoint))
            if record and record["operation_token"] == job.get("operation_token"):
                changed = record["state"] != job["state"] or record.get("job_id") != job["job_id"]
                record.update(state=job["state"], job_id=job["job_id"])
                if persist and changed:
                    self._save()

    def query(self, client, endpoint):
        record = self.current(client, endpoint)
        if not record or record["state"] == "REJECTED":
            return None
        try:
            job = client.job_status(operation_token=record["operation_token"])
        except RemoteError as exc:
            if exc.status == Status.RESULT_UNKNOWN:
                # A new device, missing journal, or reset is not proof of non-execution.
                raise PendingFlashError("暂时无法确认上次烧录结果，未重复发起烧录。请检查是否连接原设备，以及设备的任务记录是否保留。") from exc
            raise
        self.observe(client, endpoint, job)
        return job

    def start_local(self, client, endpoint, local_path, cache_dir, progress=None):
        """Recover unresolved jobs first; otherwise upload/check a PC snapshot and burn."""
        previous = self.current(client, endpoint)
        if previous and previous["state"] not in TERMINAL:
            return dict(job=self.query(client, endpoint), recovered=True)
        from .firmware import prepare_local_firmware
        firmware = prepare_local_firmware(client, local_path, cache_dir, progress)
        return self.start(client, endpoint, firmware["path"], expected_version=firmware["version"])

    def start(self, client, endpoint, path, expected_version=None, flm_path=None, flm_version=None):
        """New intent => fresh ID; unresolved intent => query only, never blind replay."""
        path = path.strip()
        if not path:
            raise ValueError("请选择 SD 卡中的 HEX 固件路径。")
        previous = self.current(client, endpoint)
        if previous and previous["state"] not in TERMINAL:
            return dict(job=self.query(client, endpoint), recovered=True)
        # Read-only preflight; a failure here has never submitted a flash request.
        version = client.stat(path)["version"]
        if expected_version is not None and version != expected_version:
            raise ValueError("SD 固件在校验后发生变化，已停止烧录，请重新选择文件。")
        if flm_path:
            flm_version = flm_version or client.stat(flm_path)["version"]
            client.check_hex(path, version=version, flm_path=flm_path, flm_version=flm_version)
        target_id = client.call(Op.TARGET_INFO)["target_id"]
        key = self._key(client, endpoint)
        record = dict(endpoint=key[0], device_id=key[1], operation_token=uuid.uuid4().hex,
                      path=path, version=version, target_id=target_id, state="REQUESTING", job_id="")
        if flm_path:
            record.update(flm_path=flm_path, flm_version=flm_version)
        with self.lock:
            if key not in self.records and len(self.records) >= 128:
                raise PendingFlashError("本地设备恢复记录已达上限，请先备份整理记录。")
            self.records[key] = record
            try:
                self._save(required=True)  # Must be durable BEFORE FLASH_START.
            except Exception:
                if previous:
                    self.records[key] = previous
                else:
                    self.records.pop(key, None)
                raise
        try:
            extra = dict(flm_path=flm_path, flm_version=flm_version) if flm_path else {}
            job = client.start_flash(path, record["operation_token"], version=version,
                                     target_id=target_id, run_after=False, **extra)
        except Exception as exc:
            with self.lock:
                record["state"] = "REJECTED" if isinstance(exc, RemoteError) and exc.status in REJECTED else "UNKNOWN"
                self._save()
            raise
        self.observe(client, endpoint, job)
        return dict(job=job, recovered=False)
