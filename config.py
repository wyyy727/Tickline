"""Tickline 的用户配置持久化。

以 JSON 存放在 %APPDATA%\\Tickline\\config.json。读取时忽略未知键、缺失键保留默认值，
并且**逐字段校验类型**：手改坏的 json（`{"tier": null}`、`{"font_size": "abc"}`）只会让
那一个字段回落到默认值，不会把程序弄崩。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def _roaming() -> Path:
    base = os.environ.get("APPDATA")
    return Path(base) if base else Path.home() / "AppData" / "Roaming"


def config_path() -> Path:
    """配置文件位置；%APPDATA% 不可用时退回用户目录。"""
    return _roaming() / "Tickline" / "config.json"


def legacy_config_path() -> Path:
    """更名前（Pacelite）的配置位置。只为一次性搬家保留。"""
    return _roaming() / "Pacelite" / "config.json"


def _migrate_legacy(target: Path) -> None:
    """项目从 Pacelite 更名为 Tickline 后，把旧配置搬过来，用户不必重设一遍。

    只在新路径还没有文件时搬一次，且复制而不是移动 —— 万一用户还留着旧版本，
    两边各有一份配置，互不干扰。
    """
    if target.exists():
        return
    legacy = legacy_config_path()
    try:
        if not legacy.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        # 搬家失败就按"没有配置"启动，默认值足以跑起来。
        return


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(value))  # 兼容 "13" / 13.0 这种旧写法
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):  # NaN / 无穷
        return default
    return result


def _as_bool(value: object, default: bool) -> bool:
    """只接受真正的布尔值（以及 0/1）：字符串 "false" 也不能算真。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


@dataclass
class Sections:
    """要显示哪些数据块。关掉的块整段不渲染（也算省一点采样开销）。"""

    fps: bool = True
    cpu: bool = True
    gpu: bool = True
    vram: bool = True
    ram: bool = True
    ssd: bool = False   # 硬盘温度/寿命默认不显示，避免默认条太长


@dataclass
class HotkeyConfig:
    """可自定义的全局热键，写法如 "Ctrl+Shift+M"（必须带修饰键）。

    只保留"切换显示器"一个：鼠标穿透和退出都有更自然的入口（托盘菜单、✕、任务栏），
    多一个全局热键就多一份和别的软件抢键位的风险。
    """

    cycle_monitor: str = "Ctrl+Shift+M"


@dataclass
class Config:
    # ---- 显示 ----
    tier: int = 4               # 档位 1..4，字段定义见 tiers.py
    font_size: int = 13         # 数值字号；参考图是 9-10 px 的设计
    opacity: float = 0.72       # 背景透明度的乘子
    saturation: float = 1.0     # 全局颜色饱和度乘子
    sparkline: bool = True      # 是否绘制帧时间曲线
    # row = 所有数据挤在一行；column = 按 CPU/GPU/内存/硬盘 分组换行（长方形区块）
    layout: str = "row"
    sections: Sections = field(default_factory=Sections)

    # ---- 窗口 ----
    click_through: bool = False
    capture_exclusion: bool = True
    always_on_top: bool = True
    show_in_taskbar: bool = True   # 关掉后任务栏/Alt-Tab 里看不到，只能从托盘退出
    game_only: bool = False        # 只在游戏（全屏窗口 / 有出帧目标）运行时显示
    autostart: bool = False        # 开机自启；默认关闭，只有用户在设置里勾选才生效
    # 显示在哪块屏幕上，存屏幕名（如 "\\\\.\\DISPLAY2"）。空字符串 = 自动（主屏）。
    # 用名字而不是序号，是因为插拔显示器后 Qt 给的顺序会变。
    monitor: str = ""
    x: int = -1                 # -1 表示自动放到目标屏幕的左上角
    y: int = -1

    # ---- 采样 ----
    refresh_ms: int = 250
    presentmon_path: str = ""
    target_process: str = ""    # 留空 = 自动：优先跟前台窗口的游戏，再退回最忙的出帧进程
    # 精确传感器：用提权子进程 + LibreHardwareMonitor 读 CPU/GPU 温度，
    # 需要 PawnIO 内核驱动（首次开启会弹一次 UAC）。
    precise_sensors: bool = False

    # ---- 热键 ----
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    def clamped(self) -> "Config":
        """把所有数值收进合法区间；幂等，且对任何脏值都不抛异常。"""
        self.tier = min(4, max(1, _as_int(self.tier, 4)))
        self.font_size = min(28, max(9, _as_int(self.font_size, 13)))
        self.opacity = min(1.0, max(0.1, _as_float(self.opacity, 0.72)))
        self.saturation = min(2.0, max(0.0, _as_float(self.saturation, 1.0)))
        self.refresh_ms = min(1000, max(100, _as_int(self.refresh_ms, 250)))
        self.x = _as_int(self.x, -1)
        self.y = _as_int(self.y, -1)
        self.sparkline = _as_bool(self.sparkline, True)
        self.click_through = _as_bool(self.click_through, False)
        self.capture_exclusion = _as_bool(self.capture_exclusion, True)
        self.always_on_top = _as_bool(self.always_on_top, True)
        self.show_in_taskbar = _as_bool(self.show_in_taskbar, True)
        self.game_only = _as_bool(self.game_only, False)
        self.autostart = _as_bool(self.autostart, False)
        self.precise_sensors = _as_bool(self.precise_sensors, False)
        self.monitor = _as_str(self.monitor, "")
        self.presentmon_path = _as_str(self.presentmon_path, "")
        self.target_process = _as_str(self.target_process, "")
        if self.layout not in ("row", "column"):
            self.layout = "row"
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """从磁盘读取；文件缺失、不可读、JSON 损坏、某字段类型不对，一律安全回落。"""
        explicit = path is not None
        path = path or config_path()
        if not explicit:
            _migrate_legacy(path)
        cfg = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        if not isinstance(raw, dict):
            return cfg
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key not in known:
                continue
            if key == "hotkeys":
                # 嵌套结构要按字段重建，否则会被塞进一个普通 dict，后续 .cycle_monitor 就崩了。
                if isinstance(value, dict):
                    sub = {f.name for f in fields(HotkeyConfig)}
                    cfg.hotkeys = HotkeyConfig(
                        **{
                            k: v
                            for k, v in value.items()
                            if k in sub and isinstance(v, str) and v
                        }
                    )
                continue
            if key == "sections":
                current = cfg.sections
                if isinstance(value, dict):
                    sub = {f.name for f in fields(Sections)}
                    for name in sub:
                        if name in value:
                            setattr(current, name, _as_bool(value[name], getattr(current, name)))
                continue
            default = getattr(cfg, key)
            if isinstance(default, bool):
                setattr(cfg, key, _as_bool(value, default))
            elif isinstance(default, int):
                setattr(cfg, key, _as_int(value, default))
            elif isinstance(default, float):
                setattr(cfg, key, _as_float(value, default))
            elif isinstance(default, str):
                setattr(cfg, key, _as_str(value, default))
            # 其余类型（嵌套 dataclass）已在上面单独处理，这里直接忽略
        return cfg.clamped()

    def save(self, path: Path | None = None) -> None:
        """原子写入：先写临时文件再改名，避免半截文件。"""
        path = path or config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            # 静默吞掉的话，用户看到的是"设置改了、重启又变回去"，
            # 和真的没保存成功完全无法区分，所以至少留一条日志。
            try:
                import pylog

                pylog.log(f"[config] 保存 {path} 失败：{exc}")
            except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
                pass
