"""单实例保护。

叠加层占用的是一个 ETW 会话和若干全局热键，开两份会互相把对方踢掉，所以必须保证单实例。
实现方式是持有一个命名互斥体，第二个实例发现已存在就自行退出。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\PaceliteOverlaySingleInstance"


class SingleInstance:
    """持有命名互斥体；第二个实例拿到 already_running=True 后应自行退出。"""

    def __init__(self) -> None:
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            kernel32.CloseHandle(wt.HANDLE(self._handle))
            self._handle = None
