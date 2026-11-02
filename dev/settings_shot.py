"""把设置窗口渲染成 PNG，方便在不看屏幕的情况下检查排版。"""

import sys

sys.path.insert(0, r"D:\workspace\pacecarnew\pacelite")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from config import Config
from settings import SettingsDialog
import winprobe


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = Config.load()
    dialog = SettingsDialog(
        cfg,
        apply_callback=lambda: None,
        presenters=winprobe.visible_window_processes(),
    )
    dialog.show()

    def shoot() -> None:
        dialog.grab().save(r"D:\tmp\settings.png")
        print("[settings] saved")
        app.quit()

    QTimer.singleShot(400, shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
