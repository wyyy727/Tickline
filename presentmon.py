"""基于 Intel PresentMon 的帧率数据源。

叠加层自己不开 ETW 会话：它启动 `PresentMon.exe --output_stdout`，然后消费它逐帧打印的 CSV。
真正用到的只有两列：

  MsBetweenPresents        应用两次 Present() 之间的间隔
  MsBetweenDisplayChange   屏幕两次真正变帧之间的间隔

取倒数就是参考图里并排显示的两个数字：游戏的实际出帧率，以及开启 DLSS / FSR 帧生成后玩家
看到的帧率。帧生成关闭时两者相等，这一点本身就可以用来校验采集是否正确。

两个 API 要分清：
  stats()   —— 纯读取，绝不产生副作用（重绘、判定"有没有游戏在跑"都可以随时调）
  sample()  —— 由界面的刷新定时器每格调一次，推进曲线历史与 min/max
以前两者是同一个方法，结果每重绘一次就往曲线里塞一个点，120 点的窗口实际只覆盖一半时间。
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

import pylog

# 列名一律按大小写不敏感匹配：PresentMon 2.x 有两套表头 —— 默认那套是 PascalCase
# 的 MsBetweenPresents / MsBetweenDisplayChange，加 --v2_metrics 才是 camelCase 的
# msBetweenPresents / msBetweenDisplayChange（v1 里还有 "PresentFlags." 这种带尾点的列名）。
# 只按原样大小写查表，换一套就一行都解析不出来（帧率永远停在 "—"）。
HEADER_PRESENTS = "MsBetweenPresents"
HEADER_DISPLAY = "MsBetweenDisplayChange"
HEADER_APP = "Application"
HEADER_PID = "ProcessID"
# v2 指标（--v2_metrics）里没有上面那两个名字，同样的量叫 FrameTime / DisplayedTime。
HEADER_PRESENTS_V2 = "FrameTime"
HEADER_DISPLAY_V2 = "DisplayedTime"
# 只有 v2 指标带这一列：区分"应用自己渲染的帧"和"驱动插出来的帧"。
HEADER_FRAME_TYPE = "FrameType"


def _column_index(header: list[str]) -> dict[str, int]:
    """表头行 → {小写列名: 下标}。

    顺便去掉 BOM、引号、首尾空白和末尾的点：v1 表头里带过 "PresentFlags." 这种尾点。
    重复的列名只认第一个。
    """
    columns: dict[str, int] = {}
    for index, name in enumerate(header):
        key = name.strip().strip('"').lstrip("\ufeff").rstrip(".").strip().lower()
        if key and key not in columns:
            columns[key] = index
    return columns

# 用自己的 ETW 会话名，避免与用户已经开着的 PresentMon 撞名。
SESSION_NAME = "TicklineMonitor"

# 桌面合成器一直在出帧，若参加"最忙进程"选举它会永远胜出，叠加层就会一直显示 40-60 帧。
# 这些进程永不作为目标，但它们的行仍然用于取显示变帧间隔。
COMPOSITOR_PROCESSES = frozenset(
    {"dwm.exe", "csrss.exe", "winlogon.exe", "logonui.exe", "lockapp.exe", "textinputhost.exe"}
)

_WINDOW_S = 1.0          # 退回"全进程合并"时，按到达时间取样本的窗口（秒）
_RATE_WINDOW_MS = 1000.0  # 帧率窗口：把帧间隔累加到约 1 秒，不按"到达时间"开窗
_PICK_WINDOW_S = 2.0      # 选目标进程时，看它最近几秒有没有在出帧
# 这么久没新帧就算停了：宁可显示 "—"，也不停在旧数字上。不能太小 ——
# PresentMon 的 stdout 是块缓冲，30 fps 时一块约 1.5 秒，卡太紧会在两批之间闪 "—"。
_STALE_S = 2.5
_HISTORY = 120           # 曲线保留的采样点数（250 ms 一格约 30 秒）
_MAX_SAMPLES = 600       # 每个进程保留的原始帧间隔数
_LANE_TTL_S = 10.0       # 这么久没再出帧的进程，泳道直接回收
# "谁有全屏窗口"要枚举所有顶层窗口（十几毫秒），不能每格都算；3 秒够快也不卡界面。
_FULLSCREEN_REFRESH_S = 3.0

# PresentMon 拒绝创建 ETW 会话时 stderr 里的原话；是"没管理员权限"的最可靠信号。
_ACCESS_DENIED_MARKERS = ("access denied", "access is denied", "5 (", "0x5")
_ADMIN_HINT = (
    "需要管理员权限：PresentMon 无法创建 ETW 会话。"
    "请右键程序选「以管理员身份运行」，或把当前用户加入「Performance Log Users」组后重新登录。"
)


def _to_float(text: str) -> float | None:
    """CSV 里的 "NA" 与异常值一律当作无数据。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0.0 or value > 10_000.0:
        return None
    return value


def _to_pid(text: str) -> int | None:
    """CSV 里的进程 id。

    不能借用 _to_float：它把 >10000 的值当异常丢掉（那是对帧间隔的合理性检查），
    而 Windows 的 PID 普遍是四五位数 —— 用错函数会让整行数据被当成"没有数据"，
    表现就是帧率永远显示 "—"。
    """
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    if value <= 0 or value > 0xFFFFFFFF:
        return None
    return value


def _decode_stderr(raw: bytes) -> str:
    """PresentMon 在 Windows 上把 stderr 写成 UTF-16LE（带 BOM），不带 BOM 的按 UTF-8 兜底。"""
    if not raw:
        return ""
    try:
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            return raw.decode("utf-16", "replace")
        # 一半左右是 0 字节，基本可以断定是 UTF-16LE 而没带 BOM。
        if raw.count(b"\x00") * 4 > len(raw):
            return raw.decode("utf-16-le", "replace")
        return raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace")


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
        self.target = ""
        self._series: deque[float] = deque(maxlen=_HISTORY)
        self._tick = 0
        self._min: float | None = None
        self._max: float | None = None
        self._last: FrameStats | None = None
        self._advance()

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        return None

    def set_target(self, name: str) -> None:
        self.target = name or ""

    @property
    def error(self) -> None:
        return None

    def _advance(self) -> None:
        import math

        self._tick += 1
        # 两条正弦叠加，制造"有起伏但不夸张"的帧率曲线。
        base = 64.0 + 6.0 * math.sin(self._tick / 7.0) - 4.0 * math.sin(self._tick / 2.3)
        presented = max(20.0, base)
        # 曲线和 ↓/↑ 跟着界面显示的那个数（这里是 2 倍帧生成后的显示帧率），
        # 不然 demo 里会出现"DLSS 126 / ↓63 ↑74"这种自相矛盾的组合。
        shown = presented * 2.0
        self._series.append(shown)
        self._min = shown if self._min is None else min(self._min, shown)
        self._max = shown if self._max is None else max(self._max, shown)
        self._last = FrameStats(
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

    def sample(self) -> FrameStats:
        """推进一格合成数据（由界面刷新定时器调用）。"""
        self._advance()
        assert self._last is not None
        return self._last

    def stats(self) -> FrameStats:
        """纯读取：返回最近一次 sample() 的结果。"""
        assert self._last is not None
        return self._last


class PresentMonFeed:
    """持有 PresentMon 子进程，把它的 CSV 流归约成 `FrameStats`。"""

    def __init__(self, exe: Path | None = None, target: str = "", refresh_ms: int = 250) -> None:
        self.exe = Path(exe) if exe else None
        self.target = target
        self._interval = max(0.1, refresh_ms / 1000.0)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 用可重入锁：读路径（stats → _active_rate/_displayed_rate）会在持锁期间
        # 再调内部归约函数，普通 Lock 会把自己锁死（GUI 线程第一格就永久卡住）。
        self._lock = threading.RLock()
        # 每个进程一条呈现泳道：不同进程的相邻两次 Present 相减毫无意义，必须分开算。
        self._presents: dict[int, deque[tuple[float, float, str]]] = {}
        # 显示变帧也按进程分开存：不分开的话，旁边一个视频播放器就能把倍率算歪。
        self._displays: dict[int, deque[tuple[float, float]]] = {}
        self._fps_history: deque[float] = deque(maxlen=_HISTORY)
        # 曲线属于"当前这个进程"：换游戏/换窗口就清空，不然上一局的曲线和 ↓/↑ 会跟着走。
        self._sample_pid: int | None = None
        self._error: str | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._stderr_raw = bytearray()
        self._rows = 0
        # 帧生成标签按进程缓存：枚举模块有成本，而且一个进程用的技术不会中途变。
        self._fg_cache: dict[int, tuple[float, str | None]] = {}
        self._last_pid: int | None = None
        # "自动"模式优先跟前台窗口的进程（由 sample() 每格刷新一次，只读一个 Win32 调用）。
        self._fg_hint: int | None = None
        # 当前跟住的进程：一旦跟住就粘着，前台切到聊天窗口/桌面也不跟着乱跳。
        # 只有"另一个全屏进程抢到前台"（进了另一款游戏）或它自己停帧才换人。
        self._sticky_pid: int | None = None
        self._pick_reason = ""
        self._fullscreen: frozenset[int] = frozenset()
        self._fs_at = 0.0
        # 最近一次选目标时的候选（(到达条数, pid, 进程名)），排障日志用。
        self._last_candidates: list[tuple[int, int, str]] = []
        self._debug_at = 0.0

    # ---- 生命周期 --------------------------------------------------------
    def resolve_exe(self) -> Path | None:
        """按"显式路径 → 程序同级目录 → 源码同级目录 → 打包临时目录 → PATH"找 PresentMon。"""
        if self.exe and self.exe.is_file():
            return self.exe
        candidates = [
            # 打包后的目录版：exe 旁边的 tools\\PresentMon
            Path(sys.executable).resolve().parent / "tools" / "PresentMon" / "PresentMon.exe",
            # 源码运行
            Path(__file__).resolve().parent.parent / "tools" / "PresentMon" / "PresentMon.exe",
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
        if self._proc is not None and self._proc.poll() is None:
            return True  # 已经在跑
        exe = self.resolve_exe()
        if exe is None:
            self._error = "未找到 PresentMon.exe（tools\\PresentMon\\PresentMon.exe）"
            pylog.log(f"[fps] {self._error}")
            return False
        args = [
            str(exe),
            "--output_stdout",
            "--no_console_stats",
            "--session_name",
            SESSION_NAME,
            "--stop_existing_session",
            # v1（默认）没有 FrameType 列，分不出"应用渲染的帧"和"驱动插出来的帧"，
            # 帧生成倍率只能靠显示变帧率反推；v2 指标自带 FrameType，能直接分开算。
            # 解析层同时认两套列名（HEADER_*_V2），所以换 v2 不影响别的。
            "--v2_metrics",
            "--track_frame_type",
        ]
        if self.target:
            args += ["--process_name", self.target]
        with self._lock:
            self._error = None
            self._rows = 0
            self._stderr.clear()
            self._stderr_raw.clear()
        self._stop.clear()
        try:
            # 不用 text=True：stderr 是 UTF-16LE，stdout 是 UTF-8，两者编码不同，
            # 统一按字节读回来各自解码。
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._error = f"启动 PresentMon 失败: {exc}"
            pylog.log(f"[fps] {self._error}")
            return False
        self.exe = exe
        self._thread = threading.Thread(target=self._read_loop, name="tickline-presentmon", daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="tickline-presentmon-err", daemon=True
        )
        self._stderr_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        for thread in (self._thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.5)
        self._thread = None
        self._stderr_thread = None
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
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        columns: dict[str, int] | None = None
        while not self._stop.is_set():
            try:
                raw = stdout.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").lstrip("\ufeff").strip()
            if not line:
                continue
            if columns is None and line.lower().startswith("application,"):
                columns = _column_index(line.split(","))
                # 只写一次：PresentMon 的列名/顺序随版本变，出问题时这一行是唯一的证据。
                pylog.log(f"[fps] 表头 列数={len(columns)} 列={','.join(columns)}")
                continue
            if columns is None:
                continue
            self._handle_row(columns, line.split(","))
        self._on_exit(proc)

    def _on_exit(self, proc: subprocess.Popen) -> None:
        """子进程结束：把"为什么没有数据"翻译成一句用户能看懂的话。"""
        if self._stop.is_set():
            return
        try:
            code = proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            code = None
        # stderr 常常比 stdout 晚一步到（两条管道各读各的）。不等它一下，
        # "access denied" 就会被误判成"没有任何帧数据"，用户看到的提示完全没用。
        thread = self._stderr_thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=0.5)
        text = self._stderr_text()
        with self._lock:
            if self._error is not None:
                return
            if self._rows == 0:
                lowered = text.lower()
                if any(marker in lowered for marker in _ACCESS_DENIED_MARKERS):
                    self._error = _ADMIN_HINT
                elif "already" in lowered and "session" in lowered:
                    self._error = "PresentMon 的 ETW 会话被占用，请关闭其它抓帧工具后重启程序"
                elif text.strip():
                    self._error = f"PresentMon 退出（返回码 {code}）：{text.strip().splitlines()[-1]}"
                else:
                    self._error = f"PresentMon 已退出（返回码 {code}），没有输出任何帧数据"
                pylog.log(f"[fps] {self._error}")
                return
            # 有数据但进程没了：记一笔，界面会显示旧的曲线直到重启
            self._error = f"PresentMon 已退出（返回码 {code}）"
            pylog.log(f"[fps] {self._error}")

    def _stderr_loop(self) -> None:
        """一直把 stderr 读空。

        不读的话，子进程往管道里写满约 4 KB 就会阻塞；而且"access denied"这种关键
        信息原本就这样被吞掉了，界面上只剩一个 `—`。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        while not self._stop.is_set():
            try:
                chunk = stderr.read1(4096)  # type: ignore[attr-defined]
            except AttributeError:
                chunk = stderr.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._stderr_raw.extend(chunk)
            self._note_stderr()
        self._note_stderr(final=True)

    def _note_stderr(self, final: bool = False) -> None:
        """解出 stderr 文本：保留最后几行，遇到权限错误立刻把错误设好。"""
        text = self._stderr_text()
        if not text:
            return
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        with self._lock:
            self._stderr.clear()
            self._stderr.extend(lines[-20:])
            if self._error is not None or self._rows > 0:
                return
            lowered = text.lower()
            if any(marker in lowered for marker in _ACCESS_DENIED_MARKERS):
                self._error = _ADMIN_HINT
                pylog.log(f"[fps] {self._error}")
            elif final and lines:
                pylog.log(f"[fps] PresentMon stderr: {lines[-1]}")

    def _stderr_text(self) -> str:
        with self._lock:
            raw = bytes(self._stderr_raw)
        return _decode_stderr(raw)

    def _handle_row(self, columns: dict[str, int], row: list[str]) -> None:
        def field(*names: str) -> str:
            # 列的顺序和名字都会随 PresentMon 版本变，所以永远按表头查，不写死下标；
            # 大小写也按归一化后的小写查（见 _column_index）。给多个名字时取第一个有值的
            # —— v1 的 MsBetweenPresents 和 v2 的 FrameTime 就是这么兼容的。
            for name in names:
                index = columns.get(name.lower())
                if index is None or index >= len(row):
                    continue
                text = row[index].strip()
                if text:
                    return text
            return ""

        now = time.monotonic()
        pid = _to_pid(field(HEADER_PID))
        if pid is None:
            return
        name = field(HEADER_APP)
        present_ms = _to_float(field(HEADER_PRESENTS, HEADER_PRESENTS_V2))
        display_ms = _to_float(field(HEADER_DISPLAY, HEADER_DISPLAY_V2))
        frame_type = field(HEADER_FRAME_TYPE).strip().lower()
        with self._lock:
            if present_ms is not None:
                lane = self._presents.get(pid)
                if lane is None:
                    lane = deque(maxlen=_MAX_SAMPLES)
                    self._presents[pid] = lane
                # 第四格是 FrameType（v1 指标没有这一列，留空串），
                # 用来把"应用渲染的帧"和"驱动插出来的帧"分开算帧率。
                lane.append((now, present_ms, name, frame_type))
                self._rows += 1
                if self._rows == 1:
                    # 第一行原样记一笔：pid 解析对不对、单位是不是毫秒，一眼能看出来。
                    pylog.log(
                        f"[fps] 首行 pid={pid} 进程={name!r}"
                        f" 出帧间隔={present_ms} 变帧间隔={display_ms}"
                    )
            if display_ms is not None:
                display = self._displays.get(pid)
                if display is None:
                    display = deque(maxlen=_MAX_SAMPLES)
                    self._displays[pid] = display
                display.append((now, display_ms))
            if self._error is not None and self._rows > 0:
                self._error = None  # 开始有数据了，之前的错误提示就撤掉

    # ---- 归约 ------------------------------------------------------------
    def presenters(self) -> list[str]:
        """最近几秒内在出帧的进程名，给设置窗口的"监视进程"下拉用。"""
        now = time.monotonic()
        names: set[str] = set()
        with self._lock:
            self._prune(now)
            for lane in self._presents.values():
                for stamp, _ms, name in lane:
                    if now - stamp <= 5.0 and name and name.lower() not in COMPOSITOR_PROCESSES:
                        names.add(name)
        return sorted(names)

    def _prune(self, now: float) -> None:
        """回收过期泳道。

        以前只在读帧率时 popleft，空泳道和 pid 永不删除 —— 换一局游戏、开一堆进程之后
        字典就慢慢涨。调用方必须已持有 self._lock。
        """
        stale = [
            pid
            for pid, lane in self._presents.items()
            if not lane or now - lane[-1][0] > _LANE_TTL_S
        ]
        for pid in stale:
            self._presents.pop(pid, None)
            self._displays.pop(pid, None)
            self._fg_cache.pop(pid, None)

    def stats(self) -> FrameStats:
        """纯读取：当前窗口内的两条帧率、历史最值与曲线。不推进任何状态。"""
        with self._lock:
            now = time.monotonic()
            presented, target, frame_time, pid, overall = self._active_rate(now)
            displayed = self._displayed_rate(pid, now)
            # 显示那一路没数据、但 present 总数明显高于应用帧率时，说明确实在插帧：
            # 用全部 present 的帧率兜底（每个 present 都会变成一次显示变帧）。
            if (
                displayed is None
                and overall is not None
                and presented is not None
                and overall > presented * 1.05
            ):
                displayed = overall
            history = list(self._fps_history)
            # 最值跟着曲线走（最近约 30 秒），不是从启动到现在的全局最值：
            # 全局最值会把开局菜单那一下 144 帧永久钉在 ↑ 上，看起来就像卡住了。
            min_fps = min(history) if history else None
            max_fps = max(history) if history else None
            error = self._error
        # 枚举模块可能要几十毫秒，绝不能放在锁里做（否则读线程和界面一起卡）。
        label = self._fg_label(pid, now)
        return FrameStats(
            presented_fps=presented,
            displayed_fps=displayed,
            min_fps=min_fps,
            max_fps=max_fps,
            frame_time_ms=frame_time,
            target=target,
            pid=pid,
            fg_label=label,
            active=presented is not None,
            error=error,
            series=history,
        )

    def sample(self) -> FrameStats:
        """推进一格：把"玩家看到的那条帧率"写进曲线历史（由界面定时器调用）。

        开了帧生成时取显示帧率（玩家真正看到的），否则取应用帧率；曲线和 ↓/↑ 都基于
        这一条序列，所以三者永远说的是同一个数。min/max 只在这个窗口（约 30 秒）内取，
        不再是从启动累计的全局最值。
        """
        self._update_foreground()
        stats = self.stats()
        value = stats.displayed_fps if stats.generating else stats.presented_fps
        if value and value >= 1.0:
            with self._lock:
                if stats.pid is not None and stats.pid != self._sample_pid:
                    self._sample_pid = stats.pid
                    self._fps_history.clear()
                self._fps_history.append(value)
                stats.series = list(self._fps_history)
                stats.min_fps = min(self._fps_history)
                stats.max_fps = max(self._fps_history)
        self._log_debug(stats)
        return stats

    # ---- 排障 ------------------------------------------------------------
    def _update_foreground(self) -> None:
        """记下前台窗口的进程，并每 3 秒刷新一次"谁有全屏窗口"。

        GetForegroundWindow 几十微秒，可以每格都做；EnumWindows 要十几毫秒，
        只能隔几秒做一次，否则 250 ms 一格会把界面拖出可见的停顿。
        """
        try:
            from winprobe import foreground_pid

            self._fg_hint = foreground_pid()
        except Exception:  # noqa: BLE001 - 拿不到前台窗口只影响"自动"的优选，不影响出数
            self._fg_hint = None
        now = time.monotonic()
        if now - self._fs_at >= _FULLSCREEN_REFRESH_S:
            self._fs_at = now
            try:
                from winprobe import fullscreen_pids

                self._fullscreen = frozenset(item.pid for item in fullscreen_pids())
            except Exception:  # noqa: BLE001
                self._fullscreen = frozenset()

    def _log_debug(self, stats: FrameStats) -> None:
        """换目标时、以及每 20 秒往 tickline.log 写一行"这里跟到谁了"。

        PresentMon 只有在管理员权限下才跑得起来，出问题时没法在开发机上复现，
        这一行是唯一的现场证据：能看到选中的进程名、两个帧率、候选排行和总行数。
        """
        now = time.monotonic()
        changed = stats.pid != self._last_pid
        if not changed and now - self._debug_at < 20.0:
            return
        self._last_pid = stats.pid
        self._debug_at = now

        def value(v: float | None) -> str:
            return "—" if v is None else f"{v:.1f}"

        candidates = ", ".join(
            f"{name}({count})" for count, _pid, name in self._last_candidates
        ) or "无"
        pylog.log(
            f"[fps] 跟踪 {stats.target or '—'}({stats.pid if stats.pid is not None else '—'})"
            f" 原生={value(stats.presented_fps)} 显示={value(stats.displayed_fps)}"
            f" 标签={stats.fg_label} 理由={self._pick_reason or '—'} 前台={self._fg_hint or '—'}"
            f" 类型={self._frame_type_summary(stats.pid)} 候选=[{candidates}] 行数={self._rows}"
        )

    def _frame_type_summary(self, pid: int | None) -> str:
        """最近一批帧的 FrameType 分布，排障用（v1 指标没这列，返回 "—"）。"""
        if pid is None:
            return "—"
        with self._lock:
            lane = self._presents.get(pid)
            entries = list(lane)[-200:] if lane else []
        counts: dict[str, int] = {}
        for entry in entries:
            if len(entry) <= 3:
                continue
            counts[entry[3] or "—"] = counts.get(entry[3] or "—", 0) + 1
        if not counts:
            return "—"
        top = sorted(counts.items(), key=lambda item: -item[1])[:3]
        return ",".join(f"{key}:{value}" for key, value in top)

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

    def _lane_name(self, pid: int) -> str:
        """某个进程最近一次出帧时的进程名（没有泳道就返回空串）。"""
        lane = self._presents.get(pid)
        return lane[-1][2] if lane else ""

    def _displayed_rate(self, pid: int | None, now: float) -> float | None:
        """显示变帧率（屏幕真正换了几次画面）。指定了进程就只算它的。

        调用方（stats）已持有 self._lock，这里不再自己加锁。
        """
        if pid is not None and pid in self._displays:
            lane = self._displays[pid]
            # 和 presented 用同一条"停在多久以前就不算数"的规则：游戏一停，
            # 两个数一起变 "—"，不会出现"原生 — / 显示 120"这种半截状态。
            if lane and now - lane[-1][0] <= _STALE_S:
                return self._rate(lane)
            return None
        # 对应泳道还没建（刚启动、那批行还没到）：退回全进程合并，
        # 但要把桌面合成器排除掉 —— DWM 一直在改显示，掺进来会永远拉满刷新率。
        merged: list[tuple[float, float]] = []
        for lane_pid, lane in self._displays.items():
            if self._lane_name(lane_pid).lower() in COMPOSITOR_PROCESSES:
                continue
            if lane and now - lane[-1][0] > _STALE_S:
                continue
            merged.extend(lane)
        return self._rate(merged)

    @staticmethod
    def _rate(samples) -> float | None:
        """把泳道尾部的帧间隔累加到约 1 秒，用"帧数 / 这段时长"算帧率。

        为什么不按"最近 1 秒内到达的样本"开窗：PresentMon 的 stdout 是块缓冲的，
        几十帧会在几毫秒内一起到达。按到达时间开窗，同一批帧全落在窗内、下一批到达前
        窗口又空掉 —— 帧率就会一直卡在上一批的值上，甚至刷成 "—"。这里只认帧间隔
        本身，什么时候被读到都不影响算出来的数。
        """
        total = 0.0
        count = 0
        for entry in reversed(samples):
            total += entry[1]
            count += 1
            if total >= _RATE_WINDOW_MS:
                break
        if count < 3 or total <= 0.0:
            return None
        return 1000.0 * count / total

    @staticmethod
    def _rates(samples) -> tuple[float | None, float | None]:
        """(应用帧率, 全部 present 帧率)。样本不够时给 (None, None)。

        开了帧生成时，present 里混着驱动插出来的帧，必须把它们剔掉，否则"原生帧率"
        会等于显示帧率（实测 PBZ.exe：全部 present 87/s、显示 87/s，原生只有 44/s）。
        两种识别方式：
        1. v2 指标的 FrameType：插帧的 FrameType 不是 application，直接分开数；
        2. 只有 v1 指标时看 present 间隔：插帧紧跟在真帧后面 present，间隔极小
           （实测 0.5 ms 上下），而真帧之间是正常的一整帧时间（21 ms 上下）。
           把明显偏小的间隔算作插帧，剩下的就是应用自己渲染的帧。
        """
        entries = list(samples)
        if not entries:
            return None, None
        typed = [entry for entry in entries if len(entry) > 3 and entry[3]]
        has_types = any(entry[3] != "application" for entry in typed)
        total = 0.0
        count = 0
        app_count = 0
        intervals: list[float] = []
        for entry in reversed(entries):
            total += entry[1]
            count += 1
            intervals.append(entry[1])
            frame_type = entry[3] if len(entry) > 3 else ""
            if not frame_type or frame_type == "application":
                app_count += 1
            if total >= _RATE_WINDOW_MS:
                break
        if count < 3 or total <= 0.0:
            return None, None
        overall = 1000.0 * count / total
        if has_types:
            if app_count < 3 or app_count >= count:
                return overall, overall
            return 1000.0 * app_count / total, overall
        mean = total / count
        threshold = max(1.5, mean * 0.4)  # 绝对下限 1.5 ms：正常帧不可能这么密
        generated = sum(1 for interval in intervals if interval < threshold)
        if generated < 2 or generated >= count - 1:
            return overall, overall
        return 1000.0 * (count - generated) / total, overall

    def _candidates(self, now: float) -> list[tuple[int, int, str]]:
        """[(最近几秒的到达条数, pid, 进程名)]，按条数降序。

        只用来**挑目标进程**（谁还在出帧、谁最忙），帧率本身仍按帧间隔算。
        合成器（dwm 等）永不参选：它们一直在出帧，选中它们帧率就钉死在刷新率上。
        调用方必须已持有 self._lock。
        """
        self._prune(now)
        rows: list[tuple[int, int, str]] = []
        for pid, lane in self._presents.items():
            if not lane or now - lane[-1][0] > _STALE_S:
                continue
            name = lane[-1][2]
            if name.lower() in COMPOSITOR_PROCESSES:
                continue
            count = 0
            for entry in reversed(lane):
                if now - entry[0] > _PICK_WINDOW_S:
                    break
                count += 1
            rows.append((count, pid, name))
        rows.sort(reverse=True)
        self._last_candidates = rows[:4]
        return rows

    def _pick_target(self, now: float) -> tuple[int, str, str] | None:
        """选这次要报谁的帧率，返回 (pid, 进程名, 理由)。

        优先级：显式指定 → 抢到前台的全屏进程 → 上次数出来的目标（粘住）→ 前台 → 最忙。

        为什么要"粘住"：按"最忙/前台"每格重新选，玩家 alt-tab 出去看一眼聊天窗口，
        目标就会跳到壁纸引擎、浏览器这些一直在出帧的程序上，长条上的数字跟着乱跳。
        跟住一局之后只有两种情况换人：它自己停帧了，或者另一个**全屏**窗口抢到了前台
        （换了游戏）。这条规则只写 self._sticky_pid 这一个"跟谁"的记忆，
        曲线、最值这些历史状态仍然只在 sample() 里推进。
        """
        wanted = (self.target or "").strip().lower()
        candidates = self._candidates(now)
        alive = {pid: (count, name) for count, pid, name in candidates}
        if wanted:
            for count, pid, name in candidates:
                if name.lower() == wanted:
                    self._pick_reason = "指定"
                    self._sticky_pid = pid
                    return (pid, name, self._pick_reason) if count >= 3 else None
            self._pick_reason = "指定的进程没在出帧"
            return None
        hint = self._fg_hint
        # 1) 另一个全屏进程抢到前台：换了游戏，应该跟着换。
        if hint and hint != self._sticky_pid and hint in self._fullscreen:
            entry = alive.get(hint)
            if entry and entry[0] >= 3:
                self._sticky_pid = hint
                self._pick_reason = "前台全屏"
                return (hint, entry[1], self._pick_reason)
        # 2) 已经跟住的进程还在出帧：继续跟它，前台怎么切都不动摇。
        entry = alive.get(self._sticky_pid) if self._sticky_pid is not None else None
        if entry and entry[0] >= 3:
            self._pick_reason = "保持"
            return (self._sticky_pid, entry[1], self._pick_reason)
        # 3) 没有可保持的目标：前台 → 最忙。
        if hint is not None:
            entry = alive.get(hint)
            if entry and entry[0] >= 3:
                self._sticky_pid = hint
                self._pick_reason = "前台"
                return (hint, entry[1], self._pick_reason)
        for count, pid, name in candidates:
            if count >= 3:
                self._sticky_pid = pid
                self._pick_reason = "最忙"
                return (pid, name, self._pick_reason)
        self._pick_reason = "无数据"
        return None

    def _active_rate(
        self, now: float
    ) -> tuple[float | None, str, float | None, int | None, float | None]:
        """选出目标进程，返回 (应用帧率, 进程名, 帧时间, pid, 全部 present 帧率)。

        显式指定了进程时只认它：没在出帧就返回 None，界面显示 "—"，
        而不是悄悄回落到别的进程让人以为选错了也能用。
        应用帧率已经把插帧剔掉了（见 _rates）；第 5 个值是不剔插帧的原始帧率，
        用来在"显示帧率没数据"时兜底。
        """
        picked = self._pick_target(now)
        if picked is None:
            return None, "", None, None, None
        pid, name, _reason = picked
        lane = self._presents.get(pid)
        if not lane:
            return None, name, None, None, None
        fps, overall = self._rates(lane)
        if fps is None:
            return None, name, None, pid, overall
        return fps, name, lane[-1][1], pid, overall
