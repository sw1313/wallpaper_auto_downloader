# -*- coding: utf-8 -*-
"""
we_tray.py
系统托盘启动器：后台运行 we_auto_fetch（worker 进程），托盘左键“双击”显示实时控制台，
右键菜单：打开/隐藏控制台、立即更换（重启一次）、开机自启开关、退出

修复点：Windows 息屏/解锁或 Explorer 重启后，托盘图标丢失。
做法：后台 TrayKeeper 线程监测 Shell_TrayWnd 句柄变化，自动重建托盘图标。

依赖：pip install pystray pillow
"""
from __future__ import annotations
import os, sys, threading, subprocess, queue, time, ctypes
from dataclasses import dataclass
from pathlib import Path
from collections import deque

# ---- 配置区 ------------------------------------------------------
WORKER_SCRIPT = "we_auto_fetch.py"   # 与本文件同目录
MAX_BUFFER_LINES = 5000
WORKER_ARGS = []
KEEPER_POLL_SECONDS = 2.5            # 任务栏检测间隔
# -----------------------------------------------------------------

# Win32：FindWindowW("Shell_TrayWnd", None)
_user32 = ctypes.windll.user32
_FindWindowW = _user32.FindWindowW
_FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_FindWindowW.restype  = ctypes.c_void_p

def _get_taskbar_hwnd():
    try:
        return int(_FindWindowW("Shell_TrayWnd", None))  # 0 = 不存在
    except Exception:
        return 0

# 单实例（命名互斥量）
class SingleInstance:
    def __init__(self, name: str):
        self.mutex = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{name}")
        self.already_running = (ctypes.GetLastError() == 183)  # ERROR_ALREADY_EXISTS
    def __del__(self):
        if getattr(self, "mutex", None):
            ctypes.windll.kernel32.CloseHandle(self.mutex)

# 开机自启（注册表 HKCU\...\Run）
def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        # 打包：直接运行 exe 本体
        return f'"{sys.executable}"'
    else:
        # 开发：python.exe -u "绝对路径\we_tray.py"
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
    # 简洁“WE”
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
        self.root.withdraw()  # 默认隐藏
        self.root.mainloop()

    def append(self, s: str):
        self._queue.put(s if s.endswith("\n") else s + "\n")

    def show(self):
        self.start()
        if self.root:
            self.root.deiconify()
            self._visible_flag = True
            # 首次打开时把历史刷进去
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
        if not self._visible_flag:
            self.show()
        else:
            self.hide()

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
        si.wShowWindow = 0  # SW_HIDE
        return {"startupinfo": si, "creationflags": CREATE_NO_WINDOW}
    except Exception:
        return {"creationflags": CREATE_NO_WINDOW}

def start_worker_and_reader(console: ConsoleWindow) -> WorkerProc:
    exe = sys.executable
    if not getattr(sys, "frozen", False):
        cmd = [exe, "-u", WORKER_SCRIPT, *WORKER_ARGS]
    else:
        cmd = [exe, "--worker", *WORKER_ARGS]

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        **_win_hidden_popen_kwargs()
    )

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
    except Exception:
        pass

# ------- 托盘 + 自动重建（修复消失问题） --------------------------
class TrayApp:
    def __init__(self):
        self.console = ConsoleWindow("Wallpaper Engine - 实时控制台")
        self.wp = start_worker_and_reader(self.console)

        self._icon = None           # pystray.Icon
        self._icon_thread = None
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._keeper_thread = None
        self._last_taskbar = 0
        self._last_nudge = 0.0

    # 菜单回调
    def on_toggle_console(self, icon, _):
        self.console.toggle()

    def on_exit(self, icon, _):
        self.console.append("[exit] 正在退出...\n")
        self.stop_icon()
        try:
            stop_worker(self.wp, timeout=6)
        finally:
            os._exit(0)  # 避免残留线程阻塞

    def dyn_autostart_text(self, _item):
        return "关闭开机自启" if is_autostart_enabled() else "开启开机自启"

    def on_toggle_autostart(self, icon, _):
        cur = is_autostart_enabled()
        set_autostart(not cur)   # ← 修复：Python 用 not，而不是 !
        self.console.append(f"[autostart] 已设置开机自启 = {not cur}\n")
        # 刷新菜单文本
        try:
            if self._icon:
                self._icon.update_menu()
        except Exception:
            pass

    # 新增：立即更换（重启一次 worker 触发新一轮运行）
    def on_force_switch(self, icon, _):
        self.console.append("[action] 立即更换：正在重启 worker 以立刻执行一轮...\n")
        try:
            stop_worker(self.wp, timeout=2.0)
        finally:
            self.wp = start_worker_and_reader(self.console)
            self.console.append("[action] 已重启 worker，新的运行循环已开始。\n")

    # 构建 pystray Icon
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
        ic = pystray.Icon("WEAutoTray", icon=icon_img, title="WE Auto Runner", menu=menu)
        return ic

    # 运行 Icon（单独线程）
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
                try:
                    self._icon.visible = False
                except Exception:
                    pass
                try:
                    self._icon.stop()
                except Exception:
                    pass
                self._icon = None
            if self._icon_thread:
                self._icon_thread.join(timeout=2.0)
                self._icon_thread = None
            self.console.append("[tray] 托盘图标已停止。\n")

    def rebuild_icon(self):
        self.console.append("[tray] 检测到任务栏变化，重建托盘图标...\n")
        self.stop_icon()
        time.sleep(0.2)  # 稍等任务栏就绪
        self.start_icon()

    # 守护线程：监测任务栏句柄
    def _keeper_loop(self):
        self._last_taskbar = _get_taskbar_hwnd()
        # 初始确保图标已在
        self.start_icon()
        while not self._stop_evt.is_set():
            try:
                h = _get_taskbar_hwnd()
                if h != 0:
                    # 句柄恢复或改变 -> 重建图标
                    if (self._last_taskbar == 0) or (h != self._last_taskbar):
                        if self._last_taskbar != 0:
                            self.rebuild_icon()
                        else:
                            self.start_icon()
                # 轻触发：句柄可能没变但图标丢失，定期触发可见性
                now = time.time()
                if self._last_nudge + 60 <= now:
                    with self._lock:
                        if self._icon:
                            try:
                                self._icon.visible = True
                            except Exception:
                                self.rebuild_icon()
                    self._last_nudge = now

                self._last_taskbar = h
            except Exception as e:
                self.console.append(f"[tray] keeper 异常：{e}\n")
            finally:
                self._stop_evt.wait(KEEPER_POLL_SECONDS)

    def start(self):
        # 启动守护线程 + 控制台缓冲
        self.console.start()
        self._keeper_thread = threading.Thread(target=self._keeper_loop, daemon=True)
        self._keeper_thread.start()
        self.console.append("[tray] 守护线程已启动。\n")

        # 主线程 idle：防止进程退出
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            self.on_exit(None, None)

# ---------------- 程序入口 ----------------
def run_worker_inline():
    """打包后本 exe --worker 模式"""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    base = Path(sys.argv[0]).resolve().parent
    sys.path.insert(0, str(base))
    try:
        import we_auto_fetch
    except Exception as e:
        print("[fatal] 无法导入 we_auto_fetch.py：", e)
        sys.exit(1)
    try:
        we_auto_fetch.main()
    except KeyboardInterrupt:
        print("\n[worker] 收到中断，退出。")
    except Exception as e:
        print("[worker] 未捕获异常：", e)
        time.sleep(0.5)

def main():
    if "--worker" in sys.argv:
        run_worker_inline()
    else:
        si = SingleInstance("WEAutoTrayMutex")
        if si.already_running:
            # 已在运行：可选唤起已有实例，这里简单退出
            return
        app = TrayApp()
        app.start()

if __name__ == "__main__":
    main()