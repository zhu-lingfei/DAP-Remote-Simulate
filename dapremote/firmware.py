"""PC firmware snapshots and bounded local previews, without new wire commands."""
import hashlib
import os
import tempfile
from pathlib import Path

from .client import RemoteError, sha256_file
from .protocol import Status

ARTIFACT_DIRS = {"hex": "/HEX", "flm": "/FLM"}
ARTIFACT_EXTENSIONS = {"hex": (".hex",), "flm": (".flm",)}


def artifact_kind(path):
    suffix = Path(path).suffix.lower()
    return next((kind for kind, extensions in ARTIFACT_EXTENSIONS.items() if suffix in extensions), None)


class UploadConflict(ValueError):
    """The GUI must obtain explicit consent before replacing this file version."""
    def __init__(self, path, version):
        self.path, self.version = path, version
        super().__init__("目标目录已有同名文件：" + Path(path).name)


def upload_regular_file(client, local_path, remote_path, progress=None, overwrite_version=None):
    """Upload to a predetermined directory; raise UploadConflict only if needed."""
    local = Path(local_path)
    try:
        existing = client.stat(remote_path)
    except RemoteError as exc:
        if exc.status != Status.NOT_FOUND:
            raise
        existing = None
    if existing:
        if existing["is_dir"]:
            raise ValueError("目标名称已被文件夹占用，无法上传文件。")
        if existing["version"] == sha256_file(local).hex():
            return existing
        if existing["version"] != overwrite_version:
            raise UploadConflict(remote_path, existing["version"])
    return client.upload(local, remote_path, overwrite=existing is not None,
                         resume_file=str(local) + ".dap-upload.json", progress=progress)


def artifact_path(path, kind):
    """Accept only a matching extension inside the designated SD directory."""
    from pathlib import PurePosixPath
    root = ARTIFACT_DIRS[kind]
    candidate = PurePosixPath(path)
    if (not path.startswith(root + "/") or any(part in (".", "..") for part in path.split("/"))
            or "\\" in path or candidate.suffix.lower() not in ARTIFACT_EXTENSIONS[kind]):
        raise ValueError("请选择 %s 目录中的 %s 文件。" % (root, "/".join(ARTIFACT_EXTENSIONS[kind])))
    return str(candidate)


def ensure_artifact_directory(client, kind):
    root = ARTIFACT_DIRS[kind]
    try:
        info = client.stat(root)
    except RemoteError as exc:
        if exc.status != Status.NOT_FOUND:
            raise
        try:
            client.mkdir(root)
        except RemoteError as created:
            if created.status != Status.ALREADY_EXISTS:
                raise
        info = client.stat(root)
    if not info["is_dir"]:
        raise ValueError(root + " 已被同名文件占用，请整理 SD 卡。")
    return root


def import_artifact(client, local_path, kind, cache_dir, progress=None, collision="rename", overwrite_version=None):
    """Snapshot a local HEX/FLM and reliably import it into its fixed SD directory.

    Returns path, name, version, size. Does not check/execute a flash algorithm.
    Same name+content reuses the file; different content uses a hash suffix.
    """
    if collision not in ("rename", "ask"):
        raise ValueError("Unknown collision policy")
    local = Path(local_path)
    suffix = local.suffix.lower()
    if suffix not in ARTIFACT_EXTENSIONS[kind]:
        raise ValueError("请选择 %s 文件。" % "/".join(ARTIFACT_EXTENSIONS[kind]))
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    temporary = None
    digest = hashlib.sha256()
    try:
        with local.open("rb") as source, tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            for block in iter(lambda: source.read(65536), b""):
                output.write(block)
                digest.update(block)
        if temporary.stat().st_size == 0:
            raise ValueError("不能选择空文件。")
        version = digest.hexdigest()
        snapshot = cache / (version + suffix)
        os.replace(temporary, snapshot)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    root = ensure_artifact_directory(client, kind)
    name = local.stem[:60] + suffix
    def stat_or_none(path):
        try:
            return client.stat(path)
        except RemoteError as exc:
            if exc.status != Status.NOT_FOUND:
                raise
            return None
    remote = root + "/" + name
    existing = stat_or_none(remote)
    overwrite = False
    if existing and (existing["is_dir"] or existing["version"] != version):
        if collision == "ask":
            if existing["is_dir"]:
                raise ValueError("目标名称已被文件夹占用，无法上传文件。")
            if existing["version"] != overwrite_version:
                raise UploadConflict(remote, existing["version"])
            overwrite = True
        else:
            name = local.stem[:40] + "-" + version + suffix
            remote = root + "/" + name
            existing = stat_or_none(remote)
    if existing and not overwrite and (existing["is_dir"] or existing["version"] != version):
        raise ValueError("SD 文件名冲突，未覆盖已有文件。")
    if existing is None or overwrite:
        owner = hashlib.sha256((client.info["device_id"] + ":" + str(client.info["boot_id"]) + ":" + remote
                                + (":overwrite" if overwrite else "")).encode()).hexdigest()
        resume = cache / (version + "-" + owner + ".upload.json")
        result = client.upload(snapshot, remote, overwrite=overwrite, resume_file=resume, progress=progress)
        if result["version"] != version:
            raise ValueError("上传校验不一致，未选用文件。")
    info = client.stat(remote)
    if info["version"] != version or info["is_dir"]:
        raise ValueError("SD 文件已变化，请重新选择。")
    return dict(path=remote, name=name, version=version, size=info["size"])


def local_preview(path, max_bytes=16384):
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 256 * 1024:
        raise ValueError("预览长度应为 1..262144 字节。")
    with open(path, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        data = source.read(max_bytes)
    return dict(path=str(path), size=size, data=data, truncated=size > len(data), verified=False)


def prepare_local_firmware(client, local_path, cache_dir, progress=None):
    """Compatibility helper: import a HEX into /HEX and check it without burning."""
    result = import_artifact(client, local_path, "hex", cache_dir, progress)
    result["check"] = client.check_hex(result["path"], version=result["version"])
    return result
