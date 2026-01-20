# -*- coding: utf-8 -*-
"""
we_tray.py（Win32 托盘 · 登录完善最终版 r16）
- 逐字符读取 + 静默间隙推断
- 等待手机批准：Tk toast（非阻塞），在“登录成功/进入2FA”时自动销毁
- 登录成功 toast：显示 5s 后自动关闭，然后再重启 worker（严格顺序）
- [修复] 重启 worker 前 Reset 退出事件（否则新 worker 立即退出）
- [防抖] 登录流程并发保护，避免重复触发
- [稳健] “立即更换一次”改为脉冲触发（Set→短暂停→Reset）
- 行级写回 config 的 [auth].steam_username（保留注释/格式）
- PyInstaller 单文件：优先加载 EXE 内嵌图标，其次 MEIPASS/同目录
- 隐藏 steamcmd 窗口（CREATE_NO_WINDOW + 兜底按 PID 隐藏顶层窗）

r16 变更（彻底移除“TaskbarCreated 重建”）：
- [移除] 不再注册/处理 TaskbarCreated 广播，不再在任务栏重启时重建托盘图标。
- [保持] 仅启动时执行 NIM_ADD，电源/会话消息仅执行 NIM_MODIFY（刷新），不会触发重建。
- [保持] NIM_SETVERSION 失败不影响 added 状态。
"""

from __future__ import annotations
import os, sys, ctypes, threading, subprocess, time, queue, hashlib, gc, configparser, locale
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from collections import deque
from uuid import UUID
from typing import Optional

# ------------------ 配置 ------------------
WORKER_SCRIPT = "we_auto_fetch.py"
WORKER_ARGS: list[str] = []
MAX_BUFFER_LINES = 5000

STEAMCMD_LOGIN_TIMEOUT_S = 45.0
MOBILE_CONFIRM_MAX_WAIT_S = 60.0
MOBILE_GAP_DETECT_S = 6.0

SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT_ABS = (SCRIPT_DIR / WORKER_SCRIPT).resolve()

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32  = ctypes.windll.shell32
wtsapi32 = ctypes.windll.wtsapi32
credui   = ctypes.windll.credui

# ---- 兼容 ----
HANDLE    = wintypes.HANDLE
HWND      = getattr(wintypes, "HWND", HANDLE)
HICON     = getattr(wintypes, "HICON", HANDLE)
HCURSOR   = getattr(wintypes, "HCURSOR", HANDLE)
HBRUSH    = getattr(wintypes, "HBRUSH", HANDLE)
HINSTANCE = getattr(wintypes, "HINSTANCE", HANDLE)
HMENU     = getattr(wintypes, "HMENU", HANDLE)
HBITMAP   = getattr(wintypes, "HBITMAP", HANDLE)

PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_longlong if PTR_SIZE == 8 else ctypes.c_long)
WPARAM  = getattr(wintypes, "WPARAM",  ctypes.c_size_t)
LPARAM  = getattr(wintypes, "LPARAM",  ctypes.c_ssize_t)

def _errcheck_bool(result, func, args):
    if not result: raise ctypes.WinError()
    return result

# ------------- 常量/消息 -------------
WM_USER               = 0x0400
WM_TRAYICON           = WM_USER + 1
WM_NULL               = 0x0000
WM_DESTROY            = 0x0002
WM_CLOSE              = 0x0010
WM_COMMAND            = 0x0111
WM_CONTEXTMENU        = 0x007B
WM_POWERBROADCAST     = 0x0218
PBT_APMRESUMEAUTOMATIC= 0x0012
PBT_APMRESUMESUSPEND  = 0x0007
WM_WTSSESSION_CHANGE  = 0x02B1
WTS_SESSION_LOGON     = 0x0005
WTS_SESSION_UNLOCK    = 0x0008
NOTIFY_FOR_THIS_SESSION = 0
WM_APP_LOGIN          = WM_USER + 100

# 鼠标
WM_LBUTTONUP     = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP     = 0x0205

# 托盘
NIM_ADD       = 0x00000000
NIM_MODIFY    = 0x00000001
NIM_DELETE    = 0x00000002
NIM_SETVERSION= 0x00000004
NIF_MESSAGE   = 0x00000001
NIF_ICON      = 0x00000002
NIF_TIP       = 0x00000004
NIF_GUID      = 0x00000020
NIF_SHOWTIP   = 0x00000080
NOTIFYICON_VERSION_4 = 4

# 菜单
MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD   = 0x0100

IDM_LOGIN           = 1000
IDM_TOGGLE_CONSOLE  = 1001
IDM_FORCE_SWITCH    = 1002
IDM_EXCLUDE_CREATOR = 1005
IDM_TOGGLE_AUTOSTART= 1003
IDM_EXIT            = 1004

# 类样式
CS_VREDRAW  = 0x0001
CS_HREDRAW  = 0x0002
CS_DBLCLKS  = 0x0008

# 等待/事件
WAIT_OBJECT_0 = 0x00000000
SYNCHRONIZE   = 0x00100000
EVENT_MODIFY_STATE = 0x0002

# Job
PROCESS_TERMINATE   = 0x0001
PROCESS_SET_QUOTA   = 0x0100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation  = 9

# --------- Shell / Win32 原型 ----------
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, WPARAM, LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style",        wintypes.UINT),
        ("lpfnWndProc",  WNDPROCTYPE),
        ("cbClsExtra",   ctypes.c_int),
        ("cbWndExtra",   ctypes.c_int),
        ("hInstance",    HINSTANCE),
        ("hIcon",        HICON),
        ("hCursor",      HCURSOR),
        ("hbrBackground",HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName",wintypes.LPCWSTR),
    ]

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]
    @classmethod
    def from_uuid(cls, u: UUID):
        data = u.bytes_le
        d1 = int.from_bytes(data[0:4], "little")
        d2 = int.from_bytes(data[4:6], "little")
        d3 = int.from_bytes(data[6:8], "little")
        d4 = (ctypes.c_ubyte * 8).from_buffer_copy(data[8:16])
        return cls(d1, d2, d3, d4)

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wintypes.DWORD),
        ("hWnd",          HWND),
        ("uID",           wintypes.UINT),
        ("uFlags",        wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon",         HICON),
        ("szTip",         ctypes.c_wchar * 128),
        ("dwState",       wintypes.DWORD),
        ("dwStateMask",   wintypes.DWORD),
        ("szInfo",        ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle",   ctypes.c_wchar * 64),
        ("dwInfoFlags",   wintypes.DWORD),
        ("guidItem",      GUID),
        ("hBalloonIcon",  HICON),
    ]

def MAKEINTRESOURCE(i: int):
    return ctypes.cast(ctypes.c_void_p(i), wintypes.LPCWSTR)

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype  = wintypes.BOOL

user32.RegisterClassW.errcheck   = _errcheck_bool
user32.RegisterClassW.argtypes   = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.errcheck  = _errcheck_bool
user32.CreateWindowExW.argtypes  = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, HMENU, HINSTANCE, wintypes.LPVOID
]
user32.DestroyWindow.argtypes    = [HWND]
user32.LoadIconW.argtypes        = [HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype         = HICON
user32.CreatePopupMenu.restype   = HMENU
user32.AppendMenuW.restype       = wintypes.BOOL
user32.TrackPopupMenu.restype    = wintypes.UINT
user32.GetCursorPos.argtypes     = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [HWND]
user32.DestroyMenu.argtypes      = [HMENU]
user32.DefWindowProcW.argtypes   = [HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype    = LRESULT
user32.PostMessageW.argtypes     = [HWND, wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype      = wintypes.BOOL
user32.GetMessageW.argtypes      = [ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype       = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype  = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype  = LRESULT

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype  = HINSTANCE

wtsapi32.WTSRegisterSessionNotification.argtypes   = [HWND, wintypes.DWORD]
wtsapi32.WTSUnRegisterSessionNotification.argtypes = [HWND]

# ---- 枚举/隐藏窗口（兜底用）----
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype  = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype  = wintypes.DWORD
user32.IsWindowVisible.argtypes = [HWND]
user32.IsWindowVisible.restype  = wintypes.BOOL
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.ShowWindow.restype  = wintypes.BOOL
SW_HIDE = 0

# --------- CredUI ----------
class CREDUI_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",       wintypes.DWORD),
        ("hwndParent",   HWND),
        ("pszMessageText", wintypes.LPCWSTR),
        ("pszCaptionText", wintypes.LPCWSTR),
        ("hbmBanner",    HBITMAP),
    ]

CREDUI_FLAGS_ALWAYS_SHOW_UI      = 0x80
CREDUI_FLAGS_GENERIC_CREDENTIALS = 0x40000
CREDUI_FLAGS_DO_NOT_PERSIST      = 0x02

credui.CredUIPromptForCredentialsW.argtypes = [
    ctypes.POINTER(CREDUI_INFO), wintypes.LPCWSTR, ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.LPWSTR, wintypes.ULONG,
    wintypes.LPWSTR, wintypes.ULONG,
    ctypes.POINTER(wintypes.BOOL), wintypes.DWORD
]
credui.CredUIPromptForCredentialsW.restype = wintypes.DWORD

ERROR_CANCELLED       = 1223
NO_ERROR              = 0

# --------- MessageBox 常量 ----------
MB_ICONINFORMATION    = 0x40
MB_ICONERROR          = 0x10
MB_OK                 = 0x00000000

# --------- ResetEvent 绑定 ----------
kernel32.ResetEvent.argtypes = [HANDLE]
kernel32.ResetEvent.restype  = wintypes.BOOL

# ----------------- 单实例 -----------------
class SingleInstance:
    def __init__(self, name: str):
        self.mutex = kernel32.CreateMutexW(None, False, f"Global\\{name}")
        self.already_running = (ctypes.GetLastError() == 183)
    def __del__(self):
        if getattr(self, "mutex", None):
            kernel32.CloseHandle(self.mutex)

# ----------------- 开机自启 -----------------
def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    else:
        script = Path(__file__).resolve()
        return f'"{sys.executable}" -u "{script}"'

def set_autostart(enable: bool, app_name="WEAutoTray"):
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    cmd = _autostart_command()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
        if enable:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        else:
            try: winreg.DeleteValue(key, app_name)
            except FileNotFoundError: pass

def is_autostart_enabled(app_name="WEAutoTray"):
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            _ = winreg.QueryValueEx(key, app_name)
            return True
    except FileNotFoundError:
        return False

# ----------------- 命名事件 -----------------
def _exit_event_name() -> str:
    try:
        base = str(Path(sys.executable).resolve())
    except Exception:
        base = sys.argv[0]
    h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"Global\\WEAutoTrayExit_{h}"

def _run_now_event_name() -> str:
    try:
        base = str(Path(sys.executable).resolve())
    except Exception:
        base = sys.argv[0]
    h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"Global\\WEAutoTrayRunNow_{h}"

def _create_named_event_manual_reset(name: str, initial: bool=False):
    return kernel32.CreateEventW(None, True, bool(initial), name)

def _open_named_event(name: str):
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    return kernel32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)

def _set_event(h) -> None:
    try: kernel32.SetEvent(h)
    except Exception: pass

def _reset_event(h) -> None:
    try: kernel32.ResetEvent(h)
    except Exception: pass

def _pulse_event(h, duration_s: float = 0.08):
    """手动复位事件的脉冲触发：Set → 短暂停 → Reset。"""
    _set_event(h)
    try: time.sleep(max(0.02, duration_s))
    finally: _reset_event(h)

# ----------------- Job(Kill-on-close) -----------------
class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit",     wintypes.LARGE_INTEGER),
        ("LimitFlags",              wintypes.DWORD),
        ("MinimumWorkingSetSize",   ctypes.c_size_t),
        ("MaximumWorkingSetSize",   ctypes.c_size_t),
        ("ActiveProcessLimit",      wintypes.DWORD),
        ("Affinity",                ctypes.c_size_t),
        ("PriorityClass",           wintypes.DWORD),
        ("SchedulingClass",         wintypes.DWORD),
    ]

class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount",   ctypes.c_ulonglong),
        ("WriteOperationCount",  ctypes.c_ulonglong),
        ("OtherOperationCount",  ctypes.c_ulonglong),
        ("ReadTransferCount",    ctypes.c_ulonglong),
        ("WriteTransferCount",   ctypes.c_ulonglong),
        ("OtherTransferCount",   ctypes.c_ulonglong),
    ]

class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo",                IO_COUNTERS),
        ("ProcessMemoryLimit",    ctypes.c_size_t),
        ("JobMemoryLimit",        ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed",     ctypes.c_size_t),
    ]

def _create_kill_on_close_job() -> int:
    try:
        hjob = kernel32.CreateJobObjectW(None, None)
        if not hjob: return 0
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            hjob, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(hjob); return 0
        return hjob
    except Exception:
        return 0

def _assign_pid_to_job(job_handle: int, pid: int) -> bool:
    if not job_handle or not pid: return False
    hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not hproc: return False
    try:
        ok = kernel32.AssignProcessToJobObject(job_handle, hproc)
        return bool(ok)
    finally:
        kernel32.CloseHandle(hproc)

# ----------------- Tk 实时控制台 + toast -----------------
class ConsoleWindow:
    def __init__(self, title="WE - 实时控制台"):
        import tkinter as tk
        self._tk = tk
        self.root = None
        self.text = None
        self._thread = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._buffer = deque(maxlen=MAX_BUFFER_LINES)
        self._ready = threading.Event()
        self._visible = False
        self._stop_called = False
        self.title = title
        self._toasts = {}  # key -> Toplevel

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self):
        from tkinter.scrolledtext import ScrolledText
        tk = self._tk
        self.root = tk.Tk()
        self.root.title(self.title)
        self.root.geometry("900x520")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.text = ScrolledText(self.root, wrap="none", state="disabled")
        self.text.pack(fill="both", expand=True)
        self._ready.set()

        def poll():
            try:
                while True:
                    line = self._queue.get_nowait()
                    self._buffer.append(line)
                    self.text.configure(state="normal")
                    self.text.insert("end", line)
                    self.text.see("end")
                    self.text.configure(state="disabled")
            except queue.Empty:
                pass
            finally:
                self.root.after(100, poll)
        poll()
        self.root.withdraw()
        self.root.mainloop()

    def append(self, s: str):
        if not self._stop_called:
            self._queue.put(s if s.endswith("\n") else s + "\n")

    def show(self):
        self.start()
        if self.root:
            self.root.deiconify()
            self._visible = True
            if self.text and float(self.text.index("end-1c").split(".")[0]) <= 1:
                self.text.configure(state="normal")
                self.text.delete("1.0", "end")
                self.text.insert("end", "".join(self._buffer))
                self.text.see("end")
                self.text.configure(state="disabled")

    def hide(self):
        if self.root:
            self.root.withdraw()
            self._visible = False

    def toggle(self):
        (self.show() if not self._visible else self.hide())

    def stop(self, join_timeout: float = 2.0):
        self._stop_called = True
        if self.root:
            try: self.root.after(0, self.root.quit)
            except Exception: pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

    # -------- toast：可控、可自毁 --------
    def show_toast(self, key: str, title: str, text: str, timeout_ms: Optional[int] = None):
        def _create():
            if key in self._toasts:
                try: self._toasts[key].destroy()
                except Exception: pass
            win = self._tk.Toplevel(self.root)
            win.title(title)
            try:
                win.overrideredirect(True)
            except Exception:
                pass
            try:
                win.attributes("-topmost", True)
            except Exception:
                pass
            frm = self._tk.Frame(win, bd=1, relief="solid")
            frm.pack(fill="both", expand=True)
            lbl_title = self._tk.Label(frm, text=title, font=("Segoe UI", 10, "bold"))
            lbl_title.pack(anchor="w", padx=12, pady=(10, 2))
            lbl_text = self._tk.Label(frm, text=text, wraplength=360, justify="left")
            lbl_text.pack(anchor="w", padx=12, pady=(0, 10))
            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            x = max(0, sw - w - 16)
            y = max(0, sh - h - 48)
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.bind("<Button-1>", lambda e: self.close_toast(key))
            self._toasts[key] = win
            if timeout_ms and timeout_ms > 0:
                win.after(timeout_ms, lambda: self.close_toast(key))
        if self.root:
            self.root.after(0, _create)

    def close_toast(self, key: str):
        def _close():
            win = self._toasts.pop(key, None)
            if win:
                try: win.destroy()
                except Exception: pass
        if self.root:
            self.root.after(0, _close)

# ----------------- worker 进程 -----------------
@dataclass
class WorkerProc:
    proc: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None

def _win_hidden_popen_kwargs():
    if os.name != "nt": return {}
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}
    except Exception:
        return {"creationflags": CREATE_NO_WINDOW}

def _hide_top_windows_by_pid(pid: int, duration_s: float = 3.0, poll_interval_s: float = 0.1):
    if not pid: return
    end_ts = time.time() + max(0.1, duration_s)
    @WNDENUMPROC
    def _enum_proc(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            dw_pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(dw_pid))
            if dw_pid.value == pid:
                try: user32.ShowWindow(hwnd, SW_HIDE)
                except Exception: pass
        except Exception:
            pass
        return True
    while time.time() < end_ts:
        try: user32.EnumWindows(_enum_proc, 0)
        except Exception: pass
        time.sleep(poll_interval_s)

def start_worker_and_reader(console: ConsoleWindow, job_handle: int | None = None) -> WorkerProc:
    exe = sys.executable
    if getattr(sys, "frozen", False):
        cmd = [exe, "--worker", *WORKER_ARGS]
    else:
        cmd = [exe, "-u", str(WORKER_SCRIPT_ABS), *WORKER_ARGS]
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        close_fds=True,
        cwd=str(SCRIPT_DIR),
        **_win_hidden_popen_kwargs()
    )
    if job_handle:
        try: _assign_pid_to_job(job_handle, p.pid)
        except Exception: pass

    def reader():
        assert p.stdout is not None
        for line in p.stdout:
            console.append(line)
        try:
            rest = p.stdout.read()
            if rest: console.append(rest)
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return WorkerProc(proc=p, reader_thread=t)

def stop_worker(wp: WorkerProc, timeout=5.0):
    if not wp or not wp.proc: return
    try:
        wp.proc.terminate()
        try:
            wp.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            wp.proc.kill()
            try: wp.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired: pass
    except Exception:
        pass
    try:
        if wp.reader_thread and wp.reader_thread.is_alive():
            wp.reader_thread.join(timeout=2.0)
    except Exception:
        pass

# ----------------- worker 内的退出监听 -----------------
class _SafeStream:
    def __init__(self, base): self._b = base
    def write(self, s):
        try: return self._b.write(s)
        except OSError: return 0
    def flush(self):
        try: return self._b.flush()
        except OSError: return None
    def writelines(self, lines):
        try: return self._b.writelines(lines)
        except OSError: return None
    def __getattr__(self, name): return getattr(self._b, name)

def _start_worker_exit_watcher_thread():
    name = _exit_event_name()
    h = _open_named_event(name) or _create_named_event_manual_reset(name, initial=False)
    if not h: return
    def _wait_and_die():
        try:
            while True:
                rc = kernel32.WaitForSingleObject(h, 1000)
                if rc == WAIT_OBJECT_0:
                    os._exit(0)
        except Exception:
            os._exit(0)
    threading.Thread(target=_wait_and_die, daemon=True).start()

def run_worker_inline():
    sys.stdout = _SafeStream(sys.stdout)
    sys.stderr = _SafeStream(sys.stderr)
    try: sys.stdout.reconfigure(line_buffering=True)
    except Exception: pass
    _start_worker_exit_watcher_thread()
    base = Path(sys.argv[0]).resolve().parent
    sys.path.insert(0, str(base))
    try:
        import we_auto_fetch
    except Exception as e:
        try: print("[fatal] 无法导入 we_auto_fetch.py：", e)
        except Exception: pass
        sys.exit(1)
    try:
        we_auto_fetch.main()
    except KeyboardInterrupt:
        try: print("\n[worker] 收到中断，退出。")
        except Exception: pass
    except Exception as e:
        try: print("[worker] 未捕获异常：", e)
        except Exception: pass
        time.sleep(0.5)

# ----------------- ini 写入（保留注释/格式） -----------------
def _ini_set_key_preserve_comments(path: Path, section: str, key: str, value: str):
    section_l = section.strip().lower()
    key_l = key.strip().lower()

    orig = ""
    if path.exists():
        orig = path.read_text(encoding="utf-8", errors="ignore")
    newline = "\r\n" if "\r\n" in orig else "\n"
    lines = orig.splitlines()

    if not lines:
        text = [
            "# Auto-generated config (comments preserved on future updates)",
            f"[{section}]",
            f"{key} = {value}",
            ""
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(newline.join(text))
        return

    sec_start, sec_end = None, None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            name = s[1:-1].strip().lower()
            if name == section_l:
                sec_start = i
                for j in range(i + 1, len(lines)):
                    s2 = lines[j].strip()
                    if s2.startswith("[") and s2.endswith("]"):
                        sec_end = j
                        break
                if sec_end is None:
                    sec_end = len(lines)
                break

    if sec_start is None:
        insert = [f"[{section}]", f"{key} = {value}"]
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend(insert)
        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(newline.join(lines) + newline)
        return

    key_line_idx = None
    for i in range(sec_start + 1, sec_end):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip().lower()
            if k == key_l:
                key_line_idx = i
                break

    if key_line_idx is not None:
        leading_ws = lines[key_line_idx][:len(lines[key_line_idx]) - len(lines[key_line_idx].lstrip())]
        lines[key_line_idx] = f"{leading_ws}{key} = {value}"
    else:
        lines.insert(sec_end, f"{key} = {value}")

    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(newline.join(lines) + newline)

def _parse_steamid64_list(s: str) -> list[str]:
    import re
    pat = re.compile(r"(?<!\d)(\d{17})(?!\d)")
    out: list[str] = []
    seen = set()
    for token in (s or "").split(","):
        token = token.strip()
        if not token:
            continue
        for m in pat.finditer(token):
            sid = m.group(1)
            if sid not in seen:
                seen.add(sid); out.append(sid)
    return out

# ----------------- 原生 Win32 托盘 APP -----------------
class Win32TrayApp:
    def __init__(self):
        self.console = ConsoleWindow("Wallpaper Engine - 实时控制台")
        self.console.start()
        self._exit_evt_name = _exit_event_name()
        self._exit_evt = _create_named_event_manual_reset(self._exit_evt_name, initial=False)

        self._run_now_evt_name = _run_now_event_name()
        self._run_now_evt = _open_named_event(self._run_now_evt_name) or _create_named_event_manual_reset(self._run_now_evt_name, initial=False)

        self._job = _create_kill_on_close_job()
        self.wp = start_worker_and_reader(self.console, job_handle=self._job)

        self.hwnd = None
        self.hicon = None
        self.added = False
        self.tray_guid = self._make_guid_from_exe()

        self.class_name = "WEAutoTrayWin32HiddenWindow"
        self.tip_text = "WE Auto Runner - 正在运行"
        self._wndproc = None

        # 手机确认 toast 控制
        self._mobile_prompt_shown = False
        self._mobile_prompt_lock = threading.Lock()

        # 登录流程防抖
        self._login_active = False
        self._login_lock = threading.Lock()

        user32.LoadImageW.argtypes = [HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = HANDLE

    # ---------- Utilities ----------
    def _make_guid_from_exe(self) -> GUID:
        try:
            base = str(Path(sys.executable).resolve())
        except Exception:
            base = sys.argv[0]
        h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()
        u = UUID(h[:32])
        return GUID.from_uuid(u)

    def _notify(self, msg, data: NOTIFYICONDATAW):
        return bool(shell32.Shell_NotifyIconW(msg, ctypes.byref(data)))

    def _build_nid(self, flags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_GUID, hicon=None) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 0
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon or self.hicon
        nid.szTip = self.tip_text
        nid.guidItem = self.tray_guid
        return nid

    # 仅用于 NIM_SETVERSION
    def _build_nid_for_setver(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 0
        nid.uFlags = 0
        nid.guidItem = self.tray_guid
        nid.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        return nid

    def _load_icon(self):
        IMAGE_ICON      = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE  = 0x00000040
        # 1) EXE 内嵌
        try:
            hinst = kernel32.GetModuleHandleW(None)
            for resid in (1, 101):
                try:
                    h = user32.LoadImageW(hinst, MAKEINTRESOURCE(resid), IMAGE_ICON, 0, 0, LR_DEFAULTSIZE)
                    if h:
                        self.console.append(f"[tray] 已加载嵌入式 EXE 图标（资源ID={resid}）。")
                        return HICON(h)
                except Exception:
                    pass
        except Exception:
            pass
        # 2) MEIPASS
        try:
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass:
                for name in ("app.ico", "we.ico", "tray.ico"):
                    p = Path(meipass) / name
                    if p.exists():
                        h = user32.LoadImageW(None, str(p), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                        if h:
                            self.console.append(f"[tray] 已从 MEIPASS 加载图标：{p.name}")
                            return HICON(h)
        except Exception:
            pass
        # 3) EXE 同目录 / 脚本目录
        for base in (Path(sys.executable).parent, Path(__file__).resolve().parent):
            for name in ("app.ico", "we.ico", "tray.ico"):
                p = base / name
                if p.exists():
                    try:
                        h = user32.LoadImageW(None, str(p), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                        if h:
                            self.console.append(f"[tray] 已加载图标文件：{p}")
                            return HICON(h)
                    except Exception:
                        pass
        # 4) 默认（Win32 原生）
        IDI_APPLICATION = 32512
        self.console.append("[tray] 未找到自定义图标，使用系统默认图标。")
        return user32.LoadIconW(None, MAKEINTRESOURCE(IDI_APPLICATION))

    def _set_tray_version(self):
        nid_ver = self._build_nid_for_setver()
        ok_ver = self._notify(NIM_SETVERSION, nid_ver)
        self.console.append(f"[tray] 设定托盘协议版本：{'成功' if ok_ver else '失败（忽略）'}。")
        return ok_ver

    def _add_icon(self):
        if not self.hwnd: return
        if not self.hicon: self.hicon = self._load_icon()
        nid = self._build_nid()
        ok_add = self._notify(NIM_ADD, nid)

        if ok_add:
            self.added = True
            self._set_tray_version()
        else:
            self.added = False

        self.console.append(f"[tray] 添加托盘图标：{'成功' if ok_add else '失败'}（added={self.added}）。")

    def _modify_icon(self):
        if not self.added:
            self.console.append("[tray] 跳过刷新：图标未标记为已添加。")
            return
        nid = self._build_nid()
        ok = self._notify(NIM_MODIFY, nid)
        self.console.append(f"[tray] 刷新托盘图标：{'成功' if ok else '失败（不重建）'}。")

    def _delete_icon(self):
        if not self.added: return
        nid = self._build_nid(flags=NIF_GUID)  # 仅凭 GUID 删除
        try:
            ok = self._notify(NIM_DELETE, nid)
            self.console.append(f"[tray] 托盘图标已删除：{'成功' if ok else '失败'}。")
        except Exception:
            pass
        self.added = False

    # ---------- 右键菜单 ----------
    def _show_context_menu(self):
        hMenu = user32.CreatePopupMenu()
        autostart_txt = "关闭开机自启" if is_autostart_enabled() else "开启开机自启"
        user32.AppendMenuW(hMenu, MF_STRING, IDM_LOGIN, "登录账号...")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_TOGGLE_CONSOLE, "打开/隐藏 控制台")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_FORCE_SWITCH, "立即更换一次")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_EXCLUDE_CREATOR, "排除当前壁纸上传者并立即切换")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_TOGGLE_AUTOSTART, autostart_txt)
        user32.AppendMenuW(hMenu, MF_STRING, IDM_EXIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hMenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        if cmd:
            user32.PostMessageW(self.hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(hMenu)

    # ---------- 简易 MsgBox（仅报错） ----------
    def _msgbox(self, title: str, text: str, flags: int) -> int:
        return user32.MessageBoxW(self.hwnd, text, title, flags)

    def _msg_error(self, title: str, text: str):
        self._msgbox(title, text, MB_ICONERROR | MB_OK)

    # ---------- CredUI（账号/密码/验证码输入） ----------
    def _cred_prompt(self, caption: str, message: str, target: str,
                     default_user: str = "") -> Optional[tuple[str, str]]:
        ui = CREDUI_INFO()
        ui.cbSize = ctypes.sizeof(CREDUI_INFO)
        ui.hwndParent = self.hwnd
        ui.pszMessageText = message
        ui.pszCaptionText = caption
        ui.hbmBanner = None

        user_buf = ctypes.create_unicode_buffer(default_user or "", 256)
        pass_buf = ctypes.create_unicode_buffer(256)
        save = wintypes.BOOL(False)
        flags = (CREDUI_FLAGS_ALWAYS_SHOW_UI |
                 CREDUI_FLAGS_GENERIC_CREDENTIALS |
                 CREDUI_FLAGS_DO_NOT_PERSIST)

        rc = credui.CredUIPromptForCredentialsW(
            ctypes.byref(ui), target, None, 0,
            user_buf, ctypes.sizeof(user_buf) // ctypes.sizeof(wintypes.WCHAR),
            pass_buf, ctypes.sizeof(pass_buf) // ctypes.sizeof(wintypes.WCHAR),
            ctypes.byref(save), flags
        )
        if rc == ERROR_CANCELLED:
            return None
        if rc != NO_ERROR:
            try:
                raise ctypes.WinError(rc)
            except Exception as e:
                self.console.append(f"[login] CredUI 错误：{e}")
                self._msg_error("登录", f"无法显示凭据对话框：{e}")
            return None
        return (user_buf.value, pass_buf.value)

    # ---------- 配置 ----------
    def _config_candidates(self):
        names = ("config", "config.ini")
        out = []
        env_p = os.environ.get("WE_CONFIG") or os.environ.get("WE_CONF")
        if env_p: out.append(Path(env_p))
        base = SCRIPT_DIR
        out += [base / n for n in names]
        cwd = Path.cwd()
        if cwd != base:
            out += [cwd / n for n in names]
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            m = Path(mei)
            out += [m / n for n in names]
        seen, uniq = set(), []
        for p in out:
            rp = p.resolve()
            if rp not in seen:
                uniq.append(rp); seen.add(rp)
        return uniq

    def _load_config_path(self) -> Path:
        for p in self._config_candidates():
            if p.exists():
                return p
        return (SCRIPT_DIR / "config").resolve()

    def _load_cfg_readonly(self) -> tuple[configparser.ConfigParser, Path]:
        path = self._load_config_path()
        cfg = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
        if path.exists():
            cfg.read(path, encoding="utf-8")
        return cfg, path

    def _save_username_to_cfg_preserve(self, username: str):
        _, path = self._load_cfg_readonly()
        _ini_set_key_preserve_comments(path, "auth", "steam_username", username)
        self.console.append(f"[login] 已写入配置 {path.name} [auth].steam_username={username}（保留注释）")

    def _get_steamcmd_from_cfg(self) -> Optional[Path]:
        cfg, _ = self._load_cfg_readonly()
        steamcmd = (cfg.get("paths", "steamcmd", fallback="") or "").strip()
        if not steamcmd:
            return None
        p = Path(steamcmd)
        return p if p.exists() else None

    # ---------- 验证码规范化 ----------
    def _normalize_guard_code(self, code: str) -> str:
        code = (code or "").strip().replace(" ", "").replace("-", "")
        return code.upper()

    # ---------- 输出解析 ----------
    @staticmethod
    def _contains_any(text: str, keywords: list[str]) -> bool:
        return any(k in text for k in keywords)

    def _parse_login_outcome(self, out_low: str) -> dict:
        success_kw = [
            "logged in ok", "logged in", "logged on",
            "waiting for client config...ok",
            "waiting for user info...ok",
            "登录成功", "已登录", "已登入", "登錄成功"
        ]
        invalid_pw_kw = [
            "invalid password", "incorrect password",
            "错误的帐户名或密码", "密码错误", "密碼錯誤", "口令错误", "口令錯誤"
        ]
        guard_kw = [
            "two-factor","two factor","steam guard","authenticator","enter the current code",
            "guard code","2fa","verification code","verify code","auth code",
            "验证码","驗證碼","验证代码","驗證代碼","二次验证","兩步驗證","双重验证","双重身份验证",
            "手机令牌","輸入當前","请输入当前"
        ]
        mobile_kw = [
            "waiting for confirmation","waiting for your confirmation","mobile app",
            "在手机上确认","在手機上確認","请在手机上确认","請在手機上確認","等待您在手机上确认",
            "在移动设备上批准","在移動設備上批准","手机确认","手機確認","移动端确认","移動端確認","批准","同意"
        ]

        success = self._contains_any(out_low, success_kw)
        if ("logging in user" in out_low) and ("to steam public...ok" in out_low):
            success = True

        return dict(
            success=success,
            invalid_pw=self._contains_any(out_low, invalid_pw_kw),
            need_guard=self._contains_any(out_low, guard_kw),
            need_mobile_confirm=self._contains_any(out_low, mobile_kw),
        )

    # ---------- 登录一次 ----------
    def _steamcmd_login_once(self, steamcmd_exe: Path, username: str, password: Optional[str], guard: Optional[str]) -> tuple[bool, str, dict, bool]:
        args = []
        if guard:
            guard = self._normalize_guard_code(guard)
            args += ["+login", username, password or "", guard]
        else:
            if password: args += ["+login", username, password]
            else:        args += ["+login", username]
        args += ["+quit"]

        self.console.append("[login] 正在尝试登录 Steam（仅登录，不下载）...")

        p = subprocess.Popen(
            [str(steamcmd_exe), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            **_win_hidden_popen_kwargs()
        )
        threading.Thread(target=lambda: _hide_top_windows_by_pid(p.pid, duration_s=3.0),
                         daemon=True).start()

        enc = "mbcs" if os.name == "nt" else (locale.getpreferredencoding(False) or "utf-8")

        timed_out = {"v": False}
        finished = {"v": False}

        def _on_timeout():
            if p.poll() is None:
                timed_out["v"] = True
                try:
                    self.console.append("[login] 登录等待超时，结束 steamcmd（很可能在等待手机确认或 2FA）。")
                    p.terminate()
                except Exception:
                    try: p.kill()
                    except Exception: pass

        timer = [None]
        def _start_timer(seconds: float):
            if timer[0] is not None:
                try: timer[0].cancel()
                except Exception: pass
            t = threading.Timer(seconds, _on_timeout)
            t.start()
            timer[0] = t

        _start_timer(STEAMCMD_LOGIN_TIMEOUT_S)

        all_bytes = bytearray()
        last_activity_ts = time.time()
        login_phase_started = {"v": True}
        mobile_hint_shown = {"v": False}

        def _gap_watchdog():
            nonlocal last_activity_ts
            while not finished["v"] and p.poll() is None:
                time.sleep(0.5)
                if mobile_hint_shown["v"]: continue
                if not login_phase_started["v"]: continue
                gap = time.time() - last_activity_ts
                if gap >= MOBILE_GAP_DETECT_S:
                    with self._mobile_prompt_lock:
                        if not self._mobile_prompt_shown:
                            self._mobile_prompt_shown = True
                            mobile_hint_shown["v"] = True
                            self.console.append(f"[login] {gap:.1f}s 无新输出，推断在等待手机确认，延长等待 {int(MOBILE_CONFIRM_MAX_WAIT_S)}s。")
                            self.console.show_toast(
                                key="mobile_confirm",
                                title="请在手机上确认",
                                text=f"很可能正在等待你在手机端批准这次登录。\n本轮等待已延长至 {int(MOBILE_CONFIRM_MAX_WAIT_S)} 秒。",
                                timeout_ms=int(MOBILE_CONFIRM_MAX_WAIT_S * 1000)
                            )
                            _start_timer(MOBILE_CONFIRM_MAX_WAIT_S)
                    last_activity_ts = time.time()
            with self._mobile_prompt_lock:
                self._mobile_prompt_shown = False
                self.console.close_toast("mobile_confirm")

        threading.Thread(target=_gap_watchdog, daemon=True).start()

        try:
            assert p.stdout is not None
            line_buf = bytearray()
            while True:
                b = p.stdout.read(1)
                if not b:
                    break
                all_bytes.extend(b)
                line_buf.extend(b)
                last_activity_ts = time.time()

                try:
                    all_text = all_bytes.decode(enc, errors="ignore")
                except Exception:
                    all_text = all_bytes.decode("utf-8", errors="ignore")
                low = all_text.lower()

                if (not mobile_hint_shown["v"]) and (
                    ("waiting for confirmation" in low) or
                    ("waiting for your confirmation" in low) or
                    ("请在手机上确认" in all_text) or
                    ("在手机上确认" in all_text) or
                    ("在移动设备上批准" in all_text)
                ):
                    with self._mobile_prompt_lock:
                        if not self._mobile_prompt_shown:
                            self._mobile_prompt_shown = True
                            mobile_hint_shown["v"] = True
                            self.console.append("[login] 侦测到“等待手机确认”关键字，延长等待并显示提示。")
                            self.console.show_toast(
                                key="mobile_confirm",
                                title="请在手机上确认",
                                text=f"账号开启手机确认：请在 Steam App/令牌中批准本次登录。\n本轮等待已延长至 {int(MOBILE_CONFIRM_MAX_WAIT_S)} 秒。",
                                timeout_ms=int(MOBILE_CONFIRM_MAX_WAIT_S * 1000)
                            )
                            _start_timer(MOBILE_CONFIRM_MAX_WAIT_S)

                if b in (b"\n", b"\r") and line_buf:
                    try:
                        self.console.append(line_buf.decode(enc, errors="ignore").rstrip("\r\n"))
                    except Exception:
                        self.console.append(line_buf.decode("utf-8", errors="ignore").rstrip("\r\n"))
                    line_buf.clear()

            if line_buf:
                try:
                    self.console.append(line_buf.decode(enc, errors="ignore"))
                except Exception:
                    self.console.append(line_buf.decode("utf-8", errors="ignore"))
                line_buf.clear()

            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try: p.kill()
                except Exception: pass
        finally:
            finished["v"] = True
            try:
                if timer[0] is not None:
                    timer[0].cancel()
            except Exception:
                pass
            with self._mobile_prompt_lock:
                self._mobile_prompt_shown = False
            self.console.close_toast("mobile_confirm")

        try:
            out = all_bytes.decode(enc, errors="ignore")
        except Exception:
            out = all_bytes.decode("utf-8", errors="ignore")
        low = out.lower()
        flags = self._parse_login_outcome(low)

        ok = (p.returncode == 0 and flags["success"])
        if timed_out["v"] and not flags["invalid_pw"]:
            flags.setdefault("maybe_waiting_mobile", True)

        return ok, out, flags, timed_out["v"]

    def _restart_worker_after_success_toast(self):
        self.console.show_toast(
            key="login_success",
            title="登录成功",
            text="账号登录成功，已记录至配置。\n即将应用新账号并重启 worker …",
            timeout_ms=5000
        )
        def _do():
            self.console.close_toast("login_success")
            self._restart_worker()
        threading.Timer(5.1, _do).start()

    def _restart_worker(self):
        self.console.append("[login] 正在重启 worker 以应用新账号 ...")
        try:
            # 1) 通知退出 + 等待
            self._signal_worker_exit_and_wait(wait_s=2.5)
            # 2) **关键**：Reset 退出事件，避免新 worker 立即退出
            _reset_event(self._exit_evt)
        except Exception:
            pass
        try:
            # 3) 启动新 worker
            self.wp = start_worker_and_reader(self.console, job_handle=self._job)
            self.console.append("[login] worker 已重启。")
        except Exception as e:
            self.console.append(f"[login] 重启 worker 失败：{e}")

    # ---------- 登录主流程（带并发防抖） ----------
    def _login_flow_wincred(self):
        with self._login_lock:
            if self._login_active:
                self.console.append("[login] 登录流程已在进行中，忽略重复触发。")
                return
            self._login_active = True
        try:
            steamcmd = self._get_steamcmd_from_cfg()
            if not steamcmd:
                self._msg_error("登录", "缺少 steamcmd 路径：请先在配置 [paths] 中设置 steamcmd= 的绝对路径。")
                return

            username: str = ""
            for _ in range(3):
                cred = self._cred_prompt(
                    caption="登录 Steam 账号",
                    message="请输入 Steam 账号与密码。\n（密码不会被保存，仅用于本次登录）",
                    target="steam://login",
                    default_user=username
                )
                if not cred:
                    self.console.append("[login] 用户取消了账号输入。"); return
                username, password = cred

                ok, out, flags, timed_out = self._steamcmd_login_once(steamcmd, username, password, guard=None)

                if flags.get("invalid_pw"):
                    self._msg_error("登录失败", "密码不正确，请重新输入。")
                    continue

                if ok:
                    self.console.close_toast("mobile_confirm")
                    try: self._save_username_to_cfg_preserve(username)
                    except Exception as e: self.console.append(f"[login] 写入配置失败：{e}")
                    self._restart_worker_after_success_toast()
                    return

                if flags.get("need_mobile_confirm") or flags.get("maybe_waiting_mobile") or timed_out:
                    self.console.append("[login] 手机确认等待未完成，转入 2FA 验证码流程。")

                self.console.close_toast("mobile_confirm")

                for _try in range(3):
                    cred2 = self._cred_prompt(
                        caption="输入 2FA 验证码",
                        message=("此账号开启了 Steam Guard 二次验证。\n"
                                 "请在“密码”一栏输入 **5 位**（或 **6 位**）验证码；不区分大小写，可直接输入。"),
                        target="steam://guard",
                        default_user=username
                    )
                    if not cred2:
                        self.console.append("[login] 用户取消了手机令牌输入。"); break
                    _, guard = cred2
                    ok2, out2, flags2, _ = self._steamcmd_login_once(steamcmd, username, password, guard=guard)
                    if flags2.get("invalid_pw"):
                        self._msg_error("登录失败", "密码已失效或被修改，请返回重输密码。")
                        break
                    if ok2:
                        self.console.close_toast("mobile_confirm")
                        try: self._save_username_to_cfg_preserve(username)
                        except Exception as e: self.console.append(f"[login] 写入配置失败：{e}")
                        self._restart_worker_after_success_toast()
                        return
                    self._msg_error("登录失败", "验证码/令牌无效或登录失败，请重试。")
                continue

            self._msg_error("登录失败", "多次尝试未成功。请检查账号/密码/验证码后再试。")
        finally:
            with self._login_lock:
                self._login_active = False

    # ---------- 退出/消息循环 ----------
    def _signal_worker_exit_and_wait(self, wait_s: float = 3.0):
        try: _set_event(self._exit_evt)
        except Exception: pass
        t0 = time.time()
        try:
            if self.wp and self.wp.proc:
                while self.wp.proc.poll() is None and (time.time() - t0) < wait_s:
                    time.sleep(0.05)
        except Exception: pass
        if self.wp and self.wp.proc and self.wp.proc.poll() is None:
            stop_worker(self.wp, timeout=2.0)

    def _exit_app(self):
        self.console.append("[exit] 正在优雅退出...")
        try: self._delete_icon()
        except Exception: pass
        try:
            self._signal_worker_exit_and_wait(wait_s=3.0)
        except Exception: pass
        try:
            self.console.stop()
        except Exception: pass
        try:
            meipass = getattr(sys, "_MEIPASS", "")
            if meipass and Path.cwd().as_posix().startswith(Path(meipass).as_posix()):
                os.chdir(str(Path.home()))
        except Exception: pass
        try:
            gc.collect(); time.sleep(0.15)
        except Exception: pass
        try:
            if self._job:
                kernel32.CloseHandle(self._job); self._job = 0
        except Exception: pass
        try:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd); self.hwnd = None
        finally:
            user32.PostQuitMessage(0)

    def _on_cmd(self, cmd):
        if cmd == IDM_LOGIN:
            user32.PostMessageW(self.hwnd, WM_APP_LOGIN, 0, 0); return
        if cmd == IDM_TOGGLE_CONSOLE:
            self.console.toggle()
        elif cmd == IDM_FORCE_SWITCH:
            self.console.append("[action] 立即更换一次：已通知 worker 立刻执行一轮。")
            try:
                _pulse_event(self._run_now_evt, duration_s=0.08)
            except Exception:
                self.console.append("[action] 通知失败：RUN_NOW 事件句柄无效。")
        elif cmd == IDM_EXCLUDE_CREATOR:
            threading.Thread(target=self._exclude_current_creator_and_switch, daemon=True).start()
        elif cmd == IDM_TOGGLE_AUTOSTART:
            cur = is_autostart_enabled()
            set_autostart(not cur)
            self.console.append(f"[autostart] 已设置开机自启 = {not cur}")
            self._modify_icon()  # 仅刷新
        elif cmd == IDM_EXIT:
            self._exit_app()

    def _exclude_current_creator_and_switch(self):
        """
        读取 state.last_applied -> 查询该作品详情的 creator(SteamID64) -> 写入 config 的 [filters].creator_exclude_ids
        然后重启 worker，并触发 RUN_NOW 立即切换。
        """
        try:
            cfg, cfg_path = self._load_cfg_readonly()
            state_rel = (cfg.get("paths", "state_file", fallback="we_auto_state.json") or "").strip() or "we_auto_state.json"
            state_path = (SCRIPT_DIR / state_rel).resolve()
            if not state_path.exists():
                self._msg_error("排除上传者", f"未找到状态文件：{state_path}")
                return

            import json
            state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore") or "{}")
            wid = state.get("last_applied")
            try:
                wid_i = int(wid)
            except Exception:
                wid_i = 0
            if wid_i <= 0:
                self._msg_error("排除上传者", "当前没有可用的 last_applied（可能尚未成功应用过壁纸）。")
                return

            # 查 creator（复用 we_auto_fetch 的 API）
            try:
                import we_auto_fetch
                det = we_auto_fetch.fetch_details([wid_i], https_proxy=(cfg.get("network","https_proxy",fallback="") or "").strip())
                it = det.get(wid_i, {}) if isinstance(det, dict) else {}
            except Exception as e:
                self._msg_error("排除上传者", f"获取作品详情失败：{e}")
                return

            creator = it.get("creator")
            creator_s = str(creator).strip() if creator is not None else ""
            if not creator_s or not creator_s.isdigit():
                self._msg_error("排除上传者", f"未能从作品详情中解析 creator（作品ID={wid_i}）。")
                return

            title = str(it.get("title") or "").strip().replace("\n", " ")
            if len(title) > 80:
                title = title[:77] + "..."

            # 合并写回 creator_exclude_ids（保留注释）
            exist_raw = (cfg.get("filters", "creator_exclude_ids", fallback="") or "")
            exist_ids = _parse_steamid64_list(exist_raw)
            if creator_s not in set(exist_ids):
                exist_ids.append(creator_s)
            new_val = ",".join(exist_ids)
            _ini_set_key_preserve_comments(cfg_path, "filters", "creator_exclude_ids", new_val)

            self.console.append(f"[filters] 已加入上传者排除：{creator_s}（来自作品 {wid_i}）")
            if title:
                self.console.append(f"[filters]  - Title: {title}")
            self.console.append(f"[filters] 已写入配置：{cfg_path}")

            # 让改动立刻生效：重启 worker + 触发 RUN_NOW
            try:
                self._restart_worker()
            except Exception:
                pass
            try:
                _pulse_event(self._run_now_evt, duration_s=0.08)
            except Exception:
                self.console.append("[action] 通知失败：RUN_NOW 事件句柄无效。")

        except Exception as e:
            self.console.append(f"[exclude_creator] 失败：{e}")

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # 注：已移除 TaskbarCreated 处理，不再重建托盘图标

        if msg == WM_TRAYICON:
            sub = int(lparam) & 0xFFFFFFFF
            if sub in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.console.toggle(); return 0
            if sub == WM_RBUTTONUP:
                self._show_context_menu(); return 0
            return 0

        if msg == WM_POWERBROADCAST and wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            self.console.append("[tray] 电源恢复，刷新托盘（不重建）。")
            self._modify_icon(); return 1

        if msg == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
            self.console.append("[tray] 会话解锁/登录，刷新托盘（不重建）。")
            self._modify_icon(); return 0

        if msg == WM_COMMAND:
            self._on_cmd(wparam & 0xFFFF); return 0

        if msg == WM_APP_LOGIN:
            if not self._login_active:
                threading.Thread(target=self._login_flow_wincred, daemon=True).start()
            else:
                self.console.append("[login] 登录流程已在进行中（WM）。")
            return 0

        if msg == WM_DESTROY:
            self._delete_icon()
            user32.PostQuitMessage(0); return 0

        if msg == WM_CLOSE:
            self._exit_app(); return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        def _proc(hwnd, msg, wparam, lparam):
            return self._wnd_proc(hwnd, msg, wparam, lparam)
        wndproc = WNDPROCTYPE(_proc)
        self._wndproc = wndproc

        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.style = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS
        wc.lpfnWndProc = wndproc
        wc.cbClsExtra = wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = "WEAutoTrayWin32HiddenWindow"
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, "hidden", 0, 0, 0, 0, 0,
            None, None, hinst, None
        )
        self.hwnd = hwnd

        try: wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
        except Exception: pass

        # 仅启动时添加一次托盘图标
        self._add_icon()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        try: wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        except Exception: pass

# ----------------- 入口 -----------------
def main():
    if "--worker" in sys.argv:
        run_worker_inline(); return
    si = SingleInstance("WEAutoTrayMutex")
    if si.already_running:
        return
    app = Win32TrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app._exit_app()

if __name__ == "__main__":
    main()