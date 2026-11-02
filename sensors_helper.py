"""提权传感器子进程：加载 LibreHardwareMonitor，把温度写成一个 JSON 文件。

为什么要单独一个进程：读 CPU 温度要访问 MSR，必须走 PawnIO 内核驱动，而加载驱动和
读 MSR 都需要管理员权限。主程序保持 asInvoker（普通权限），只在这个子进程里提权，
既满足叠加层不该要管理员权限的安全考量，也能让 .NET 加载失败时不牵连主程序。

协议很简单：子进程每 0.8 秒把结果覆盖写入 sensors.json，主进程按需读文件。
文件比管道好调试，而且子进程崩了主进程也只是读到旧数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

POLL_SECONDS = 0.8
# 父进程消失后多久自动退出：避免主程序被强杀后留下一个常驻的提权进程。
ORPHAN_GRACE_SECONDS = 10.0


def default_paths() -> tuple[Path, Path]:
    """返回 (LibreHardwareMonitor.dll 所在目录, 输出 JSON 路径)。"""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Tickline"
    candidates: list[Path] = []
    # 单文件打包时资源在 _MEIPASS 里；目录版在 exe 旁边；源码运行在工程的 tools/ 下。
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "libs" / "lhm")
    candidates += [
        Path(sys.executable).resolve().parent / "libs" / "lhm",
        Path(sys.executable).resolve().parent / "tools" / "lhm",
        Path(__file__).resolve().parent.parent / "tools" / "lhm",
    ]
    for candidate in candidates:
        if (candidate / "LibreHardwareMonitorLib.dll").is_file():
            return candidate, base / "sensors.json"
    return candidates[0], base / "sensors.json"


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    kernel32 = __import__("ctypes").WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


class _Reader:
    """把 LHM 的硬件树摊平成"我们要的几个数"。"""

    def __init__(self, dll_dir: Path) -> None:
        try:
            import clr  # pythonnet
        except ImportError as exc:
            # 这是"没装 pythonnet"最常见的样子，给一句能直接照做的原因，
            # 而不是让用户对着 "No module named 'clr'" 猜。
            raise RuntimeError(
                "缺少 pythonnet，无法加载 LibreHardwareMonitor：请执行 pip install pythonnet"
            ) from exc

        clr.AddReference(str(dll_dir / "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor.Hardware import Computer, SensorType

        self._sensor_type = SensorType
        self._computer = Computer()
        self._computer.IsCpuEnabled = True
        self._computer.IsGpuEnabled = True
        self._computer.IsMemoryEnabled = True
        self._computer.IsMotherboardEnabled = True
        # 硬盘传感器（温度、寿命）只在「显示 SSD」打开时才值得读，
        # 但 LHM 的硬件列表是开一次就定死的，所以这里统一开启，取值时按需使用。
        self._computer.IsStorageEnabled = True
        self._computer.Open()

    # CPU 温度的候选名字，按优先级排：AMD 的 Tctl 才是"CPU 温度"的正主，
    # Intel 才是 Package；Tdie / Tccd 是各家主板和 HWiNFO 常见的叫法。
    _CPU_TEMP_PRIORITY = ("Tctl", "Package", "Tdie", "Tccd", "Core Max")
    # 单核温度：LHM 在 AMD 上叫 "Core #1"，Intel 上叫 "CPU Core #1"。
    _CORE_TEMP_KEYS = ("CPU Core #", "Core #", "CCD")

    def _walk(self, hardware, depth: int = 0):
        """深度优先遍历硬件树，连子硬件（CPU 各核、主板 SuperIO）一起读。

        LHM 把不少传感器挂在 SubHardware 下面，只看顶层 Hardware 会漏掉一半读数。
        """
        if depth > 3:
            return
        try:
            hardware.Update()
        except Exception:  # noqa: BLE001 - 单个硬件读失败不该拖垮整棵树
            pass
        yield hardware
        for sub in hardware.SubHardware:
            yield from self._walk(sub, depth + 1)

    def _walk_many(self):
        for hardware in self._computer.Hardware:
            yield from self._walk(hardware)

    def _pick_cpu_temp(self, temps: list[tuple[str, float]]) -> tuple[float | None, float | None]:
        """从 CPU 温度列表里挑出 (CPU 温度, 单核最高温)。

        AMD 只有 "Core (Tctl/Tdie)"，Intel 是 "CPU Package" + "Core Max"，
        所以不能只认 "Package"——那样 AMD 机器上永远读不到温度。
        0.0 一律当作"没读到"：非管理员读 SMU 时 LHM 会静默返回 0，画出来就是 0 ℃。
        """
        usable = [(name, value) for name, value in temps if value > 0.0]
        chosen = None
        for key in self._CPU_TEMP_PRIORITY:
            for name, value in usable:
                if key in name:
                    chosen = value
                    break
            if chosen is not None:
                break
        core_values = [
            value
            for name, value in usable
            if any(key in name for key in self._CORE_TEMP_KEYS)
        ]
        core_max = max(core_values) if core_values else None
        if chosen is None and usable:
            # 名字不认识的机器：只要有一个像样的 CPU 温度就用它，总比完全不显示好。
            chosen = max(value for _, value in usable)
        return chosen, core_max

    def read(self) -> dict:
        cpu_temps: list[tuple[str, float]] = []
        cpu_power = None
        gpu_temp = None
        gpu_power = None
        mem_temp = None
        fan = None
        disks: list[dict] = []
        for hardware in self._walk_many():
            label = str(hardware.HardwareType)
            is_cpu = "Cpu" in label
            is_gpu = "Gpu" in label
            is_memory = "Memory" in label
            is_storage = "Storage" in label
            disk = {"temp": None, "read": None, "write": None} if is_storage else None
            for sensor in hardware.Sensors:
                if sensor.Value is None:
                    continue
                kind = sensor.SensorType
                name = sensor.Name
                value = float(sensor.Value)
                if kind == self._sensor_type.Temperature:
                    if is_cpu:
                        cpu_temps.append((name, value))
                    elif is_gpu:
                        # 一块卡有好几个温度点（Core / Hot Spot / Memory Junction），
                        # 界面要的是核心温度。
                        if gpu_temp is None or "GPU Core" in name:
                            gpu_temp = value
                    elif is_memory and mem_temp is None:
                        # DDR5 内存条自带温度传感器（SPD Hub），DDR4 一般没有，
                        # 所以这里拿不到就是没有，界面自然不显示。
                        mem_temp = value if value > 0.0 else None
                    elif is_storage and disk is not None and disk["temp"] is None:
                        # 一块盘有多个温度点（Composite / #1 / #3 / 告警阈值），取第一个
                        # Composite 是最有代表性的整体温度。
                        if "Composite" in name or "Warning" not in name:
                            disk["temp"] = value
                elif kind == self._sensor_type.Power:
                    # CPU 只取封装功耗（各核子项加起来会重复计数）；GPU 取第一个功耗项。
                    if is_cpu and "Package" in name and cpu_power is None:
                        cpu_power = value if value > 0.0 else None
                    elif is_gpu and gpu_power is None:
                        gpu_power = value if value > 0.0 else None
                elif kind == self._sensor_type.Throughput and disk is not None:
                    # 读写速率（字节/秒）。LHM 用它自己维护的间隔算差值，
                    # 所以子进程第一次 Update 拿不到值，从第二轮开始才有。
                    if "Read" in name and disk["read"] is None:
                        disk["read"] = value
                    elif "Write" in name and disk["write"] is None:
                        disk["write"] = value
                elif kind == self._sensor_type.Fan and fan is None:
                    fan = value
            if disk is not None:
                # LHM 在部分机器上拿不到硬盘型号（Name 是空白），留空由界面补 SSD1/SSD2。
                disk["name"] = (hardware.Name or "").strip()
                disks.append(disk)
        cpu_temp, cpu_core_max = self._pick_cpu_temp(cpu_temps)
        payload = {
            "cpu_temp": cpu_temp,
            "cpu_core_max": cpu_core_max,
            "cpu_power": cpu_power,
            "cpu_cores": [],
            "gpu_temp": gpu_temp,
            "gpu_power": gpu_power,
            "mem_temp": mem_temp,
            "ssd": disks,
            "fan_rpm": fan,
            # 调试用：把 CPU 温度传感器的原始名字和读数一起写出去。万一某台机器
            # 又读不到温度，看这个就知道 LHM 到底给了什么名字、是不是非管理员读到 0。
            "cpu_temps": {name: value for name, value in cpu_temps},
        }
        if cpu_temp is None and cpu_temps:
            payload["warn"] = "CPU 温度读数为空：" + ", ".join(
                "%s=%.1f" % (name, value) for name, value in cpu_temps
            )
        return payload

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:  # noqa: BLE001
            pass


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def ensure_pawnio(output_path: Path) -> str | None:
    """确保 PawnIO 驱动就绪；返回错误文案（成功返回 None）。

    这个进程本身已经提过权，所以可以直接静默安装驱动 —— 用户只在启动传感器子进程时
    看到一次 UAC，不会再为装驱动弹第二次。
    """
    from sensors import pawnio_setup_path, pawnio_state

    if pawnio_state() == "running":
        return None
    setup = pawnio_setup_path()
    if not setup.is_file():
        return f"缺少驱动安装包：{setup}"
    try:
        subprocess.run(
            [str(setup), "-install", "-silent"],
            check=False,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"驱动安装失败：{exc}"
    # 服务起来要一点时间
    for _ in range(20):
        if pawnio_state() == "running":
            return None
        time.sleep(0.5)
    return "驱动已安装但服务未就绪"


def run(parent_pid: int, output_path: Path) -> int:
    dll_dir, fallback_path = default_paths()
    output_path = output_path or fallback_path

    driver_error = ensure_pawnio(output_path)
    if driver_error:
        _write_atomic(
            output_path,
            {"ok": False, "error": driver_error, "ts": time.time(), "pid": os.getpid()},
        )
        return 2

    try:
        reader = _Reader(dll_dir)
    except Exception as exc:  # noqa: BLE001 - 加载失败要把原因写出去，而不是静默退出
        _write_atomic(output_path, {"ok": False, "error": f"加载 LHM 失败: {exc}"})
        return 1

    last_parent_check = 0.0
    try:
        while True:
            try:
                reading = reader.read()
                payload = {"ok": True, "ts": time.time(), "pid": os.getpid(), **reading}
            except Exception as exc:  # noqa: BLE001
                payload = {
                    "ok": False,
                    "error": f"读取失败: {exc}",
                    "ts": time.time(),
                    "pid": os.getpid(),
                }
            _write_atomic(output_path, payload)

            now = time.time()
            if now - last_parent_check > 2.0:
                last_parent_check = now
                if not _parent_alive(parent_pid):
                    return 0
            time.sleep(POLL_SECONDS)
    finally:
        reader.close()


def main(argv: list[str]) -> int:
    parent_pid = int(argv[0]) if argv else 0
    output = Path(argv[1]) if len(argv) > 1 else default_paths()[1]
    return run(parent_pid, output)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
