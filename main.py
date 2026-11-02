"""Pacelite —— Windows 上的单行「档位式」性能叠加层。

直接 `python main.py` 跑常驻叠加层；`--demo` 把四个档位叠在一个窗口里对照；
`--shot out.png` 只渲染一帧到磁盘（不改动桌面，用于像素评审）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from config import Config
from metrics import Sampler
from overlay import StripWindow
from presentmon import COMPOSITOR_PROCESSES, FakeFrameFeed, PresentMonFeed
from sensors import SensorsClient, uninstall_pawnio
from settings import SettingsDialog
from single_instance import SingleInstance
from sysintegrate import autostart_enabled, set_autostart
from tiers import TIER_NAMES
import winprobe
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pacelite 性能叠加层")
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
    tray.setToolTip("Pacelite")
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
        print("[pacelite] 已经有一个 Pacelite 在运行，本次启动退出。", file=sys.stderr)
        return 0

    app = QApplication(sys.argv[:1])
    # 叠加层没有"主窗口"概念，关掉窗口事件不应退出程序（托盘还在）。
    app.setQuitOnLastWindowClosed(False)

    cfg = Config.load()
    # 命令行参数临时覆盖配置，方便反复调样式。记下原值，退出时还原，
    # 否则 --font/--opacity 这类一次性参数会被写进用户配置里。
    original_style = (cfg.tier, cfg.font_size, cfg.opacity, cfg.saturation)
    if args.tier:
        cfg.tier = args.tier
    if args.font:
        cfg.font_size = args.font
    if args.opacity is not None:
        cfg.opacity = args.opacity
    if args.saturation is not None:
        cfg.saturation = args.saturation
    cfg.clamped()

    # 精确传感器：默认关闭。开启后先确保 PawnIO 驱动就绪，再拉起提权子进程。
    sensor_client = SensorsClient()
    if cfg.precise_sensors:
        precise_error = sensor_client.start()
        if precise_error:
            print(f"[pacelite] 精确传感器不可用: {precise_error}", file=sys.stderr)

    sampler = Sampler(
        cfg.refresh_ms,
        precise=sensor_client if cfg.precise_sensors and not sensor_client.last_error else None,
    )
    sampler.start()
    feed = (
        FakeFrameFeed()
        if args.fake_fps
        else PresentMonFeed(
            exe=Path(cfg.presentmon_path) if cfg.presentmon_path else DEFAULT_PRESENTMON,
            target=cfg.target_process,
            refresh_ms=cfg.refresh_ms,
        )
    )
    if not args.no_fps and not args.fake_fps and feed.start() is False:
        print(f"[pacelite] FPS 不可用: {feed.error}", file=sys.stderr)

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
    window.show()
    # 窗口句柄要等 show() 之后才有效，所以这两件事必须排到事件循环里做。
    QTimer.singleShot(0, lambda: set_capture_exclusion(int(window.winId()), cfg.capture_exclusion))
    QTimer.singleShot(50, window._refresh)

    settings_dialog: SettingsDialog | None = None

    def set_precise(enabled: bool) -> str | None:
        """开关精确传感器：拉起/停止提权子进程，驱动由子进程自己装（只弹一次 UAC）。"""
        if not enabled:
            sampler.set_precise(None)
            sensor_client.stop()
            return None
        error = sensor_client.start()
        if error:
            return error
        sampler.set_precise(sensor_client)
        return None

    def remove_driver() -> str | None:
        """卸载 PawnIO 驱动；顺手关掉精确传感器。"""
        cfg.precise_sensors = False
        sampler.set_precise(None)
        sensor_client.stop()
        ok, message = uninstall_pawnio(sensor_client.setup_path)
        cfg.save()
        return None if ok else message

    def apply_settings() -> None:
        """设置改动后重建叠加层；监视进程变了就重启 PresentMon 换目标。"""
        if not args.fake_fps and not args.no_fps and feed.target != cfg.target_process:
            feed.stop()
            feed.target = cfg.target_process
            feed.start()
        set_capture_exclusion(int(window.winId()), cfg.capture_exclusion)
        window.apply_config()
        apply_game_visibility()
        cfg.save()

    def open_settings() -> None:
        nonlocal settings_dialog
        # 候选进程 = 正在出帧的 + 当前有窗口的，后者让"手动指定"真正可用
        names: set[str] = set()
        if hasattr(feed, "presenters"):
            names.update(feed.presenters())
        try:
            names.update(winprobe.visible_window_processes())
        except Exception:  # noqa: BLE001 - 枚举失败不影响设置窗口打开
            pass
        settings_dialog = SettingsDialog(
            cfg,
            apply_settings,
            sorted(names),
            on_monitor_change=window.move_to_screen,
            on_hotkeys_changed=reload_hotkeys,
            suspend_hotkeys=hotkeys.release if hotkeys is not None else None,
            on_precise_changed=set_precise,
            on_remove_driver=remove_driver,
        )
        settings_dialog.show()
        settings_dialog.raise_()

    def apply_game_visibility() -> None:
        """"只在游戏运行时显示"的判定：不需要时只隐藏窗口，不销毁。"""
        if args.demo or not cfg.game_only:
            window.set_game_visible(True)
            return
        window.set_game_visible(game_is_running(feed))

    game_timer = QTimer()
    game_timer.timeout.connect(apply_game_visibility)
    game_timer.start(GAME_CHECK_MS)

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
        否则用户会遇到"按了没反应"且完全看不出原因。
        """
        if hotkeys is None:
            return None
        hotkeys.release()
        # 档位键固定，避免把四个位置也变成可录项后带来一堆冲突。
        for vk, tier in ((VK_1, 1), (VK_2, 2), (VK_3, 3), (VK_4, 4)):
            hotkeys.bind(MOD_CONTROL | MOD_SHIFT, vk, lambda value=tier: window.set_tier(value))

        spec = cfg.hotkeys.cycle_monitor or DEFAULT_CYCLE_HOTKEY
        if hotkeys.bind_spec(spec, window.cycle_screen):
            return None
        if spec != DEFAULT_CYCLE_HOTKEY and hotkeys.bind_spec(
            DEFAULT_CYCLE_HOTKEY, window.cycle_screen
        ):
            cfg.hotkeys.cycle_monitor = DEFAULT_CYCLE_HOTKEY
            return f"{spec} 被其他程序占用，已回退为 {DEFAULT_CYCLE_HOTKEY}"
        return f"{spec} 注册失败（可能被其他程序占用）"

    hotkey_warning = reload_hotkeys()
    if hotkey_warning:
        print(f"[pacelite] {hotkey_warning}", file=sys.stderr)

    def shutdown() -> None:
        """退出路径：注销热键 → 停 PresentMon（并清理 ETW）→ 停采样与传感器 → 存配置。"""
        if hotkeys is not None:
            hotkeys.release()
        feed.stop()
        sampler.stop()
        sensor_client.stop()
        # 命令行覆盖项还原成用户原本的设置再写盘
        cfg.tier, cfg.font_size, cfg.opacity, cfg.saturation = original_style
        cfg.save()
        guard.release()

    app.aboutToQuit.connect(shutdown)

    if args.shot:
        target = Path(args.shot)

        def shoot() -> None:
            window._refresh()
            QGuiApplication.processEvents()
            target.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(target))
            print(f"[pacelite] screenshot -> {target}")
            app.quit()

        QTimer.singleShot(int(args.shot_delay * 1000), shoot)

    code = app.exec()
    if tray is not None:
        tray.hide()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
