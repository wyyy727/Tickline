"""Win32 探测：全屏窗口归属进程、目标进程加载的帧生成模块。

两件事都靠 ctypes 直接问系统，不引入额外依赖：

- `fullscreen_pids()`：哪些进程拥有"覆盖整块屏幕"的可见窗口。用来判断"游戏是不是在跑"。
- `frame_generation_label(pid)`：目标进程有没有加载 DLSS / FSR 的帧生成模块。用来决定
  帧生成那段显示 `DLSS`、`FSR` 还是中性的 `FG`。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# 模块名 → 标签。命中 nvngx_dlssg 就是 NVIDIA 的帧生成，命中 ffx 就是 AMD 的。
_DLSS_MARKERS = ("nvngx_dlssg", "nvngx_dlss", "_nvngx")
_FSR_MARKERS = ("ffx_fsr", "fidelityfx", "amd_fidelityfx_framegeneration", "fsr3")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MONITOR_DEFAULTTONEAREST = 2

# 桌面上永远"铺满屏幕"的系统窗口：桌面本身（Program Manager）、输入法体验层等。
# 不排除它们的话，"有全屏窗口"这个判定永远为真，游戏模式就形同虚设。
DESKTOP_PROCESSES = frozenset(
    {
        "explorer.exe",
        "textinputhost.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "lockapp.exe",
        "dwm.exe",
    }
)


class MONITORINFO(ctypes.Structure):
    """ctypes.wintypes 里没有 MONITORINFO，这里按 Win32 定义补齐。"""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


@dataclass(frozen=True)
class FullscreenWindow:
    pid: int
    title: str


def _monitor_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    monitor = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return None
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(wt.HMONITOR(monitor), ctypes.byref(info)):
        return None
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def _window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def _process_name(pid: int) -> str:
    """进程可执行文件名（小写）；取不到返回空串。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(260)
        buffer = ctypes.create_unicode_buffer(260)
        if not kernel32.QueryFullProcessImageNameW(
            wt.HANDLE(handle), 0, buffer, ctypes.byref(size)
        ):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(wt.HANDLE(handle))


def fullscreen_pids() -> list[FullscreenWindow]:
    """返回拥有"基本铺满显示器"的可见顶层窗口的进程。

    判定用窗口矩形与显示器矩形比较，容差 2 px：无边框全屏、独占全屏都能命中；最大化窗口
    因为留了任务栏高度会被排除，正是我们想要的。
    """
    found: list[FullscreenWindow] = []
    seen: set[int] = set()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(wt.HWND(hwnd)):
            return True
        if user32.IsIconic(wt.HWND(hwnd)):  # 最小化
            return True
        if user32.GetWindowTextLengthW(wt.HWND(hwnd)) == 0:
            # 没有标题的窗口不一定是游戏，也可能是输入法/提示层，先跳过以免误判。
            return True
        window = _window_rect(hwnd)
        monitor = _monitor_rect(hwnd)
        if not window or not monitor:
            return True
        tolerance = 2
        covers = (
            window[0] <= monitor[0] + tolerance
            and window[1] <= monitor[1] + tolerance
            and window[2] >= monitor[2] - tolerance
            and window[3] >= monitor[3] - tolerance
        )
        if not covers:
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        if pid.value and pid.value not in seen:
            if _process_name(pid.value) in DESKTOP_PROCESSES:
                return True
            seen.add(pid.value)
            length = user32.GetWindowTextLengthW(wt.HWND(hwnd))
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(wt.HWND(hwnd), buffer, length + 1)
            found.append(FullscreenWindow(pid=pid.value, title=buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def loaded_modules(pid: int) -> list[str]:
    """列出进程已加载的模块名（小写）。没有权限时返回空列表。"""
    for access in (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                   PROCESS_QUERY_LIMITED_INFORMATION):
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            continue
        try:
            needed = wt.DWORD(0)
            if not psapi.EnumProcessModules(
                wt.HANDLE(handle), None, 0, ctypes.byref(needed)
            ):
                continue
            if needed.value == 0:
                continue
            count = needed.value // ctypes.sizeof(wt.HMODULE)
            modules = (wt.HMODULE * max(1, count))()
            if not psapi.EnumProcessModules(
                wt.HANDLE(handle),
                modules,
                ctypes.sizeof(modules),
                ctypes.byref(needed),
            ):
                continue
            names: list[str] = []
            size = wt.DWORD(ctypes.sizeof(ctypes.c_wchar) * 260)
            buffer = ctypes.create_unicode_buffer(260)
            for module in modules:
                if not module:
                    continue
                if psapi.GetModuleBaseNameW(
                    wt.HANDLE(handle), wt.HMODULE(module), buffer, 260
                ):
                    names.append(buffer.value.lower())
            return names
        finally:
            kernel32.CloseHandle(wt.HANDLE(handle))
    return []


def visible_window_processes() -> list[str]:
    """当前有可见窗口的进程名（小写、去重）。

    给设置里的"监视进程"下拉用：光靠"正在出帧的进程"往往只有一两项甚至为空，
    而用户想选的通常是屏幕上开着的那个程序。
    """
    names: set[str] = set()

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(wt.HWND(hwnd)):
            return True
        if user32.GetWindowTextLengthW(wt.HWND(hwnd)) == 0:
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return True
        name = _process_name(pid.value)
        if name and name not in DESKTOP_PROCESSES:
            names.add(name)
        return True

    user32.EnumWindows(callback, 0)
    return sorted(names)


def frame_generation_label(pid: int | None) -> str | None:
    """判断目标进程用的是 DLSS 还是 FSR 帧生成；判断不出来返回 None。"""
    if not pid:
        return None
    names = loaded_modules(pid)
    if not names:
        return None
    joined = "\n".join(names)
    if any(marker in joined for marker in _DLSS_MARKERS):
        return "DLSS"
    if any(marker in joined for marker in _FSR_MARKERS):
        return "FSR"
    return None
