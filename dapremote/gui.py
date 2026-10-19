"""Small Tkinter integration GUI; all protocol work runs in one worker thread."""
import queue
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from .client import Client
from .gui_flash import FlashCoordinator
from .gui_text import (Notice, PHASES, RttTextDecoder, detail_text, error_notice,
                       job_notice, result_notice, scan_notice, size_text)
from .protocol import Op


class App:
    def __init__(self, root, endpoint, baudrate, state_path=None):
        self.root, self.baudrate = root, baudrate
        self.client, self.stream_id, self.job_id = None, None, None
        self.connected_endpoint = None
        self.flash_operations = FlashCoordinator(state_path if state_path is not None else
                                                  Path(__file__).resolve().parents[1] / ".dapremote" / "gui-flash.json")
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.messages = queue.Queue()
        self.busy, self.closed = False, False
        self.poll_pending = self.poll_suspended = False
        self.connection_generation = 0
        self._last_job = self._last_rtt_warning = None
        self.rtt_decoder = RttTextDecoder()
        self._transfer_percent = -1
        self.error_dialogs = tk.BooleanVar(value=True)
        self.auto_scroll = tk.BooleanVar(value=True)
        self.log_count = 0
        self.log_count_text = tk.StringVar(value="0 条记录")
        self._configure_style()
        self.bench_running = False
        self.bench_cancel = threading.Event()
        self.buttons = []
        root.title("DAP Remote · 远程调试工作台")
        root.geometry("1180x830")
        root.minsize(1000, 700)
        root.configure(background="#F3F6FB")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._callback_error
        self.endpoint = tk.StringVar(value=endpoint)
        self.port_baud = tk.StringVar(value=str(baudrate))
        self.status = tk.StringVar(value="就绪 · 请连接串口设备或本地模拟器")
        self.device_summary = tk.StringVar(value="设备未连接")
        self.rtt_stats = tk.StringVar(value="尚未采集 · 默认显示文本，二进制控制字节转义显示")
        self.directory = tk.StringVar(value="/")
        self.destination = tk.StringVar(value="电脑 + SD 卡")
        self.rtt_path = tk.StringVar(value="/rtt.log")
        header = ttk.Frame(root, padding=(20, 16), style="Card.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="DAP Remote", style="Brand.TLabel").pack(side="left")
        ttk.Entry(header, textvariable=self.endpoint, width=35).pack(side="left", padx=14)
        ttk.Label(header, text="波特率", style="Card.TLabel").pack(side="left")
        ttk.Entry(header, textvariable=self.port_baud, width=9).pack(side="left", padx=5)
        self.button(header, "连接设备", self.connect, style="Primary.TButton").pack(side="left")
        self.button(header, "断开", self.disconnect).pack(side="left", padx=5)
        status_bar = ttk.Frame(root, padding=(20, 10))
        status_bar.pack(fill="x")
        self.connection_badge = ttk.Label(status_bar, text="● 未连接", style="Muted.TLabel")
        self.connection_badge.pack(side="left", padx=(0, 14))
        ttk.Label(status_bar, textvariable=self.device_summary, style="Muted.TLabel").pack(side="left")
        self.button(status_bar, "刷新设备状态", self.device_status).pack(side="right")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=20)
        files, rtt, jobs, bench = [ttk.Frame(notebook, padding=16) for _ in range(4)]
        for frame, title in ((files, "文件管理"), (rtt, "RTT 终端"), (jobs, "烧录任务"), (bench, "串口测速")):
            notebook.add(frame, text=title)
        bar = ttk.Frame(files)
        bar.pack(fill="x")
        ttk.Entry(bar, textvariable=self.directory, width=35).pack(side="left")
        for label, action in (("上一级", self.parent_directory), ("刷新", self.refresh), ("上传文件", self.upload),
                              ("取回文件", self.download), ("新建目录", self.mkdir),
                              ("删除所选", self.delete)):
            self.button(bar, label, action).pack(side="left", padx=3)
        table_frame = ttk.Frame(files)
        table_frame.pack(fill="both", expand=True, pady=10)
        self.files = ttk.Treeview(table_frame, columns=("path", "size", "version"), show="headings")
        for name, title, width in (("path", "设备路径", 450), ("size", "大小 / 类型", 140), ("version", "文件校验摘要", 220)):
            self.files.heading(name, text=title)
            self.files.column(name, width=width)
        self.files.pack(side="left", fill="both", expand=True)
        file_scroll = ttk.Scrollbar(table_frame, command=self.files.yview)
        file_scroll.pack(side="right", fill="y")
        self.files.configure(yscrollcommand=file_scroll.set)
        self.files.bind("<Double-1>", self.open_directory)
        self.entries = {}
        self.file_summary = tk.StringVar(value="选择文件可取回、删除；双击文件夹进入目录。")
        ttk.Label(files, textvariable=self.file_summary, style="Muted.TLabel").pack(anchor="w")
        bar = ttk.Frame(rtt)
        bar.pack(fill="x")
        ttk.Combobox(bar, state="readonly", textvariable=self.destination,
                     values=("电脑", "SD 卡", "电脑 + SD 卡"), width=15).pack(side="left")
        ttk.Entry(bar, textvariable=self.rtt_path, width=25).pack(side="left", padx=6)
        self.button(bar, "开始 RTT", self.start_rtt).pack(side="left")
        self.button(bar, "停止 RTT", self.stop_rtt).pack(side="left", padx=5)
        ttk.Label(rtt, textvariable=self.rtt_stats, style="Muted.TLabel").pack(anchor="w", pady=10)
        self.rtt_text = tk.Text(rtt, height=10, wrap="word", font=("Consolas", 11), state="disabled",
                               background="#142238", foreground="#DCE7F6", selectbackground="#345B86",
                               relief="flat", padx=14, pady=12)
        self.rtt_text.pack(fill="both", expand=True)
        bar = ttk.Frame(rtt)
        bar.pack(fill="x", pady=6)
        self.rtt_input = ttk.Entry(bar, width=60)
        self.rtt_input.pack(side="left", fill="x", expand=True)
        self.button(bar, "发送 UTF-8 输入", self.write_rtt).pack(side="left", padx=6)
        bar = ttk.Frame(jobs)
        bar.pack(fill="x")
        self.flash_path = ttk.Entry(bar, width=35)
        self.flash_path.insert(0, "/blink.hex")
        self.flash_path.pack(side="left")
        for label, action in (("检查 HEX", self.check_hex), ("开始烧录", self.flash),
                              ("取消任务", self.cancel_job), ("设备状态", self.device_status)):
            button = self.button(bar, label, action)
            button.pack(side="left", padx=4)
            if action == self.flash:
                self.flash_button = button
        row = ttk.Frame(jobs)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="烧录完成后可再次点击开始新的烧录；断线后可刷新任务状态。", style="Muted.TLabel").pack(side="left")
        self.button(row, "刷新任务状态", self.refresh_flash).pack(side="right", padx=5)
        self.job_phase = tk.StringVar(value="等待选择固件")
        self.job_progress_text = tk.StringVar(value="开始烧录后，在这里查看阶段与进度")
        ttk.Label(jobs, textvariable=self.job_phase, style="Section.TLabel").pack(anchor="w", pady=(4, 6))
        self.job_bar = ttk.Progressbar(jobs, maximum=100, style="Accent.Horizontal.TProgressbar")
        self.job_bar.pack(fill="x", pady=(0, 5))
        ttk.Label(jobs, textvariable=self.job_progress_text, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.job_text = tk.Text(jobs, height=5, wrap="word", font=("Microsoft YaHei UI", 10),
                               relief="flat", background="#FFFFFF", padx=12, pady=10, state="disabled")
        self.job_text.pack(fill="both", expand=True)
        self._build_benchmark(bench)
        log_header = ttk.Frame(root, padding=(20, 10, 20, 4))
        log_header.pack(fill="x")
        ttk.Label(log_header, text="操作日志", style="Section.TLabel").pack(side="left")
        ttk.Label(log_header, textvariable=self.log_count_text, style="Muted.TLabel").pack(side="left", padx=12)
        self.copy_button = ttk.Button(log_header, text="复制", command=self.copy_log)
        self.copy_button.pack(side="right")
        self.clear_button = ttk.Button(log_header, text="清空", command=self.clear_log)
        self.clear_button.pack(side="right", padx=6)
        ttk.Checkbutton(log_header, text="错误弹窗", variable=self.error_dialogs).pack(side="right", padx=10)
        ttk.Checkbutton(log_header, text="自动滚动", variable=self.auto_scroll).pack(side="right")
        log_frame = ttk.Frame(root)
        log_frame.pack(fill="x", padx=20)
        self.log = tk.Text(log_frame, height=6, wrap="word", font=("Consolas", 10),
                           background="#FFFFFF", foreground="#334155", relief="flat", padx=12, pady=10, state="disabled")
        self.log.pack(side="left", fill="x", expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)
        for widget in (self.log, self.job_text):
            for level, color in (("INFO", "#475569"), ("SUCCESS", "#15803D"), ("WARN", "#A16207"), ("ERROR", "#B91C1C")):
                widget.tag_configure(level, foreground=color)
        footer = ttk.Frame(root, padding=(20, 7, 20, 10))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="left")
        self.activity_bar = ttk.Progressbar(footer, length=120, mode="indeterminate", style="Accent.Horizontal.TProgressbar")
        self.activity_bar.pack(side="right")
        self.log_message("欢迎使用 DAP Remote。连接设备后即可管理文件、烧录和采集 RTT。")
        if self.flash_operations.load_error:
            self.log_message(self.flash_operations.load_error, "ERROR")
        self._update_flash_button()
        self.root.after(50, self.drain)
        self.root.after(250, self.poll)

    def _configure_style(self):
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#F3F6FB")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("TLabel", background="#F3F6FB", foreground="#172338")
        style.configure("Card.TLabel", background="#FFFFFF")
        style.configure("Muted.TLabel", foreground="#64748B", font=("Microsoft YaHei UI", 9))
        style.configure("Brand.TLabel", background="#FFFFFF", foreground="#1E3A5F", font=("Segoe UI", 20, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", padding=(10, 7), background="#E8EEF7", foreground="#243B58", borderwidth=0)
        style.map("TButton", background=[("active", "#D9E5F4"), ("disabled", "#EDF1F6")], foreground=[("disabled", "#94A3B8")])
        style.configure("Primary.TButton", background="#2563EB", foreground="#FFFFFF")
        style.map("Primary.TButton", background=[("active", "#1D4ED8"), ("disabled", "#CBD5E1")], foreground=[("disabled", "#FFFFFF")])
        style.configure("Danger.TButton", background="#FEECEC", foreground="#B91C1C")
        style.map("Danger.TButton", background=[("active", "#FCDDDD"), ("disabled", "#EDF1F6")])
        style.configure("TEntry", padding=6, bordercolor="#D5DFED", fieldbackground="#FFFFFF")
        style.configure("TCombobox", padding=5, fieldbackground="#FFFFFF", bordercolor="#D5DFED")
        style.configure("TNotebook", background="#F3F6FB", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(24, 11), background="#E9EFF8", foreground="#64748B")
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", "#1D4ED8")])
        style.configure("Treeview", rowheight=34, background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#243B58", borderwidth=0)
        style.configure("Treeview.Heading", padding=(10, 8), background="#E9EFF8", foreground="#52657E", relief="flat")
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", "#1E40AF")])
        style.configure("TCheckbutton", background="#F3F6FB", foreground="#64748B")
        style.configure("Accent.Horizontal.TProgressbar", background="#2563EB", troughcolor="#E2EAF6", borderwidth=0)

    def button(self, parent, text, command, style="TButton"):
        if style == "TButton" and text in ("删除所选", "取消任务"):
            style = "Danger.TButton"
        elif style == "TButton" and text in ("上传文件", "开始烧录", "开始 RTT"):
            style = "Primary.TButton"
        button = ttk.Button(parent, text=text, command=command, style=style)
        self.buttons.append(button)
        return button

    def show(self, value, widget=None):
        notice = result_notice(value)
        self.log_message(notice.text, notice.level, widget)

    def _append_text(self, widget, text, tag=None, newline=True):
        widget.configure(state="normal")
        widget.insert("end", text + ("\n" if newline else ""), tag or ())
        if int(widget.index("end-1c").split(".")[0]) > 600:
            widget.delete("1.0", "150.0")
        widget.configure(state="disabled")
        if self.auto_scroll.get():
            widget.see("end")

    def log_message(self, text, level="INFO", widget=None):
        self._append_text(widget or self.log, "%s  %-7s  %s" % (datetime.now().strftime("%H:%M:%S"), level, text), level)
        if widget is None or widget is self.log:
            self.log_count += 1
            self.log_count_text.set("%d 条记录" % self.log_count)

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.log_count = 0
        self.log_count_text.set("0 条记录")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))
        self.status.set("已复制操作日志")

    def report_error(self, exc, context="操作失败", popup=True):
        notice = error_notice(exc)
        if notice.level == "WARN":
            context = "操作已停止"
        self.log_message(context + "：" + notice.text, notice.level)
        self.status.set(context + " · " + notice.text.splitlines()[0])
        if notice.level == "ERROR" and popup and self.error_dialogs.get() and not self.closed:
            messagebox.showerror(context, notice.text, parent=self.root)

    def _callback_error(self, exc_type, exc, traceback):
        if not self.closed:
            self.report_error(exc, "界面操作未完成")

    def submit(self, work, done=None, label="操作", background=False):
        if self.busy or self.closed or (background and self.poll_pending):
            return
        if background:
            self.poll_pending = True
        else:
            self.busy = True
            self.status.set("正在%s…" % label)
            self.activity_bar.configure(mode="indeterminate")
            self.activity_bar.start(14)
            for button in self.buttons:
                button.configure(state="disabled")
        future = self.executor.submit(work)
        generation = self.connection_generation
        def finished(result):
            if self.closed:
                try:
                    value = result.result()
                    if isinstance(value, Client):
                        value.close()
                except Exception:
                    pass
                return
            self.messages.put((result, done, label, background, generation))
        future.add_done_callback(finished)

    def drain(self):
        while not self.messages.empty():
            event = self.messages.get()
            future, done = event[:2]
            if future is None:
                if done.get("event") == "transfer":
                    current, total, action = done["current"], done["total"], done["action"]
                    percent = current * 100 / total if total else 100
                    self.activity_bar.stop()
                    self.activity_bar.configure(mode="determinate", maximum=100, value=percent)
                    self.status.set("%s · %.0f%% · %s / %s" % (action, percent, size_text(current), size_text(total)))
                else:
                    self._benchmark_progress(done)
                continue
            label, background, generation = event[2:]
            if background:
                self.poll_pending = False
                if generation != self.connection_generation:
                    continue
            else:
                self.busy = False
                self.activity_bar.stop()
                self.activity_bar.configure(mode="determinate", value=0)
                self.status.set(label + "已完成")
                for button in self.buttons:
                    button.configure(state="normal")
            try:
                result = future.result()
                if done:
                    done(result)
                else:
                    self.show(result)
            except Exception as exc:
                if self.bench_running:
                    self.bench_running = False
                    self.bench_stop.configure(state="disabled")
                if background:
                    self.poll_suspended = True
                self.report_error(exc, label + "失败", popup=not background)
            self._update_flash_button()
        if not self.closed:
            self.root.after(50, self.drain)

    def require_client(self):
        if self.client is None:
            self.log_message("请先连接串口设备或本地模拟器。", "WARN")
            self.status.set("尚未连接设备")
            return False
        return True

    def connect(self):
        if self.client:
            return
        endpoint = self.endpoint.get()
        try:
            baudrate = int(self.port_baud.get())
            if not 1 <= baudrate <= 4000000:
                raise ValueError()
        except ValueError:
            self.report_error(ValueError("请输入 1..4000000 的整数波特率。"), "连接参数不正确")
            return
        self.submit(lambda: Client.open(endpoint, baudrate), lambda client: self.connected(client, endpoint), label="连接设备")

    def connected(self, client, endpoint=None):
        self.client = client
        self.connected_endpoint = endpoint or self.endpoint.get().strip()
        endpoint = self.connected_endpoint
        self.connection_generation += 1
        self.poll_suspended = False
        self.connection_badge.configure(text="● 已连接", foreground="#15803D")
        self.status.set("设备连接成功")
        self.log_message("连接成功 · " + client.info["device_id"], "SUCCESS")
        def attached(result):
            device, job, recovery_error = result
            self._device_updated(device, log=False)
            streams = device.get("streams", [])
            if len(streams) == 1:
                self.stream_id = streams[0]["stream_id"]
                self.rtt_decoder = RttTextDecoder()
                self.log_message("已重新接入设备上的 RTT 采集。")
            if job is not None:
                self.log_message("已恢复上次烧录任务的状态。")
                self.job_updated(job)
            if recovery_error is not None:
                self.report_error(recovery_error, "恢复烧录状态失败", popup=False)
            self.refresh()
        def work():
            device = client.call(Op.DEV_STATUS)
            try:
                return device, self.flash_operations.query(client, endpoint), None
            except Exception as exc:
                return device, None, exc
        self.submit(work, attached, label="读取设备状态")

    def disconnect(self):
        if self.client:
            self.client.close()
        self.client, self.stream_id, self.job_id = None, None, None
        self.connected_endpoint = None
        self.connection_generation += 1
        self.connection_badge.configure(text="● 未连接", foreground="#64748B")
        self.device_summary.set("设备未连接")
        self.log_message("连接已断开。设备上的任务与 SD 日志可能继续，重连后可查询。", "WARN")
        self.status.set("已断开；设备侧任务和 SD 日志可能继续，重连后自动恢复任务状态")
        self._update_flash_button()

    def refresh(self):
        if self.require_client():
            path = self.directory.get()
            client = self.client
            self.submit(lambda: client.list_files(path), self.listed, label="读取文件列表")

    def listed(self, entries):
        self.files.delete(*self.files.get_children())
        self.entries = {entry["path"]: entry for entry in entries}
        for entry in entries:
            self.files.insert("", "end", iid=entry["path"], values=(entry["path"],
                              "文件夹" if entry["is_dir"] else size_text(entry["size"]),
                              entry["version"][:12] + "…" if entry["version"] else "—"))
        self.file_summary.set("目录 %s · %d 项；双击文件夹进入。" % (self.directory.get(), len(entries)))
        self.log_message("已读取目录 %s · %d 项" % (self.directory.get(), len(entries)))

    def parent_directory(self):
        parts = self.directory.get().strip("/").split("/")
        self.directory.set("/" + "/".join(parts[:-1]))
        self.refresh()

    def _transfer_callback(self, action):
        self._transfer_percent = -1
        def progress(current, total):
            percent = int(current * 100 / total) if total else 100
            if percent != self._transfer_percent:
                self._transfer_percent = percent
                self.messages.put((None, dict(event="transfer", current=current, total=total, action=action)))
        return progress

    def selected(self):
        selection = self.files.selection()
        return self.entries.get(selection[0]) if selection else None

    def open_directory(self, _):
        entry = self.selected()
        if entry and entry["is_dir"]:
            self.directory.set(entry["path"])
            self.refresh()

    def upload(self):
        if not self.require_client():
            return
        local = filedialog.askopenfilename()
        if not local:
            return
        remote = simpledialog.askstring("上传路径", "设备上的完整路径：", initialvalue=self.directory.get().rstrip("/") + "/" + Path(local).name)
        if remote:
            overwrite = messagebox.askyesno("覆盖策略", "允许替换设备上同名文件？")
            client, progress = self.client, self._transfer_callback("上传文件")
            def done(result):
                self.log_message("上传完成 · %s → %s · %s · 文件校验通过" % (Path(local).name, remote, size_text(result["size"])), "SUCCESS")
                self.refresh()
            self.submit(lambda: client.upload(local, remote, overwrite,
                                               resume_file=local + ".dap-upload.json", progress=progress), done, label="上传文件")

    def file_done(self, result):
        self.show(result)
        self.refresh()

    def download(self):
        entry = self.selected()
        if not self.require_client() or not entry or entry["is_dir"]:
            return
        local = filedialog.asksaveasfilename(initialfile=entry["name"])
        if local:
            client, progress = self.client, self._transfer_callback("取回文件")
            def done(result):
                self.log_message("取回完成 · %s → %s · %s · 文件校验通过" % (entry["path"], local, size_text(result["size"])), "SUCCESS")
            self.submit(lambda: client.download(entry["path"], local, overwrite=True, progress=progress), done, label="取回文件")

    def mkdir(self):
        if self.require_client():
            path = simpledialog.askstring("新建目录", "设备完整路径：")
            if path:
                client = self.client
                def done(_):
                    self.log_message("已创建目录 · " + path, "SUCCESS")
                    self.refresh()
                self.submit(lambda: client.mkdir(path), done, label="创建目录")

    def delete(self):
        entry = self.selected()
        if self.require_client() and entry and messagebox.askyesno("删除", "删除 %s？" % entry["path"]):
            client = self.client
            def done(_):
                self.log_message("已删除 · " + entry["path"], "SUCCESS")
                self.refresh()
            self.submit(lambda: client.delete(entry["path"], entry["version"]), done, label="删除文件")

    def start_rtt(self):
        if self.require_client() and self.stream_id is None:
            destination = {"GUI": 1, "SD": 2, "GUI + SD": 3, "电脑": 1, "SD 卡": 2, "电脑 + SD 卡": 3}[self.destination.get()]
            path = self.rtt_path.get()
            client = self.client
            self.submit(lambda: client.start_rtt(destination, path), self.rtt_started, label="开始 RTT 采集")

    def rtt_started(self, result):
        self.stream_id = result["stream_id"]
        self.rtt_decoder = RttTextDecoder()
        self._last_rtt_warning = None
        self.rtt_stats.set("正在采集 · " + self.destination.get())
        self.show(result)

    def stop_rtt(self):
        if self.require_client() and self.stream_id is not None:
            stream = self.stream_id
            client = self.client
            self.submit(lambda: client.stop_rtt(stream), self.rtt_stopped, label="停止 RTT 采集")

    def rtt_stopped(self, result):
        self.stream_id = None
        tail = self.rtt_decoder.feed(b"", final=True)
        if tail:
            self._append_text(self.rtt_text, tail, newline=False)
        self.rtt_stats.set("采集已停止 · SD 已记录 %s · 电脑缓存丢失 %s" % (size_text(result.get("sd_bytes", 0)), size_text(result.get("dropped_bytes", 0))))
        self.show(result)

    def write_rtt(self):
        if self.require_client() and self.stream_id is not None:
            data, stream = self.rtt_input.get().encode("utf-8"), self.stream_id
            if not data or self.busy:
                return
            client = self.client
            self.submit(lambda: client.call(Op.RTT_WRITE, stream_id=stream, data=data), label="发送 RTT 文本")

    def check_hex(self):
        if self.require_client():
            path = self.flash_path.get()
            client = self.client
            def done(value):
                self.show(value, self.job_text)
                self.show(value)
            self.submit(lambda: client.check_hex(path), done, label="检查 HEX 固件")

    def flash(self):
        if self.busy or not self.require_client():
            return
        path, client, endpoint = self.flash_path.get(), self.client, self.connected_endpoint
        def done(outcome):
            if outcome["recovered"]:
                self.log_message("已确认上次烧录任务的状态，没有重复启动。")
            else:
                self.log_message("已提交烧录 · " + path)
            self.job_updated(outcome["job"])
            if self.flash_operations.last_save_error:
                self.log_message("本地任务状态更新失败，重连后将重新查询设备。", "WARN")
        self.submit(lambda: self.flash_operations.start(client, endpoint, path), done, label="开始烧录")

    def refresh_flash(self):
        if self.busy or not self.require_client():
            return
        client, endpoint = self.client, self.connected_endpoint
        def done(job):
            if job is None:
                self.log_message("当前没有已提交的烧录任务。")
                return
            self.poll_suspended = False
            self.job_updated(job)
        self.submit(lambda: self.flash_operations.query(client, endpoint), done, label="查询烧录任务")

    def _update_flash_button(self):
        if hasattr(self, "flash_button"):
            self.flash_button.configure(state="disabled" if self.busy or self.client is None or self.job_id is not None else "normal")

    def job_updated(self, result):
        if self.client is not None:
            self.flash_operations.observe(self.client, self.connected_endpoint, result, persist=False)
        self.job_id = None if result["state"] in ("DONE", "FAILED", "CANCELLED", "INTERRUPTED") else result["job_id"]
        state, phase = result["state"], result["phase"]
        completed, total = result.get("completed_bytes", 0), result.get("total_bytes", 0)
        self.job_phase.set(PHASES.get(phase, "正在执行"))
        self.job_bar["value"] = 100 if state == "DONE" else completed * 100 / total if total else 0
        self.job_progress_text.set("已写入 %s / %s%s" % (size_text(completed), size_text(total), " · 校验通过" if state == "DONE" else ""))
        signature = (result["job_id"], phase, state)
        if signature != self._last_job:
            self._last_job = signature
            notice = job_notice(result)
            self.show(notice, self.job_text)
            self.show(notice)
            if notice.level == "ERROR" and self.error_dialogs.get():
                messagebox.showerror("烧录任务未完成", notice.text, parent=self.root)
        self._update_flash_button()

    def cancel_job(self):
        if self.require_client() and self.job_id:
            job_id = self.job_id
            client = self.client
            endpoint = self.connected_endpoint
            def work():
                job = client.call(Op.JOB_CANCEL, job_id=job_id)
                self.flash_operations.observe(client, endpoint, job)
                return job
            self.submit(work, self.job_updated, label="取消烧录任务")

    def device_status(self):
        if self.require_client():
            client = self.client
            self.submit(lambda: client.call(Op.DEV_STATUS), self._device_updated, label="读取设备状态")

    def _device_updated(self, result, log=True):
        device = self.client.info.get("device_id", "设备") if self.client else "设备"
        self.device_summary.set("%s  ·  电量 %d%%（%.2f V）  ·  SD 卡%s" %
                                (device, result.get("battery_percent", 0), result.get("battery_mv", 0) / 1000,
                                 "已就绪" if result.get("sd_ready") else "未就绪"))
        if log:
            self.show(result)

    def poll(self):
        if not self.closed and not self.busy and not self.poll_pending and not self.poll_suspended and self.client:
            stream, job, client, endpoint = self.stream_id, self.job_id, self.client, self.connected_endpoint
            if stream is not None or job:
                def work():
                    rtt_result = client.read_rtt(stream) if stream is not None else None
                    job_result = client.job_status(job_id=job) if job else None
                    if job_result:
                        self.flash_operations.observe(client, endpoint, job_result)
                    return rtt_result, job_result
                def done(result):
                    rtt, job_status = result
                    if rtt is not None:
                        if rtt.get("data"):
                            self._append_text(self.rtt_text, self.rtt_decoder.feed(rtt["data"]), newline=False)
                        self.rtt_stats.set("正在采集 · 电脑缓存丢失 %s · SD 已记录 %s" %
                                           (size_text(rtt["dropped_bytes"]), size_text(rtt["sd_bytes"])))
                        warning = (rtt["sd_error"], bool(rtt["dropped_bytes"]))
                        if warning != self._last_rtt_warning:
                            if rtt["sd_error"]:
                                self.log_message("RTT 的 SD 输出异常：" + detail_text(rtt["sd_error"]), "ERROR")
                            elif rtt["dropped_bytes"]:
                                self.log_message("RTT 电脑接收缓存已溢出，部分实时显示数据丢失。", "WARN")
                            self._last_rtt_warning = warning
                    if job_status:
                        self.job_updated(job_status)
                self.submit(work, done, label="刷新设备数据", background=True)
        if not self.closed:
            self.root.after(250, self.poll)

    def close(self):
        self.closed = True
        self.bench_cancel.set()
        if self.client:
            self.client.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def _build_benchmark(self, parent):
        self.bench_rates = tk.StringVar(value="115200,230400,460800,921600")
        self.bench_size = tk.StringVar(value="64")
        self.bench_rounds = tk.StringVar(value="3")
        self.bench_sink = tk.StringVar(value="内存接收")
        self.bench_file = tk.StringVar()
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="候选波特率").pack(side="left")
        ttk.Entry(row, textvariable=self.bench_rates, width=42).pack(side="left", padx=5)
        ttk.Label(row, text="每轮 KiB").pack(side="left")
        ttk.Entry(row, textvariable=self.bench_size, width=7).pack(side="left", padx=5)
        ttk.Label(row, text="重复").pack(side="left")
        ttk.Entry(row, textvariable=self.bench_rounds, width=4).pack(side="left", padx=5)
        ttk.Combobox(row, textvariable=self.bench_sink, state="readonly", values=("内存接收", "写入 SD 卡"), width=12).pack(side="left")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=8)
        ttk.Entry(row, textvariable=self.bench_file, width=45).pack(side="left")
        self.button(row, "选择测试文件", self.choose_bench_file).pack(side="left", padx=4)
        self.button(row, "测当前连接", lambda: self.start_benchmark(False), style="Primary.TButton").pack(side="left", padx=4)
        self.button(row, "扫描波特率", lambda: self.start_benchmark(True)).pack(side="left", padx=4)
        # This button intentionally stays usable while the worker is busy.
        self.bench_stop = ttk.Button(row, text="停止测试", state="disabled", command=self.bench_cancel.set)
        self.bench_stop.pack(side="left", padx=4)
        ttk.Label(parent, text="未选文件时使用生成数据。连续发送后核对整份数据；扫描结束会恢复原串口设置。",
                  wraplength=960, style="Muted.TLabel").pack(anchor="w", pady=5)
        self.bench_table = ttk.Treeview(parent, columns=("rate", "rounds", "speed", "state", "error"), show="headings", height=9)
        for key, label, width in (("rate", "波特率 / 连接", 140), ("rounds", "通过轮数", 90),
                                  ("speed", "接收 KiB/s", 120), ("state", "结果", 100), ("error", "说明", 460)):
            self.bench_table.heading(key, text=label)
            self.bench_table.column(key, width=width)
        self.bench_table.pack(fill="both", expand=True, pady=8)
        self.bench_table.tag_configure("error", foreground="#B91C1C")
        self.bench_table.tag_configure("success", foreground="#15803D")
        self.bench_summary = tk.StringVar(value="尚未测试。扫描结果仅代表本轮条件下通过的候选档位。")
        ttk.Label(parent, textvariable=self.bench_summary, wraplength=960).pack(anchor="w")

    def choose_bench_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="选择测速文件")
        if path:
            self.bench_file.set(path)

    def start_benchmark(self, scan):
        if self.busy or not self.require_client():
            return
        try:
            rates = [int(value.strip()) for value in self.bench_rates.get().split(",")]
            size, rounds = int(self.bench_size.get()) * 1024, int(self.bench_rounds.get())
            if not rates or any(not 1 <= rate <= 4000000 for rate in rates) or not 1 <= size <= 16 * 1024 * 1024 or not 1 <= rounds <= 20:
                raise ValueError()
        except ValueError:
            self.report_error(ValueError("波特率用英文逗号分隔，取值 1..4000000；大小 1..16384 KiB；重复 1..20 轮。"), "测速参数不正确")
            return
        source, client = self.bench_file.get() or None, self.client
        sink = {"内存接收": "memory", "写入 SD 卡": "sd"}.get(self.bench_sink.get(), self.bench_sink.get())
        self.bench_table.delete(*self.bench_table.get_children())
        self.bench_cancel.clear()
        self.bench_running = True
        self.bench_stop.configure(state="normal")
        self.bench_summary.set("测试进行中，结束后校验整份数据；可以停止测试。")
        self.log_message("开始%s · %s · %s" % ("波特率扫描" if scan else "当前连接测速",
                                               Path(source).name if source else size_text(size) + " 生成数据",
                                               "写入 SD 卡" if sink == "sd" else "内存接收"))
        def progress(event):
            if event["event"] in ("candidate", "round", "result"):
                self.messages.put((None, event))
        def work():
            if scan:
                return client.scan_baudrates(rates, rounds=rounds, size=size, file_path=source,
                                             sink=sink, progress=progress, cancel_event=self.bench_cancel)
            return client.benchmark(size=size, file_path=source, sink=sink,
                                     cancel_event=self.bench_cancel)
        def done(result):
            self.bench_running = False
            self.bench_stop.configure(state="disabled")
            self.bench_table.delete(*self.bench_table.get_children())
            if scan:
                for row in result["results"]:
                    self._benchmark_row(row)
                text = scan_notice(result).text
                if not result["restored"]:
                    self.disconnect()
                self.bench_summary.set(text)
            else:
                self._benchmark_row(dict(baudrate=getattr(client.transport, "baudrate", None) or "当前连接",
                                         rounds_passed=int(result["passed"]), rounds_requested=1,
                                         bytes_per_s=result["bytes_per_s"], passed=result["passed"], error=result["error"]))
                self.bench_summary.set("发送 %d B，接收 %d B；SHA-256：%s；分帧错误 %d；序号异常 %d。" %
                                       (result["sent_bytes"], result["received_bytes"],
                                        "一致" if result["received_sha256"] == result["expected_sha256"] else "不一致",
                                        result["framing_errors"], result["sequence_errors"]))
            self.show(result)
            notice = result_notice(result)
            if notice.level == "ERROR" and self.error_dialogs.get():
                messagebox.showerror("测速未通过", notice.text, parent=self.root)
        self.submit(work, done, label="波特率扫描" if scan else "串口测速")

    def _benchmark_row(self, row):
        self.bench_table.insert("", "end", values=(row["baudrate"],
                               "%d/%d" % (row["rounds_passed"], row["rounds_requested"]),
                               "%.2f" % (row["bytes_per_s"] / 1024) if row["passed"] else "—",
                               "通过" if row["passed"] else "未通过", detail_text(row["error"]) or "整份数据校验一致"),
                               tags=("success" if row["passed"] else "error",))

    def _benchmark_progress(self, event):
        if event["event"] == "candidate":
            self.bench_summary.set("正在测试 %d%s" % (event["baudrate"], "（TCP 模拟）" if event["simulated"] else ""))
        elif event["event"] == "round":
            self.bench_summary.set("%d · 第 %d 轮 · %s · %.2f KiB/s" %
                                   (event["baudrate"], event["round"], "通过" if event["passed"] else "失败", event["bytes_per_s"] / 1024))
            self.log_message("%s baud · 第 %d 轮%s%s" % (event["baudrate"], event["round"],
                             "校验通过" if event["passed"] else "校验未通过",
                             " · %.2f KiB/s" % (event["bytes_per_s"] / 1024) if event["passed"] else " · " + detail_text(event["error"])),
                             "SUCCESS" if event["passed"] else "ERROR")
        elif event["event"] == "result" and not event["passed"] and not event["rounds"]:
            self.log_message("%s baud 未通过 · %s" % (event["baudrate"], detail_text(event["error"])), "ERROR")


def main(endpoint="tcp://127.0.0.1:8765", baudrate=115200):
    root = tk.Tk()
    App(root, endpoint, baudrate)
    root.mainloop()
    return 0
