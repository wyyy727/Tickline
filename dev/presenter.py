"""合成帧源：用来验证 FPS 采集链路。

一个持续做动画的 Qt Quick 窗口，会让 Qt 通过 D3D11 出帧 —— 正是 PresentMon 跟踪的那种交换链。
机器上没有游戏（或没有独显）时用它当靶子。
"""

import sys

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtQuick import QQuickView
from PySide6.QtGui import QGuiApplication

QML = """
import QtQuick

Rectangle {
    width: 360; height: 220
    color: "#1b2430"
    Rectangle {
        id: box
        width: 70; height: 70; radius: 8
        color: "#48c9ff"
        anchors.centerIn: parent
        NumberAnimation on rotation {
            from: 0; to: 360; duration: 900; loops: Animation.Infinite; running: true
        }
    }
    Text { anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
           color: "#8fa8bf"; text: "presenter"; font.pixelSize: 12; bottomPadding: 6 }
}
"""


def main() -> int:
    app = QGuiApplication(sys.argv[:1])
    view = QQuickView()
    # 用内联 QML 免去额外的 .qml 文件，跑起来就一个窗口在转。
    view.loadData(QML.encode("utf-8"), QUrl("qrc:/inline.qml"))
    view.show()
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    QTimer.singleShot(seconds * 1000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
