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
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Pacelite"
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
        import clr  # pythonnet

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

    def read(self) -> dict:
        cpu_package = None
        cpu_core_max = None
        cpu_power = None
        gpu_power = None
        cores: list[float] = []
        gpu_temp = None
        mem_temp = None
        fan = None
        disks: list[dict] = []
        for hardware in self._computer.Hardware:
            hardware.Update()
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
                    if "Package" in name and is_cpu and cpu_package is None:
                        cpu_package = value
                    elif "Core Max" in name:
                        cpu_core_max = value
                    elif is_cpu and (name.startswith("CPU Core #") or name.startswith("Core #")):
                        cores.append(value)
                    elif is_gpu:
                        gpu_temp = value
                    elif is_memory and mem_temp is None:
                        # DDR5 内存条自带温度传感器（SPD Hub），DDR4 一般没有，
                        # 所以这里拿不到就是没有，界面自然不显示。
                        mem_temp = value
                    elif is_storage and disk is not None and disk["temp"] is None:
                        # 一块盘有多个温度点（Composite / #1 / #3 / 告警阈值），取第一个
                        # Composite 是最有代表性的整体温度。
                        if "Composite" in name or "Warning" not in name:
                            disk["temp"] = value
                elif kind == self._sensor_type.Power:
                    # CPU 只取封装功耗（各核子项加起来会重复计数）；GPU 取第一个功耗项。
                    if is_cpu and "Package" in name and cpu_power is None:
                        cpu_power = value
                    elif is_gpu and gpu_power is None:
                        gpu_power = value
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
        return {
            "cpu_temp": cpu_package if cpu_package is not None else cpu_core_max,
            "cpu_core_max": cpu_core_max,
            "cpu_power": cpu_power,
            "cpu_cores": cores,
            "gpu_temp": gpu_temp,
            "gpu_power": gpu_power,
            "mem_temp": mem_temp,
            "ssd": disks,
            "fan_rpm": fan,
        }

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
