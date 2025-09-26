# -*- coding: utf-8 -*-
"""
we_tray.py
系统托盘启动器：后台运行 we_auto_fetch（worker 进程），托盘左键“双击”显示实时控制台，
右键菜单：打开/隐藏控制台、立即更换（重启一次）、开机自启开关、退出

要点：
- 优雅退出顺序：停守护/监听 -> 停托盘 -> 通知 worker 自退 -> 兜底强杀/Job 关闭 -> 停 Tk -> 切 CWD/GC -> 主线程自然退出（码 0）
- worker 进程内监听命名事件，收到信号立即退出，杜绝残留
- 父进程为 worker 创建 Windows Job（Kill-On-Close），父进程退出/关闭 Job 时系统强制清理所有子进程
"""

from __future__ import annotations
import os, sys, threading, subprocess, queue, time, ctypes, gc, hashlib
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from collections import deque

# ---- 配置区 ------------------------------------------------------
WORKER_SCRIPT = "we_auto_fetch.py"   # 与本文件同目录
MAX_BUFFER_LINES = 5000
WORKER_ARGS = []
KEEPER_POLL_SECONDS = 2.5            # 任务栏检测间隔
# -----------------------------------------------------------------

# 全局退出事件：主线程等待它被置位后自然返回（退出码 0）
EXIT_EVENT = threading.Event()

# ---------- Win32 基础 ----------
_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_FindWindowW = _user32.FindWindowW
_FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_FindWindowW.restype  = ctypes.c_void_p

# Wait/Event constants
WAIT_OBJECT_0 = 0x00000000
INFINITE      = 0xFFFFFFFF
SYNCHRONIZE   = 0x00100000
EVENT_MODIFY_STATE = 0x0002

# Process / Job constants
PROCESS_TERMINATE   = 0x0001
PROCESS_SET_QUOTA   = 0x0100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation  = 9

# --- Job Object 结构体定义（ctypes） ---
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

# 函数原型
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

def _get_taskbar_hwnd():
    try:
        return int(_FindWindowW("Shell_TrayWnd", None))  # 0 = 不存在
    except Exception:
        return 0

# -------- 命名事件（托盘 <-> worker 的退出信号） ----------
def _exit_event_name() -> str:
    """生成全局命名事件名；用 exe 绝对路径哈希避免多副本冲突"""
    try:
        base = str(Path(sys.executable).resolve())
    except Exception:
        base = sys.argv[0]
    h = hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"Global\\WEAutoTrayExit_{h}"

def _create_named_event_manual_reset(name: str, initial: bool=False):
    return _kernel32.CreateEventW(None, True, bool(initial), name)

def _open_named_event(name: str):
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    return _kernel32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)

def _set_event(h) -> None:
    try:
        _kernel32.SetEvent(h)
    except Exception:
        pass

# ---- Job：Kill-on-close ----
def _create_kill_on_close_job() -> int:
    """创建带 KillOnClose 的 Job；返回句柄（int），失败返 0。"""
    try:
        hjob = _kernel32.CreateJobObjectW(None, None)
        if not hjob:
            return 0
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            hjob, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            _kernel32.CloseHandle(hjob)
            return 0
        return hjob
    except Exception:
        return 0

def _assign_pid_to_job(job_handle: int, pid: int) -> bool:
    if not job_handle or not pid:
        return False
    hproc = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not hproc:
        return False
    try:
        ok = _kernel32.AssignProcessToJobObject(job_handle, hproc)
        return bool(ok)
    finally:
        _kernel32.CloseHandle(hproc)

# 单实例（命名互斥量）
class SingleInstance:
    def __init__(self, name: str):
        self.mutex = _kernel32.CreateMutexW(None, False, f"Global\\{name}")
        self.already_running = (ctypes.GetLastError() == 183)  # ERROR_ALREADY_EXISTS
    def __del__(self):
        if getattr(self, "mutex", None):
            _kernel32.CloseHandle(self.mutex)

# 开机自启（注册表 HKCU\...\Run）
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

# 托盘图标
def _make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 15, 15], outline=(0, 0, 0, 255), width=1)
    d.text((2, 1), "W", fill=(0, 0, 0, 255))
    d.text((2, 8), "E", fill=(0, 0, 0, 255))
    return img

# Tk 实时控制台窗口
class ConsoleWindow:
    def __init__(self, title="WE - 实时控制台"):
        import tkinter as tk
        from tkinter.scrolledtext import ScrolledText
        self._tk = tk
        self.root = None
        self.text = None
        self._thread = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._buffer = deque(maxlen=MAX_BUFFER_LINES)
        self._ready = threading.Event()
        self._visible_flag = False
        self.title = title
        self._stop_called = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run_loop(self):
        tk = self._tk
        from tkinter.scrolledtext import ScrolledText
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
            self._visible_flag = True
            if self.text and float(self.text.index("end-1c").split(".")[0]) <= 1:
                self.text.configure(state="normal")
                self.text.delete("1.0", "end")
                self.text.insert("end", "".join(self._buffer))
                self.text.see("end")
                self.text.configure(state="disabled")

    def hide(self):
        if self.root:
            self.root.withdraw()
            self._visible_flag = False

    def toggle(self):
        (self.show() if not self._visible_flag else self.hide())

    def stop(self, join_timeout: float = 2.0):
        self._stop_called = True
        if self.root:
            try: self.root.after(0, self.root.quit)
            except Exception: pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

# worker 进程管理
@dataclass
class WorkerProc:
    proc: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None

def _win_hidden_popen_kwargs():
    if os.name != "nt":
        return {}
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
    cmd = [exe, "-u", WORKER_SCRIPT, *WORKER_ARGS] if not getattr(sys, "frozen", False) else [exe, "--worker", *WORKER_ARGS]

    # 子进程 cwd 放到安全位置（远离 _MEI）
    safe_cwd = Path.home()
    try:
        meipass = getattr(sys, "_MEIPASS", "")
        if not safe_cwd.exists() or (meipass and Path.cwd().as_posix().startswith(Path(meipass).as_posix())):
            safe_cwd = Path(os.environ.get("TEMP") or os.environ.get("TMP") or (Path.cwd().drive + "\\"))
    except Exception:
        pass

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

    # 把 worker 放入 Kill-on-close Job
    try:
        if job_handle:
            _assign_pid_to_job(job_handle, p.pid)
    except Exception:
        pass

    def reader():
        assert p.stdout is not None
        for line in p.stdout:
            console.append(line)
        try:
            rest = p.stdout.read()
            if rest:
                console.append(rest)
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return WorkerProc(proc=p, reader_thread=t)

def stop_worker(wp: WorkerProc, timeout=5.0):
    if not wp or not wp.proc:
        return
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

# ------- 托盘 + 自动重建 --------------------------
class TrayApp:
    def __init__(self):
        self.console = ConsoleWindow("Wallpaper Engine - 实时控制台")
        self._exit_event_name = _exit_event_name()
        self._exit_evt = _create_named_event_manual_reset(self._exit_event_name, initial=False)

        # 创建 Kill-on-close Job，并保存句柄至退出时关闭
        self._job = _create_kill_on_close_job()

        self.wp = start_worker_and_reader(self.console, job_handle=self._job)

        self._icon = None
        self._icon_thread = None
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._keeper_thread = None
        self._last_taskbar = 0
        self._watcher: TaskbarWatcher | None = None

    def on_toggle_console(self, icon, _): self.console.toggle()

    def _signal_worker_exit_and_wait(self, wait_s: float = 3.0):
        # 通知 worker 自退（命名事件）→ 等待 → 兜底强杀
        try:
            if self._exit_evt:
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

    def on_exit(self, icon, _):
        # 优雅退出：停守护/监听 -> 停托盘 -> 通知 worker -> 兜底强杀 -> 停 Tk -> 切 CWD/GC -> 关闭 Job -> 退出
        self.console.append("[exit] 正在优雅退出...\n")
        try:
            self.stop_guard_and_listener()
            self.stop_icon()

            self._signal_worker_exit_and_wait(wait_s=3.0)
            self.console.append("[exit] worker 已停止（或将被 Job 回收）。\n")
            self.console.stop()
            self.console.append = lambda *_args, **_kwargs: None  # 防止后续写入

            # 移出 _MEI，做一次 GC，给系统释放文件锁
            try:
                meipass = getattr(sys, "_MEIPASS", "")
                if meipass and Path.cwd().as_posix().startswith(Path(meipass).as_posix()):
                    os.chdir(str(Path.home()))
            except Exception:
                pass
            try:
                gc.collect()
                time.sleep(0.15)
            except Exception:
                pass

            # 关闭 Job 句柄：若仍有子进程，系统将杀掉（Kill-on-close）
            try:
                if self._job:
                    _kernel32.CloseHandle(self._job)
                    self._job = 0
            except Exception:
                pass
        finally:
            EXIT_EVENT.set()

    def dyn_autostart_text(self, _item):
        return "关闭开机自启" if is_autostart_enabled() else "开启开机自启"

    def on_toggle_autostart(self, icon, _):
        cur = is_autostart_enabled()
        set_autostart(not cur)
        self.console.append(f"[autostart] 已设置开机自启 = {not cur}\n")
        try:
            if self._icon: self._icon.update_menu()
        except Exception: pass

    def on_force_switch(self, icon, _):
        self.console.append("[action] 立即更换：正在重启 worker...\n")
        try:
            self._signal_worker_exit_and_wait(wait_s=2.0)
        finally:
            self.wp = start_worker_and_reader(self.console, job_handle=self._job)
            self.console.append("[action] 已重启 worker。\n")

    def _build_icon(self):
        import pystray
        from pystray import MenuItem as item, Menu
        icon_img = _make_icon_image()
        menu = Menu(
            item("打开/隐藏 控制台", self.on_toggle_console, default=True),
            item("立即更换（重启一次）", self.on_force_switch),
            item(self.dyn_autostart_text, self.on_toggle_autostart),
            item("退出", self.on_exit)
        )
        return pystray.Icon("WEAutoTray", icon=icon_img, title="WE Auto Runner", menu=menu)

    def start_icon(self):
        with self._lock:
            if self._icon_thread and self._icon_thread.is_alive():
                return
            self._icon = self._build_icon()
            self._icon_thread = threading.Thread(
                target=lambda: self._icon.run(setup=lambda i: setattr(i, "visible", True)),
                daemon=True
            )
            self._icon_thread.start()
            self.console.append("[tray] 托盘图标已创建。\n")

    def stop_icon(self):
        with self._lock:
            if self._icon:
                try: self._icon.visible = False
                except Exception: pass
                try: self._icon.stop()
                except Exception: pass
                try: self._icon.icon = None
                except Exception: pass
                self._icon = None
            if self._icon_thread:
                self._icon_thread.join(timeout=2.0)
                self._icon_thread = None
            self.console.append("[tray] 托盘图标已停止。\n")

    def rebuild_icon(self):
        self.console.append("[tray] 重建托盘图标...\n")
        self.stop_icon()
        time.sleep(0.2)
        self.start_icon()

    def _keeper_loop(self):
        self._last_taskbar = _get_taskbar_hwnd()
        self.start_icon()
        while not self._stop_evt.is_set():
            try:
                h = _get_taskbar_hwnd()
                if h != 0 and h != self._last_taskbar:
                    self.rebuild_icon()
                self._last_taskbar = h
            except Exception as e:
                self.console.append(f"[tray] keeper 异常：{e}\n")
            finally:
                self._stop_evt.wait(KEEPER_POLL_SECONDS)

    def start_background(self):
        self.console.start()
        self._keeper_thread = threading.Thread(target=self._keeper_loop, daemon=True)
        self._keeper_thread.start()
        self.console.append("[tray] 守护线程已启动。\n")
        self._watcher = TaskbarWatcher(self)
        self._watcher.start()

    def stop_guard_and_listener(self):
        try:
            self._stop_evt.set()
            if self._keeper_thread and self._keeper_thread.is_alive():
                self._keeper_thread.join(timeout=3.0)
                self.console.append("[tray] 守护线程已停止。\n")
        except Exception:
            pass
        try:
            if self._watcher:
                self._watcher.stop()
                self._watcher.join(timeout=3.0)
                self.console.append("[tray] Taskbar watcher 已停止。\n")
        except Exception:
            pass

# ------- TaskbarCreated / 电源恢复 / 会话解锁 监听 --------------------------
class TaskbarWatcher(threading.Thread):
    def __init__(self, app: TrayApp):
        super().__init__(daemon=True)
        self.app = app
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.wtsapi32 = ctypes.windll.wtsapi32

        self.WM_POWERBROADCAST = 0x0218
        self.PBT_APMRESUMEAUTOMATIC = 0x0012
        self.PBT_APMRESUMESUSPEND  = 0x0007
        self.WM_WTSSESSION_CHANGE  = 0x02B1
        self.WTS_SESSION_LOGON     = 0x0005
        self.WTS_SESSION_UNLOCK    = 0x0008
        self.NOTIFY_FOR_THIS_SESSION = 0
        self.WM_CLOSE   = 0x0010
        self.WM_DESTROY = 0x0002
        self._hwnd = 0
        self._ready_evt = threading.Event()

    def run(self):
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)

        def py_wnd_proc(hwnd, msg, wparam, lparam):
            if msg == self._taskbar_created:
                self.app.console.append("[tray] 收到 TaskbarCreated，重建图标。\n")
                self._schedule_rebuild()
            elif msg == self.WM_POWERBROADCAST and wparam in (self.PBT_APMRESUMEAUTOMATIC, self.PBT_APMRESUMESUSPEND):
                self.app.console.append("[tray] 电源恢复，重建托盘图标。\n")
                self._schedule_rebuild()
            elif msg == self.WM_WTSSESSION_CHANGE and wparam in (self.WTS_SESSION_UNLOCK, self.WTS_SESSION_LOGON):
                self.app.console.append("[tray] 会话解锁/登录，重建托盘图标。\n")
                self._schedule_rebuild()
            elif msg == self.WM_DESTROY:
                self.user32.PostQuitMessage(0); return 0
            elif msg == self.WM_CLOSE:
                self.user32.DestroyWindow(hwnd); return 0
            return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        hinst = self.kernel32.GetModuleHandleW(None)
        class_name = "WEAutoTrayHiddenWindow"

        WNDCLASS = wintypes.WNDCLASS
        wc = WNDCLASS()
        wc.lpszClassName = class_name
        wc.lpfnWndProc = WNDPROC(py_wnd_proc)
        wc.hInstance = hinst

        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            return

        self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")

        hwnd = self.user32.CreateWindowExW(
            0, class_name, "hidden", 0, 0, 0, 0, 0,
            None, None, hinst, None
        )
        self._hwnd = hwnd
        self._ready_evt.set()

        try:
            self.wtsapi32.WTSRegisterSessionNotification(hwnd, self.NOTIFY_FOR_THIS_SESSION)
        except Exception:
            pass

        msg = wintypes.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0) != 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

        try:
            self.wtsapi32.WTSUnRegisterSessionNotification(hwnd)
        except Exception:
            pass

    def _schedule_rebuild(self):
        def _do():
            try: time.sleep(0.3)
            except Exception: pass
            self.app.rebuild_icon()
        threading.Thread(target=_do, daemon=True).start()

    def stop(self):
        self._ready_evt.wait(timeout=1.5)
        try:
            if self._hwnd:
                self.user32.PostMessageW(self._hwnd, self.WM_CLOSE, 0, 0)
        except Exception:
            pass

# ---------------- 程序入口 ----------------

class _SafeStream:
    """包装 stdout/stderr：在句柄无效时吞掉 OSError，避免退出阶段的 Invalid argument 报错。"""
    def __init__(self, base):
        self._b = base
    def write(self, s):
        try:    return self._b.write(s)
        except OSError: return 0
    def flush(self):
        try:    return self._b.flush()
        except OSError: return None
    def writelines(self, lines):
        try:    return self._b.writelines(lines)
        except OSError: return None
    def __getattr__(self, name): return getattr(self._b, name)

def _start_worker_exit_watcher_thread():
    """worker 内监听命名事件；收到后立即 os._exit(0)"""
    name = _exit_event_name()
    h = _open_named_event(name) or _create_named_event_manual_reset(name, initial=False)
    if not h:
        return
    def _wait_and_die():
        try:
            while True:
                rc = _kernel32.WaitForSingleObject(h, 1000)
                if rc == WAIT_OBJECT_0:
                    os._exit(0)
        except Exception:
            os._exit(0)
    threading.Thread(target=_wait_and_die, daemon=True).start()

def run_worker_inline():
    # 保护 worker 的 stdout/stderr
    sys.stdout = _SafeStream(sys.stdout)
    sys.stderr = _SafeStream(sys.stderr)
    try: sys.stdout.reconfigure(line_buffering=True)
    except Exception: pass

    # worker 监听退出事件
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

def main():
    if "--worker" in sys.argv:
        run_worker_inline()
    else:
        si = SingleInstance("WEAutoTrayMutex")
        if si.already_running: return
        app = TrayApp()
        app.start_background()
        try:
            while not EXIT_EVENT.is_set():
                EXIT_EVENT.wait(1.0)
        except KeyboardInterrupt:
            app.on_exit(None, None)

if __name__ == "__main__":
    main()