"""渲染大字号的叠加层并把 ✕ 强制显示出来，用于检查它是否被拉变形。"""

import sys

sys.path.insert(0, r"D:\workspace\Tickline-space\Tickline")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import FakeFrameFeed


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = Config.load()
    cfg.font_size = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    cfg.click_through = False

    sampler = Sampler(cfg.refresh_ms)
    sampler.start()
    window = StripWindow(cfg, sampler, FakeFrameFeed())
    window.show()

    def shoot() -> None:
        window._tick()
        window._hover = True   # 强制显示 ✕
        window.repaint()
        out = rf"D:\tmp\close_{cfg.font_size}.png"
        window.grab().save(out)
        print(f"保存 {out} | 字号 {cfg.font_size} | 条高 {window._band_height():.0f}")
        sampler.stop()
        app.quit()

    QTimer.singleShot(600, shoot)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
