"""单行叠加层的配色系统。

参考图的做法是：每个指标的**标签**带颜色、数值一律白色，所以这里也是一色一标签；
再叠一个全局饱和度乘子，方便贴合不同游戏的画面色调。
"""

from __future__ import annotations

import colorsys

from PySide6.QtGui import QColor

# 标签色取自参考图：帧生成洋红、CPU 黄绿、GPU 绿、内存紫。数值保持白色，让数字最抢眼。
LABEL_COLORS: dict[str, str] = {
    "fg": "#ff4d8d",        # 帧生成帧率（DLSS / FSR）
    "fps": "#3ddc84",       # 应用实际出帧帧率
    "cpu": "#c8f04a",
    "gpu": "#5ef08a",
    "ram": "#c98bff",
    "cpu_clock": "#c8f04a",
    "gpu_mem": "#5ef08a",
    "temp": "#ffb454",
    "ssd": "#62c8ff",
    "net": "#ffa14d",
    "misc": "#9aa3ad",
}

VALUE_COLOR = "#ffffff"
UNIT_COLOR = "#98a0aa"
SEPARATOR_COLOR = "#4b4f56"
BACKGROUND_COLOR = "#000000"
SPARK_OK = "#3ddc84"
SPARK_BAD = "#ff5252"


def _apply_saturation(hex_color: str, saturation: float) -> QColor:
    """在 HSV 空间里按比例调整饱和度，明度与透明度不变。"""
    color = QColor(hex_color)
    if abs(saturation - 1.0) < 1e-3:
        return color
    h, s, v = colorsys.rgb_to_hsv(color.redF(), color.greenF(), color.blueF())
    s = max(0.0, min(1.0, s * saturation))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return QColor.fromRgbF(r, g, b, color.alphaF())


class Theme:
    """一次绘制用到的解析后颜色集合。"""

    def __init__(self, saturation: float = 1.0, opacity: float = 0.72) -> None:
        self.saturation = saturation
        self.background = QColor(BACKGROUND_COLOR)
        self.background.setAlphaF(max(0.0, min(1.0, opacity)))
        self.value = QColor(VALUE_COLOR)
        self.unit = QColor(UNIT_COLOR)
        self.separator = QColor(SEPARATOR_COLOR)
        self.spark_ok = QColor(SPARK_OK)
        self.spark_bad = QColor(SPARK_BAD)
        self.labels = {k: _apply_saturation(v, saturation) for k, v in LABEL_COLORS.items()}

    def label(self, key: str) -> QColor:
        """取某个标签色；未知键退回中性灰，避免整段消失。"""
        return self.labels.get(key, self.labels["misc"])
