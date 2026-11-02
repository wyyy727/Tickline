"""设置窗口。

所有控件都是即时生效的：一改动就回写配置并让叠加层重建，不需要"确定"按钮。
这样调字号/透明度/饱和度的过程可以直接看到效果。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
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

AUTO_TARGET = "自动（优先跟随前台游戏）"


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
        on_install_driver: Callable[[], str | None] | None = None,
        on_remove_driver: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(None)
        self.cfg = cfg
        self._apply = apply_callback
        self._on_monitor_change = on_monitor_change
        self._on_hotkeys_changed = on_hotkeys_changed
        self._suspend_hotkeys = suspend_hotkeys
        self._on_precise_changed = on_precise_changed
        self._on_install_driver = on_install_driver
        self._on_remove_driver = on_remove_driver
        # 构造期间控件会自发信号（setValue/setChecked 等），此时控件还没建全，
        # 让 _push 提前返回，否则会用半成品状态覆盖用户配置。
        self._ready = False
        # 拖滑块时 valueChanged 每移 1 像素就发一次，而每次回写都要重建叠加层 + 写盘。
        # 收敛成 120ms 一次，肉眼无感，磁盘和重绘都省下来了。
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(120)
        self._apply_timer.timeout.connect(self._flush)
        self._precise_busy = False
        self._driver_busy = False
        self._syncing = False
        # 关掉就销毁：下次打开设置是全新实例，不会拿着一份过期状态。
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("Tickline 设置")
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

        self.sparkline_box = QCheckBox("帧时间曲线")
        self.sparkline_box.setChecked(cfg.sparkline)
        self.sparkline_box.toggled.connect(self._push)
        form.addRow("", self.sparkline_box)

        # 刷新间隔越大越省 CPU，但数字变化也越迟钝；100ms 以下对 OLED 屏的残影不友好。
        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(100, 1000)
        self.refresh_spin.setSingleStep(50)
        self.refresh_spin.setValue(cfg.refresh_ms)
        self.refresh_spin.setSuffix(" ms")
        self.refresh_spin.valueChanged.connect(self._push)
        form.addRow("刷新间隔", self.refresh_spin)

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
        # 只允许下拉选择，不给输入框：这个值最终会作为 --process_name 传给 PresentMon，
        # 一旦存进半截/写错的进程名，一帧都匹配不上，FPS 会永远显示 "—"，且重启还留着。
        self.target_box = QComboBox()
        self.target_box.setEditable(False)
        self.target_box.addItem(AUTO_TARGET)
        wanted = (cfg.target_process or "").strip()
        names = sorted(set(presenters or []))
        if wanted and wanted not in names:
            # 上次选的进程这次没在出帧：也要列出来，否则下拉框会"看起来"变成了自动，
            # 用户一碰别的控件就把真实选择覆盖掉。
            names.insert(0, wanted)
        for name in names:
            self.target_box.addItem(name)
        index = self.target_box.findText(wanted) if wanted else 0
        self.target_box.setCurrentIndex(max(0, index))
        self.target_box.setToolTip("列表里是当前有出帧的进程；列表外的进程选「自动」即可")
        self.target_box.currentIndexChanged.connect(self._push)
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

        # 驱动没装时这里必须是"安装"：以前只有一个"卸载"按钮，第一次打开设置的人
        # 面对一个只能拆、不能装的按钮，根本不知道温度要怎么才出得来。
        self.driver_button = QPushButton()
        self.driver_button.clicked.connect(self._driver_clicked)

        # 装驱动是勾选"精确传感器"之外的第二条路，好处得写在这儿，不能指望用户去翻 README。
        self.driver_hint = QLabel(
            "装它能读到 CPU 温度（以及主板、硬盘这些走内核的传感器）。\n"
            "驱动由 LibreHardwareMonitor 使用，只在本机内核里开放传感器读数：不联网、不常驻后台、不写系统目录。\n"
            "安装时会弹一次管理员授权；不装也能用，只是没有 CPU 温度。"
        )
        self.driver_hint.setStyleSheet("color: #888;")
        self.driver_hint.setWordWrap(True)

        driver_row = QHBoxLayout()
        driver_row.addWidget(self.driver_button)
        driver_row.addStretch(1)
        self._refresh_driver_ui()

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
        layout.addWidget(self.driver_hint)
        layout.addWidget(QLabel("快捷键"))
        layout.addLayout(hotkey_form)
        layout.addWidget(self.hotkey_hint)
        layout.addLayout(buttons)
        # 关闭窗口时：把还悬着的防抖改动落地，再重新注册一次热键，
        # 避免"录到一半就关窗"导致热键全失效。
        self.finished.connect(lambda _code=0: self._on_closed())
        # 控件全部就位后才允许回写配置。
        self._ready = True

    def _on_closed(self) -> None:
        if self._apply_timer.isActive():
            self._apply_timer.stop()
            self._apply()
        self._reload_hotkeys()

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
        state = pawnio_state()
        label = {
            "running": "驱动就绪，勾选后即可读 CPU 温度",
            "stopped": "驱动已安装，但服务没在运行（勾选一次会自动拉起）",
            "missing": "未安装驱动：点下面的「安装 PawnIO 驱动」，或直接勾选这里（也会装，但只弹一次授权）",
        }.get(state, "无法查询驱动状态")
        self.precise_hint.setText(label)
        if hasattr(self, "driver_button"):
            # 服务被外部停掉/救回来时，按钮文字要跟着系统实际状态走。
            self._refresh_driver_ui()

    def _set_precise_box(self, value: bool) -> None:
        """改开关但不把这次改动当成"用户操作"再回调出去。"""
        self._syncing = True
        try:
            self.precise_box.setChecked(value)
        finally:
            self._syncing = False

    def _save_precise(self, checked: bool) -> None:
        self.cfg.precise_sensors = checked
        self.cfg.save()

    def _push_precise(self, checked: bool) -> None:
        if not self._ready or self._syncing or self._precise_busy:
            return
        error = None
        if self._on_precise_changed is not None:
            error = self._on_precise_changed(checked)
        if error == "pending":
            # 后台正在弹 UAC / 等子进程写出第一份数据（最长 12 秒），
            # 界面不能卡住，这里先挂"启动中"，结果由 finish_precise() 收尾。
            self._precise_busy = True
            self.precise_box.setEnabled(False)
            self.precise_hint.setStyleSheet("color: #888;")
            self.precise_hint.setText("正在启动精确传感器（需要管理员授权）…")
            return
        if error:
            # 失败就把开关拨回去，别让界面显示一个其实没生效的状态。
            self._set_precise_box(not checked)
            self._save_precise(not checked)
            self.precise_hint.setText(error)
            self.precise_hint.setStyleSheet("color: #d9534f;")
            return
        self._save_precise(checked)
        self.precise_hint.setStyleSheet("color: #888;")
        self._refresh_precise_hint()

    def finish_precise(self, error: str | None) -> None:
        """后台启动结束：成功落配置，失败把开关拨回去并显示原因。"""
        if not self._precise_busy:
            return
        self._precise_busy = False
        self.precise_box.setEnabled(True)
        if error:
            self._set_precise_box(False)
            self._save_precise(False)
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText(str(error))
            return
        self._set_precise_box(True)
        self._save_precise(True)
        self.precise_hint.setStyleSheet("color: #888;")
        self._refresh_precise_hint()

    # ---- PawnIO 驱动：装/卸是同一个按钮 ------------------------------------
    def _driver_installed(self) -> bool:
        """服务在跑或至少装过都算"已安装"；只有查不到才算没装。"""
        return pawnio_state() in ("running", "stopped")

    def _refresh_driver_ui(self) -> None:
        installed = self._driver_installed()
        self.driver_button.setText("卸载 PawnIO 驱动" if installed else "安装 PawnIO 驱动")
        # 已装的人不需要再读一遍好处，把版面留给状态行。
        self.driver_hint.setVisible(not installed)

    def _driver_clicked(self) -> None:
        if self._driver_busy:
            return
        if self._driver_installed():
            self._uninstall_driver()
        else:
            self._install_driver()

    def _install_driver(self) -> None:
        """安装驱动：后台跑（提权 + 服务起来要几秒），结束后刷新按钮文字。"""
        if self._on_install_driver is None:
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText("没有可用的驱动安装入口")
            return
        error = self._on_install_driver()
        if error == "pending":
            self._driver_busy = True
            self.driver_button.setEnabled(False)
            self.precise_hint.setStyleSheet("color: #888;")
            self.precise_hint.setText("正在安装驱动（需要管理员授权）…")
            return
        if error:
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText(error)
            return
        self._after_driver_change()

    def _uninstall_driver(self) -> None:
        """卸载驱动：在后台执行，结束后统一刷新开关和提示。"""
        if self._on_remove_driver is None or self._driver_busy:
            return
        error = self._on_remove_driver()
        if error == "pending":
            self._driver_busy = True
            self.driver_button.setEnabled(False)
            self.precise_hint.setStyleSheet("color: #888;")
            self.precise_hint.setText("正在卸载驱动（需要管理员授权）…")
            return
        if error:
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText(error)
            return
        self._set_precise_box(False)
        self._save_precise(False)
        self._after_driver_change()

    def _after_driver_change(self) -> None:
        """装/卸完成后统一收尾：按钮文字、状态行、勾选框都按系统实际状态重新对账。"""
        self._refresh_driver_ui()
        if not self._driver_installed():
            self._set_precise_box(False)
            self._save_precise(False)
        self.precise_hint.setStyleSheet("color: #888;")
        self._refresh_precise_hint()

    def finish_driver(self, action: str, error: str | None) -> None:
        """后台装/卸结束。action 为 "install" 或 "uninstall"。"""
        if not self._driver_busy:
            return
        self._driver_busy = False
        self.driver_button.setEnabled(True)
        if error:
            self.precise_hint.setStyleSheet("color: #d9534f;")
            self.precise_hint.setText(str(error))
            self._refresh_driver_ui()
            return
        if action == "uninstall":
            # 驱动没了，精确传感器必然不可用，把开关和配置一起拨回去。
            self._set_precise_box(False)
            self._save_precise(False)
        self._after_driver_change()
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
        self.cfg.sparkline = self.sparkline_box.isChecked()
        self.cfg.refresh_ms = self.refresh_spin.value()
        # 只有用户真的动了这个开关才碰注册表。
        want_autostart = self.autostart_box.isChecked()
        if want_autostart != self.cfg.autostart:
            self.cfg.autostart = want_autostart
            set_autostart(want_autostart)
        self.cfg.clamped()
        self._apply_timer.start()

    def _flush(self) -> None:
        """把攒起来的改动真正落地一次：重建叠加层 + 写盘。"""
        self._apply()
