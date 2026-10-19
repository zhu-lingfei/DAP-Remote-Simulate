"""Human-readable presentation only; never changes protocol data or CLI output."""
import codecs
from dataclasses import dataclass

from .benchmark import BenchmarkCancelled
from .client import RemoteError, ResumeError, RpcTimeout
from .protocol import Status


@dataclass(frozen=True)
class Notice:
    text: str
    level: str = "INFO"


PHASES = {
    "QUEUED": "等待执行", "CONNECTING": "连接目标芯片", "CHECKING": "检查固件",
    "ERASING": "擦除目标扇区", "PROGRAMMING": "写入固件", "VERIFYING": "校验烧录内容",
    "RESETTING": "复位目标芯片", "DONE": "烧录完成", "FAILED": "烧录失败",
    "CANCELLED": "任务已取消", "INTERRUPTED": "任务已中断", "RUNNING": "正在执行",
}
ERRORS = {
    Status.INVALID_ARGUMENT: "参数不正确，请检查填写的路径和数值。",
    Status.UNSUPPORTED: "设备暂不支持此功能，请检查设备固件版本。",
    Status.NOT_FOUND: "文件或记录不存在，可能已被删除或因设备重启失效。",
    Status.ALREADY_EXISTS: "目标文件或目录已存在，请换一个名称或明确允许覆盖。",
    Status.BUSY: "设备资源正在使用中，请先停止相关任务再试。",
    Status.NO_SPACE: "SD 卡可用空间不足，请清理文件后再试。",
    Status.SD_NOT_READY: "SD 卡未就绪，请检查卡是否插入并成功挂载。",
    Status.IO_ERROR: "SD 卡读写失败，请检查存储状态。",
    Status.FILE_CHANGED: "文件或目录已变化，请刷新后重新操作。",
    Status.OFFSET_MISMATCH: "文件进度不一致，请重新查询传输状态后续传。",
    Status.HASH_MISMATCH: "文件完整性校验失败，未发布为可用固件。",
    Status.INVALID_HEX: "HEX 文件格式、校验或地址范围不符合要求。",
    Status.TARGET_MISMATCH: "目标芯片与当前烧录配置不匹配。",
    Status.TARGET_ERROR: "访问目标芯片失败，请检查连接和供电。",
    Status.VERIFY_FAILED: "烧录后校验失败，请检查目标芯片与供电。",
    Status.CANCELLED: "操作已取消。",
    Status.RESULT_UNKNOWN: "暂时无法确认操作结果，请先查询任务状态，避免重复执行。",
    Status.REQUEST_CONFLICT: "请求与之前的操作冲突，请核对操作令牌和参数。",
    Status.SESSION_INVALID: "连接会话已失效，请断开后重新连接。",
    Status.POWER_INSUFFICIENT: "剩余电量不足，暂不能开始烧录，请充电或接入外部电源。",
}


def size_text(value):
    value = max(0, int(value))
    if value < 1024:
        return "%d B" % value
    if value < 1024 * 1024:
        return "%.1f KiB" % (value / 1024)
    return "%.2f MiB" % (value / 1024 / 1024)


def rate_text(value):
    return "暂无通过档位" if value is None else "{:,} baud".format(value)


def detail_text(value):
    text = str(value or "").strip()
    return {
        "VERIFY_FAILED": "烧录后校验失败",
        "device restarted": "设备重启，任务已中断",
        "cancelled at simulator checkpoint": "任务已在安全检查点取消",
        "length, SHA-256, sequence or framing mismatch": "接收长度、完整性或分帧检查未通过",
        "whole-file verification failed": "整文件校验未通过",
        "RTT SD limit reached": "RTT 日志已达到存储上限",
        "test cancelled/expired": "测试已停止或等待超时",
        "benchmark cancelled": "测试已停止",
        "scan cancelled": "扫描已停止",
    }.get(text, text)


def error_notice(exc):
    if isinstance(exc, BenchmarkCancelled):
        return Notice("测试已停止，正在执行的操作已结束。", "WARN")
    if isinstance(exc, RemoteError):
        text = ERRORS.get(exc.status, "设备未能完成操作。")
        detail = detail_text(exc.fields.get("message", exc.fields.get("error", "")))
        return Notice(text + ("\n设备提示：" + detail if detail else ""), "ERROR")
    if isinstance(exc, ResumeError):
        return Notice("无法继续上次传输：文件、设备或恢复记录已经变化。请核对后再重新开始。\n详情：" + str(exc), "ERROR")
    if isinstance(exc, ConnectionRefusedError):
        return Notice("无法连接设备。请确认模拟器已经启动，或串口设备和连接地址正确。", "ERROR")
    if isinstance(exc, RpcTimeout):
        return Notice("设备响应超时。操作可能已经执行，请先查询任务或传输状态，再决定是否重试。", "ERROR")
    if isinstance(exc, ConnectionError):
        return Notice("连接已断开。请重新连接后查询任务状态；设备端烧录或 SD 日志可能仍在继续。", "ERROR")
    if isinstance(exc, FileExistsError):
        return Notice("文件已经存在，请确认覆盖选项，或更换保存路径。\n位置：" + str(exc), "ERROR")
    if isinstance(exc, FileNotFoundError):
        return Notice("找不到所需文件，请检查路径是否正确。\n详情：" + str(exc), "ERROR")
    return Notice(detail_text(exc) or "操作未能完成，请检查设备与连接后重试。", "ERROR")


def job_notice(job):
    state = job.get("state", "")
    phase = PHASES.get(job.get("phase", state), "正在执行")
    completed, total = job.get("completed_bytes", 0), job.get("total_bytes", 0)
    text = phase
    if total:
        text += " · %s / %s" % (size_text(completed), size_text(total))
    if job.get("error"):
        text += "\n说明：" + detail_text(job["error"])
    level = "ERROR" if state in ("FAILED", "INTERRUPTED") else "WARN" if state == "CANCELLED" else "SUCCESS" if state == "DONE" else "INFO"
    return Notice(text, level)


def scan_notice(summary):
    rows = summary.get("results", [])
    passed = sum(bool(row.get("passed")) for row in rows)
    title = "模拟扫描完成" if summary.get("simulated") else "波特率扫描完成"
    if summary.get("cancelled"):
        title = "扫描已停止"
    lines = ["%s · %d / %d 个档位通过" % (title, passed, len(rows))]
    if summary.get("simulated"):
        lines.append("本次使用 TCP 模拟连接，不代表真实串口波特率。")
    else:
        lines.append("最高通过：%s；吞吐推荐：%s。" %
                     (rate_text(summary.get("highest_passed_baudrate")), rate_text(summary.get("recommended_baudrate"))))
    restored = summary.get("restored", True)
    lines.append("原连接设置已恢复。" if restored else "恢复连接失败：" + detail_text(summary.get("restore_error")))
    level = "ERROR" if not restored or (not passed and not summary.get("cancelled")) else "WARN" if summary.get("cancelled") else "SUCCESS"
    return Notice("\n".join(lines), level)


def result_notice(value):
    """Summarize known results; deliberately never stringify an entire mapping."""
    if isinstance(value, Notice):
        return value
    if isinstance(value, str):
        return Notice(value)
    if not isinstance(value, dict):
        return Notice("操作已完成。")
    if "results" in value and "restored" in value:
        return scan_notice(value)
    if "bench_id" in value and "passed" in value:
        text = "测速校验通过" if value["passed"] else "测速校验未通过"
        text += " · 接收 %s / %s" % (size_text(value.get("received_bytes", 0)), size_text(value.get("size", 0)))
        if value.get("passed"):
            text += " · %.2f KiB/s" % (value.get("bytes_per_s", 0) / 1024)
        if value.get("error"):
            text += "\n说明：" + detail_text(value["error"])
        return Notice(text, "SUCCESS" if value["passed"] else "ERROR")
    if "job_id" in value:
        return job_notice(value)
    if "sd_ready" in value:
        power = "外部供电" if value.get("external_power") else "电池供电"
        charging = "，正在充电" if value.get("charging") else ""
        return Notice("SD 卡%s · 电量 %d%%（%.2f V，%s%s）\n活动文件传输 %d 个 · 活动 RTT 流 %d 个" %
                      ("已就绪" if value["sd_ready"] else "未就绪", value.get("battery_percent", 0),
                       value.get("battery_mv", 0) / 1000, power, charging,
                       len(value.get("transfers", [])), len(value.get("streams", []))))
    if "min_address" in value:
        return Notice("HEX 检查通过 · 有效数据 %s · 地址 0x%08X ～ 0x%08X" %
                      (size_text(value.get("total_bytes", 0)), value["min_address"], value.get("max_address", 0)), "SUCCESS")
    if "path" in value and "size" in value:
        return Notice("文件已处理：%s · %s · 完整性校验通过" % (value["path"], size_text(value["size"])), "SUCCESS")
    if "stream_id" in value:
        names = {1: "电脑", 2: "SD 卡", 3: "电脑 + SD 卡"}
        text = "RTT 已停止" if value.get("state") == "STOPPED" else "RTT 已启动"
        text += " · 输出到" + names.get(value.get("destinations"), "所选位置")
        if value.get("sd_path"):
            text += " · " + value["sd_path"]
        return Notice(text, "SUCCESS")
    if "written_bytes" in value:
        return Notice("已发送 RTT 输入 · %s" % size_text(value["written_bytes"]), "SUCCESS")
    if value.get("error"):
        return Notice(detail_text(value["error"]), "ERROR")
    if value.get("message"):
        return Notice(detail_text(value["message"]))
    return Notice("操作已完成。", "SUCCESS")


class RttTextDecoder:
    """Keep UTF-8 codepoints across batches; escape binary controls for display."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="backslashreplace")

    def feed(self, data, final=False):
        text = self.decoder.decode(data, final=final)
        return "".join("\\x%02x" % ord(char) if (ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127
                       else char for char in text)
