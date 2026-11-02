"""Pacelite 的用户配置持久化。

以 JSON 存放在 %APPDATA%\\Pacelite\\config.json。读取时忽略未知键、缺失键保留默认值，
因此旧配置文件不会拖坏新版本。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def config_path() -> Path:
    """配置文件位置；%APPDATA% 不可用时退回用户目录。"""
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "Pacelite" / "config.json"


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
    # 显示在哪块屏幕上，存屏幕名（如 "\\.\DISPLAY2"）。空字符串 = 自动（主屏）。
    # 用名字而不是序号，是因为插拔显示器后 Qt 给的顺序会变。
    monitor: str = ""
    x: int = -1                 # -1 表示自动放到目标屏幕的左上角
    y: int = -1

    # ---- 采样 ----
    refresh_ms: int = 250
    presentmon_path: str = ""
    target_process: str = ""    # 留空 = 自动选择最忙的出帧进程
    # 精确传感器：用提权子进程 + LibreHardwareMonitor 读 CPU/GPU 温度，
    # 需要 PawnIO 内核驱动（首次开启会弹一次 UAC）。
    precise_sensors: bool = False

    # ---- 热键 ----
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    def clamped(self) -> "Config":
        """把所有数值收进合法区间；幂等。"""
        self.tier = min(4, max(1, int(self.tier)))
        self.font_size = min(28, max(9, int(self.font_size)))
        self.opacity = min(1.0, max(0.1, float(self.opacity)))
        self.saturation = min(2.0, max(0.0, float(self.saturation)))
        self.refresh_ms = min(1000, max(100, int(self.refresh_ms)))
        if self.layout not in ("row", "column"):
            self.layout = "row"
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """从磁盘读取；文件缺失、不可读、JSON 损坏一律回落到默认值。"""
        path = path or config_path()
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
                        **{k: v for k, v in value.items() if k in sub and isinstance(v, str)}
                    )
                continue
            if key == "sections":
                if isinstance(value, dict):
                    sub = {f.name for f in fields(Sections)}
                    cfg.sections = Sections(
                        **{k: bool(v) for k, v in value.items() if k in sub}
                    )
                continue
            setattr(cfg, key, value)
        return cfg.clamped()

    def save(self, path: Path | None = None) -> None:
        """原子写入：先写临时文件再改名，避免半截文件。"""
        path = path or config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
