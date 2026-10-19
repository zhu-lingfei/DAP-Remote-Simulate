"""Hidden-window integration check. Run manually in a desktop session, not in CI.

Builds the real widgets, connects to a temporary simulator and invokes benchmark
and scan workflows. No dialogs, no existing devices, no visible application window.
"""
import sys
import tempfile
import time
import tkinter as tk
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dapremote.gui import App
from dapremote.gui_flash import FlashCoordinator
from dapremote.firmware import ensure_artifact_directory
from tests.flm_fixture import flm_envelope
from dapremote.client import RemoteError
from dapremote.protocol import Status
from dapremote.simulator import FaultPlan, SimulatorServer
from dapremote.protocol import Op
from unittest.mock import patch


def main():
    with tempfile.TemporaryDirectory() as directory, SimulatorServer(directory, port=0) as server:
        root = tk.Tk()
        root.withdraw()
        state_path = Path(directory) / "gui-state" / "flash.json"
        app = App(root, "tcp://127.0.0.1:%d" % server.server_address[1], 115200, state_path=state_path)
        app.error_dialogs.set(False)
        def wait_until(predicate, timeout=8):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError("GUI operation did not finish: " + app.status.get())
        try:
            app.connect()
            wait_until(lambda: app.client is not None and not app.busy)
            app.bench_size.set("8")
            app.start_benchmark(False)
            wait_until(lambda: not app.busy)
            assert "一致" in app.bench_summary.get(), app.bench_summary.get()
            app.bench_rates.set("115200,230400")
            app.bench_rounds.set("1")
            app.start_benchmark(True)
            wait_until(lambda: not app.busy)
            assert "TCP 模拟" in app.bench_summary.get(), app.bench_summary.get()
            assert len(app.bench_table.get_children()) == 2
            assert str(app.bench_stop.cget("state")) == "disabled"
            text = app.log.get("1.0", "end-1c")
            assert "TCP 模拟" in text and "SUCCESS" in text, text
            assert '"received_bytes"' not in text and "{'" not in text, text
            assert str(app.log.cget("state")) == "disabled"

            # User action errors: readable message, red tag, optional one-time dialog.
            app.report_error(RemoteError(Status.NO_SPACE, dict(message="insufficient space")), "上传失败")
            assert app.log.tag_ranges("ERROR"), "missing ERROR highlighting"
            assert app.log.tag_cget("ERROR", "foreground") == "#B91C1C"
            assert "可用空间不足" in app.log.get("1.0", "end-1c")
            with patch("dapremote.gui.messagebox.showerror") as dialog:
                app.error_dialogs.set(True)
                app.report_error(ValueError("请检查文件路径"), "操作失败")
                assert dialog.call_count == 1
                app.error_dialogs.set(False)

            # Background errors are logged once, suspend polling, and never open dialogs.
            def failing_poll():
                raise RemoteError(Status.IO_ERROR, dict(message="injected failure"))
            with patch("dapremote.gui.messagebox.showerror") as dialog:
                app.submit(failing_poll, label="刷新设备数据", background=True)
                wait_until(lambda: not app.poll_pending)
                assert dialog.call_count == 0
                assert app.poll_suspended
            app.poll_suspended = False

            # Repeated polling of the same job phase must not flood either log.
            job = dict(job_id="display-only", state="RUNNING", phase="PROGRAMMING",
                       completed_bytes=10, total_bytes=100, error="")
            app.job_updated(job)
            count = app.log_count
            app.job_updated(dict(job, completed_bytes=20))
            assert app.log_count == count
            assert float(app.job_bar["value"]) == 20
            app.job_id = None  # This display-only record is not a real device task.

            # Actual RTT output is inserted as text, not repr(bytes) or JSON.
            app.destination.set("电脑")
            app.start_rtt()
            wait_until(lambda: app.stream_id is not None and not app.busy)
            wait_until(lambda: "SIM RTT" in app.rtt_text.get("1.0", "end-1c"))
            displayed = app.rtt_text.get("1.0", "end-1c")
            assert "b'" not in displayed and "\\x00" in displayed, displayed
            app.stop_rtt()
            wait_until(lambda: app.stream_id is None and not app.busy)

            # Each explicit click after completion creates a new flash job without IDs in UI.
            assert not hasattr(app, "token"), "internal ID must not have an entry widget"
            ensure_artifact_directory(app.client, "hex")
            ensure_artifact_directory(app.client, "flm")
            app.client.upload(Path(__file__).resolve().parents[1] / "examples" / "blink.hex", "/HEX/blink.hex")
            algorithm = Path(directory) / "local-pc" / "chip.flm"
            algorithm.parent.mkdir()
            algorithm.write_bytes(flm_envelope())
            app.client.upload(algorithm, "/FLM/chip.flm")
            app.refresh_flm_files()
            wait_until(lambda: not app.busy and "chip.flm" in app.flm_files)
            app.flm_choice.set("chip.flm")
            app.select_flm()
            app._artifact_selected("hex", app.client.stat("/HEX/blink.hex"))
            app.notebook.select(app.jobs_page)
            app.load_hex_preview()
            wait_until(lambda: not app.busy and app._preview_loaded_path == "/HEX/blink.hex")
            assert len(app.flash_panes.panes()) == 2
            assert not hasattr(app, "flash_path"), "manual firmware path entry must be removed"
            assert not hasattr(app, "source_selector"), "source switching belongs in the explorer"
            displayed_hex = app.hex_text.get("1.0", "end-1c")
            assert ":020000040800F2" in displayed_hex and ":00000001FF" in displayed_hex
            assert app.hex_text.tag_ranges("eof")
            assert str(app.hex_text.cget("state")) == "disabled"

            # A slow old-path response must not replace the preview after editing the path.
            original_preview = app.client.read_preview
            release = threading.Event()
            def delayed_preview(*args, **kwargs):
                assert release.wait(3)
                return original_preview(*args, **kwargs)
            with patch.object(app.client, "read_preview", side_effect=delayed_preview):
                app.load_hex_preview()
                app.flash_path_var.set("/HEX/another.hex")
                release.set()
                wait_until(lambda: not app.busy)
            assert ":020000040800F2" not in app.hex_text.get("1.0", "end-1c")
            app.flash_path_var.set("/HEX/blink.hex")
            app.load_hex_preview()
            wait_until(lambda: not app.busy and app._preview_loaded_path == "/HEX/blink.hex")
            def flash_finished():
                record = app.flash_operations.current(app.client, app.connected_endpoint) if app.client else None
                return not app.busy and record is not None and record["state"] == "DONE" and app.job_id is None
            tokens = []
            for expected_jobs in (1, 2):
                app.flash()
                wait_until(flash_finished)
                assert len(server.device.jobs) == expected_jobs
                tokens.append(app.flash_operations.current(app.client, app.connected_endpoint)["operation_token"])
                assert str(app.flash_button.cget("state")) == "normal"
            assert tokens[0] != tokens[1]
            app.refresh_flash()
            wait_until(lambda: not app.busy)
            assert len(server.device.jobs) == 2

            # Execute-then-disconnect: reload hidden state and reconnect, query not reflash.
            server.faults = FaultPlan([("disconnect", Op.FLASH_START, 1)])
            app.flash()
            wait_until(lambda: not app.busy)
            pending = app.flash_operations.current(app.client, app.connected_endpoint)
            assert pending["state"] == "UNKNOWN"
            tokens.append(pending["operation_token"])
            app.disconnect()
            app.flash_operations = FlashCoordinator(state_path)
            app.connect()
            wait_until(flash_finished)
            assert len(server.device.jobs) == 3
            assert app.flash_operations.current(app.client, app.connected_endpoint)["operation_token"] == tokens[-1]
            # Explorer uses a single sidebar for SD and PC, with the type fixed per dialog.
            original_toplevel = tk.Toplevel
            def hidden_toplevel(*args, **kwargs):
                dialog = original_toplevel(*args, **kwargs)
                dialog.attributes("-alpha", 0.01)
                return dialog
            pc = Path(directory) / "local-pc"
            local_hex = pc / "电脑固件.hex"
            local_hex.write_bytes((Path(__file__).resolve().parents[1] / "examples" / "blink.hex").read_bytes())
            (pc / "ignore.txt").write_text("not firmware")
            (pc / "nested").mkdir()
            root.attributes("-alpha", 0.01)
            root.deiconify()
            root.update()
            with patch("dapremote.gui.tk.Toplevel", side_effect=hidden_toplevel):
                app.choose_firmware()
            picker = app.artifact_picker
            wait_until(lambda: not app.busy and not picker.loading)
            from dapremote.ui_dialogs import workarea, centered_rect
            initial_bounds = workarea(root)
            initial_geometry = centered_rect(initial_bounds, min(940, initial_bounds[2] - initial_bounds[0] - 48),
                                               min(620, initial_bounds[3] - initial_bounds[1] - 80))
            root.update()
            assert abs(picker.window.winfo_x() - initial_geometry[2]) <= 32
            assert abs(picker.window.winfo_y() - initial_geometry[3]) <= 40
            # Exercise actual window geometry invisibly; the footer must stay inside at minimum size.
            wait_until(lambda: picker.window.winfo_ismapped(), timeout=3)
            scaling = float(root.tk.call("tk", "scaling"))
            for scale in (scaling, 2.0):
                root.tk.call("tk", "scaling", scale)
                for size in ("900x580", "720x420"):
                    picker.window.geometry(size)
                    picker.filename.set("长文件名" * 100 + ".hex")
                    picker.location.set("此电脑 › " + "长目录/" * 100)
                    root.update()
                    button = picker.choose_button
                    bottom = button.winfo_rooty() - picker.window.winfo_rooty() + button.winfo_height()
                    right = button.winfo_rootx() - picker.window.winfo_rootx() + button.winfo_width()
                    wait_until(lambda: button.winfo_ismapped() and button.winfo_height() > 10, timeout=3)
                    assert 0 < bottom <= picker.window.winfo_height() - 5, (size, scale, bottom, picker.window.winfo_height())
                    assert 0 < right <= picker.window.winfo_width() - 5
            root.tk.call("tk", "scaling", scaling)
            assert picker.mode == "sd" and picker.directory == "/HEX"
            assert picker.sidebar.exists("sd") and picker.sidebar.exists("pc-0")
            assert {entry["name"] for entry in picker.entries.values()} == {"blink.hex"}
            picker.up()
            assert picker.directory == "/HEX", "SD navigation escaped fixed library"
            picker.navigate("pc", pc)
            wait_until(lambda: not app.busy and not picker.loading)
            names = {entry["name"] for entry in picker.entries.values()}
            assert "电脑固件.hex" in names and "chip.flm" not in names and "ignore.txt" not in names
            key = next(key for key, entry in picker.entries.items() if entry["name"] == "电脑固件.hex")
            picker.table.selection_set(key)
            picker.choose()
            wait_until(lambda: not app.busy and app._preview_loaded_path == "/HEX/电脑固件.hex")
            assert not picker.exists()
            assert app.hex_filename.get() == "电脑固件.hex"
            assert app.selected_hex["path"] == "/HEX/电脑固件.hex"
            assert len(server.device.jobs) == 3, "selection must upload without starting a flash job"
            # FLM is selected only via a readonly dropdown from the /FLM root.
            app.client.upload(local_hex, "/FLM/ignore.hex")
            app.client.mkdir("/FLM/nested")
            app.refresh_flm_files()
            wait_until(lambda: not app.busy)
            assert tuple(app.flm_combo.cget("values")) == ("chip.flm",)
            assert str(app.flm_combo.cget("state")) == "readonly"
            app.flm_choice.set("chip.flm")
            app.flm_combo.event_generate("<<ComboboxSelected>>")
            root.update()
            assert app.flm_filename.get() == "chip.flm"
            assert app.selected_flm["path"] == "/FLM/chip.flm"
            # A refreshed missing file clears selection; no silent switch to a different algorithm.
            info = app.selected_flm
            app.client.delete(info["path"], info["version"])
            app.refresh_flm_files()
            wait_until(lambda: not app.busy)
            assert app.selected_flm is None and not app.flm_choice.get()
            assert str(app.flm_combo.cget("state")) == "disabled"
            assert str(app.flash_button.cget("state")) == "disabled"
            app.client.upload(algorithm, "/FLM/chip.flm")
            app.refresh_flm_files()
            wait_until(lambda: not app.busy)
            app.flm_choice.set("chip.flm")
            app.select_flm()
            app.flash()
            wait_until(flash_finished)
            assert len(server.device.jobs) == 4
            record = app.flash_operations.current(app.client, app.connected_endpoint)
            assert record["path"] == "/HEX/电脑固件.hex" and record["flm_path"] == "/FLM/chip.flm"
            # Invalid import keeps the previous selection intact and logs a readable error.
            with patch("dapremote.gui.tk.Toplevel", side_effect=hidden_toplevel):
                app.choose_firmware()
            picker = app.artifact_picker
            wait_until(lambda: not app.busy and not picker.loading)
            picker.navigate("pc", pc)
            wait_until(lambda: not app.busy and not picker.loading)
            key = next(key for key, entry in picker.entries.items() if entry["name"] == "电脑固件.hex")
            picker.table.selection_set(key)
            old = dict(app.selected_hex)
            with patch("dapremote.file_picker.import_artifact", side_effect=OSError("测试上传失败")):
                picker.choose()
                wait_until(lambda: not app.busy and not picker.loading)
            assert app.selected_hex == old and picker.exists()
            assert "测试上传失败" in app.log.get("1.0", "end-1c")
            picker.window.destroy()
            # Upload destination is automatic; only differing existing content prompts.
            upload_source = pc / "notes.txt"
            upload_source.write_bytes(b"first version")
            app.directory.set("/")
            with patch("dapremote.gui.filedialog.askopenfilename", return_value=str(upload_source)), \
                 patch("dapremote.gui.simpledialog.askstring") as path_dialog, \
                 patch("dapremote.gui.messagebox.askyesno", return_value=False) as overwrite_dialog:
                app.upload()
                wait_until(lambda: not app.busy)
                assert path_dialog.call_count == 0 and overwrite_dialog.call_count == 0
                upload_source.write_bytes(b"second version")
                app.upload()
                wait_until(lambda: not app.busy)
                assert overwrite_dialog.call_count == 1
                assert app.client.read_preview("/notes.txt")["data"] == b"first version"
                overwrite_dialog.return_value = True
                app.upload()
                wait_until(lambda: not app.busy)
                assert overwrite_dialog.call_count == 2 and path_dialog.call_count == 0
                assert app.client.read_preview("/notes.txt")["data"] == b"second version"
                app.upload()
                wait_until(lambda: not app.busy)
                assert overwrite_dialog.call_count == 2, "identical content should reuse the file"
            # An explicitly opened /HEX directory uses ordinary current-directory uploads too.
            app.directory.set("/HEX")
            local_hex.write_bytes(local_hex.read_bytes() + b"\n")
            old_data = app.client.read_preview("/HEX/电脑固件.hex")["data"]
            with patch("dapremote.gui.filedialog.askopenfilename", return_value=str(local_hex)), \
                 patch("dapremote.gui.simpledialog.askstring") as path_dialog, \
                 patch("dapremote.gui.messagebox.askyesno", return_value=False) as overwrite_dialog:
                app.upload()
                wait_until(lambda: not app.busy)
                assert overwrite_dialog.call_count == 1 and path_dialog.call_count == 0
                assert app.client.read_preview("/HEX/电脑固件.hex")["data"] == old_data
                overwrite_dialog.return_value = True
                app.upload()
                wait_until(lambda: not app.busy)
                assert overwrite_dialog.call_count == 2
                assert app.client.read_preview("/HEX/电脑固件.hex")["data"] == local_hex.read_bytes()
            # Algorithms: only FLM is selectable, even if FLX files are stored on SD.
            app.client.upload(algorithm, "/FLM/AM29x800DB.FLX")
            app.refresh_flm_files()
            wait_until(lambda: not app.busy)
            names = tuple(app.flm_combo.cget("values"))
            assert "AM29x800DB.FLX" not in names and "chip.flm" in names
            # File-manager uploads follow a folder entered by double-click, regardless of suffix.
            app.client.mkdir("/my-files")
            app.directory.set("/")
            app.refresh()
            wait_until(lambda: not app.busy)
            app.notebook.select(app.files.master.master)
            app.files.see("/my-files")
            root.update()
            bounds = app.files.bbox("/my-files")
            assert bounds
            from types import SimpleNamespace
            app.open_directory(SimpleNamespace(y=bounds[1] + bounds[3] // 2))
            wait_until(lambda: not app.busy)
            assert app.directory.get() == "/my-files"
            for source in (local_hex, algorithm, upload_source):
                with patch("dapremote.gui.filedialog.askopenfilename", return_value=str(source)), \
                     patch("dapremote.gui.simpledialog.askstring") as path_dialog, \
                     patch("dapremote.gui.messagebox.askyesno") as overwrite_dialog:
                    app.upload()
                    wait_until(lambda: not app.busy)
                    assert path_dialog.call_count == 0 and overwrite_dialog.call_count == 0
                assert app.client.read_preview("/my-files/" + source.name)["data"] == source.read_bytes()
                assert app.directory.get() == "/my-files", "upload must not switch directories"
            assert {entry["name"] for entry in app.client.list_files("/my-files")} == {local_hex.name, algorithm.name, upload_source.name}
            # Exercise a real native confirmation. Close only this test-owned dialog automatically.
            import os
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes as w
                from tkinter import messagebox as native_messages
                from dapremote.ui_dialogs import native_dialog_centering
                title = "DAP Remote placement check %d" % os.getpid()
                user = ctypes.WinDLL("user32", use_last_error=True)
                user.FindWindowW.argtypes, user.FindWindowW.restype = [w.LPCWSTR, w.LPCWSTR], w.HWND
                user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
                user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
                user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
                checked, stop = [], threading.Event()
                def positioned(hwnd, area):
                    rect = w.RECT()
                    user.GetWindowRect(hwnd, ctypes.byref(rect))
                    checked.append((rect.left, rect.top, rect.right, rect.bottom, area))
                    user.PostMessageW(hwnd, 0x0111, 7, 0)  # WM_COMMAND / IDNO
                def watchdog():
                    if stop.wait(3):
                        return
                    hwnd = user.FindWindowW("#32770", title)
                    process = w.DWORD()
                    if hwnd:
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
                        if process.value == os.getpid():
                            user.PostMessageW(hwnd, 0x0111, 7, 0)
                guard = threading.Thread(target=watchdog, daemon=True)
                guard.start()
                try:
                    with native_dialog_centering(root, positioned):
                        answer = native_messages.askyesno(title, "Automatic dialog placement check", parent=root, default="no")
                    assert answer is False and checked, "native confirmation was not centered"
                    left, top, right, bottom, area = checked[0]
                    target = centered_rect(area, right - left, bottom - top)
                    assert abs(left - target[2]) <= 2 and abs(top - target[3]) <= 2
                finally:
                    stop.set()
                    guard.join(1)
            visible_log = app.log.get("1.0", "end-1c") + app.job_text.get("1.0", "end-1c")
            assert all(token not in visible_log for token in tokens), "internal ID leaked to visible logs"
            app.clear_log()
            assert not app.log.get("1.0", "end-1c")
            assert app.log_count == 0
            print("GUI smoke passed: flat theme, centered HEX picker/native confirmation, minimum-size/high-DPI footer, current-directory uploads, FLM-only, flashing, recovery, RTT")
        finally:
            app.close()


if __name__ == "__main__":
    main()
