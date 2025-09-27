# -*- coding: utf-8 -*-
"""
we_tray.py（原生 Win32 托盘·稳定版：修复点击无响应 + 全量代码）

- 原生 Shell_NotifyIcon（NOTIFYICON_VERSION_4）+ 稳定 GUID
- 睡眠/解锁/Explorer重启：瞬时恢复托盘（TaskbarCreated / 电源恢复 / 会话解锁）
- 左键（单击/双击）/右键菜单：已修复不响应问题
- 单实例、Kill-on-close Job、命名事件优雅退出、worker 输出到 Tk 实时控制台
- 自定义图标：同目录 we.ico / app.ico / tray.ico
"""
from __future__ import annotations
import os, sys, ctypes, threading, subprocess, time, queue, hashlib, gc
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from collections import deque
from uuid import UUID

# ------------------ 配置 ------------------
WORKER_SCRIPT = "we_auto_fetch.py"
WORKER_ARGS = []
MAX_BUFFER_LINES = 5000

# 仅新增 ↓↓↓（最小化修复：固定工作目录与脚本绝对路径）
SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT_ABS = (SCRIPT_DIR / WORKER_SCRIPT).resolve()
# -----------------------------------------

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32  = ctypes.windll.shell32
wtsapi32 = ctypes.windll.wtsapi32

# ---- 兼容：部分 Python 没有这些 wintypes 定义，统一兜底到 HANDLE ----
HANDLE    = wintypes.HANDLE
HWND      = getattr(wintypes, "HWND", HANDLE)
HICON     = getattr(wintypes, "HICON", HANDLE)
HCURSOR   = getattr(wintypes, "HCURSOR", HANDLE)
HBRUSH    = getattr(wintypes, "HBRUSH", HANDLE)
HINSTANCE = getattr(wintypes, "HINSTANCE", HANDLE)
HMENU     = getattr(wintypes, "HMENU", HANDLE)

# ---- 兼容：LRESULT/WPARAM/LPARAM 在不同版本的缺省类型 ----
PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_longlong if PTR_SIZE == 8 else ctypes.c_long)
WPARAM  = getattr(wintypes, "WPARAM",  ctypes.c_size_t)
LPARAM  = getattr(wintypes, "LPARAM",  ctypes.c_ssize_t)

def _errcheck_bool(result, func, args):
    if not result: raise ctypes.WinError()
    return result

# ------------- 常量/消息 -------------
WM_USER               = 0x0400
WM_TRAYICON           = WM_USER + 1       # 我们的托盘回调消息
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

# 鼠标相关（lParam 会携带这些子消息）
WM_LBUTTONDOWN   = 0x0201
WM_LBUTTONUP     = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN   = 0x0204
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

IDM_TOGGLE_CONSOLE   = 1001
IDM_FORCE_SWITCH     = 1002
IDM_TOGGLE_AUTOSTART = 1003
IDM_EXIT             = 1004

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

# ------------- 结构体与类型 -------------
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT, WPARAM, LPARAM)

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

# Shell_NotifyIcon
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype  = wintypes.BOOL

# 关键 API 原型（64 位安全）
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

# ★ 关键：这些原型不声明会导致 64 位下 LPARAM 溢出 → 回调异常 → 点击无响应
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

def _create_named_event_manual_reset(name: str, initial: bool=False):
    return kernel32.CreateEventW(None, True, bool(initial), name)

def _open_named_event(name: str):
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    return kernel32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)

def _set_event(h) -> None:
    try: kernel32.SetEvent(h)
    except Exception: pass

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

# ----------------- Tk 实时控制台 -----------------
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

def start_worker_and_reader(console: ConsoleWindow, job_handle: int | None = None) -> WorkerProc:
    exe = sys.executable

    # 仅最小化修改：绝对路径 + 固定工作目录为脚本目录
    if getattr(sys, "frozen", False):
        cmd = [exe, "--worker", *WORKER_ARGS]
    else:
        cmd = [exe, "-u", str(WORKER_SCRIPT_ABS), *WORKER_ARGS]

    safe_cwd = SCRIPT_DIR  # 关键：不再用用户主目录/临时目录

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        close_fds=True,
        cwd=str(safe_cwd),
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

# ----------------- 原生 Win32 托盘 APP -----------------
class Win32TrayApp:
    def __init__(self):
        self.console = ConsoleWindow("Wallpaper Engine - 实时控制台")
        self.console.start()
        self._exit_evt_name = _exit_event_name()
        self._exit_evt = _create_named_event_manual_reset(self._exit_evt_name, initial=False)

        self._job = _create_kill_on_close_job()
        self.wp = start_worker_and_reader(self.console, job_handle=self._job)

        self.hwnd = None
        self.hicon = None
        self.added = False
        self.tray_guid = self._make_guid_from_exe()
        self._taskbar_created_msg = 0

        self.class_name = "WEAutoTrayWin32HiddenWindow"
        self.tip_text = "WE Auto Runner - 正在运行"

        # 保存回调，防止被 GC
        self._wndproc = None

        # LoadImageW 原型（为自定义 .ico）
        user32.LoadImageW.argtypes = [HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = HANDLE

    # --- GUID 基于 exe 路径稳定生成 ---
    def _make_guid_from_exe(self) -> GUID:
        try:
            base = str(Path(sys.executable).resolve())
        except Exception:
            base = sys.argv[0]
        h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()
        u = UUID(h[:32])
        return GUID.from_uuid(u)

    # --- 托盘核心 ---
    def _notify(self, msg, data: NOTIFYICONDATAW):
        return bool(shell32.Shell_NotifyIconW(msg, ctypes.byref(data)))

    def _build_nid(self, flags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_GUID, hicon=None) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 0  # 使用 GUID 管理，uID 可置 0
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon or self.hicon
        nid.szTip = self.tip_text
        nid.guidItem = self.tray_guid
        return nid

    def _add_icon(self):
        if not self.hwnd: return
        if not self.hicon: self.hicon = self._load_icon()
        nid = self._build_nid()
        self._notify(NIM_ADD, nid)
        nid.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        self._notify(NIM_SETVERSION, nid)
        self.added = True
        self.console.append("[tray] 已添加托盘图标（v4）。")

    def _modify_icon(self):
        if not self.added:
            self._add_icon(); return
        nid = self._build_nid()
        self._notify(NIM_MODIFY, nid)

    def _delete_icon(self):
        if not self.added: return
        nid = self._build_nid(flags=NIF_GUID)
        try: self._notify(NIM_DELETE, nid)
        except Exception: pass
        self.added = False
        self.console.append("[tray] 托盘图标已删除。")

    # --- 自定义图标加载（we.ico / app.ico / tray.ico）→ 退回系统图标 ---
    def _load_icon(self):
        IMAGE_ICON      = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE  = 0x00000040

        candidates = ["we.ico", "app.ico", "tray.ico"]
        for name in candidates:
            p = Path(__file__).with_name(name)
            if p.exists():
                h = user32.LoadImageW(None, str(p), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    self.console.append(f"[tray] 已加载自定义图标：{p.name}")
                    return HICON(h)

        IDI_APPLICATION = 32512
        self.console.append("[tray] 未找到自定义图标，使用系统图标。")
        return user32.LoadIconW(None, MAKEINTRESOURCE(IDI_APPLICATION))

    # --- 右键菜单 ---
    def _show_context_menu(self):
        hMenu = user32.CreatePopupMenu()
        autostart_txt = "关闭开机自启" if is_autostart_enabled() else "开启开机自启"
        user32.AppendMenuW(hMenu, MF_STRING, IDM_TOGGLE_CONSOLE, "打开/隐藏 控制台")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_FORCE_SWITCH, "立即更换（重启一次）")
        user32.AppendMenuW(hMenu, MF_STRING, IDM_TOGGLE_AUTOSTART, autostart_txt)
        user32.AppendMenuW(hMenu, MF_STRING, IDM_EXIT, "退出")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hMenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self.hwnd, None)
        # 关闭菜单的常见技巧（防止菜单“粘住”）
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        if cmd:
            user32.PostMessageW(self.hwnd, WM_COMMAND, cmd, 0)
        user32.DestroyMenu(hMenu)

    # --- 命令处理 ---
    def _on_cmd(self, cmd):
        if cmd == IDM_TOGGLE_CONSOLE:
            self.console.toggle()
        elif cmd == IDM_FORCE_SWITCH:
            self.console.append("[action] 立即更换：正在重启 worker...")
            self._signal_worker_exit_and_wait(2.0)
            self.wp = start_worker_and_reader(self.console, job_handle=self._job)
            self.console.append("[action] 已重启 worker。")
        elif cmd == IDM_TOGGLE_AUTOSTART:
            cur = is_autostart_enabled()
            set_autostart(not cur)
            self.console.append(f"[autostart] 已设置开机自启 = {not cur}")
            self._modify_icon()
        elif cmd == IDM_EXIT:
            self._exit_app()

    # --- worker 优雅退出 ---
    def _signal_worker_exit_and_wait(self, wait_s: float = 3.0):
        try:
            _set_event(self._exit_evt)
        except Exception:
            pass
        t0 = time.time()
        try:
            if self.wp and self.wp.proc:
                while self.wp.proc.poll() is None and (time.time() - t0) < wait_s:
                    time.sleep(0.05)
        except Exception:
            pass
        if self.wp and self.wp.proc and self.wp.proc.poll() is None:
            stop_worker(self.wp, timeout=2.0)

    # --- 退出清理 ---
    def _exit_app(self):
        self.console.append("[exit] 正在优雅退出...")
        try: self._delete_icon()
        except Exception: pass
        try:
            self._signal_worker_exit_and_wait(wait_s=3.0)
            self.console.append("[exit] worker 已停止（或将被 Job 回收）。")
        except Exception: pass
        try: self.console.stop()
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

    # --- WindowProc ---
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == self._taskbar_created_msg:
            self.console.append("[tray] 收到 TaskbarCreated，重加托盘。")
            self._add_icon()
            return 0

        if msg == WM_TRAYICON:
            # 规范化为无符号 32 位（避免 ctypes 有符号解释）
            sub = int(lparam) & 0xFFFFFFFF
            # 左键：单击/双击 都打开/切换控制台
            if sub in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.console.toggle(); return 0
            # 右键：弹出菜单（两种路径都支持）
            if sub in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_context_menu(); return 0
            return 0

        if msg == WM_POWERBROADCAST and wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            self.console.append("[tray] 电源恢复，刷新托盘。")
            self._modify_icon(); return 1

        if msg == WM_WTSSESSION_CHANGE and wparam in (WTS_SESSION_UNLOCK, WTS_SESSION_LOGON):
            self.console.append("[tray] 会话解锁/登录，刷新托盘。")
            self._modify_icon(); return 0

        if msg == WM_COMMAND:
            self._on_cmd(wparam & 0xFFFF); return 0

        if msg == WM_DESTROY:
            self._delete_icon()
            user32.PostQuitMessage(0); return 0

        if msg == WM_CLOSE:
            self._exit_app(); return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # --- 创建隐藏窗口 + 消息循环 ---
    def run(self):
        def _proc(hwnd, msg, wparam, lparam):
            return self._wnd_proc(hwnd, msg, wparam, lparam)
        wndproc = WNDPROCTYPE(_proc)
        self._wndproc = wndproc  # 持有引用防 GC

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
        self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")

        self._add_icon()

        # ★ 关键：不过滤窗口，保证线程队列里的所有消息都能收到
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