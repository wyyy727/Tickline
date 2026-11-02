"""验证多屏切换：把窗口搬到另一块屏幕、轮换、以及坐标悬空时的回退。"""

import sys

sys.path.insert(0, r"D:\workspace\pacecarnew\pacelite")

from PySide6.QtWidgets import QApplication

from config import Config
from metrics import Sampler
from overlay import StripWindow, screen_choices
from presentmon import FakeFrameFeed


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = Config.load()
    sampler = Sampler(cfg.refresh_ms)
    sampler.start()
    window = StripWindow(cfg, sampler, FakeFrameFeed())
    window.show()
    window._refresh()

    print("屏幕清单:")
    for name, label in screen_choices():
        print("   ", repr(name), label)

    print("初始所在屏幕:", window.screen().name(), "位置:", window.x(), window.y())
    choices = screen_choices()
    if len(choices) > 1:
        target = choices[1][0]
        window.move_to_screen(target)
        print("搬到", repr(target), "后：屏幕 =", window.screen().name(), "位置 =", window.x(), window.y())
        window.cycle_screen()
        print("轮换一次后：屏幕 =", window.screen().name(), "配置里的显示器 =", repr(cfg.monitor))

    # 模拟"外接屏被拔掉，保存的坐标悬空"
    cfg.x, cfg.y = 99999, 99999
    window._ensure_placed()
    print("坐标悬空后回退到：屏幕 =", window.screen().name(), "位置 =", window.x(), window.y())

    sampler.stop()
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
