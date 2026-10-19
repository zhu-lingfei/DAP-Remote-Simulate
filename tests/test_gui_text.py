import unittest

from dapremote.benchmark import BenchmarkCancelled
from dapremote.client import RemoteError, RpcTimeout
from dapremote.gui_text import (RttTextDecoder, error_notice, job_notice, rate_text,
                               result_notice, scan_notice, size_text)
from dapremote.protocol import Status


class GuiTextTests(unittest.TestCase):
    def test_device_status_uses_readable_labels_not_mapping_representation(self):
        notice = result_notice(dict(sd_ready=True, battery_percent=80, battery_mv=4000,
                                    transfers=[], streams=[], external_power=False))
        self.assertIn("SD 卡已就绪", notice.text)
        self.assertIn("80%", notice.text)
        self.assertIn("4.00 V", notice.text)
        for fragment in ("sd_ready", "battery_mv", "{}", "True", "False"):
            self.assertNotIn(fragment, notice.text)

    def test_unknown_mapping_is_never_dumped_to_the_log(self):
        notice = result_notice(dict(unknown_internal_key=dict(secret_raw=b"abc")))
        self.assertEqual(notice.text, "操作已完成。")
        self.assertNotIn("unknown_internal_key", notice.text)

    def test_sizes_and_missing_baudrates_have_readable_units(self):
        self.assertEqual(size_text(0), "0 B")
        self.assertEqual(size_text(1024), "1.0 KiB")
        self.assertEqual(size_text(1048576), "1.00 MiB")
        self.assertIn("暂无", rate_text(None))
        self.assertEqual(rate_text(115200), "115,200 baud")

    def test_remote_errors_have_chinese_actionable_messages(self):
        notice = error_notice(RemoteError(Status.NO_SPACE, dict(message="insufficient space")))
        self.assertEqual(notice.level, "ERROR")
        self.assertIn("可用空间不足", notice.text)
        self.assertNotIn("Status.", notice.text)
        self.assertNotIn("{'", notice.text)
        self.assertIn("查询", error_notice(RpcTimeout()).text)
        self.assertIn("模拟器", error_notice(ConnectionRefusedError()).text)

    def test_cancel_is_a_warning_and_failed_job_is_an_error(self):
        self.assertEqual(error_notice(BenchmarkCancelled()).level, "WARN")
        failed = job_notice(dict(state="FAILED", phase="FAILED", completed_bytes=128,
                                 total_bytes=256, error="VERIFY_FAILED"))
        self.assertEqual(failed.level, "ERROR")
        self.assertIn("烧录后校验失败", failed.text)
        completed = job_notice(dict(state="DONE", phase="DONE", completed_bytes=256, total_bytes=256))
        self.assertEqual(completed.level, "SUCCESS")

    def test_failed_benchmark_does_not_advertise_unverified_throughput(self):
        notice = result_notice(dict(bench_id=1, passed=False, size=1024,
                                    received_bytes=900, bytes_per_s=1000000,
                                    error="length, SHA-256, sequence or framing mismatch"))
        self.assertEqual(notice.level, "ERROR")
        self.assertNotIn("KiB/s", notice.text)
        self.assertIn("未通过", notice.text)

    def test_scan_summary_handles_simulation_cancel_and_restore_failure(self):
        summary = dict(results=[dict(passed=True)], restored=True, simulated=True, cancelled=False)
        simulated = scan_notice(summary)
        self.assertIn("TCP 模拟", simulated.text)
        self.assertNotIn("None", simulated.text)
        summary.update(cancelled=True)
        self.assertEqual(scan_notice(summary).level, "WARN")
        summary.update(restored=False, restore_error="port closed")
        self.assertEqual(scan_notice(summary).level, "ERROR")

    def test_rtt_utf8_survives_splits_and_escapes_binary_without_repr(self):
        decoder = RttTextDecoder()
        raw = "目标正常\n".encode("utf-8") + b"\x00\xff"
        shown = "".join(decoder.feed(bytes([byte])) for byte in raw)
        self.assertEqual(shown, "目标正常\n\\x00\\xff")
        self.assertNotIn("b'", shown)
        self.assertNotIn("�", shown)

    def test_rtt_flush_exposes_incomplete_utf8_at_stop(self):
        decoder = RttTextDecoder()
        self.assertEqual(decoder.feed(b"\xe4"), "")
        self.assertEqual(decoder.feed(b"", final=True), "\\xe4")


if __name__ == "__main__":
    unittest.main()
