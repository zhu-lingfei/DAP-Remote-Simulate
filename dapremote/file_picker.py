"""Explorer-style HEX/FLM chooser for a fixed device directory and PC drives."""
import os
from pathlib import Path, PurePosixPath
from tkinter import ttk
from .ui_dialogs import messagebox, center_window, workarea
import tkinter as tk

from .firmware import ARTIFACT_DIRS, ARTIFACT_EXTENSIONS, artifact_path, ensure_artifact_directory, import_artifact, UploadConflict
from .gui_text import size_text


def local_entries(directory, kind):
    entries = []
    with os.scandir(directory) as source:
        for item in source:
            try:
                is_dir = item.is_dir()
                if is_dir or Path(item.name).suffix.lower() in ARTIFACT_EXTENSIONS[kind]:
                    entries.append(dict(name=item.name, path=item.path, is_dir=is_dir,
                                        size=0 if is_dir else item.stat().st_size))
            except OSError:
                continue
    return sorted(entries, key=lambda entry: (not entry["is_dir"], entry["name"].casefold()))


def sd_entries(entries, kind):
    visible = []
    for entry in entries:
        path = entry["path"]
        root = ARTIFACT_DIRS[kind]
        if not path.startswith(root + "/") or ".." in path.split("/"):
            continue
        if entry["is_dir"] or PurePosixPath(path).suffix.lower() in ARTIFACT_EXTENSIONS[kind]:
            visible.append(entry)
    return sorted(visible, key=lambda entry: (not entry["is_dir"], entry["name"].casefold()))


def local_roots():
    roots = [("此电脑", Path.home())]
    if os.name == "nt":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots += [("本地磁盘 (%s:)" % chr(65 + index), Path(chr(65 + index) + ":/"))
                  for index in range(26) if mask & (1 << index)]
    else:
        roots.append(("文件系统", Path("/")))
    return roots


class ArtifactPicker:
    def __init__(self, app, kind, on_selected):
        self.app, self.kind, self.on_selected = app, kind, on_selected
        self.mode, self.directory = "sd", ARTIFACT_DIRS[kind]
        self.loading = False
        self.entries = {}
        self.history = []
        self.window = tk.Toplevel(app.root)
        self.window.title("选择 %s %s" % (kind.upper(), "固件" if kind == "hex" else "烧录算法"))
        bounds = workarea(app.root)
        width = min(940, bounds[2] - bounds[0] - 48)
        height = min(620, bounds[3] - bounds[1] - 80)
        center_window(self.window, app.root, width, height)
        self.window.minsize(min(720, width), min(420, height))
        self.window.transient(app.root)
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        bar = ttk.Frame(outer)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.back_button = ttk.Button(bar, text="← 返回", command=self.back)
        self.back_button.pack(side="left")
        self.up_button = ttk.Button(bar, text="↑ 上一级", command=self.up)
        self.up_button.pack(side="left", padx=5)
        ttk.Button(bar, text="刷新", command=lambda: self.navigate(self.mode, self.directory, remember=False)).pack(side="left")
        self.location = tk.StringVar()
        ttk.Label(outer, textvariable=self.location, width=1, anchor="w").grid(row=1, column=0, sticky="ew", pady=(0, 8))
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")
        sidebar, content = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(sidebar, weight=0)
        panes.add(content, weight=1)
        self.sidebar = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=12)
        self.sidebar.column("#0", width=170, minwidth=150)
        self.sidebar.pack(fill="both", expand=True, padx=(0, 10))
        self.sidebar.insert("", "end", iid="sd", text="设备 SD 卡", open=True)
        self.sidebar.insert("sd", "end", iid="library", text=kind.upper() + " 文件库")
        self.roots = {}
        for index, (name, path) in enumerate(local_roots()):
            key = "pc-%d" % index
            self.roots[key] = path
            self.sidebar.insert("" if index == 0 else "pc-0", "end", iid=key, text=name, open=True)
        self.sidebar.bind("<<TreeviewSelect>>", self.sidebar_selected)
        self.table = ttk.Treeview(content, columns=("name", "type", "size"), show="headings", selectmode="browse")
        for key, title, width in (("name", "名称", 310), ("type", "类型", 95), ("size", "大小", 90)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=60)
        self.table.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(content, command=self.table.yview)
        scroll.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scroll.set)
        self.table.bind("<Double-1>", self.choose)
        self.table.bind("<Return>", self.choose)
        self.table.bind("<<TreeviewSelect>>", self.selection_changed)
        self.notice = tk.StringVar(value="仅显示 %s 文件；选择电脑文件后自动上传。" % kind.upper())
        ttk.Label(outer, textvariable=self.notice, width=1, anchor="w", style="Muted.TLabel").grid(row=3, column=0, sticky="ew", pady=10)
        footer = self.footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.filename = tk.StringVar(value="请选择文件")
        ttk.Label(footer, textvariable=self.filename, width=1, anchor="w").grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.choose_button = ttk.Button(footer, text="选择文件", command=self.choose, style="Primary.TButton")
        self.choose_button.grid(row=0, column=2)
        ttk.Button(footer, text="取消", command=self.window.destroy).grid(row=0, column=1, padx=(0, 8))
        self.navigate("sd", ARTIFACT_DIRS[kind], remember=False)

    def exists(self):
        return bool(self.window.winfo_exists())

    def selection_changed(self, _=None):
        selected = self.table.selection()
        entry = self.entries.get(selected[0]) if selected else None
        self.filename.set(entry["name"] if entry else "请选择文件")
        self.choose_button.configure(text="打开文件夹" if entry and entry["is_dir"] else
                                     "上传并选用" if self.mode == "pc" else "选用文件")

    def sidebar_selected(self, _=None):
        selected = self.sidebar.selection()
        if not selected or self.loading:
            return
        key = selected[0]
        if key in ("sd", "library"):
            self.navigate("sd", ARTIFACT_DIRS[self.kind])
        elif key in self.roots:
            self.navigate("pc", self.roots[key])

    def back(self):
        if not self.loading and self.history:
            mode, directory = self.history.pop()
            self.navigate(mode, directory, remember=False)

    def up(self):
        if self.loading:
            return
        if self.mode == "sd":
            root = PurePosixPath(ARTIFACT_DIRS[self.kind])
            parent = PurePosixPath(self.directory).parent
            if parent == root or root in parent.parents:
                self.navigate("sd", str(parent))
        else:
            current = Path(self.directory)
            if current.parent != current:
                self.navigate("pc", current.parent)

    def navigate(self, mode, directory, remember=True):
        if self.loading or self.app.busy or not self.exists():
            return
        if mode == "sd":
            root = ARTIFACT_DIRS[self.kind]
            if str(directory) != root and not str(directory).startswith(root + "/"):
                return
            if not self.app.require_client():
                self.notice.set("SD 卡浏览需要连接设备；可从左侧浏览电脑文件。")
                return
        if remember and (mode, str(directory)) != (self.mode, str(self.directory)):
            self.history.append((self.mode, self.directory))
        self.mode, self.directory = mode, directory
        self.location.set(("设备 SD 卡 › " + str(directory).lstrip("/").replace("/", " › ")) if mode == "sd"
                          else "此电脑 › " + str(directory))
        self.table.delete(*self.table.get_children())
        self.entries.clear()
        self.filename.set("请选择文件")
        self.loading = True
        self.choose_button.configure(state="disabled")
        self.notice.set("正在读取文件列表…")
        client = self.app.client
        def work():
            if mode == "sd":
                ensure_artifact_directory(client, self.kind)
                return sd_entries(client.list_files(str(directory)), self.kind)
            return local_entries(directory, self.kind)
        def done(entries):
            if not self.exists():
                return
            self.loading = False
            for index, entry in enumerate(entries):
                key = str(index)
                self.entries[key] = entry
                self.table.insert("", "end", iid=key, values=(entry["name"], "文件夹" if entry["is_dir"] else self.kind.upper() + " 文件",
                                                            "—" if entry["is_dir"] else size_text(entry["size"])))
            self.notice.set("%d 项 · 仅显示 %s 文件；%s" % (len(entries), self.kind.upper(),
                            "双击文件选用" if mode == "sd" else "选择文件后自动上传到设备 SD 卡"))
            self.choose_button.configure(state="normal")
            self.back_button.configure(state="normal" if self.history else "disabled")
            self.up_button.configure(state="disabled" if mode == "sd" and str(directory) == ARTIFACT_DIRS[self.kind] else "normal")
        self.app.submit(work, done, label="浏览 %s 文件" % self.kind.upper(), error_handler=self.failed)

    def failed(self, exc):
        if self.exists():
            self.loading = False
            self.choose_button.configure(state="normal")
            self.notice.set("操作失败，当前选择保持不变；可重试。")
        if isinstance(exc, UploadConflict) and self.exists():
            if messagebox.askyesno("文件重名", "目标目录已存在“%s”，是否覆盖？" % Path(exc.path).name,
                                   parent=self.window, default="no"):
                self.choose(overwrite_version=exc.version, entry_override=self._upload_entry)
            else:
                self.notice.set("已取消上传，保留设备同名文件和当前选择。")
            return
        self.app.report_error(exc, "文件选择失败")

    def choose(self, _=None, overwrite_version=None, entry_override=None):
        if self.loading or self.app.busy or self.app.job_id is not None:
            return
        selected = self.table.selection()
        entry = entry_override if entry_override is not None else self.entries.get(selected[0]) if selected else None
        if not entry:
            return
        if entry["is_dir"]:
            self.navigate(self.mode, entry["path"])
            return
        if not self.app.require_client():
            self.notice.set("请先连接设备，才能上传并选用电脑文件。")
            return
        client = self.app.client
        self._upload_entry = dict(entry)
        self.loading = True
        self.choose_button.configure(state="disabled")
        self.notice.set("正在上传并校验，成功后自动选用…" if self.mode == "pc" else "正在确认 SD 文件…")
        mode = self.mode
        cache, progress = self.app._firmware_cache(), self.app._transfer_callback("上传 " + self.kind.upper())
        def work():
            if mode == "pc":
                return import_artifact(client, entry["path"], self.kind, cache, progress,
                                       collision="ask", overwrite_version=overwrite_version)
            info = client.stat(artifact_path(entry["path"], self.kind))
            if info["is_dir"] or info["version"] != entry["version"]:
                raise ValueError("文件已改变，请刷新列表后重新选择。")
            return info
        def done(info):
            if self.exists():
                self.window.destroy()
                self.on_selected(info)
        self.app.submit(work, done, label="上传并选用文件" if mode == "pc" else "选用 SD 文件", error_handler=self.failed)
