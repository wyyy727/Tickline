"""渲染两种布局（整行 / 按组换行），不改动用户配置。"""

import sys

sys.path.insert(0, r"D:\workspace\Tickline-space\Tickline")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import FakeFrameFeed
from sensors import SensorsClient


def shoot(layout: str) -> None:
    app = QApplication(sys.argv[:1])
    cfg = Config.load()
    cfg.tier = 4
    cfg.font_size = 13
    cfg.layout = layout
    cfg.sections.ssd = True
    cfg.sections.fps = True

    client = SensorsClient()
    if client.snapshot() is None:
        client.start()
    sampler = Sampler(cfg.refresh_ms, precise=client)
    sampler.start()
    window = StripWindow(cfg, sampler, FakeFrameFeed())
    window.show()

    def grab() -> None:
        window._tick()
        out = rf"D:\tmp\layout_{layout}.png"
        window.grab().save(out)
        print(f"[{layout}] 保存 {out} | 尺寸 {window.size().width()}x{window.size().height()}")
        sampler.stop()
        app.quit()

    QTimer.singleShot(1200, grab)
    app.exec()


if __name__ == "__main__":
    shoot(sys.argv[1] if len(sys.argv) > 1 else "row")
