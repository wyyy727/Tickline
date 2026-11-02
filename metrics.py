"""单行叠加层的硬件采样。

CPU / 内存走 psutil；GPU 占用与显存走 Windows 自带性能计数器（`GPU Engine`、
`GPU Adapter Memory`）——不依赖任何厂商 SDK，所以 Intel 核显也能用，这也是 pacecar 的做法。
只有存在 NVIDIA 卡时才会额外加载 NVML，用来补一个温度读数。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from dataclasses import dataclass, field

import psutil

_pdh = ctypes.WinDLL("pdh", use_last_error=True)

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0

# 显式声明参数与返回类型：PDH 的返回值是 LONG，不声明就会被当成有符号 32 位整数，
# 导致 PDH_MORE_DATA 这类高位为 1 的状态码永远比较不相等。
_pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
_pdh.PdhOpenQueryW.restype = ctypes.c_long
_pdh.PdhAddEnglishCounterW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_void_p),
]
_pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
_pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
_pdh.PdhCollectQueryData.restype = ctypes.c_long
_pdh.PdhGetFormattedCounterArrayW.argtypes = [
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p,
]
_pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
_pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
_pdh.PdhCloseQuery.restype = ctypes.c_long


def _status(code: int) -> int:
    """把 PDH 的 NTSTATUS 风格返回值统一成无符号数再比较。"""
    return code & 0xFFFFFFFF


def _as_float(value) -> float | None:
    """JSON 里的数字统一转 float；None/非法值返回 None。"""
    return float(value) if isinstance(value, (int, float)) else None


class _FmtValue(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE：DWORD 状态 + 8 字节联合体（这里只取 double）。"""

    _fields_ = [("CStatus", wt.DWORD), ("_pad", wt.DWORD), ("doubleValue", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE_ITEM_W：实例名 + 格式化后的值。"""

    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _FmtValue)]


class PdhCounterArray:
    """一个带通配符的性能计数器，按 double 读取它的全部实例。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self.ok = False
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != ERROR_SUCCESS:
            return
        if (
            _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counter))
            != ERROR_SUCCESS
        ):
            return
        # 速率型计数器需要两次采样才有值，这里先采第一次把基线立起来。
        _pdh.PdhCollectQueryData(self._query)
        self.ok = True

    def collect(self) -> list[tuple[str, float]]:
        """采样一次，返回 [(实例名, 值)]；失败返回空列表（调用方按"不可用"处理）。"""
        if not self.ok or _status(_pdh.PdhCollectQueryData(self._query)) != ERROR_SUCCESS:
            return []
        size = wt.DWORD(0)
        count = wt.DWORD(0)
        # 第一次调用只为问出所需缓冲区大小，返回 PDH_MORE_DATA 属正常。
        status = _status(
            _pdh.PdhGetFormattedCounterArrayW(
                self._counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
            )
        )
        if status != PDH_MORE_DATA or size.value == 0:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        status = _status(
            _pdh.PdhGetFormattedCounterArrayW(
                self._counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buffer
            )
        )
        if status != ERROR_SUCCESS:
            return []
        items = ctypes.cast(buffer, ctypes.POINTER(_FmtItem))
        out: list[tuple[str, float]] = []
        for index in range(count.value):
            item = items[index]
            if item.FmtValue.CStatus == 0:  # PDH_CSTATUS_VALID_DATA
                out.append((item.szName or "", item.FmtValue.doubleValue))
        return out

    def close(self) -> None:
        if self._query:
            _pdh.PdhCloseQuery(self._query)
            self._query = ctypes.c_void_p()
        self.ok = False


def _vram_total_from_registry() -> int | None:
    """从显示适配器的注册表块里读显存总量。

    PDH 只暴露显存**用量**，没有容量计数器；而 `Win32_VideoController.AdapterRAM` 是 32 位值，
    4 GB 以上的卡会溢出。显卡类键下的 `qwMemorySize` 是 64 位，多数监控工具都读它。
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows 平台
        return None
    key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best = None
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
    except OSError:
        return None
    with root:
        # 适配器挂在 0000、0001… 子键下，取所有适配器里最大的那个容量。
        for index in range(8):
            try:
                sub = winreg.OpenKey(root, f"{index:04d}")
            except OSError:
                continue
            with sub:
                for name, size in (
                    ("HardwareInformation.qwMemorySize", 8),
                    ("HardwareInformation.MemorySize", 4),
                ):
                    try:
                        raw, _kind = winreg.QueryValueEx(sub, name)
                    except OSError:
                        continue
                    if isinstance(raw, bytes) and len(raw) >= size:
                        value = int.from_bytes(raw[:size], "little")
                        if value > 0 and (best is None or value > best):
                            best = value
                    elif isinstance(raw, int) and raw > 0:
                        best = max(best or 0, raw)
    return best


@dataclass
class SsdReading:
    """一块硬盘的读数。多块盘时列表里就有多项，界面按块各占一组。"""

    name: str = ""
    temp: float | None = None
    read: float | None = None      # 字节/秒
    write: float | None = None     # 字节/秒


@dataclass
class Snapshot:
    """一次采样得到的硬件状态。取不到的值保持 None，交给界面决定"跳过"而不是显示假 0。"""

    cpu_total: float | None = None
    cpu_core_peak: float | None = None   # 当前最忙单核的占用（瞬时，不是历史峰值）
    cpu_cores: list[float] = field(default_factory=list)
    cpu_freq: float | None = None      # 单位 MHz
    cpu_freq_max: float | None = None  # 单位 MHz
    cpu_temp: float | None = None      # 来自提权子进程（需 PawnIO）
    cpu_power: float | None = None     # CPU 封装功耗（瓦）
    mem_used: int | None = None
    mem_total: int | None = None
    mem_temp: float | None = None      # 只有 DDR5 内存条才有
    gpu_util: float | None = None
    vram_used: int | None = None
    vram_total: int | None = None
    gpu_temp: float | None = None
    gpu_power: float | None = None     # 显卡功耗（瓦）
    ssd: list[SsdReading] = field(default_factory=list)
    available: dict[str, bool] = field(default_factory=dict)


class _Nvml:
    """可选的 NVIDIA 温度源；非 NVIDIA 机器上是一个什么都不做的空实现。"""

    def __init__(self) -> None:
        self.ok = False
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.ok = True
        except Exception:  # noqa: BLE001 - 没有厂商 SDK 是常态，绝不能因此崩溃
            self.ok = False

    def temperature(self) -> float | None:
        if not self.ok:
            return None
        try:
            return float(self._nvml.nvmlDeviceGetTemperature(self._handle, 0))
        except Exception:  # noqa: BLE001
            return None

    def power(self) -> float | None:
        """显卡功耗（瓦）；NVML 返回的是毫瓦。"""
        if not self.ok:
            return None
        try:
            return float(self._nvml.nvmlDeviceGetPowerUsage(self._handle)) / 1000.0
        except Exception:  # noqa: BLE001
            return None


class Sampler:
    """后台采样器。界面只读 `latest()`，不直接碰计数器。

    `precise` 是可选的高精度传感器客户端（提权子进程）。不给它时温度就只剩 NVML 一条路，
    也就是 N 卡才有 GPU 温度、CPU 温度完全没有。
    """

    def __init__(self, refresh_ms: int = 250, precise=None) -> None:
        self._interval = max(0.1, refresh_ms / 1000.0)
        self._snapshot = Snapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu_peak = 0.0
        self._core_peak = 0.0
        self._nvml = _Nvml()
        self._precise = precise
        self._gpu = PdhCounterArray(r"\GPU Engine(*)\Utilization Percentage")
        # 显存先看独占（真独显），再退回共享（核显）。核显的工作集全在系统内存里，
        # Dedicated Usage 恒为 0，只用它会导致显存永远是 0。
        self._vram_dedicated = PdhCounterArray(r"\GPU Adapter Memory(*)\Dedicated Usage")
        self._vram_shared = PdhCounterArray(r"\GPU Adapter Memory(*)\Shared Usage")
        self._vram_total = _vram_total_from_registry()
        self._freq_max = None
        freq = psutil.cpu_freq()
        if freq and freq.max:
            self._freq_max = float(freq.max)

    def start(self) -> None:
        # psutil 的 cpu_percent 首次调用只建立基线（返回 0），先空跑两次避免第一帧是 0%。
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        self._thread = threading.Thread(target=self._run, name="pacelite-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._gpu.close()
        self._vram_dedicated.close()
        self._vram_shared.close()

    def set_precise(self, precise) -> None:
        """开/关精确传感器；采样线程下一轮就会用上（或停用）它。"""
        self._precise = precise

    def latest(self) -> Snapshot:
        # 发布即一次引用交换：采样线程每轮构造全新且之后不再改动的快照，读方直接取指针即可，
        # 不需要加锁。调用方必须把返回值当作只读对象。
        return self._snapshot

    # ---- 采样 ------------------------------------------------------------
    def _sample(self) -> Snapshot:
        snap = Snapshot()
        snap.cpu_total = float(psutil.cpu_percent(interval=None))
        cores = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        snap.cpu_cores = cores
        # "最忙单核"取当前这一瞬间的最大值。累计历史峰值对玩家没意义（开局一个尖峰会永远
        # 挂在上面），所以不做累计。
        snap.cpu_core_peak = max(cores) if cores else None

        freq = psutil.cpu_freq()
        if freq and freq.current:
            snap.cpu_freq = float(freq.current)
            if freq.max:
                self._freq_max = float(freq.max)
        snap.cpu_freq_max = self._freq_max

        mem = psutil.virtual_memory()
        snap.mem_used = int(mem.total - mem.available)
        snap.mem_total = int(mem.total)

        # GPU 占用按 3D 引擎求和后截断；这是任务管理器的口径，够直观且跨厂商可用。
        engines = self._gpu.collect()
        if engines:
            total = sum(value for name, value in engines if "engtype_3D" in name)
            snap.gpu_util = max(0.0, min(100.0, total))

        dedicated = self._vram_dedicated.collect()
        shared = self._vram_shared.collect()
        dedicated_bytes = int(sum(v for _n, v in dedicated if v > 0)) if dedicated else 0
        shared_bytes = int(sum(v for _n, v in shared if v > 0)) if shared else 0
        if dedicated_bytes > 0:
            snap.vram_used = dedicated_bytes
            snap.vram_total = self._vram_total
        elif shared_bytes > 0:
            snap.vram_used = shared_bytes
            snap.vram_total = self._vram_total
        else:
            snap.vram_used = None
        # 核显报的是共享用量，而注册表里的容量只是那一小片独占显存，两者不可比。
        # 与其显示"1.1 / 0.1 GB"这种鬼话，不如把分母丢掉。
        if snap.vram_total and snap.vram_used and snap.vram_used > snap.vram_total:
            snap.vram_total = None

        snap.gpu_temp = self._nvml.temperature()
        snap.gpu_power = self._nvml.power()
        # 精确传感器（提权子进程）补上 NVML 拿不到的：CPU 温度、内存温度，
        # 以及非 NVIDIA 显卡的 GPU 温度。NVML 优先，因为它不需要额外提权。
        if self._precise is not None:
            try:
                reading = self._precise.snapshot()
            except Exception:  # noqa: BLE001 - 子进程出问题只影响温度
                reading = None
            if reading:
                if snap.cpu_temp is None and isinstance(reading.get("cpu_temp"), (int, float)):
                    snap.cpu_temp = float(reading["cpu_temp"])
                if snap.gpu_temp is None and isinstance(reading.get("gpu_temp"), (int, float)):
                    snap.gpu_temp = float(reading["gpu_temp"])
                if isinstance(reading.get("mem_temp"), (int, float)):
                    snap.mem_temp = float(reading["mem_temp"])
                if isinstance(reading.get("cpu_power"), (int, float)):
                    snap.cpu_power = float(reading["cpu_power"])
                if isinstance(reading.get("gpu_power"), (int, float)):
                    snap.gpu_power = float(reading["gpu_power"])
                if isinstance(reading.get("ssd"), list):
                    disks: list[SsdReading] = []
                    for item in reading["ssd"]:
                        if not isinstance(item, dict):
                            continue
                        disks.append(
                            SsdReading(
                                name=str(item.get("name") or ""),
                                temp=_as_float(item.get("temp")),
                                read=_as_float(item.get("read")),
                                write=_as_float(item.get("write")),
                            )
                        )
                    snap.ssd = disks

        snap.available = {
            "gpu_util": snap.gpu_util is not None,
            "vram": snap.vram_used is not None,
            "gpu_temp": snap.gpu_temp is not None,
            "cpu_temp": snap.cpu_temp is not None,
            "cpu_freq": snap.cpu_freq is not None,
            "mem_temp": snap.mem_temp is not None,
            "ssd": any(
                disk.temp is not None or (disk.read or 0) > 0 or (disk.write or 0) > 0
                for disk in snap.ssd
            ),
        }
        return snap

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._sample()
            except Exception:  # noqa: BLE001 - 单次读取失败不能让叠加层挂掉
                snap = Snapshot()
            self._snapshot = snap
            time.sleep(self._interval)
