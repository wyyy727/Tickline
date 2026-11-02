"""Tickline —— Windows 上的单行「档位式」性能叠加层。

直接 `python main.py` 跑常驻叠加层；`--demo` 把四个档位叠在一个窗口里对照；
`--shot out.png` 只渲染一帧到磁盘（不改动桌面，用于像素评审）。

启动顺序有意做成"先把窗口显出来，再去拉数据"：精确传感器会弹 UAC 并轮询最长 12 秒，
以前这一步挡在 show() 前面，用户只会看到屏幕上什么都没有。
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import pylog
import winprobe
from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import COMPOSITOR_PROCESSES, FakeFrameFeed, PresentMonFeed
from sensors import SensorsClient, install_pawnio, uninstall_pawnio
from settings import SettingsDialog
from single_instance import SingleInstance
from sysintegrate import autostart_enabled, purge_legacy_autostart, set_autostart
from tiers import TIER_NAMES
from winutil import (
    MOD_CONTROL,
    MOD_SHIFT,
    VK_1,
    VK_2,
    VK_3,
    VK_4,
    Hotkeys,
    set_capture_exclusion,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PRESENTMON = PROJECT_ROOT.parent / "tools" / "PresentMon" / "PresentMon.exe"
GAME_CHECK_MS = 1500   # "只在游戏运行时显示"的判定频率
DEFAULT_CYCLE_HOTKEY = "Ctrl+Shift+M"   # 切换显示器的内置默认键，被占用时回退到这


class BackgroundTask(QObject):
    """把一次可能阻塞的调用（UAC、装驱动、等子进程）丢到后台线程，完成后回主线程。

    这些操作以前都直接跑在 GUI 线程上：弹 UAC 时整屏变暗、窗口冻结十几秒不动，
    看起来就像程序挂了。
    """

    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._busy = False
        self._callbacks: list = []
        self.finished.connect(self._done)

    @property
    def busy(self) -> bool:
        return self._busy

    def run(self, work, callback=None) -> bool:
        """在后台跑 work()，返回值通过 callback(result) 回到主线程；已在跑则返回 False。"""
        if self._busy:
            return False
        if callback is not None:
            self._callbacks.append(callback)
        self._busy = True

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - 后台失败只影响这一项功能
                result = f"{type(exc).__name__}: {exc}"
            self.finished.emit(result)

        threading.Thread(target=runner, name="tickline-task", daemon=True).start()
        return True

    def _done(self, result) -> None:
        self._busy = False
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(result)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tickline 性能叠加层")
    parser.add_argument("--demo", action="store_true", help="把四个档位叠在一个窗口里")
    parser.add_argument("--tier", type=int, default=None, help="档位 1..4")
    parser.add_argument("--font", type=int, default=None, help="数值字号（像素）")
    parser.add_argument("--opacity", type=float, default=None, help="背景透明度 0.1..1.0")
    parser.add_argument("--saturation", type=float, default=None, help="颜色饱和度 0..2")
    parser.add_argument("--no-fps", action="store_true", help="不启动 PresentMon")
    parser.add_argument("--fake-fps", action="store_true", help="用合成帧率做样式评审")
    parser.add_argument("--no-tray", action="store_true", help="不创建托盘图标")
    parser.add_argument("--no-hotkey", action="store_true", help="不注册全局热键")
    parser.add_argument("--shot", type=str, default=None, help="渲染一帧到指定 PNG 后退出")
    parser.add_argument("--shot-delay", type=float, default=3.0, help="截图前先采集多少秒")
    return parser.parse_args(argv)


def apply_cli_style(cfg: Config, args: argparse.Namespace) -> dict[str, object]:
    """把 --tier/--font/--opacity/--saturation 一次性套到配置上。

    返回**被覆盖字段的原值**，退出时只还原这几个字段（见 restore_cli_style）。
    以前退出时无条件把四个字段整体写回启动快照，结果设置页里改过的
    字号/档位/透明度/饱和度下次启动全被打回原样。
    """
    overrides: dict[str, object] = {}
    if args.tier:
        overrides["tier"] = cfg.tier
        cfg.tier = args.tier
    if args.font:
        overrides["font_size"] = cfg.font_size
        cfg.font_size = args.font
    if args.opacity is not None:
        overrides["opacity"] = cfg.opacity
        cfg.opacity = args.opacity
    if args.saturation is not None:
        overrides["saturation"] = cfg.saturation
        cfg.saturation = args.saturation
    return overrides


def restore_cli_style(cfg: Config, overrides: dict[str, object]) -> None:
    """退出写盘前还原命令行一次性覆盖；没覆盖过的字段保持用户当前值。"""
    for field, value in overrides.items():
        setattr(cfg, field, value)


def game_is_running(feed) -> bool:
    """粗略判断"现在有游戏在跑"：有全屏窗口，或者有非合成器进程在持续出帧。"""
    try:
        if winprobe.fullscreen_pids():
            return True
    except Exception:  # noqa: BLE001 - 探测失败就当没有全屏窗口
        pass
    stats = feed.stats()
    return bool(stats.active and stats.target and stats.target.lower() not in COMPOSITOR_PROCESSES)


def build_tray(
    app: QApplication,
    window: StripWindow,
    cfg: Config,
    feed,
    open_settings,
) -> QSystemTrayIcon:
    """托盘菜单：设置、档位、穿透、录制隐藏、任务栏、游戏模式、退出。"""
    tray = QSystemTrayIcon(app.style().standardIcon(app.style().StandardPixmap.SP_ComputerIcon))
    tray.setToolTip("Tickline")
    menu = QMenu()

    settings_action = QAction("设置", menu)
    settings_action.triggered.connect(open_settings)
    menu.addAction(settings_action)

    monitor_action = QAction("切换到下一块显示器", menu)
    monitor_action.triggered.connect(window.cycle_screen)
    menu.addAction(monitor_action)

    def sync_monitor_action() -> None:
        # 只有一块屏时这条没意义，直接隐藏；插拔显示器后菜单每次打开都会重新判断。
        monitor_action.setVisible(len(QGuiApplication.screens()) > 1)

    menu.aboutToShow.connect(sync_monitor_action)
    sync_monitor_action()
    menu.addSeparator()

    for tier, name in TIER_NAMES.items():
        action = QAction(f"{tier} · {name}", menu)
        action.triggered.connect(lambda _=False, value=tier: window.set_tier(value))
        menu.addAction(action)
    menu.addSeparator()

    click_action = QAction("鼠标穿透", menu, checkable=True, checked=cfg.click_through)
    click_action.triggered.connect(lambda checked: toggle_click_through(window, cfg, checked))
    menu.addAction(click_action)

    capture_action = QAction("录制中隐藏", menu, checkable=True, checked=cfg.capture_exclusion)
    capture_action.triggered.connect(
        lambda checked: set_capture_exclusion(int(window.winId()), checked)
    )
    menu.addAction(capture_action)

    game_action = QAction("只在游戏运行时显示", menu, checkable=True, checked=cfg.game_only)
    game_action.triggered.connect(lambda checked: toggle_game_only(window, cfg, checked))
    menu.addAction(game_action)

    taskbar_action = QAction("显示在任务栏", menu, checkable=True, checked=cfg.show_in_taskbar)
    taskbar_action.triggered.connect(lambda checked: toggle_taskbar(window, cfg, checked))
    menu.addAction(taskbar_action)
    menu.addSeparator()

    quit_action = QAction("退出", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    return tray


def toggle_click_through(window: StripWindow, cfg: Config, checked: bool) -> None:
    """穿透状态要写回配置，下次启动保持一致。"""
    cfg.click_through = checked
    window.apply_config()
    cfg.save()


def toggle_game_only(window: StripWindow, cfg: Config, checked: bool) -> None:
    cfg.game_only = checked
    window.apply_config()
    cfg.save()


def toggle_taskbar(window: StripWindow, cfg: Config, checked: bool) -> None:
    cfg.show_in_taskbar = checked
    window.apply_config()
    cfg.save()


def main(argv: list[str] | None = None) -> int:
    raw_args = argv if argv is not None else sys.argv[1:]
    # 提权子进程走同一个入口（打包后 exe 会被自己再调起来一次），必须放在最前面，
    # 否则会去抢单实例互斥体、开窗口。
    if "--sensors-helper" in raw_args:
        from sensors import helper_main

        return helper_main(list(raw_args))

    args = parse_args(raw_args)

    guard = SingleInstance()
    if guard.already_running:
        pylog.log("[tickline] 已经有一个 Tickline 在运行，本次启动退出。")
        return 0

    app = QApplication(sys.argv[:1])
    # 叠加层没有"主窗口"概念，关掉窗口事件不应退出程序（托盘还在）。
    app.setQuitOnLastWindowClosed(False)

    cfg = Config.load()
    # 命令行参数临时覆盖配置，方便反复调样式。只还原真的被覆盖过的字段，
    # 否则退出时会拿启动快照把设置页的改动一起写回去。
    style_overrides = apply_cli_style(cfg, args)
    cfg.clamped()

    # 精确传感器默认关闭；这里只建对象，真正启动排到窗口显示之后。
    sensor_client = SensorsClient()
    sampler = Sampler(cfg.refresh_ms, precise=None)
    feed = (
        FakeFrameFeed()
        if args.fake_fps
        else PresentMonFeed(
            exe=Path(cfg.presentmon_path) if cfg.presentmon_path else DEFAULT_PRESENTMON,
            target=cfg.target_process,
            refresh_ms=cfg.refresh_ms,
        )
    )

    def persist_position(x: int, y: int) -> None:
        cfg.x, cfg.y = x, y
        cfg.save()

    window = StripWindow(
        cfg,
        sampler,
        feed,
        demo=args.demo,
        on_close=app.quit,
        on_moved=persist_position,
    )
    tray: QSystemTrayIcon | None = None
    settings_dialog: SettingsDialog | None = None
    precise_task = BackgroundTask()
    driver_task = BackgroundTask()

    def notify(message: str) -> None:
        """出错时既要落盘也要让用户看得见（窗口版没有控制台，光 print 等于没说）。"""
        pylog.log(f"[tickline] {message}")
        if tray is not None:
            tray.showMessage("Tickline", message, QSystemTrayIcon.MessageIcon.Warning, 8000)

    # 窗口先出来：之后无论是拉 PresentMon 还是弹 UAC，用户至少看得见叠加层。
    window.show()
    # 窗口句柄要等 show() 之后才有效，所以这两件事必须排到事件循环里做。
    QTimer.singleShot(0, lambda: set_capture_exclusion(int(window.winId()), cfg.capture_exclusion))
    QTimer.singleShot(50, window._tick)

    def precise_work() -> str | None:
        error = sensor_client.start()
        if not error:
            sampler.set_precise(sensor_client)
        return error

    def set_precise(enabled: bool, on_done=None) -> str | None:
        """开关精确传感器：在后台拉起/停止提权子进程，驱动由子进程自己装（只弹一次 UAC）。

        返回值给设置窗口用：None = 立刻成功，字符串 = 立刻失败或"正在后台启动"。
        """
        if not enabled:
            sampler.set_precise(None)
            sensor_client.stop()
            return None
        if precise_task.busy:
            return "上一项操作还没结束，请稍候"
        precise_task.run(precise_work, on_done)
        return "pending"

    def install_driver(on_done=None) -> str | None:
        """安装 PawnIO 驱动。

        不要求先勾"精确传感器"：想先装好、回头再开温度的人，走这条更直接。
        提权 + 等服务起来要几秒，所以和卸载一样放后台。
        """
        if driver_task.busy:
            return "上一项操作还没结束，请稍候"

        def work() -> str | None:
            ok, message = install_pawnio(sensor_client.setup_path)
            return None if ok else message

        driver_task.run(work, on_done)
        return "pending"

    def remove_driver(on_done=None) -> str | None:
        """卸载 PawnIO 驱动；顺手关掉精确传感器。卸载同样放到后台（可能要几秒）。"""
        cfg.precise_sensors = False
        sampler.set_precise(None)
        sensor_client.stop()
        cfg.save()
        if driver_task.busy:
            return "上一项操作还没结束，请稍候"

        def work() -> str | None:
            ok, message = uninstall_pawnio(sensor_client.setup_path)
            return None if ok else message

        driver_task.run(work, on_done)
        return "pending"

    def start_backends() -> None:
        sampler.start()
        if not args.no_fps and not args.fake_fps and not feed.start():
            notify(f"帧率不可用：{feed.error}")
        if cfg.precise_sensors:
            error = set_precise(True)
            if error and error != "pending":
                notify(f"精确传感器不可用：{error}")

    def apply_settings() -> None:
        """设置改动后重建叠加层；监视进程变了就重启 PresentMon 换目标。"""
        if not args.fake_fps and not args.no_fps and feed.target != cfg.target_process:
            feed.stop()
            feed.target = cfg.target_process
            if not feed.start():
                notify(f"帧率不可用：{feed.error}")
        sampler.set_interval(cfg.refresh_ms)
        set_capture_exclusion(int(window.winId()), cfg.capture_exclusion)
        window.apply_config()
        apply_game_visibility()
        cfg.save()

    def open_settings() -> None:
        nonlocal settings_dialog
        # 已经开着就拉到前面，不要每点一次托盘就再造一个窗口（以前会越点越多、越点越卡）。
        if settings_dialog is not None:
            settings_dialog.show()
            settings_dialog.raise_()
            settings_dialog.activateWindow()
            return
        # 候选进程 = 正在出帧的 + 当前有窗口的，后者让"手动指定"真正可用
        names: set[str] = set()
        if hasattr(feed, "presenters"):
            names.update(feed.presenters())
        try:
            names.update(winprobe.visible_window_processes())
        except Exception:  # noqa: BLE001 - 枚举失败不影响设置窗口打开
            pass

        def precise_done(error) -> None:
            dialog = settings_dialog
            if dialog is not None:
                dialog.finish_precise(error)
            if error:
                notify(f"精确传感器不可用：{error}")

        def install_done(error) -> None:
            dialog = settings_dialog
            if dialog is not None:
                dialog.finish_driver("install", error)
            notify(
                "PawnIO 驱动已安装，勾选「精确传感器」即可读 CPU 温度"
                if not error
                else f"安装驱动失败：{error}"
            )

        def uninstall_done(error) -> None:
            dialog = settings_dialog
            if dialog is not None:
                dialog.finish_driver("uninstall", error)
            notify("PawnIO 驱动已卸载" if not error else f"卸载驱动失败：{error}")

        dialog = SettingsDialog(
            cfg,
            apply_settings,
            sorted(names),
            on_monitor_change=window.move_to_screen,
            on_hotkeys_changed=reload_hotkeys,
            suspend_hotkeys=hotkeys.release if hotkeys is not None else None,
            # 这两个操作以前同步跑在 GUI 线程上（UAC + 最长 12 秒），现在一律走后台。
            on_precise_changed=lambda enabled: set_precise(enabled, precise_done),
            on_install_driver=lambda: install_driver(install_done),
            on_remove_driver=lambda: remove_driver(uninstall_done),
        )
        settings_dialog = dialog

        def forget(*_args) -> None:
            nonlocal settings_dialog
            settings_dialog = None

        dialog.destroyed.connect(forget)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def apply_game_visibility() -> None:
        """"只在游戏运行时显示"的判定：不需要时只隐藏窗口，不销毁。"""
        if args.demo or not cfg.game_only:
            window.set_game_visible(True)
            return
        window.set_game_visible(game_is_running(feed))

    game_timer = QTimer()
    game_timer.timeout.connect(apply_game_visibility)
    game_timer.start(GAME_CHECK_MS)

    # 更名前写过 "Pacelite" 自启项，留着会开机把旧版本也拉起来；顺手清掉。
    purge_legacy_autostart()

    # 自启默认关闭，程序绝不主动开启。这里只做一次"配置与注册表对账"：
    # 配置说关就把可能残留的注册表项清掉，说开就补上，两者一致时什么都不做。
    if cfg.autostart != autostart_enabled():
        set_autostart(cfg.autostart)

    tray = None if args.no_tray else build_tray(app, window, cfg, feed, open_settings)
    hotkeys: Hotkeys | None = None
    if not args.no_hotkey:
        hotkeys = Hotkeys(int(window.winId()))

    def reload_hotkeys() -> str | None:
        """重新注册热键，返回给用户看的错误文案（成功返回 None）。

        切换显示器允许自定义；档位键固定。若自定义键被别的程序占走，自动回落到内置默认组合，
        档位键被占用时也要说出来 —— 以前失败被完全忽略，用户只能遇到"按了没反应"。
        """
        if hotkeys is None:
            return None
        hotkeys.release()
        # 档位键固定，避免把四个位置也变成可录项后带来一堆冲突。
        failed: list[str] = []
        for vk, tier in ((VK_1, 1), (VK_2, 2), (VK_3, 3), (VK_4, 4)):
            if not hotkeys.bind(
                MOD_CONTROL | MOD_SHIFT, vk, lambda value=tier: window.set_tier(value)
            ):
                failed.append(f"Ctrl+Shift+{tier}")

        spec = cfg.hotkeys.cycle_monitor or DEFAULT_CYCLE_HOTKEY
        message: str | None = None
        if not hotkeys.bind_spec(spec, window.cycle_screen):
            if spec != DEFAULT_CYCLE_HOTKEY and hotkeys.bind_spec(
                DEFAULT_CYCLE_HOTKEY, window.cycle_screen
            ):
                cfg.hotkeys.cycle_monitor = DEFAULT_CYCLE_HOTKEY
                message = f"{spec} 被其他程序占用，已回退为 {DEFAULT_CYCLE_HOTKEY}"
            else:
                message = f"{spec} 注册失败（可能被其他程序占用）"
        if failed:
            extra = f"档位热键 {'/'.join(failed)} 被其他程序占用，对应档位只能用托盘菜单切换"
            message = f"{message}；{extra}" if message else extra
        return message

    hotkey_warning = reload_hotkeys()
    if hotkey_warning:
        notify(hotkey_warning)

    def shutdown() -> None:
        """退出路径：注销热键 → 停 PresentMon（并清理 ETW）→ 停采样与传感器 → 存配置。"""
        if hotkeys is not None:
            hotkeys.release()
        feed.stop()
        sampler.stop()
        sensor_client.stop()
        # 命令行覆盖项还原成用户原本的设置再写盘（没覆盖的字段保持设置页里的当前值）
        restore_cli_style(cfg, style_overrides)
        cfg.save()
        guard.release()

    app.aboutToQuit.connect(shutdown)

    # 采样与 PresentMon 在事件循环开始后才真正启动，窗口已经画出来了。
    QTimer.singleShot(0, start_backends)

    if args.shot:
        target = Path(args.shot)

        def shoot() -> None:
            window._tick()
            QGuiApplication.processEvents()
            target.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(target))
            pylog.log(f"[tickline] screenshot -> {target}")
            app.quit()

        QTimer.singleShot(int(args.shot_delay * 1000), shoot)

    code = app.exec()
    if tray is not None:
        tray.hide()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
