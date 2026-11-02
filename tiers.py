"""档位定义：每个档位显示哪些指标、什么顺序、标签用什么颜色。

四个档位对应参考图的信息层级：

  1 简洁 FPS      只显示帧率（开了帧生成时附带 FG 帧率）
  2 FPS 详情      帧生成帧率、实际帧率、最小/最大帧、帧时间曲线
  3 加入 CPU/GPU  再挂上处理器与显卡占用
  4 全面监控      再挂上 CPU 温度/功耗/频率、GPU 温度与功耗、显存、内存、SSD

每个小段都带 `group`（fps / cpu / gpu / mem / ssd），这样"整行显示"与"按组换行显示"
两种布局都能从同一份数据切出来。
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Sections
from metrics import Snapshot
from presentmon import FrameStats

TIER_NAMES = {
    1: "简洁 FPS",
    2: "FPS 详情",
    3: "加入 CPU/GPU",
    4: "全面监控",
}

DASH = "—"


@dataclass
class Segment:
    """一行里的一个"标签 + 数值 + 单位"小段，或者一段曲线。"""

    label: str
    value: str
    unit: str = ""
    color: str = "misc"
    kind: str = "text"
    # 箭头（↑ / ↓）视觉上属于数字的一部分，所以间距要比文字标签更紧。
    tight: bool = False
    # 曲线段的宽度（DIP）；文字段忽略此项。
    spark_width: int = 96
    # 归属分组，换行布局按它切行。
    group: str = "fps"


def _fps(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}"


def _percent(value: float | None) -> str:
    return DASH if value is None else f"{value:.0f}"


def _clock(mhz: float | None) -> str:
    return DASH if mhz is None else f"{mhz / 1000.0:.2f}"


def _gib(value: int | None) -> str:
    return DASH if value is None else f"{value / (1024 ** 3):.1f}"


def _rate(bytes_per_second: float | None) -> tuple[str, str]:
    """把字节/秒压成 (数值, 单位)，单位留一位小数够看，不要 12 位数。"""
    if bytes_per_second is None:
        return DASH, ""
    if bytes_per_second >= 1024 ** 3:
        return f"{bytes_per_second / 1024 ** 3:.1f}", "GB/S"
    if bytes_per_second >= 1024 ** 2:
        return f"{bytes_per_second / 1024 ** 2:.1f}", "MB/S"
    if bytes_per_second >= 1024:
        return f"{bytes_per_second / 1024:.0f}", "KB/S"
    return f"{bytes_per_second:.0f}", "B/S"


def build_segments(
    tier: int,
    snap: Snapshot,
    fps: FrameStats,
    sections: Sections | None = None,
    sparkline: bool = True,
) -> list[Segment]:
    """按档位、开关与硬件可用性拼出所有小段。取不到的指标整段省略，绝不编造 0。"""
    sections = sections or Sections()
    segments: list[Segment] = []
    available = snap.available

    # ---- 帧率（group=fps）----
    # 1 档只给一个数字：开帧生成时给玩家看到的那个帧率，且只带一个标签。
    if sections.fps:
        if fps.generating:
            segments.append(
                Segment(fps.fg_label, _fps(fps.displayed_fps), color="fg", group="fps")
            )
            if tier >= 2:
                segments.append(Segment("FPS", _fps(fps.presented_fps), color="fps", group="fps"))
        else:
            segments.append(Segment("FPS", _fps(fps.presented_fps), color="fps", group="fps"))
        if tier >= 2:
            segments.append(Segment("↓", _fps(fps.min_fps), color="fps", tight=True, group="fps"))
            segments.append(Segment("↑", _fps(fps.max_fps), color="fps", tight=True, group="fps"))
            if sparkline:
                segments.append(
                    Segment("", "", kind="spark", color="fps", spark_width=110, group="fps")
                )

    if tier < 3:
        return segments

    # ---- CPU 组：占用 → 最忙单核 → 温度 → 功耗 → 频率 ----
    # 同一硬件的字段必须连在一起；之前中间插过 GPU，读起来像显卡的频率。
    if sections.cpu:
        segments.append(Segment("CPU", _percent(snap.cpu_total), "%", color="cpu", group="cpu"))
        if tier >= 4:
            if snap.cpu_core_peak is not None:
                segments.append(
                    Segment(
                        "↑",
                        _percent(snap.cpu_core_peak),
                        "%",
                        color="cpu",
                        tight=True,
                        group="cpu",
                    )
                )
            if available.get("cpu_temp") and snap.cpu_temp is not None:
                segments.append(
                    Segment("", f"{snap.cpu_temp:.0f}", "°C", color="temp", group="cpu")
                )
            if snap.cpu_power is not None and snap.cpu_power >= 1:
                segments.append(Segment("", f"{snap.cpu_power:.0f}", "W", color="temp", group="cpu"))
            if snap.cpu_freq is not None or snap.cpu_freq_max is not None:
                clock = f"{_clock(snap.cpu_freq)}/{_clock(snap.cpu_freq_max)}"
                segments.append(Segment("", clock, "GHz", color="cpu_clock", group="cpu"))

    # ---- GPU 组：占用 → 温度 → 功耗 → 显存 ----
    # 显存属于显卡，跟 GPU 放同一组；之前和内存挤在一组，按组换行时会跑到内存那行去。
    if sections.gpu:
        segments.append(Segment("GPU", _percent(snap.gpu_util), "%", color="gpu", group="gpu"))
        if tier >= 4:
            if available.get("gpu_temp") and snap.gpu_temp is not None:
                segments.append(
                    Segment("", f"{snap.gpu_temp:.0f}", "°C", color="temp", group="gpu")
                )
            if snap.gpu_power is not None and snap.gpu_power >= 1:
                segments.append(Segment("", f"{snap.gpu_power:.0f}", "W", color="temp", group="gpu"))
            if sections.vram and snap.vram_used is not None:
                # 有总量就是 used/total，没有就只给一个数（核显拿不到可信的共享显存上限）。
                if snap.vram_total:
                    value = f"{_gib(snap.vram_used)}/{_gib(snap.vram_total)}"
                else:
                    value = _gib(snap.vram_used)
                segments.append(Segment("VRAM", value, "GB", color="gpu_mem", group="gpu"))

    if tier < 4:
        return segments

    # ---- 内存组：内存用量（DDR5 才有内存温度）----
    if sections.ram and snap.mem_used is not None and snap.mem_total is not None:
        segments.append(
            Segment(
                "RAM",
                f"{_gib(snap.mem_used)}/{_gib(snap.mem_total)}",
                "GB",
                color="ram",
                group="mem",
            )
        )
        if available.get("mem_temp") and snap.mem_temp is not None:
            segments.append(Segment("", f"{snap.mem_temp:.0f}", "°C", color="temp", group="mem"))

    # ---- 硬盘组：每块盘一组，温度 → R 读速 → W 写速 ----
    if sections.ssd and available.get("ssd"):
        multiple = len(snap.ssd) > 1
        for index, disk in enumerate(snap.ssd):
            group = f"ssd{index + 1}" if multiple else "ssd"
            label = f"SSD{index + 1}" if multiple else "SSD"
            has_temp = disk.temp is not None
            has_read = disk.read is not None and disk.read > 0
            has_write = disk.write is not None and disk.write > 0
            if not (has_temp or has_read or has_write):
                continue
            if has_temp:
                segments.append(
                    Segment(label, f"{disk.temp:.0f}", "°C", color="ssd", group=group)
                )
            # 速度只在真的有 IO 时出现：空闲时挂两个 "0 B/S" 纯属噪声。
            # 没有温度传感器时，第一个可用数值挂盘标签，避免出现只有标签没有数值的空壳段。
            if has_read:
                value, unit = _rate(disk.read)
                segments.append(
                    Segment("R" if has_temp else label, value, unit, color="ssd", group=group)
                )
            if has_write:
                value, unit = _rate(disk.write)
                segments.append(
                    Segment(
                        "W" if (has_temp or has_read) else label,
                        value,
                        unit,
                        color="ssd",
                        group=group,
                    )
                )

    return segments


def build_rows(segments: list[Segment], layout: str) -> list[list[Segment]]:
    """把同一份分段数据切成"整行"或"按组换行"两种布局。

    分组顺序按小段"首次出现"的顺序，而不是写死一张组名表 —— 这样多块硬盘
    （ssd1/ssd2/…）能自动各占一行，不需要改布局代码。
    """
    if layout != "column":
        return [segments]
    grouped: dict[str, list[Segment]] = {}
    for segment in segments:
        grouped.setdefault(segment.group, []).append(segment)
    return [row for row in grouped.values() if row] or [segments]
