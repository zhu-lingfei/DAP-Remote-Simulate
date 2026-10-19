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
from .firmware import ARTIFACT_DIRS, ARTIFACT_EXTENSIONS, artifact_path, ensure_artifact_directory, UploadConflict, upload_regular_file
from .file_picker import ArtifactPicker
from .hex_preview import MAX_PREVIEW_LINES, preview_lines
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
        self.notebook = notebook
        notebook.pack(fill="both", expand=True, padx=20)
        files, rtt, jobs, bench = [ttk.Frame(notebook, padding=16) for _ in range(4)]
        self.jobs_page = jobs
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
        self.file_summary = tk.StringVar(value="双击文件夹进入；选择文件后使用上方按钮操作。")
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
        self._build_flash_page(jobs)
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
        notebook.bind("<<NotebookTabChanged>>", lambda _: self._maybe_auto_preview())
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

    def _build_flash_page(self, page):
        self.flash_panes = ttk.Panedwindow(page, orient="horizontal")
        self.flash_panes.pack(fill="both", expand=True)
        left, right = ttk.Frame(self.flash_panes, padding=(0, 0, 12)), ttk.Frame(self.flash_panes, padding=(12, 0, 0))
        self.flash_panes.add(left, weight=1)
        self.flash_panes.add(right, weight=1)
        self.hex_preview_info = tk.StringVar(value="选择 HEX 固件后加载预览")
        self._preview_limit = 16 * 1024
        self._preview_request = 0
        self._preview_attempt = self._preview_loaded_path = None
        self._preview_truncated = False
        ttk.Label(left, text="HEX 文件预览", style="Section.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(left)
        toolbar.pack(fill="x", pady=(8, 6))
        self.preview_refresh_button = self.button(toolbar, "刷新预览", self.load_hex_preview)
        self.preview_refresh_button.pack(side="left")
        self.preview_more_button = self.button(toolbar, "加载更多", self.load_more_preview)
        self.preview_more_button.pack(side="left", padx=6)
        ttk.Label(left, textvariable=self.hex_preview_info, wraplength=420, style="Muted.TLabel").pack(anchor="w", pady=(0, 7))
        text_frame = ttk.Frame(left)
        text_frame.pack(fill="both", expand=True)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.hex_text = tk.Text(text_frame, wrap="none", width=1, height=6, state="disabled", relief="flat",
                                background="#FFFFFF", foreground="#243B58", font=("Consolas", 10), padx=10, pady=10)
        self.hex_text.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(text_frame, orient="vertical", command=self.hex_text.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(text_frame, orient="horizontal", command=self.hex_text.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.hex_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        for tag, color in (("number", "#94A3B8"), ("data", "#243B58"), ("address", "#2563EB"),
                            ("start", "#7C3AED"), ("eof", "#15803D"), ("error", "#B91C1C"),
                            ("partial", "#A16207"), ("blank", "#64748B")):
            self.hex_text.tag_configure(tag, foreground=color)
        ttk.Label(left, text="蓝色：地址记录  ·  绿色：结束记录  ·  红色：格式或校验异常", style="Muted.TLabel", wraplength=420).pack(anchor="w", pady=(7, 0))

        ttk.Label(right, text="烧录控制", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        self.selected_hex = self.selected_flm = None
        self.artifact_picker = None
        self.flash_path_var = tk.StringVar()
        self.hex_filename = tk.StringVar(value="尚未选择 HEX 固件")
        self.flm_filename = tk.StringVar(value="尚未选择 FLM 算法")
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(0, 8))
        self.button(row, "选择 HEX…", self.choose_firmware).pack(side="right", padx=(8, 0))
        ttk.Label(row, textvariable=self.hex_filename, wraplength=240).pack(side="left", fill="x", expand=True)
        ttk.Label(right, text="烧录算法 · SD 文件库（仅 FLM）", style="Muted.TLabel").pack(anchor="w", pady=(0, 4))
        row = ttk.Frame(right)
        row.pack(fill="x", pady=(0, 4))
        self.flm_choice = tk.StringVar()
        self.flm_files = {}
        self._flm_list_generation = None
        self.flm_combo = ttk.Combobox(row, textvariable=self.flm_choice, state="disabled", width=1)
        self.flm_combo.pack(side="left", fill="x", expand=True)
        self.flm_combo.bind("<<ComboboxSelected>>", self.select_flm)
        self.button(row, "刷新 FLM", self.refresh_flm_files).pack(side="right", padx=(8, 0))
        self.flm_list_hint = tk.StringVar(value="连接设备后加载 /FLM 目录中的 仅 FLM")
        ttk.Label(right, textvariable=self.flm_list_hint, style="Muted.TLabel", wraplength=390).pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(right)
        actions.pack(fill="x")
        actions.columnconfigure(0, weight=1, uniform="flash-actions")
        actions.columnconfigure(1, weight=1, uniform="flash-actions")
        for index, (label, action) in enumerate((("检查 HEX", self.check_hex), ("开始烧录", self.flash),
                                                ("取消任务", self.cancel_job), ("设备状态", self.device_status))):
            button = self.button(actions, label, action)
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0 if index % 2 == 0 else 6, 0), pady=(0, 6))
            if action == self.flash:
                self.flash_button = button
        self.button(right, "刷新任务状态", self.refresh_flash).pack(anchor="w", pady=(0, 8))
        self.job_phase = tk.StringVar(value="等待选择固件")
        self.job_progress_text = tk.StringVar(value="烧录完成后可再次点击开始新任务")
        ttk.Label(right, textvariable=self.job_phase, style="Section.TLabel").pack(anchor="w", pady=(4, 6))
        self.job_bar = ttk.Progressbar(right, maximum=100, style="Accent.Horizontal.TProgressbar")
        self.job_bar.pack(fill="x", pady=(0, 5))
        ttk.Label(right, textvariable=self.job_progress_text, wraplength=370, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.job_text = tk.Text(right, width=1, height=4, wrap="word", font=("Microsoft YaHei UI", 10),
                               relief="flat", background="#FFFFFF", padx=12, pady=10, state="disabled")
        self.job_text.pack(fill="both", expand=True)
        self.flash_path_var.trace_add("write", self._preview_path_changed)
        self._preview_placeholder("尚未加载 HEX 文件。\n\n点击右侧“选择 HEX 固件…”，从 SD 文件库或电脑中选择。")

    def _firmware_path(self):
        return self.flash_path_var.get()

    def choose_firmware(self):
        self.choose_artifact("hex")

    def choose_artifact(self, kind):
        if kind == "flm":
            self.refresh_flm_files()
            return
        if self.busy or self.job_id is not None:
            return
        if self.artifact_picker is not None and self.artifact_picker.exists():
            self.artifact_picker.window.lift()
            return
        self.artifact_picker = ArtifactPicker(self, kind, lambda info: self._artifact_selected(kind, info))

    def refresh_flm_files(self, auto=False):
        if self.busy or self.job_id is not None or self.client is None:
            if not auto and self.client is None:
                self.require_client()
            return False
        generation = self.connection_generation
        if auto and self._flm_list_generation == generation:
            return False
        self._flm_list_generation = generation
        client = self.client
        self.flm_list_hint.set("正在读取 SD 算法列表…")
        def work():
            root = ensure_artifact_directory(client, "flm")
            return client.list_files(root)
        def done(entries):
            self._apply_flm_files(entries)
            self._maybe_auto_preview()
        def failed(exc):
            self.flm_files.clear()
            self.flm_combo.configure(values=())
            self.selected_flm = None
            self.flm_choice.set("")
            self.flm_filename.set("尚未选择 FLM 算法")
            self.flm_list_hint.set("加载失败，请检查连接后刷新 FLM")
            self.report_error(exc, "加载 FLM 列表失败", popup=not auto)
        self.submit(work, done, label="读取 FLM 列表", error_handler=failed)
        return True

    def _apply_flm_files(self, entries):
        self.flm_files = {entry["name"]: entry for entry in sorted(entries, key=lambda item: item["name"].casefold())
                          if not entry["is_dir"] and entry["name"].lower().endswith(ARTIFACT_EXTENSIONS["flm"])
                          and entry["path"].rsplit("/", 1)[0] == ARTIFACT_DIRS["flm"]}
        self.flm_combo.configure(values=tuple(self.flm_files))
        selected = self.selected_flm
        match = self.flm_files.get(selected["name"]) if selected else None
        unchanged = (selected is not None and match is not None and selected["path"] == match["path"]
                     and selected["version"] == match["version"] and selected["device_id"] == self.client.info["device_id"]
                     and selected["endpoint"] == self.connected_endpoint)
        if not unchanged:
            self.selected_flm = None
            self.flm_choice.set("")
            self.flm_filename.set("尚未选择 FLM 算法")
            if selected:
                self.log_message("所选 FLM 已变化或不存在，请重新选择算法。", "WARN")
        else:
            self.flm_choice.set(selected["name"])
        ignored = sum(not entry["is_dir"] and not entry["name"].lower().endswith(ARTIFACT_EXTENSIONS["flm"])
                      for entry in entries)
        text = "%d 个算法 · 仅 FLM · 请从下拉框选择" % len(self.flm_files) if self.flm_files else \
               "/FLM 中没有 仅 FLM 算法，请在文件管理中上传"
        if ignored:
            text += " · 已过滤 %d 个其他类型文件" % ignored
        self.flm_list_hint.set(text)
        self._update_flash_button()

    def select_flm(self, _=None):
        if self.busy or self.job_id is not None or self.client is None:
            return
        entry = self.flm_files.get(self.flm_choice.get())
        if entry:
            self._artifact_selected("flm", entry)

    def _artifact_selected(self, kind, info):
        artifact_path(info["path"], kind)
        selected = dict(info, device_id=self.client.info["device_id"], endpoint=self.connected_endpoint)
        if kind == "hex":
            self.selected_hex = selected
            self.flash_path_var.set(info["path"])
            self.hex_filename.set(info["name"])
        else:
            self.selected_flm = selected
            self.flm_filename.set(info["name"])
            self.flm_choice.set(info["name"])
        self.log_message("已选用 %s · %s" % (kind.upper(), info["name"]), "SUCCESS")
        self._update_flash_button()
        if kind == "hex":
            self.load_hex_preview()

    def _selected_flash_files(self):
        if not self.selected_hex or not self.selected_flm:
            raise ValueError("请分别选择 HEX 固件和 FLM 烧录算法。")
        for kind, selected in (("hex", self.selected_hex), ("flm", self.selected_flm)):
            artifact_path(selected["path"], kind)
            if (selected["device_id"] != self.client.info["device_id"] or
                    selected["endpoint"] != self.connected_endpoint):
                raise ValueError("设备连接已变化，请重新选择 HEX 和 FLM。")
        return dict(self.selected_hex), dict(self.selected_flm)

    def _preview_placeholder(self, text):
        self.hex_text.configure(state="normal")
        self.hex_text.delete("1.0", "end")
        self.hex_text.insert("end", text, "blank")
        self.hex_text.configure(state="disabled")

    def _preview_path_changed(self, *_):
        self._preview_request += 1
        self._preview_limit = 16 * 1024
        self._preview_attempt = self._preview_loaded_path = None
        self._preview_truncated = False
        self.hex_preview_info.set("固件已更换，正在准备预览")
        self._preview_placeholder("预览内容已清除。\n\n刷新后查看当前来源的固件，避免混淆不同文件。")
        self._update_preview_actions()

    def _update_preview_actions(self):
        if not hasattr(self, "preview_refresh_button"):
            return
        available = self.client is not None and not self.busy and self.job_id is None
        self.preview_refresh_button.configure(state="normal" if available else "disabled")
        self.preview_more_button.configure(state="normal" if available and self._preview_truncated and self._preview_limit < 256 * 1024 else "disabled")

    def _maybe_auto_preview(self):
        if self.client is not None and self.refresh_flm_files(auto=True):
            return
        if (not self.closed and self.client is not None and not self.busy and self.job_id is None
                and self.notebook.select() == str(self.jobs_page)):
            self.load_hex_preview(auto=True)

    def load_more_preview(self):
        if not self._preview_truncated or self.busy:
            return
        self._preview_limit = min(256 * 1024, self._preview_limit * 2)
        self.load_hex_preview()

    def load_hex_preview(self, auto=False):
        if self.busy or self.job_id is not None:
            return
        if self.client is None:
            if not auto:
                self.require_client()
            return
        path, client, generation = self._firmware_path(), self.client, self.connection_generation
        if not path:
            if not auto:
                self.report_error(ValueError("请先通过弹窗选择 HEX 固件。"), "无法加载 HEX 预览")
            return
        key = (generation, path)
        if auto and self._preview_attempt == key:
            return
        self._preview_attempt = key
        self._preview_request += 1
        request, limit = self._preview_request, self._preview_limit
        self.hex_preview_info.set("正在读取 · " + Path(path).name)
        def current():
            return (request == self._preview_request and generation == self.connection_generation and
                    client is self.client and path == self._firmware_path())
        def work():
            result = client.read_preview(path, max_bytes=limit)
            lines = preview_lines(result["data"], result["truncated"], max_lines=MAX_PREVIEW_LINES + 1)
            result["lines_limited"] = len(lines) > MAX_PREVIEW_LINES
            return result, lines[:MAX_PREVIEW_LINES]
        def done(value):
            if not current():
                return
            result, lines = value
            self.hex_text.configure(state="normal")
            self.hex_text.delete("1.0", "end")
            segments = []
            for line in lines:
                segments.extend(("%5d  " % line.number, ("number",), line.text + "\n", (line.kind,)))
            if segments:
                self.hex_text.insert("end", *segments)
            else:
                self.hex_text.insert("end", "文件为空。", "blank")
            self.hex_text.configure(state="disabled")
            self.hex_text.yview_moveto(0)
            self.hex_text.xview_moveto(0)
            self._preview_loaded_path, self._preview_truncated = path, result["truncated"]
            total, shown = size_text(result["size"]), size_text(len(result["data"]))
            if result["truncated"]:
                tail = "可加载更多" if limit < 256 * 1024 else "已达预览上限，可取回文件查看全文"
                text = "%s · 已显示 %s / %s · %s" % (Path(path).name, shown, total, tail)
            else:
                text = "%s · %s · %d 行 · %s" % (Path(path).name, total, len(lines), "文件完整性一致")
            if result["lines_limited"]:
                text += " · 仅显示前 %d 行" % MAX_PREVIEW_LINES
            bad = sum(line.kind == "error" for line in lines)
            if bad:
                text += " · %d 条异常记录" % bad
                self.log_message("HEX 预览 · %s · %d 条记录存在格式或校验异常" % (Path(path).name, bad), "ERROR")
            else:
                self.log_message("已加载 HEX 预览 · %s · %s / %s" % (Path(path).name, shown, total))
            self.hex_preview_info.set(text)
            self._update_preview_actions()
        def failed(exc):
            if current():
                self._preview_loaded_path = None
                self._preview_truncated = False
                self.hex_preview_info.set("预览加载失败 · 请检查文件路径")
                self._preview_placeholder("无法读取当前文件。\n\n" + error_notice(exc).text)
                self.report_error(exc, "HEX 预览失败", popup=not auto)
                self._update_preview_actions()
        self.submit(work, done, label="读取 HEX 预览", error_handler=failed)

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

    def submit(self, work, done=None, label="操作", background=False, error_handler=None):
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
        self._update_flash_button()
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
            self.messages.put((result, done, label, background, generation, error_handler))
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
            label, background, generation, error_handler = event[2:]
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
                if error_handler:
                    error_handler(exc)
                else:
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
        if self.artifact_picker is not None and self.artifact_picker.exists():
            self.artifact_picker.window.destroy()
        if self.client:
            self.client.close()
        self.client, self.stream_id, self.job_id = None, None, None
        self.connected_endpoint = None
        self._flm_list_generation = None
        self.flm_files.clear()
        self.flm_combo.configure(values=())
        self.flm_choice.set("")
        self.selected_flm = None
        self.flm_filename.set("尚未选择 FLM 算法")
        self.flm_list_hint.set("连接设备后加载 /FLM 目录中的 仅 FLM")
        self.connection_generation += 1
        self.connection_badge.configure(text="● 未连接", foreground="#64748B")
        self.device_summary.set("设备未连接")
        self.log_message("连接已断开。设备上的任务与 SD 日志可能继续，重连后可查询。", "WARN")
        self.status.set("已断开；设备侧任务和 SD 日志可能继续，重连后自动恢复任务状态")
        self._preview_request += 1
        self._preview_attempt = self._preview_loaded_path = None
        self._preview_truncated = False
        self.hex_preview_info.set("连接已断开 · 重连后重新加载")
        self._preview_placeholder("连接已断开。\n\n重新连接后加载当前设备的 HEX 文件。")
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
        self.file_summary.set("目录 %s · %d 项；双击文件夹进入，文件使用上方按钮操作。" % (self.directory.get(), len(entries)))
        self.log_message("已读取目录 %s · %d 项" % (self.directory.get(), len(entries)))
        if self.directory.get().rstrip("/") == ARTIFACT_DIRS["flm"]:
            self._flm_list_generation = self.connection_generation
            self._apply_flm_files(entries)
        self._maybe_auto_preview()

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

    def open_directory(self, event):
        if self.busy or self.client is None:
            return "break"
        row = self.files.identify_row(event.y)
        entry = self.entries.get(row)
        if entry and entry["is_dir"]:
            self.files.selection_set(row)
            self.directory.set(entry["path"])
            self.refresh()
        return "break"

    def upload(self):
        if self.busy or not self.require_client():
            return
        local = filedialog.askopenfilename(parent=self.root, title="选择要上传的文件")
        if not local:
            return
        directory = self.directory.get().rstrip("/") or "/"
        remote = directory.rstrip("/") + "/" + Path(local).name
        client = self.client
        progress = self._transfer_callback("上传文件")
        def start(overwrite_version=None):
            def work():
                return upload_regular_file(client, local, remote, progress, overwrite_version)
            def done(info):
                self.log_message("上传完成 · %s · %s · 文件校验通过" % (Path(local).name, size_text(info["size"])), "SUCCESS")
                self.directory.set(directory)
                self.refresh()
            def failed(exc):
                if isinstance(exc, UploadConflict):
                    if messagebox.askyesno("文件重名", "目标目录已存在“%s”，是否覆盖？" % Path(exc.path).name,
                                           parent=self.root, default="no"):
                        start(exc.version)
                    else:
                        self.log_message("已取消上传，保留设备上的同名文件。")
                else:
                    self.report_error(exc, "上传文件失败")
            self.submit(work, done, label="上传文件", error_handler=failed)
        start()

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

    def _firmware_cache(self):
        return (self.flash_operations.path.parent if self.flash_operations.path else
                Path(__file__).resolve().parents[1] / ".dapremote") / "firmware-cache"

    def check_hex(self):
        if self.busy or self.job_id is not None or not self.require_client():
            return
        try:
            firmware, algorithm = self._selected_flash_files()
        except ValueError as exc:
            self.report_error(exc, "尚未选择完整")
            return
        client = self.client
        def done(value):
            self.show(value, self.job_text)
            self.show(value)
        self.submit(lambda: client.check_hex(firmware["path"], version=firmware["version"],
                                            flm_path=algorithm["path"], flm_version=algorithm["version"]),
                    done, label="检查 HEX 与 FLM")

    def flash(self):
        if self.busy or self.job_id is not None or not self.require_client():
            return
        client, endpoint = self.client, self.connected_endpoint
        # Recovery remains available even when the chooser has no current selection.
        def work():
            previous = self.flash_operations.current(client, endpoint)
            if previous and previous["state"] not in {"DONE", "FAILED", "CANCELLED", "INTERRUPTED", "REJECTED"}:
                return dict(job=self.flash_operations.query(client, endpoint), recovered=True)
            if selection_error is not None:
                raise selection_error
            return self.flash_operations.start(client, endpoint, firmware["path"],
                                                expected_version=firmware["version"],
                                                flm_path=algorithm["path"], flm_version=algorithm["version"])
        try:
            firmware, algorithm = self._selected_flash_files()
            selection_error = None
        except ValueError as exc:
            firmware = algorithm = None
            selection_error = exc
        def done(outcome):
            self.log_message("已确认上次烧录任务的状态，没有重复启动。" if outcome["recovered"] else
                             "已提交烧录 · %s · 算法 %s" % (firmware["name"], algorithm["name"]))
            self.job_updated(outcome["job"])
            if self.flash_operations.last_save_error:
                self.log_message("本地任务状态更新失败，重连后将重新查询设备。", "WARN")
        self.submit(work, done, label="开始烧录")

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
            ready = self.selected_hex is not None and self.selected_flm is not None
            self.flash_button.configure(state="normal" if ready and not self.busy and self.client is not None and self.job_id is None else "disabled")
        if hasattr(self, "flm_combo"):
            self.flm_combo.configure(state="readonly" if self.client is not None and not self.busy
                                     and self.job_id is None and self.flm_files else "disabled")
        self._update_preview_actions()

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
