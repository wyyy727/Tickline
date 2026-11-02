"""叠加层窗口：一条半透明长条，画出带颜色的「标签 / 数值」小段。

全部内容在同一个 `paintEvent` 里用 QPainter 画完，没有任何子控件 —— 250 ms 刷新一次，成本
只是重绘一小块表面。长条宽度跟着内容走，这正是四个档位看起来像"同一根条的四段长度"的原因。

交互：不穿透时可以拖动（松手写回配置），悬停时右侧出现 ✕ 用于退出。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

import pylog
from config import Config
from metrics import Sampler
from presentmon import FrameStats, PresentMonFeed
from theme import Theme
from tiers import Segment, build_rows, build_segments

PAD_X = 10.0        # 长条左右内边距
SEG_GAP = 9.0       # 小段之间的间距（分隔线画在正中间）
LABEL_GAP = 5.0     # 文字标签与数值的间距
UNIT_GAP = 3.0      # 数值与单位的间距
BAND_GAP = 5.0      # 多行（demo 模式）之间的行距
CLOSE_W = 18.0      # 右侧为 ✕ 预留的宽度（只在悬停时画出来，布局始终稳定）
CLOSE_SIZE = 12.0   # ✕ 本身的边长：固定值，不跟着字号放大
SCREEN_MARGIN = 16  # 贴屏幕左上角时留的边距


def screen_choices() -> list[tuple[str, str]]:
    """返回 [(屏幕名, 显示用标签)]，供设置窗口的下拉框使用。

    标签里带分辨率与"主屏"标记，笔记本外接显示器时能一眼分清哪块是哪块。
    """
    primary = QGuiApplication.primaryScreen()
    choices: list[tuple[str, str]] = []
    for index, screen in enumerate(QGuiApplication.screens()):
        name = screen.name() or f"DISPLAY{index + 1}"
        geo = screen.geometry()
        tag = f"{name}   {geo.width()}×{geo.height()}"
        if screen is primary:
            tag += "  · 主屏"
        choices.append((name, tag))
    return choices


class StripWindow(QWidget):
    def __init__(
        self,
        cfg: Config,
        sampler: Sampler,
        feed: PresentMonFeed,
        demo: bool = False,
        on_close: Callable[[], None] | None = None,
        on_moved: Callable[[int, int], None] | None = None,
    ):
        super().__init__(None)
        self.cfg = cfg
        self.sampler = sampler
        self.feed = feed
        self.demo = demo
        self.on_close = on_close
        self.on_moved = on_moved
        # demo 模式一次性排四行；正常模式只用第 0 行。
        self._segments: list[list[Segment]] = [[] for _ in range(4)]
        self._hover = False
        self._drag_origin: QPoint | None = None
        self._drag_offset = QPoint(0, 0)

        self._apply_flags()
        # 任务栏/悬停提示里显示的是窗口标题；不设的话会沿用解释器名字，变成一个"python"。
        self.setWindowTitle("Tickline")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 数值用等宽字体，位数变化时不会左右抖动（参考图也是这个做法）。
        self._mono = QFont("Consolas")
        self._mono.setStyleHint(QFont.Monospace)
        self._mono.setPixelSize(cfg.font_size)
        self._unit_font = QFont(self._mono)
        self._unit_font.setPixelSize(max(8, int(round(cfg.font_size * 0.74))))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(cfg.refresh_ms)
        self._series: list[float] = []
        self._positioned = False
        # 渲染故障的节流时间戳（每种故障各自 10 秒一次日志）。
        self._last_fault: dict[str, float] = {}

    # ---- 对外接口 --------------------------------------------------------
    def _apply_flags(self) -> None:
        """按配置重建窗口标志。窗口要在任务栏出现就必须去掉 Qt.Tool。"""
        flags = Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        if self.cfg.show_in_taskbar:
            flags |= Qt.Window
        else:
            flags |= Qt.Tool
        if self.cfg.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        if self.cfg.click_through:
            flags |= Qt.WindowTransparentForInput
        # setWindowFlags 会重建原生窗口（隐藏再显示），设置里每拖一格都调就是一片闪烁。
        if self.windowFlags() != flags:
            self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, bool(self.cfg.click_through))

    def apply_config(self) -> None:
        """设置改动后重建窗口标志并立即重绘（穿透、置顶、任务栏、字号都可能变）。"""
        was_visible = self.isVisible()
        self._apply_flags()
        self._mono.setPixelSize(self.cfg.font_size)
        self._unit_font.setPixelSize(max(8, int(round(self.cfg.font_size * 0.74))))
        # 刷新间隔也可能在设置里改了，定时器要跟着换档。
        self._timer.start(self.cfg.refresh_ms)
        if was_visible:
            self.show()
        self._keep_visible()
        self._refresh()

    def set_tier(self, tier: int) -> None:
        self.cfg.tier = min(4, max(1, tier))
        self._refresh()

    def set_game_visible(self, visible: bool) -> None:
        """"只在游戏运行时显示"的实现入口：只切换可见性，不动位置和尺寸。"""
        if visible and not self.isVisible():
            self.show()
            self._refresh()
        elif not visible and self.isVisible():
            self.hide()

    # ---- 数据 ------------------------------------------------------------
    def _tick(self) -> None:
        """定时器每格：先推进一次采样，再重绘。

        推进（sample）和读取（stats）分开，重绘、判定"有没有游戏在跑"都只读，
        不会再把曲线历史灌成两倍密度。

        两边都兜异常：数据层任何一处抛错，以前会直接把 GUI 线程的槽炸掉 —— 窗口
        停在 show() 时的默认尺寸（640x480、全透明），任务栏有条目但屏幕上看不见。
        """
        try:
            self.feed.sample()
        except Exception as exc:  # noqa: BLE001 - 界面不能因为数据层异常停摆
            self._fault("sample", exc)
        try:
            self._refresh()
        except Exception as exc:  # noqa: BLE001
            self._fault("refresh", exc)

    def _fault(self, where: str, exc: Exception) -> None:
        """记录一次渲染故障，同一种故障 10 秒内只落一次盘，避免刷爆日志。"""
        stamp = time.monotonic()
        previous = self._last_fault.get(where)
        if previous is not None and stamp - previous < 10.0:
            return
        self._last_fault[where] = stamp
        pylog.log(f"[overlay] {where} 失败: {type(exc).__name__}: {exc}")

    def _refresh(self) -> None:
        snap = self.sampler.latest()
        try:
            fps = self.feed.stats()
        except Exception as exc:  # noqa: BLE001 - 读数失败不该让整块界面停摆
            self._fault("stats", exc)
            fps = FrameStats()
        self._series = list(fps.series)
        if self.demo:
            # demo 每档一行，方便并排比对；不套用换行布局。
            self._segments = [
                build_segments(tier, snap, fps, self.cfg.sections, self.cfg.sparkline)
                for tier in (1, 2, 3, 4)
            ]
        else:
            segments = build_segments(
                self.cfg.tier, snap, fps, self.cfg.sections, self.cfg.sparkline
            )
            self._segments = build_rows(segments, self.cfg.layout)
        size = self._measure()
        # 只在尺寸真的变了才 resize，避免每帧都触发窗口重排。
        if self.size() != size:
            self.resize(size)
            self._ensure_placed()
        self.update()

    def _ensure_placed(self) -> None:
        """首次定位：保存的坐标还落在某块屏幕上就用它，否则落到指定屏幕的左上角。

        之后只在"整块窗口已经落到所有屏幕之外"时才搬回来：以前每次内容宽度变化都按
        配置坐标重新 move()，手动拖过位置的人会看到长条被拽回原处。
        """
        if self._positioned:
            self._keep_visible()
            return
        self._positioned = True
        if (
            self.cfg.x >= 0
            and self.cfg.y >= 0
            and self._point_on_a_screen(self.cfg.x, self.cfg.y)
        ):
            self.move(self.cfg.x, self.cfg.y)
            return
        self._place_on(self._screen_by_name(self.cfg.monitor))

    def _keep_visible(self) -> None:
        """窗口整体已经不在任何屏幕里（拔掉外接屏、改了分辨率）就搬回目标屏。"""
        rect = self.frameGeometry()
        for screen in QGuiApplication.screens():
            if screen.geometry().intersects(rect):
                return
        self._place_on(self._screen_by_name(self.cfg.monitor))

    @staticmethod
    def _point_on_a_screen(x: int, y: int) -> bool:
        """坐标是否还在任何一块屏幕内。拔掉外接屏后旧坐标会悬空，必须重算。"""
        for screen in QGuiApplication.screens():
            if screen.geometry().contains(x, y):
                return True
        return False

    @staticmethod
    def _screen_by_name(name: str):
        """按名字找屏幕；找不到（或被拔掉）退回主屏。"""
        if name:
            for screen in QGuiApplication.screens():
                if screen.name() == name:
                    return screen
        return QGuiApplication.primaryScreen()

    def _place_on(self, screen) -> None:
        """贴到某块屏幕可用区域的左上角，并把坐标写回配置。"""
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.left() + SCREEN_MARGIN, area.top() + SCREEN_MARGIN)
        self.cfg.x, self.cfg.y = self.x(), self.y()

    def move_to_screen(self, name: str) -> None:
        """把叠加层搬到指定屏幕（设置里的下拉框和托盘的"切换显示器"都走这里）。"""
        self.cfg.monitor = name or ""
        screen = self._screen_by_name(self.cfg.monitor)
        if screen is not None:
            self.cfg.monitor = screen.name() or ""
        self._place_on(screen)
        self._positioned = True
        if self.on_moved is not None:
            self.on_moved(self.cfg.x, self.cfg.y)
        self.update()

    def cycle_screen(self) -> None:
        """按 Qt 给出的顺序在屏幕之间轮换；只有一块屏幕时什么也不做。"""
        screens = QGuiApplication.screens()
        if len(screens) < 2:
            return
        current = self._screen_by_name(self.cfg.monitor)
        index = screens.index(current) if current in screens else 0
        self.move_to_screen(screens[(index + 1) % len(screens)].name())

    # ---- 度量 ------------------------------------------------------------
    def _band_height(self) -> float:
        return max(24.0, round(self.cfg.font_size * 2.05))

    def _close_enabled(self) -> bool:
        """穿透模式收不到鼠标，留着 ✕ 也是死按钮；demo 模式也不需要。"""
        return not self.cfg.click_through and not self.demo

    def _segment_width(self, seg: Segment, value_font: QFontMetricsF) -> float:
        if seg.kind == "spark":
            return float(seg.spark_width)
        unit_font = QFontMetricsF(self._unit_font)
        width = value_font.horizontalAdvance(seg.value)
        if seg.label:
            width += value_font.horizontalAdvance(seg.label) + self._label_gap(seg)
        if seg.unit:
            width += unit_font.horizontalAdvance(seg.unit.upper()) + UNIT_GAP
        return width

    @staticmethod
    def _label_gap(seg: Segment) -> float:
        """箭头标签紧贴数字，文字标签留正常间距。"""
        return 1.5 if seg.tight else LABEL_GAP

    def _row_width(self, segments: list[Segment]) -> float:
        value_font = QFontMetricsF(self._mono)
        close = CLOSE_W if self._close_enabled() else 0.0
        if not segments:
            return PAD_X * 2 + close
        total = sum(self._segment_width(seg, value_font) for seg in segments)
        total += SEG_GAP * (len(segments) - 1)
        return PAD_X * 2 + total + close

    NO_DATA_TEXT = "Tickline · 等待数据"

    def _placeholder_width(self) -> float:
        """一条数据都没有时的占位条宽度。

        以前 segments 全空时 _measure 会给出固定 240 的宽度，paintEvent 又什么都不画，
        结果窗口是一块全透明的默认矩形 —— 任务栏有条目、屏幕上却什么都没有。
        """
        value_font = QFontMetricsF(self._mono)
        close = CLOSE_W if self._close_enabled() else 0.0
        return PAD_X * 2 + value_font.horizontalAdvance(self.NO_DATA_TEXT) + close

    def _measure(self):
        from PySide6.QtCore import QSize

        widths = [self._row_width(row) for row in self._segments if row]
        width = int(max(widths)) if widths else int(self._placeholder_width())
        rows = sum(1 for row in self._segments if row) or 1
        height = int(self._band_height() * rows + BAND_GAP * (rows - 1))
        return QSize(max(120, width), max(int(self._band_height()), height))

    # ---- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        theme = Theme(self.cfg.saturation, self.cfg.opacity)
        band_h = self._band_height()
        visible = 0
        for row in self._segments:
            if not row:
                continue
            top = visible * (band_h + BAND_GAP)
            visible += 1
            # 每行的底色只铺到自己的内容宽度：这样四个档位才是"同一根条的不同长度"，
            # 而不是四个等宽面板。
            width = min(self.width(), self._row_width(row))
            band = QRectF(0.0, top, width, band_h)
            painter.fillRect(band, theme.background)
            self._paint_row(painter, band, row, theme)
        if visible == 0:
            # 数据一条都没到（PresentMon 起不来、采样异常等）：画一条「等待数据」的占位条，
            # 让用户至少知道程序活着，而不是对着一块看不见的透明矩形猜。
            width = min(float(self.width()), self._placeholder_width())
            band = QRectF(0.0, 0.0, width, band_h)
            painter.fillRect(band, theme.background)
            painter.setFont(self._mono)
            painter.setPen(theme.unit)
            value_font = QFontMetricsF(self._mono)
            baseline = band.top() + (band.height() + value_font.capHeight()) / 2.0
            painter.drawText(QPointF(band.left() + PAD_X, baseline), self.NO_DATA_TEXT)
        if self._hover and self._close_enabled():
            self._paint_close(painter, theme)
        painter.end()

    def _band_width(self) -> float:
        """第一行实际铺到的宽度：column 布局下比整窗窄，✕ 要贴着这一行的右端。"""
        for row in self._segments:
            if row:
                return min(float(self.width()), self._row_width(row))
        return min(float(self.width()), self._placeholder_width())

    def _close_rect(self) -> QRectF:
        """✕ 的点击区域：固定 12 px，垂直居中，不随字号变大，贴着第一行色块的右端。"""
        size = CLOSE_SIZE
        band_h = self._band_height()
        return QRectF(
            self._band_width() - CLOSE_W / 2.0 - size / 2.0,
            (band_h - size) / 2.0,
            size,
            size,
        )

    def _paint_close(self, painter: QPainter, theme: Theme) -> None:
        rect = self._close_rect()
        hovering = rect.contains(self.mapFromGlobal(self.cursor().pos()))
        color = QColor(theme.spark_bad if hovering else theme.unit)
        pen = QPen(color)
        pen.setWidthF(1.4)
        painter.setPen(pen)
        inset = 2.0
        painter.drawLine(
            QPointF(rect.left() + inset, rect.top() + inset),
            QPointF(rect.right() - inset, rect.bottom() - inset),
        )
        painter.drawLine(
            QPointF(rect.right() - inset, rect.top() + inset),
            QPointF(rect.left() + inset, rect.bottom() - inset),
        )

    def _paint_row(self, painter: QPainter, band: QRectF, segments: list[Segment], theme: Theme) -> None:
        value_font = QFontMetricsF(self._mono)
        unit_font = QFontMetricsF(self._unit_font)
        # 以"大写字母高度"为基准居中，比用字体 ascent 更贴近视觉中线。
        baseline = band.top() + (band.height() + value_font.capHeight()) / 2.0
        x = band.left() + PAD_X
        for index, seg in enumerate(segments):
            if seg.kind == "spark":
                width = float(seg.spark_width)
                box = QRectF(x, band.top() + 3.0, width, band.height() - 6.0)
                self._paint_spark(painter, box, seg, theme)
                x += width
            else:
                if seg.label:
                    painter.setFont(self._mono)
                    painter.setPen(theme.label(seg.color))
                    painter.drawText(QPointF(x, baseline), seg.label)
                    x += value_font.horizontalAdvance(seg.label) + self._label_gap(seg)
                painter.setFont(self._mono)
                painter.setPen(theme.value)
                painter.drawText(QPointF(x, baseline), seg.value)
                x += value_font.horizontalAdvance(seg.value)
                if seg.unit:
                    x += UNIT_GAP
                    painter.setFont(self._unit_font)
                    painter.setPen(theme.unit)
                    painter.drawText(QPointF(x, baseline), seg.unit.upper())
                    x += unit_font.horizontalAdvance(seg.unit.upper())
            # 小段之间画一条细分隔线，和参考图一样
            if index != len(segments) - 1:
                x += SEG_GAP / 2.0
                pen = QPen(theme.separator)
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.drawLine(QPointF(x, band.top() + 4.0), QPointF(x, band.bottom() - 4.0))
                x += SEG_GAP / 2.0

    def _paint_spark(self, painter: QPainter, box: QRectF, seg: Segment, theme: Theme) -> None:
        """帧率曲线；低于窗口均值 70% 的点标红，对应参考图里的掉帧红点。"""
        series = self._series
        if len(series) < 2:
            # 没有数据时画一条淡横线，提示"曲线在这里，只是还没数据"
            pen = QPen(QColor(theme.separator))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(QPointF(box.left(), box.center().y()), QPointF(box.right(), box.center().y()))
            return
        low, high = min(series), max(series)
        span = max(1e-6, high - low)
        # 上下各留 2 px，峰值和谷值不会被裁掉
        top = box.top() + 2.0
        usable = max(1.0, box.height() - 4.0)
        step = box.width() / (len(series) - 1)
        points = [
            QPointF(box.left() + index * step, top + usable * (1.0 - (value - low) / span))
            for index, value in enumerate(series)
        ]
        pen = QPen(theme.spark_ok)
        pen.setWidthF(1.4)
        painter.setPen(pen)
        for start in range(len(points) - 1):
            painter.drawLine(points[start], points[start + 1])
        # 掉帧点：低于窗口内均值的 70%，画成红点
        mean = sum(series) / len(series)
        threshold = mean * 0.7
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.spark_bad))
        for index, value in enumerate(series):
            if value < threshold:
                painter.drawEllipse(points[index], 1.3, 1.3)
        painter.setBrush(Qt.NoBrush)

    # ---- 鼠标交互 --------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        """任务栏的 ✕ 或 Alt+F4 都要真的退出程序，而不是把窗口藏起来。

        因为主窗口一关 Qt 不会自动退出（还有托盘），所以这里显式走退出回调。
        """
        if self.on_close is not None:
            event.accept()
            self.on_close()
        else:
            super().closeEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._close_enabled():
            self._hover = True
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover:
            self._hover = False
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self._hover and self._close_rect().contains(event.position()):
            if self.on_close is not None:
                self.on_close()
            return
        # 记下"鼠标在窗口内的偏移"，拖动时保持相对位置不变
        self._drag_origin = event.globalPosition().toPoint()
        self._drag_offset = self._drag_origin - self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is None:
            return
        target = event.globalPosition().toPoint() - self._drag_offset
        self.move(target)
        self.update()  # ✕ 的命中状态跟着鼠标走

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is None:
            return
        self._drag_origin = None
        self.cfg.x = self.x()
        self.cfg.y = self.y()
        # 拖到另一块屏幕上时同步"当前显示器"，否则设置里显示的会是旧屏幕。
        center = self.frameGeometry().center()
        for screen in QGuiApplication.screens():
            if screen.geometry().contains(center):
                self.cfg.monitor = screen.name() or ""
                break
        if self.on_moved is not None:
            self.on_moved(self.cfg.x, self.cfg.y)
        self.update()
