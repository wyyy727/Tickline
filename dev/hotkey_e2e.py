"""验证全局热键链路：先手动投递 WM_HOTKEY，再用 SendInput 合成真实按键。

两条路分别对应两种可能的故障：
- PostMessage 成功、真实按键失败 → 注册本身有问题（句柄或冲突）
- 两条都失败 → 原生事件过滤器没被调用
"""

import ctypes
import ctypes.wintypes as wt
import sys

sys.path.insert(0, r"D:\workspace\Tickline-space\Tickline")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from winutil import Hotkeys

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_HOTKEY = 0x0312
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_SHIFT, VK_M = 0x11, 0x10, 0x4D


def send_keys() -> None:
    """合成 Ctrl+Shift+M。"""
    for vk in (VK_CONTROL, VK_SHIFT, VK_M):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (VK_M, VK_SHIFT, VK_CONTROL):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def main() -> int:
    app = QApplication(sys.argv[:1])
    window = QWidget()
    window.show()
    hwnd = int(window.winId())
    fired: list[str] = []

    hotkeys = Hotkeys(hwnd)
    ok = hotkeys.bind_spec("Ctrl+Shift+M", lambda: fired.append("real"))
    print("bind 结果:", ok, "| hwnd:", hwnd, "| 热键 id:", hex(0xA000))

    def post() -> None:
        # 直接投递 WM_HOTKEY，绕过键盘，只测"注册的回调能不能被触发"
        user32.PostMessageW(wt.HWND(hwnd), WM_HOTKEY, 0xA000, 0)
        QTimer.singleShot(200, send)

    def send() -> None:
        print("投递后 fired =", fired)
        send_keys()
        QTimer.singleShot(400, finish)

    def finish() -> None:
        print("合成按键后 fired =", fired)
        hotkeys.release()
        app.quit()

    QTimer.singleShot(300, post)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
