"""Win32 薄封装：录制隐藏与全局热键。

这两件事都是叠加层的刚需：长条不能出现在录像里，切档位也不该让玩家离开游戏。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from collections.abc import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

from hotkey import parse as parse_spec

user32 = ctypes.WinDLL("user32", use_last_error=True)

WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


def set_capture_exclusion(hwnd: int, enabled: bool) -> bool:
    """把窗口从屏幕捕获中排除（OBS、Game Bar、截图工具都看不到）。

    在早于 Windows 10 2004 的系统上可能返回 False。
    """
    affinity = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
    return bool(user32.SetWindowDisplayAffinity(wt.HWND(hwnd), affinity))


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callbacks: dict[int, Callable[[], None]]) -> None:
        super().__init__()
        self._callbacks = callbacks
        # Qt 会把同一条 WM_HOTKEY 通过原生事件过滤器投递两次（一次来自消息循环，
        # 一次来自派发器）。不去重的话，按一次"切换显示器"会连跳两块屏，看起来像没反应。
        self._last_id = -1
        self._last_at = 0.0

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 的原生接口命名
        # 注意：PySide6 里这个虚函数叫 nativeEventFilter。若误写成 nativeEvent，等于纯虚函数
        # 没实现，Qt 会在第一条消息到达时从 C++ 抛 NotImplementedError，把解释器线程状态弄脏
        # （之后任何 `with lock:` 都会报 SystemError），最终进程被静默杀掉。
        try:
            if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = wt.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    hotkey_id = int(msg.wParam)
                    now = time.monotonic()
                    if hotkey_id == self._last_id and now - self._last_at < 0.35:
                        return False, 0
                    self._last_id = hotkey_id
                    self._last_at = now
                    callback = self._callbacks.get(hotkey_id)
                    if callback is not None:
                        callback()
        except Exception:  # noqa: BLE001 - 绝不能把异常抛回 C++ 回调
            return False, 0
        return False, 0


class Hotkeys:
    """绑定在叠加层窗口句柄上的 RegisterHotKey 封装。"""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_id = 0xA000
        self._filter = _HotkeyFilter(self._callbacks)
        QCoreApplication.instance().installNativeEventFilter(self._filter)

    def bind(self, modifiers: int, vk: int, callback: Callable[[], None]) -> bool:
        """注册一个组合键；被系统占用时返回 False（不抛异常）。"""
        hotkey_id = self._next_id
        self._next_id += 1
        if not user32.RegisterHotKey(wt.HWND(self.hwnd), hotkey_id, modifiers | MOD_NOREPEAT, vk):
            return False
        self._callbacks[hotkey_id] = callback
        return True

    def bind_spec(self, spec: str, callback: Callable[[], None]) -> bool:
        """按 "Ctrl+Shift+M" 这种字符串注册；写法不合法或被占用返回 False。"""
        parsed = parse_spec(spec)
        if parsed is None:
            return False
        modifiers, vk = parsed
        return self.bind(modifiers, vk, callback)

    def release(self) -> None:
        """注销全部热键，之后可以按新配置重新注册。"""
        for hotkey_id in list(self._callbacks):
            user32.UnregisterHotKey(wt.HWND(self.hwnd), hotkey_id)
        self._callbacks.clear()


# 默认绑定用到的虚拟键码
VK_1, VK_2, VK_3, VK_4 = 0x31, 0x32, 0x33, 0x34
VK_M, VK_T, VK_Q, VK_S = 0x4D, 0x54, 0x51, 0x53
