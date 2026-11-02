"""分步探针：定位叠加层在哪一步起不来（哪一步挂了就停在哪一行）。"""

import sys
import traceback

sys.path.insert(0, r"D:\workspace\pacecarnew\pacelite")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication


def step(name):
    print(f"[probe] {name}", flush=True)


step("导入模块")
from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import PresentMonFeed

step("创建 QApplication")
app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)

step("读配置")
cfg = Config.load()
cfg.tier = 4

step("启动采样")
sampler = Sampler(cfg.refresh_ms)
sampler.start()

step("跳过 PresentMon，只建对象")
feed = PresentMonFeed(target="", refresh_ms=cfg.refresh_ms)

step("创建窗口")
try:
    window = StripWindow(cfg, sampler, feed, demo=True)
except Exception:
    traceback.print_exc()
    raise
step(f"窗口已创建 {window!r}")
window.show()
step("窗口已显示")


def shoot():
    try:
        step("截图：刷新数据")
        window._refresh()
        QGuiApplication.processEvents()
        step(f"截图：窗口尺寸={window.size()}")
        ok = window.grab().save(r"D:\tmp\probe.png")
        step(f"截图：保存结果={ok}")
    except Exception:
        traceback.print_exc()
    finally:
        app.quit()


QTimer.singleShot(2000, shoot)
step("进入事件循环")
code = app.exec()
step(f"退出码 {code}")
