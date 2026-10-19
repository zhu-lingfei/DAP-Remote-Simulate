"""Flat, restrained Tk/ttk styling. No external GUI dependencies."""
from tkinter import ttk

CANVAS = "#F4F6FA"
SURFACE = "#FFFFFF"
INK = "#1E293B"
MUTED = "#64748B"
ACCENT = "#3563E9"
LINE = "#E5EAF2"


def configure_theme(root):
    root.option_add("*TCombobox*Listbox.font", ("Microsoft YaHei UI", 10))
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.selectBackground", "#EAF0FF")
    root.option_add("*TCombobox*Listbox.selectForeground", "#2349AA")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Microsoft YaHei UI", 10))
    style.configure("TFrame", background=SURFACE)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("TLabel", background=SURFACE, foreground=INK)
    style.configure("Card.TLabel", background=SURFACE)
    style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
    style.configure("Brand.TLabel", foreground="#172B4D", background=SURFACE, font=("Segoe UI", 19, "bold"))
    style.configure("Eyebrow.TLabel", foreground="#94A3B8", background=SURFACE, font=("Segoe UI", 8))
    style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"), foreground=INK)
    style.configure("Badge.TLabel", padding=(10, 5), background="#F0F3F8", foreground=MUTED,
                    font=("Microsoft YaHei UI", 9))
    style.configure("Connected.Badge.TLabel", background="#ECF8F2", foreground="#15803D")
    style.configure("TButton", padding=(12, 8), background="#EFF3F8", foreground="#42536B",
                    borderwidth=0, relief="flat", lightcolor="#EFF3F8", darkcolor="#EFF3F8",
                    focuscolor="#EFF3F8", focusthickness=0)
    style.map("TButton", background=[("disabled", "#F5F7FA"), ("pressed", "#DEE6F1"), ("active", "#E5EBF4")],
              foreground=[("disabled", "#A8B3C4")], relief=[("pressed", "flat")])
    style.configure("Primary.TButton", background=ACCENT, foreground=SURFACE, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map("Primary.TButton", background=[("disabled", "#DBE4F8"), ("pressed", "#244BC0"), ("active", "#2C55CE")],
              foreground=[("disabled", "#8CA3D2"), ("!disabled", SURFACE)])
    style.configure("Danger.TButton", background="#FFF0F0", foreground="#B91C1C", lightcolor="#FFF0F0", darkcolor="#FFF0F0")
    style.map("Danger.TButton", background=[("disabled", "#F5F7FA"), ("active", "#FEE2E2")],
              foreground=[("disabled", "#ADB6C5")])
    style.configure("TEntry", padding=(9, 7), relief="flat", borderwidth=1, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, fieldbackground="#FAFBFD", foreground=INK)
    style.map("TEntry", bordercolor=[("focus", "#93B1FF")], fieldbackground=[("disabled", "#F5F7FA")])
    style.configure("TCombobox", padding=(8, 6), relief="flat", borderwidth=1, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, fieldbackground="#FAFBFD", arrowcolor=MUTED,
                    selectbackground="#EAF0FF", selectforeground=INK)
    style.map("TCombobox", fieldbackground=[("disabled", "#F5F7FA"), ("readonly", "#FAFBFD")],
              foreground=[("disabled", "#94A3B8"), ("readonly", INK)], bordercolor=[("focus", "#93B1FF")])
    style.configure("TNotebook", background=CANVAS, borderwidth=0, bordercolor=CANVAS,
                    lightcolor=CANVAS, darkcolor=CANVAS, tabmargins=(0, 8, 0, 0))
    style.configure("TNotebook.Tab", padding=(24, 11), background=CANVAS, foreground=MUTED,
                    borderwidth=0, bordercolor=CANVAS, lightcolor=CANVAS, darkcolor=CANVAS, focuscolor=SURFACE)
    style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", "#EAF0FA")],
              foreground=[("selected", ACCENT)], lightcolor=[("selected", SURFACE)], darkcolor=[("selected", SURFACE)],
              bordercolor=[("selected", SURFACE), ("active", "#EAF0FA")])
    style.configure("Treeview", rowheight=35, background=SURFACE, fieldbackground=SURFACE, foreground="#334155",
                    borderwidth=0, relief="flat", bordercolor=LINE, lightcolor=SURFACE, darkcolor=SURFACE)
    style.configure("Treeview.Heading", padding=(12, 9), background="#F7F9FC", foreground=MUTED,
                    relief="flat", borderwidth=0, font=("Microsoft YaHei UI", 9, "bold"))
    style.map("Treeview.Heading", background=[("active", "#EFF3FA")], relief=[("active", "flat")])
    style.map("Treeview", background=[("selected", "#EAF0FF")], foreground=[("selected", "#2349AA")])
    style.configure("TCheckbutton", background=SURFACE, foreground=MUTED, padding=(3, 2))
    style.map("TCheckbutton", background=[("active", SURFACE)])
    style.configure("TSeparator", background=LINE)
    style.configure("TPanedwindow", background=LINE, sashwidth=6)
    style.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor="#EEF2F8",
                    borderwidth=0, bordercolor="#EEF2F8", lightcolor=ACCENT, darkcolor=ACCENT, thickness=7)
    for orientation in ("Vertical", "Horizontal"):
        name = orientation + ".TScrollbar"
        prefix = orientation + ".Scrollbar."
        style.layout(name, [(prefix + "trough", {"sticky": "nswe", "children": [
            (prefix + "thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure(name, background="#D2DAE6", troughcolor=SURFACE, borderwidth=0,
                        lightcolor="#D2DAE6", darkcolor="#D2DAE6", bordercolor=SURFACE, gripcount=0,
                        relief="flat", width=10, arrowsize=10)
        style.map(name, background=[("active", "#A6B4C8"), ("pressed", "#8EA0BA")])
    root.configure(background=CANVAS)
    return style
