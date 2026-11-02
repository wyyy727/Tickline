"""在真实的 StripWindow 上验证：注册热键 → 合成按键 → 窗口是否真的换了屏幕。"""

import ctypes
import sys

sys.path.insert(0, r"D:\workspace\Tickline-space\Tickline")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import FakeFrameFeed
from winutil import Hotkeys

user32 = ctypes.WinDLL("user32", use_last_error=True)
VK_CONTROL, VK_SHIFT, VK_M = 0x11, 0x10, 0x4D
KEYEVENTF_KEYUP = 0x0002


def send_keys() -> None:
    for vk in (VK_CONTROL, VK_SHIFT, VK_M):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (VK_M, VK_SHIFT, VK_CONTROL):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = Config.load()
    sampler = Sampler(cfg.refresh_ms)
    sampler.start()
    window = StripWindow(cfg, sampler, FakeFrameFeed())
    window.show()
    window._tick()

    hwnd = int(window.winId())
    hotkeys = Hotkeys(hwnd)
    fired: list[str] = []
    ok = hotkeys.bind_spec(
        cfg.hotkeys.cycle_monitor, lambda: (fired.append("hit"), window.cycle_screen())
    )
    print(f"注册 {cfg.hotkeys.cycle_monitor}: {ok} | hwnd={hwnd}")
    print("按前：屏幕 =", window.screen().name(), "位置 =", window.x(), window.y())

    def press() -> None:
        send_keys()
        QTimer.singleShot(400, report)

    def report() -> None:
        print("回调次数 =", len(fired))
        print("按后：屏幕 =", window.screen().name(), "位置 =", window.x(), window.y())
        hotkeys.release()
        sampler.stop()
        app.quit()

    QTimer.singleShot(500, press)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
