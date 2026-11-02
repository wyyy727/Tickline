"""基于 Intel PresentMon 的帧率数据源。

叠加层自己不开 ETW 会话：它启动 `PresentMon.exe --output_stdout`，然后消费它逐帧打印的 CSV。
真正用到的只有两列：

  MsBetweenPresents        应用两次 Present() 之间的间隔
  MsBetweenDisplayChange   屏幕两次真正变帧之间的间隔

取倒数就是参考图里并排显示的两个数字：游戏的实际出帧率，以及开启 DLSS / FSR 帧生成后玩家
看到的帧率。帧生成关闭时两者相等，这一点本身就可以用来校验采集是否正确。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

HEADER_PRESENTS = "MsBetweenPresents"
HEADER_DISPLAY = "MsBetweenDisplayChange"
HEADER_APP = "Application"
HEADER_PID = "ProcessID"

# 用自己的 ETW 会话名，避免与用户已经开着的 PresentMon 撞名。
SESSION_NAME = "PaceliteMonitor"

# 桌面合成器一直在出帧，若参加"最忙进程"选举它会永远胜出，叠加层就会一直显示 40-60 帧。
# 这些进程永不作为目标，但它们的行仍然用于取显示变帧间隔。
COMPOSITOR_PROCESSES = frozenset(
    {"dwm.exe", "csrss.exe", "winlogon.exe", "logonui.exe", "lockapp.exe", "textinputhost.exe"}
)

_WINDOW_S = 1.0          # 瞬时帧率的平均窗口（秒）
_HISTORY = 120           # 曲线保留的采样点数（250 ms 间隔约 30 秒）
_MAX_SAMPLES = 600       # 每个进程保留的原始帧间隔数


def _to_float(text: str) -> float | None:
    """CSV 里的 "NA" 与异常值一律当作无数据。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0.0 or value > 10_000.0:
        return None
    return value


@dataclass
class FrameStats:
    presented_fps: float | None = None
    displayed_fps: float | None = None
    min_fps: float | None = None
    max_fps: float | None = None
    frame_time_ms: float | None = None
    target: str = ""
    pid: int | None = None
    # 帧生成那段的标签：自动识别出 DLSS / FSR 就写它，识别不出退回中性的 FG。
    fg_label: str = "FG"
    active: bool = False
    error: str | None = None
    series: list[float] = field(default_factory=list)

    @property
    def ratio(self) -> float | None:
        """帧生成倍率：每个应用帧对应多少显示帧。"""
        if self.presented_fps and self.displayed_fps and self.presented_fps > 1.0:
            return self.displayed_fps / self.presented_fps
        return None

    @property
    def generating(self) -> bool:
        """倍率超过 1.25 就认为帧生成开着，避免把测量抖动误判成插帧。"""
        ratio = self.ratio
        return ratio is not None and ratio >= 1.25


class FakeFrameFeed:
    """样式评审用的合成数据源：不需要跑游戏也能看到真实感数字。

    仅供 `--fake-fps` 使用，完全不碰 ETW；数字是编的，不能当测量结果引用。
    """

    def __init__(self) -> None:
        self.synthetic = True
        self._series: deque[float] = deque(maxlen=_HISTORY)
        self._tick = 0
        self._min: float | None = None
        self._max: float | None = None

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        return None

    @property
    def error(self) -> None:
        return None

    def stats(self) -> FrameStats:
        import math

        self._tick += 1
        # 两条正弦叠加，制造"有起伏但不夸张"的帧率曲线。
        base = 64.0 + 6.0 * math.sin(self._tick / 7.0) - 4.0 * math.sin(self._tick / 2.3)
        presented = max(20.0, base)
        self._series.append(presented)
        self._min = presented if self._min is None else min(self._min, presented)
        self._max = presented if self._max is None else max(self._max, presented)
        return FrameStats(
            presented_fps=presented,
            displayed_fps=presented * 2.0,   # 模拟 2 倍帧生成
            min_fps=self._min,
            max_fps=self._max,
            frame_time_ms=1000.0 / presented,
            target="SyntheticPresenter.exe",
            fg_label="DLSS",
            active=True,
            series=list(self._series),
        )


class PresentMonFeed:
    """持有 PresentMon 子进程，把它的 CSV 流归约成 `FrameStats`。"""

    def __init__(self, exe: Path | None = None, target: str = "", refresh_ms: int = 250) -> None:
        self.exe = Path(exe) if exe else None
        self.target = target
        self._interval = max(0.1, refresh_ms / 1000.0)
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 每个进程一条呈现泳道：不同进程的相邻两次 Present 相减毫无意义，必须分开算。
        self._presents: dict[int, deque[tuple[float, float, str]]] = {}
        self._displays: deque[tuple[float, float]] = deque(maxlen=_MAX_SAMPLES)
        self._fps_history: deque[float] = deque(maxlen=_HISTORY)
        self._min_fps: float | None = None
        self._max_fps: float | None = None
        self._error: str | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        # 帧生成标签按进程缓存：枚举模块有成本，而且一个进程用的技术不会中途变。
        self._fg_cache: dict[int, tuple[float, str | None]] = {}

    # ---- 生命周期 --------------------------------------------------------
    def resolve_exe(self) -> Path | None:
        """按"显式路径 → 程序同级目录 → 源码同级目录 → 打包临时目录 → PATH"找 PresentMon。"""
        if self.exe and self.exe.is_file():
            return self.exe
        candidates = [
            # 打包后的目录版：exe 旁边的 tools\PresentMon
            Path(sys.executable).resolve().parent / "tools" / "PresentMon" / "PresentMon.exe",
            # 源码运行
            Path(__file__).resolve().parent.parent / "tools" / "PresentMon" / "PresentMon.exe",
            Path(r"D:\workspace\pacecarnew\tools\PresentMon\PresentMon.exe"),
        ]
        # 单文件版会把自己解压到 _MEIPASS
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.insert(0, Path(bundle) / "tools" / "PresentMon" / "PresentMon.exe")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        found = shutil.which("PresentMon.exe") or shutil.which("PresentMon")
        return Path(found) if found else None

    def start(self) -> bool:
        exe = self.resolve_exe()
        if exe is None:
            self._error = "未找到 PresentMon.exe"
            return False
        args = [
            str(exe),
            "--output_stdout",
            "--no_console_stats",
            "--session_name",
            SESSION_NAME,
            "--stop_existing_session",
        ]
        if self.target:
            args += ["--process_name", self.target]
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._error = f"启动 PresentMon 失败: {exc}"
            return False
        self.exe = exe
        self._thread = threading.Thread(target=self._read_loop, name="pacelite-presentmon", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=1.5)
        self._proc = None
        self._cleanup_session()

    def _cleanup_session(self) -> None:
        """收掉自己开的 ETW 会话。

        PresentMon 被强杀时 realtime 会话会留在系统里，下一次采集就会因同名会话而失败、
        甚至卡住 —— 这正是本程序绝不能制造的问题。`--terminate_existing_session` 是
        PresentMon 自带的（需要相应权限的）停止路径。
        """
        exe = self.exe or self.resolve_exe()
        if exe is None:
            return
        try:
            subprocess.run(
                [str(exe), "--terminate_existing_session", "--session_name", SESSION_NAME],
                capture_output=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass

    @property
    def error(self) -> str | None:
        return self._error

    # ---- 解析 ------------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        header: list[str] | None = None
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("Application,"):
                header = line.split(",")
                continue
            if header is None:
                continue
            self._handle_row(header, line.split(","))

    def _handle_row(self, header: list[str], row: list[str]) -> None:
        def field(name: str) -> str:
            # 列的顺序会随 PresentMon 版本变，所以永远按表头查，不写死下标。
            try:
                return row[header.index(name)]
            except (ValueError, IndexError):
                return ""

        now = time.monotonic()
        pid = _to_float(field(HEADER_PID))
        if pid is None:
            return
        pid = int(pid)
        present_ms = _to_float(field(HEADER_PRESENTS))
        if present_ms is not None:
            with self._lock:
                lane = self._presents.get(pid)
                if lane is None:
                    lane = deque(maxlen=_MAX_SAMPLES)
                    self._presents[pid] = lane
                lane.append((now, present_ms, field(HEADER_APP)))
        display_ms = _to_float(field(HEADER_DISPLAY))
        if display_ms is not None:
            with self._lock:
                self._displays.append((now, display_ms))

    # ---- 归约 ------------------------------------------------------------
    def presenters(self) -> list[str]:
        """最近几秒内在出帧的进程名，给设置窗口的"监视进程"下拉用。"""
        now = time.monotonic()
        names: set[str] = set()
        with self._lock:
            for lane in self._presents.values():
                for stamp, _ms, name in lane:
                    if now - stamp <= 5.0 and name and name.lower() not in COMPOSITOR_PROCESSES:
                        names.add(name)
        return sorted(names)

    def stats(self) -> FrameStats:
        """取当前窗口内的两条帧率、历史最值与曲线数据。"""
        with self._lock:
            now = time.monotonic()
            presented, target, frame_time, pid = self._active_rate(now)
            displayed = self._mean_rate(self._displays, now)
            if presented and presented >= 1.0:
                self._min_fps = presented if self._min_fps is None else min(self._min_fps, presented)
                self._max_fps = presented if self._max_fps is None else max(self._max_fps, presented)
                self._fps_history.append(presented)
            series = list(self._fps_history)
            stats = FrameStats(
                presented_fps=presented,
                displayed_fps=displayed,
                min_fps=self._min_fps,
                max_fps=self._max_fps,
                frame_time_ms=frame_time,
                target=target,
                pid=pid,
                fg_label=self._fg_label(pid, now),
                active=presented is not None,
                error=self._error,
                series=series,
            )
        return stats

    def _fg_label(self, pid: int | None, now: float) -> str:
        """缓存 10 秒的帧生成技术识别，识别不出就用 "FG"。"""
        if not pid:
            return "FG"
        cached = self._fg_cache.get(pid)
        if cached is not None and now - cached[0] < 10.0:
            return cached[1] or "FG"
        try:
            from winprobe import frame_generation_label

            label = frame_generation_label(pid)
        except Exception:  # noqa: BLE001 - 探测失败只影响标签，不影响数值
            label = None
        self._fg_cache[pid] = (now, label)
        return label or "FG"

    @staticmethod
    def _mean_rate(samples, now: float) -> float | None:
        """窗口内帧间隔的均值取倒；样本太少时返回 None，避免刷出跳动很大的假数。"""
        recent = [ms for stamp, ms in samples if now - stamp <= _WINDOW_S]
        if len(recent) < 3:
            return None
        mean = sum(recent) / len(recent)
        return 1000.0 / mean if mean > 0 else None

    def _active_rate(self, now: float) -> tuple[float | None, str, float | None, int | None]:
        """在窗口内选出最忙的出帧进程，并平均它最近的帧间隔。

        用户手动指定了进程时只认它：没在出帧就返回 None，界面显示 "—"，
        而不是悄悄回落到"最忙的那个"让人以为选错了也能用。
        """
        wanted = (self.target or "").strip().lower()
        best_pid = None
        best_count = 0
        best_name = ""
        for pid, lane in self._presents.items():
            # 丢掉过期泳道，关掉的游戏就不会继续"当选"。
            while lane and now - lane[0][0] > 5.0:
                lane.popleft()
            recent = [entry for entry in lane if now - entry[0] <= _WINDOW_S]
            if not recent:
                continue
            name = recent[-1][2]
            if name.lower() in COMPOSITOR_PROCESSES:
                continue
            if wanted and name.lower() != wanted:
                continue
            if len(recent) > best_count:
                best_count = len(recent)
                best_pid = pid
                best_name = name
        if best_pid is None or best_count < 3:
            return None, best_name, None, None
        lane = self._presents[best_pid]
        recent = [ms for stamp, ms, _name in lane if now - stamp <= _WINDOW_S]
        if not recent:
            return None, best_name, None, best_pid
        mean = sum(recent) / len(recent)
        if mean <= 0:
            return None, best_name, None, best_pid
        return 1000.0 / mean, best_name, recent[-1], best_pid
