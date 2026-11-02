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

LIST_MODULES_ALL = 0x03
# 不声明 argtypes 时句柄会被当 int 截断，模块数组的指针也会被猜错类型。
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
psapi.EnumProcessModulesEx.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(wt.HMODULE),
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    wt.DWORD,
]
psapi.EnumProcessModulesEx.restype = wt.BOOL
psapi.EnumProcessModules.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(wt.HMODULE),
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
psapi.EnumProcessModules.restype = wt.BOOL
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
psapi.GetModuleBaseNameW.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD

# 这个标签是给"帧生成"那一段用的，所以只认**帧生成**模块，不能只看超分：
# nvngx_dlss.dll 是 DLSS 超分/光线重建，光开超分并不产生额外帧，标成 DLSS 会误导。
# 同理 FSR 的帧生成在 amdxcffx64.dll（FSR3）里，旧标记漏了它。
_DLSS_MARKERS = ("nvngx_dlssg",)               # DLSS Frame Generation
_FSR_MARKERS = ("amdxcffx64", "ffx_fsr3", "fsr3")  # FSR3 Frame Generation

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


def foreground_pid() -> int | None:
    """当前前台窗口的进程 id；取不到（比如焦点在安全桌面）返回 None。

    用来决定"自动"模式下该跟谁：玩家前台正开着的那个才是他想看的帧率，
    比"谁出帧最忙"可靠 —— 浏览器、Steam 界面、微信都能在后台一直以 60 帧出帧，
    按"最忙"选会选中它们，叠加层就永远显示一个不动的 60。
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return pid.value or None


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
            # 优先用 Ex + LIST_MODULES_ALL：32 位游戏（很多 Unity/老引擎作品）从 64 位进程里
            # 枚举模块时，普通 EnumProcessModules 只能看到 WOW64 层，拿不到游戏的真实模块。
            if not psapi.EnumProcessModulesEx(
                wt.HANDLE(handle), None, 0, ctypes.byref(needed), LIST_MODULES_ALL
            ) and not psapi.EnumProcessModules(
                wt.HANDLE(handle), None, 0, ctypes.byref(needed)
            ):
                continue
            if needed.value == 0:
                continue
            count = needed.value // ctypes.sizeof(wt.HMODULE)
            modules = (wt.HMODULE * max(1, count))()
            if not psapi.EnumProcessModulesEx(
                wt.HANDLE(handle),
                modules,
                ctypes.sizeof(modules),
                ctypes.byref(needed),
                LIST_MODULES_ALL,
            ) and not psapi.EnumProcessModules(
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
