"""最小复现：单独验证「全局热键 + 原生事件过滤器」这条路径。

用法：python dev/hotkey_probe.py filter|hotkey|both
"""

import ctypes
import ctypes.wintypes as wt
import sys

# 日志写文件：进程被 C++ 侧异常带走时，stdout 可能来不及刷出来。
LOG = open(r"D:\tmp\hk.log", "w", encoding="utf-8", buffering=1)


def log(msg):
    LOG.write(msg + "\n")


user32 = ctypes.WinDLL("user32", use_last_error=True)

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication, QWidget


class Filter(QAbstractNativeEventFilter):
    def __init__(self):
        super().__init__()
        self.count = 0

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 接口命名
        # 注意方法名：PySide6 这里叫 nativeEventFilter，写成 nativeEvent 会导致纯虚函数未实现。
        try:
            self.count += 1
            if self.count % 500 == 0:
                log(f"过滤器调用={self.count} type={event_type!r} msg_type={type(message).__name__}")
        except Exception as exc:  # noqa: BLE001
            log(f"过滤器出错 {exc!r}")
        return False, 0


log("开始")
app = QApplication(sys.argv[:1])
w = QWidget()
w.show()
hwnd = int(w.winId())
log(f"hwnd={hwnd}")

stage = sys.argv[1] if len(sys.argv) > 1 else "both"

if stage in ("filter", "both"):
    filt = Filter()
    QCoreApplication.instance().installNativeEventFilter(filt)
    log("过滤器已安装")

if stage in ("hotkey", "both"):
    ok = user32.RegisterHotKey(wt.HWND(hwnd), 0xB001, 0x0002 | 0x0004 | 0x4000, 0x31)
    log(f"RegisterHotKey={bool(ok)} err={ctypes.get_last_error()}")

QTimer.singleShot(2500, app.quit)
log("进入事件循环")
code = app.exec()
log(f"退出码 {code}")
