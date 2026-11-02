"""设置窗口。

所有控件都是即时生效的：一改动就回写配置并让叠加层重建，不需要"确定"按钮。
这样调字号/透明度/饱和度的过程可以直接看到效果。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import Config
from hotkey import from_qt
from overlay import screen_choices
from sensors import pawnio_state
from sysintegrate import autostart_enabled, set_autostart
from tiers import TIER_NAMES

AUTO_TARGET = "自动（选最忙的出帧进程）"


class HotkeyEdit(QPushButton):
    """点一下进入录入状态，按下组合键即生效；Esc 取消。

    录入期间必须先注销程序已注册的全局热键，否则用户想录 Ctrl+Shift+M 时，
    这个组合会被系统直接吃掉、窗口根本收不到按键。
    """

    captured = Signal(str)

    def __init__(self, spec: str, on_capture_start: Callable[[], None]) -> None:
        super().__init__(spec)
        self._spec = spec
        self._capturing = False
        self._on_capture_start = on_capture_start
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self._start_capture)

    def spec(self) -> str:
        return self._spec

    def _start_capture(self) -> None:
        self._capturing = True
        self._on_capture_start()
        self.setText("按下组合键…")
        self.setFocus()

    def _finish(self, spec: str) -> None:
        self._capturing = False
        self._spec = spec
        self.setText(spec)
        self.captured.emit(spec)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._capturing:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Escape:
            self._finish(self._spec)
            return
        if event.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            # 只按住修饰键时先不判定，等真正的按键下来。
            return
        spec = from_qt(event.modifiers(), event.key())
        if spec is None:
            self.setText("需要 Ctrl/Shift/Alt 组合")
            return
        self._finish(spec)


class SettingsDialog(QDialog):
    def __init__(
        self,
        cfg: Config,
        apply_callback: Callable[[], None],
        presenters: list[str] | None = None,
        on_monitor_change: Callable[[str], None] | None = None,
        on_hotkeys_changed: Callable[[], str | None] | None = None,
        suspend_hotkeys: Callable[[], None] | None = None,
        on_precise_changed: Callable[[bool], str | None] | None = None,
        on_remove_driver: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(None)
        self.cfg = cfg
        self._apply = apply_callback
        self._on_monitor_change = on_monitor_change
        self._on_hotkeys_changed = on_hotkeys_changed
        self._suspend_hotkeys = suspend_hotkeys
        self._on_precise_changed = on_precise_changed
        self._on_remove_driver = on_remove_driver
        # 构造期间控件会自发信号（setValue/setChecked 等），此时控件还没建全，
        # 让 _push 提前返回，否则会用半成品状态覆盖用户配置。
        self._ready = False
        self.setWindowTitle("Pacelite 设置")
        self.setMinimumWidth(360)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # ---- 显示器 ----
        # 笔记本外接屏时这里是唯一的"放哪块屏"入口；空值表示跟随主屏。
        self.monitor_box = QComboBox()
        self.monitor_box.addItem("自动（跟随主屏）", "")
        for name, label in screen_choices():
            self.monitor_box.addItem(label, name)
        current = self.monitor_box.findData(cfg.monitor or "")
        self.monitor_box.setCurrentIndex(current if current >= 0 else 0)
        self.monitor_box.currentIndexChanged.connect(self._push_monitor)
        form.addRow("显示器", self.monitor_box)

        # ---- 显示 ----
        self.tier_box = QComboBox()
        for tier, name in TIER_NAMES.items():
            self.tier_box.addItem(f"{tier} · {name}", tier)
        self.tier_box.setCurrentIndex(max(0, min(3, cfg.tier - 1)))
        self.tier_box.currentIndexChanged.connect(self._push)
        form.addRow("显示档位", self.tier_box)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 28)
        self.font_spin.setValue(cfg.font_size)
        self.font_spin.setSuffix(" px")
        self.font_spin.valueChanged.connect(self._push)
        form.addRow("文字大小", self.font_spin)

        self.opacity_slider, opacity_row = self._slider_row(cfg.opacity * 100.0, 10, 100)
        form.addRow("背景透明度", opacity_row)

        self.saturation_slider, saturation_row = self._slider_row(cfg.saturation * 100.0, 0, 200)
        form.addRow("颜色饱和度", saturation_row)

        self.layout_box = QComboBox()
        self.layout_box.addItem("整行显示", "row")
        self.layout_box.addItem("按组换行（长方形）", "column")
        self.layout_box.setCurrentIndex(1 if cfg.layout == "column" else 0)
        self.layout_box.currentIndexChanged.connect(self._push)
        form.addRow("布局", self.layout_box)

        # ---- 显示内容 ----
        # 2 列网格，避免六个复选框把窗口拉得很长。
        section_host = QWidget()
        grid = QGridLayout(section_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        self.section_boxes: dict[str, QCheckBox] = {}
        for index, (key, label) in enumerate(
            (
                ("fps", "帧率"),
                ("cpu", "CPU"),
                ("gpu", "GPU"),
                ("vram", "显存"),
                ("ram", "内存"),
                ("ssd", "硬盘"),
            )
        ):
            box = QCheckBox(label)
            box.setChecked(getattr(cfg.sections, key))
            box.toggled.connect(self._push)
            self.section_boxes[key] = box
            grid.addWidget(box, index // 3, index % 3)
        form.addRow("显示内容", section_host)

        # ---- 目标进程 ----
        self.target_box = QComboBox()
        self.target_box.setEditable(True)
        self.target_box.addItem(AUTO_TARGET)
        for name in sorted(set(presenters or [])):
            self.target_box.addItem(name)
        self.target_box.setCurrentText(cfg.target_process or AUTO_TARGET)
        # 编辑框内容与下拉选择都要回写，所以两个信号都接。
        self.target_box.currentTextChanged.connect(self._push)
        form.addRow("监视进程", self.target_box)

        # ---- 行为 ----
        self.game_only_box = QCheckBox("只在游戏运行时显示")
        self.game_only_box.setChecked(cfg.game_only)
        self.game_only_box.toggled.connect(self._push)
        form.addRow("", self.game_only_box)

        self.top_box = QCheckBox("始终置顶")
        self.top_box.setChecked(cfg.always_on_top)
        self.top_box.toggled.connect(self._push)
        form.addRow("", self.top_box)

        self.click_box = QCheckBox("鼠标穿透")
        self.click_box.setChecked(cfg.click_through)
        self.click_box.toggled.connect(self._push)
        form.addRow("", self.click_box)

        self.capture_box = QCheckBox("录制/截图里隐藏")
        self.capture_box.setChecked(cfg.capture_exclusion)
        self.capture_box.toggled.connect(self._push)
        form.addRow("", self.capture_box)

        self.taskbar_box = QCheckBox("显示在任务栏")
        self.taskbar_box.setChecked(cfg.show_in_taskbar)
        self.taskbar_box.toggled.connect(self._push)
        form.addRow("", self.taskbar_box)

        # 开机自启默认关闭。勾选状态以**注册表实际值**为准，避免配置和系统不一致时界面撒谎。
        self.autostart_box = QCheckBox("开机自启")
        self.cfg.autostart = autostart_enabled()
        self.autostart_box.setChecked(self.cfg.autostart)
        self.autostart_box.toggled.connect(self._push)
        form.addRow("", self.autostart_box)

        # ---- 精确传感器 ----
        self.precise_box = QCheckBox("精确传感器（CPU/GPU 温度）")
        self.precise_box.setChecked(cfg.precise_sensors)
        self.precise_box.toggled.connect(self._push_precise)
        form.addRow("", self.precise_box)

        self.precise_hint = QLabel("")
        self.precise_hint.setStyleSheet("color: #888;")
        self.precise_hint.setWordWrap(True)
        self._refresh_precise_hint()

        # 装过驱动之后要能撤掉，否则"装了个内核驱动"这件事没法反悔。
        self.remove_driver_button = QPushButton("卸载 PawnIO 驱动")
        self.remove_driver_button.clicked.connect(self._remove_driver)
        driver_row = QHBoxLayout()
        driver_row.addWidget(self.remove_driver_button)
        driver_row.addStretch(1)

        # ---- 快捷键 ----
        hotkey_form = QFormLayout()
        self.hk_monitor = HotkeyEdit(cfg.hotkeys.cycle_monitor, self._suspend)
        self.hk_monitor.captured.connect(self._set_hotkey)
        hotkey_form.addRow("切换显示器", self.hk_monitor)

        self.hotkey_hint = QLabel("")
        self.hotkey_hint.setStyleSheet("color: #d9534f;")
        self.hotkey_hint.setWordWrap(True)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.precise_hint)
        layout.addLayout(driver_row)
        layout.addWidget(QLabel("快捷键"))
        layout.addLayout(hotkey_form)
        layout.addWidget(self.hotkey_hint)
        layout.addLayout(buttons)
        # 关闭窗口时无论如何重新注册一次，避免"录到一半就关窗"导致热键全失效。
        self.finished.connect(lambda _code=0: self._reload_hotkeys())
        # 控件全部就位后才允许回写配置。
        self._ready = True

    def _suspend(self) -> None:
        """进入录键状态前，先把程序占用的全局热键全部让出来。"""
        if self._suspend_hotkeys is not None:
            self._suspend_hotkeys()

    def _set_hotkey(self, spec: str) -> None:
        """改键：注册成功才写盘，失败则回滚并提示。

        不做回滚的话，用户改成一个被别的程序占用的组合后，热键会静默失效，
        表现就是"按了没反应"，很难自己查出原因。
        """
        previous = self.cfg.hotkeys.cycle_monitor
        self.cfg.hotkeys.cycle_monitor = spec
        error = self._reload_hotkeys()
        if error:
            self.cfg.hotkeys.cycle_monitor = previous
            self.hk_monitor.setText(previous)
            self._reload_hotkeys()
            self.hotkey_hint.setText(error)
            return
        self.hotkey_hint.setText("")
        self.cfg.save()

    def _reload_hotkeys(self) -> str | None:
        """按当前配置重新注册，返回给用户看的错误文案（成功返回 None）。"""
        if self._on_hotkeys_changed is None:
            return None
        return self._on_hotkeys_changed()

    def _refresh_precise_hint(self) -> None:
        """一行状态，够用户判断为什么没有温度即可。"""
        label = {
            "running": "驱动就绪",
            "stopped": "驱动已安装，服务未运行",
            "missing": "未安装驱动（勾选后需一次管理员授权）",
        }.get(pawnio_state(), "无法查询驱动状态")
        self.precise_hint.setText(label)

    def _push_precise(self, checked: bool) -> None:
        if not self._ready:
            return
        error = None
        if self._on_precise_changed is not None:
            error = self._on_precise_changed(checked)
        if error:
            # 失败就把开关拨回去，别让界面显示一个其实没生效的状态。
            self.precise_box.setChecked(not checked)
            self.precise_hint.setText(error)
            self.precise_hint.setStyleSheet("color: #d9534f;")
            return
        self.cfg.precise_sensors = checked
        self.cfg.save()
        self.precise_hint.setStyleSheet("color: #888;")
        self._refresh_precise_hint()

    def _remove_driver(self) -> None:
        """卸载驱动：成功后把开关和提示一起刷新回"未安装"状态。"""
        if self._on_remove_driver is None:
            return
        error = self._on_remove_driver()
        if error:
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText(error)
            return
        self.precise_box.setChecked(False)
        self.precise_hint.setStyleSheet("color: #888;")
        self._refresh_precise_hint()

    def _slider_row(self, value: float, minimum: int, maximum: int):
        """滑块 + 数值显示；返回 (滑块, 行容器)。"""
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(int(value))
        label = QLabel(f"{int(value)}%")
        label.setMinimumWidth(42)
        slider.valueChanged.connect(lambda v, l=label: l.setText(f"{v}%"))
        slider.valueChanged.connect(self._push)
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(label)
        return slider, row

    def _push_monitor(self, *_args) -> None:
        """切换显示器：只搬窗口，不重建标志，所以不走通用的 _push。"""
        name = self.monitor_box.currentData() or ""
        self.cfg.monitor = name
        if self._on_monitor_change is not None:
            self._on_monitor_change(name)
        self.cfg.save()

    def _push(self, *_args) -> None:
        """把控件状态倒回配置，再通知外部重建界面。"""
        if not self._ready:
            return
        self.cfg.tier = int(self.tier_box.currentData() or self.cfg.tier)
        self.cfg.font_size = self.font_spin.value()
        self.cfg.opacity = self.opacity_slider.value() / 100.0
        self.cfg.saturation = self.saturation_slider.value() / 100.0
        self.cfg.layout = self.layout_box.currentData() or "row"
        for key, box in self.section_boxes.items():
            setattr(self.cfg.sections, key, box.isChecked())
        target = self.target_box.currentText().strip()
        self.cfg.target_process = "" if target in ("", AUTO_TARGET) else target
        self.cfg.game_only = self.game_only_box.isChecked()
        self.cfg.always_on_top = self.top_box.isChecked()
        self.cfg.click_through = self.click_box.isChecked()
        self.cfg.capture_exclusion = self.capture_box.isChecked()
        self.cfg.show_in_taskbar = self.taskbar_box.isChecked()
        # 只有用户真的动了这个开关才碰注册表。
        want_autostart = self.autostart_box.isChecked()
        if want_autostart != self.cfg.autostart:
            self.cfg.autostart = want_autostart
            set_autostart(want_autostart)
        self.cfg.clamped()
        self._apply()
