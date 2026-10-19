"""Place Tk and Windows native dialogs on the owner's monitor, without global hooks."""
import contextlib
import ctypes
import os
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog as _files, messagebox as _messages, simpledialog as _inputs


def centered_rect(bounds, width, height):
    left, top, right, bottom = bounds
    width = max(1, min(int(width), right - left))
    height = max(1, min(int(height), bottom - top))
    return width, height, left + (right - left - width) // 2, top + (bottom - top - height) // 2


def _windows_api():
    from ctypes import wintypes as w
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetAncestor.argtypes, user.GetAncestor.restype = [w.HWND, w.UINT], w.HWND
    user.MonitorFromWindow.argtypes, user.MonitorFromWindow.restype = [w.HWND, w.DWORD], w.HANDLE
    user.GetMonitorInfoW.argtypes, user.GetMonitorInfoW.restype = [w.HANDLE, ctypes.c_void_p], w.BOOL
    user.GetWindowRect.argtypes, user.GetWindowRect.restype = [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL
    user.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
    user.SetWindowPos.restype = w.BOOL
    return user


def workarea(owner):
    if os.name == "nt":
        from ctypes import wintypes as w
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("flags", w.DWORD)]
        user = _windows_api()
        hwnd = user.GetAncestor(owner.winfo_id(), 2) or owner.winfo_id()
        monitor = user.MonitorFromWindow(hwnd, 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            return rect.left, rect.top, rect.right, rect.bottom
    x, y = owner.winfo_vrootx(), owner.winfo_vrooty()
    return x, y, x + owner.winfo_vrootwidth(), y + owner.winfo_vrootheight()


def center_window(window, owner=None, width=None, height=None):
    """Set a Tk window's client geometry before it is first shown."""
    owner = owner or window
    if width is None or height is None:
        window.update_idletasks()
    width = width if width is not None else window.winfo_reqwidth()
    height = height if height is not None else window.winfo_reqheight()
    width, height, x, y = centered_rect(workarea(owner), width, height)
    # +negative is an absolute virtual-desktop coordinate in Tk's wm geometry.
    window.geometry("%dx%d+%d+%d" % (width, height, x, y))
    return width, height, x, y


@contextlib.contextmanager
def native_dialog_centering(parent, on_center=None):
    """A current-thread CBT hook, installed only while our own modal dialog exists.

    Centers owned native file/message dialogs and Tk input dialogs at activation.
    Does not intercept other processes or change other windows. Always unhooks.
    on_center is an optional integration-test observer.
    """
    if os.name != "nt" or parent is None:
        yield
        return
    from ctypes import wintypes as w
    user = _windows_api()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThreadId.restype = w.DWORD
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
    user.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_type, w.HINSTANCE, w.DWORD]
    user.SetWindowsHookExW.restype = w.HANDLE
    user.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
    user.CallNextHookEx.restype = ctypes.c_ssize_t
    user.UnhookWindowsHookEx.argtypes, user.UnhookWindowsHookEx.restype = [w.HANDLE], w.BOOL
    user.GetWindow.argtypes, user.GetWindow.restype = [w.HWND, w.UINT], w.HWND
    user.GetClassNameW.argtypes, user.GetClassNameW.restype = [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int
    owner = user.GetAncestor(parent.winfo_id(), 2) or parent.winfo_id()
    bounds = workarea(parent)
    centered = set()
    hook = None
    @proc_type
    def callback(code, hwnd, data):
        try:
            if code == 5 and hwnd != owner and hwnd not in centered:  # HCBT_ACTIVATE
                name = ctypes.create_unicode_buffer(128)
                user.GetClassNameW(hwnd, name, len(name))
                ancestor = user.GetWindow(hwnd, 4)  # GW_OWNER
                for _ in range(16):
                    if not ancestor or ancestor == owner:
                        break
                    ancestor = user.GetWindow(ancestor, 4)
                if ancestor == owner and name.value in ("#32770", "TkTopLevel"):
                    rect = w.RECT()
                    if user.GetWindowRect(hwnd, ctypes.byref(rect)):
                        width, height, x, y = centered_rect(bounds, rect.right - rect.left, rect.bottom - rect.top)
                        user.SetWindowPos(hwnd, None, x, y, width, height, 0x0014)  # NOZORDER | NOACTIVATE
                        centered.add(hwnd)
                        if on_center:
                            on_center(hwnd, bounds)
        except Exception:
            # Positioning must never prevent a dialog from opening or being dismissed.
            pass
        return user.CallNextHookEx(hook, code, hwnd, data)
    hook = user.SetWindowsHookExW(5, callback, None, kernel.GetCurrentThreadId())
    try:
        yield
    finally:
        if hook:
            user.UnhookWindowsHookEx(hook)


def _centered(function):
    def show(*args, **kwargs):
        parent = kwargs.get("parent") or tk._default_root
        if parent is not None:
            kwargs["parent"] = parent
        with native_dialog_centering(parent):
            return function(*args, **kwargs)
    return show


# Keep normal tkinter calling conventions, so existing GUI handlers/tests stay simple.
filedialog = SimpleNamespace(askopenfilename=_centered(_files.askopenfilename),
                             asksaveasfilename=_centered(_files.asksaveasfilename))
messagebox = SimpleNamespace(askyesno=_centered(_messages.askyesno), showerror=_centered(_messages.showerror))
simpledialog = SimpleNamespace(askstring=_centered(_inputs.askstring))
