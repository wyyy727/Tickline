"""精确传感器客户端：PawnIO 驱动检测、提权子进程的启动与结果读取。

分工：
- 驱动（PawnIO）负责让普通程序能读到 MSR，安装一次即可，需要管理员；
- 子进程（sensors_helper.py）在提权环境里加载 LHM 并写 JSON；
- 主程序只读 JSON，永远保持普通权限。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_RUNNING = 0x00000004
SW_HIDE = 0

FRESH_SECONDS = 3.0     # JSON 超过这个时间没更新就当作子进程已经不在了
START_TIMEOUT = 12.0    # 等子进程写出第一份数据的上限


class _ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wt.DWORD),
        ("dwCurrentState", wt.DWORD),
        ("dwControlsAccepted", wt.DWORD),
        ("dwWin32ExitCode", wt.DWORD),
        ("dwServiceSpecificExitCode", wt.DWORD),
        ("dwCheckPoint", wt.DWORD),
        ("dwWaitTime", wt.DWORD),
    ]


# 句柄必须声明成 c_void_p：默认返回类型是 c_int，64 位下会把句柄截断，
# 后面 OpenServiceW 直接失败，看起来就像"服务不存在"。
advapi32.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
advapi32.OpenSCManagerW.restype = wt.HANDLE
advapi32.OpenServiceW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.DWORD]
advapi32.OpenServiceW.restype = wt.HANDLE
advapi32.QueryServiceStatus.argtypes = [wt.HANDLE, ctypes.POINTER(_ServiceStatus)]
advapi32.QueryServiceStatus.restype = wt.BOOL
advapi32.CloseServiceHandle.argtypes = [wt.HANDLE]
advapi32.CloseServiceHandle.restype = wt.BOOL
shell32.ShellExecuteW.argtypes = [
    wt.HWND,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.LPCWSTR,
    ctypes.c_int,
]
shell32.ShellExecuteW.restype = wt.HINSTANCE


def pawnio_state() -> str:
    """返回 "running" / "stopped" / "missing"：驱动是否可用于读 MSR。"""
    scm = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        return "missing"
    try:
        service = advapi32.OpenServiceW(wt.HANDLE(scm), "PawnIO", SERVICE_QUERY_STATUS)
        if not service:
            return "missing"
        try:
            status = _ServiceStatus()
            if not advapi32.QueryServiceStatus(wt.HANDLE(service), ctypes.byref(status)):
                return "stopped"
            return "running" if status.dwCurrentState == SERVICE_RUNNING else "stopped"
        finally:
            advapi32.CloseServiceHandle(wt.HANDLE(service))
    finally:
        advapi32.CloseServiceHandle(wt.HANDLE(scm))


def _run_elevated(executable: str, parameters: str) -> int:
    """以管理员身份启动，会弹 UAC。返回 ShellExecuteW 的值（>32 表示成功）。"""
    return int(
        shell32.ShellExecuteW(None, "runas", executable, parameters, None, SW_HIDE)
    )


def install_pawnio(setup: Path) -> tuple[bool, str]:
    """静默安装 PawnIO 驱动；返回 (是否成功, 说明)。"""
    if not setup.is_file():
        return False, f"找不到驱动安装包：{setup}"
    code = _run_elevated(str(setup), "-install -silent")
    if code <= 32:
        # 常见是 5 (SE_ERR_ACCESSDENIED)：用户点了"否"。
        return False, "安装被取消或没有管理员权限"
    # 安装器是同步等待型，但服务起来还需要一点时间。
    for _ in range(20):
        if pawnio_state() == "running":
            return True, "驱动已就绪"
        time.sleep(0.5)
    return False, "驱动安装完成但服务未就绪"


def _default_json() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Tickline"
    return base / "sensors.json"


def pawnio_setup_path() -> Path:
    """驱动安装包的位置。

    单文件打包时资源会被解到 `sys._MEIPASS`，目录版在 exe 旁边，源码运行在 tools/ 下，
    三种形态都要能找得到。
    """
    candidates: list[Path] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "libs" / "pawnio" / "PawnIO_setup.exe")
    candidates += [
        Path(sys.executable).resolve().parent / "libs" / "pawnio" / "PawnIO_setup.exe",
        Path(__file__).resolve().parent.parent / "tools" / "pawnio" / "PawnIO_setup.exe",
        Path(sys.executable).resolve().parent / "tools" / "pawnio" / "PawnIO_setup.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


# pnputil 的字段名随系统语言变化（"发布名称"/"Published Name"/…），
# 但 oem12.inf 这种文件名本身是语言无关的，直接按它找最稳。
_INF_RE = re.compile(r"\boem\d+\.inf\b", re.IGNORECASE)


def delete_pawnio_package() -> bool:
    """把驱动包从 DriverStore 里删掉（官方卸载器不一定做这一步）。"""
    try:
        listing = subprocess.run(
            ["pnputil", "/enum-drivers"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # 每条驱动是一个空行分隔的块，块里出现 "pawnio" 的就是驱动包；再从块里挑 oemN.inf。
    published = None
    for block in re.split(r"\r?\n\s*\r?\n", listing):
        if "pawnio" not in block.lower():
            continue
        match = _INF_RE.search(block)
        if match:
            published = match.group(0)
            break
    if not published:
        return False
    try:
        subprocess.run(
            ["pnputil", "/delete-driver", published, "/uninstall"],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def uninstall_pawnio(setup: Path | None = None) -> tuple[bool, str]:
    """卸载 PawnIO 驱动，给用户一个"能反悔"的出口。

    官方卸载器会停止并移除服务；但驱动是否立刻从 DriverStore 消失取决于系统，
    部分机器要重启才算彻底干净，所以这里只把"命令已执行"和"已彻底移除"分开报告。
    """
    target = setup or pawnio_setup_path()
    if not target.is_file():
        return False, f"找不到驱动安装包：{target}"
    code = _run_elevated(str(target), "-uninstall -silent")
    if code <= 32:
        return False, "卸载被取消或没有管理员权限"
    for _ in range(20):
        if pawnio_state() != "running":
            return True, "驱动已卸载"
        time.sleep(0.5)
    delete_pawnio_package()
    # 服务仍在跑通常是因为内核还引用着驱动，重启后才会真正消失。
    return True, "已执行卸载，重启后彻底移除"


class SensorsClient:
    """管理提权子进程，并按需给出最新读数。"""

    def __init__(self, json_path: Path | None = None) -> None:
        self.json_path = json_path or _default_json()
        self.setup_path = pawnio_setup_path()
        self._started_at = 0.0
        self.last_error: str | None = None
        # 子进程报"某项读不到"时的日志节流时间戳（见 snapshot()）
        self._warn_logged_at = 0.0

    # ---- 读取 ------------------------------------------------------------
    def snapshot(self) -> dict | None:
        """返回最新读数；文件不存在、损坏或过期都返回 None（界面按不可用处理）。"""
        payload = self._load()
        if payload is None:
            return None
        if not payload.get("ok"):
            self.last_error = payload.get("error")
            return None
        stamp = float(payload.get("ts") or 0.0)
        if stamp and time.time() - stamp > FRESH_SECONDS:
            return None
        # 子进程偶尔会带一句"某项读数空"的说明（例如 CPU 温度），
        # 界面上只是不显示那个数字，原因得留在日志里，不然没法排障。
        warn = payload.get("warn")
        if isinstance(warn, str) and warn:
            now = time.time()
            if now - self._warn_logged_at > 60.0:
                self._warn_logged_at = now
                try:
                    import pylog

                    pylog.log(f"[sensors] {warn}")
                except Exception:  # noqa: BLE001 - 日志失败不能影响读数
                    pass
        return payload

    def _load(self) -> dict | None:
        try:
            raw = self.json_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _helper_alive(self) -> bool:
        """用 JSON 里记的 pid 判断子进程是否还活着。

        只看文件时间戳不够：上一次运行留下的 JSON 可能"还没过期"，但写它的进程早就没了，
        这时若跳过启动就会一直没有温度。
        """
        payload = self._load()
        if not payload:
            return False
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        import ctypes.wintypes as wt_local

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        kernel32.CloseHandle(wt_local.HANDLE(handle))
        return True

    # ---- 生命周期 --------------------------------------------------------
    def _helper_command(self) -> tuple[str, str]:
        """源码运行走 pythonw + 脚本，打包后走 exe 自己的 --sensors-helper 分支。"""
        if getattr(sys, "frozen", False):
            return sys.executable, f'--sensors-helper {os.getpid()} "{self.json_path}"'
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        interpreter = pythonw if pythonw.is_file() else python
        script = Path(__file__).resolve().parent / "sensors_helper.py"
        return str(interpreter), f'"{script}" {os.getpid()} "{self.json_path}"'

    def start(self) -> str | None:
        """确保子进程在跑；返回给用户看的错误，成功返回 None。

        驱动不在这里装：子进程已经提权，由它负责静默安装，这样用户全程只看到一次 UAC。
        """
        if self._helper_alive() and self.snapshot() is not None:
            return None
        # 清掉上一次留下的旧文件，避免刚启动就读到过期数据。
        try:
            self.json_path.unlink(missing_ok=True)
        except OSError:
            pass
        executable, parameters = self._helper_command()
        code = _run_elevated(executable, parameters)
        if code <= 32:
            return "已取消管理员授权，温度不可用"
        self._started_at = time.time()
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.snapshot() is not None:
                self.last_error = None
                return None
            time.sleep(0.3)
        return self.last_error or "传感器子进程启动后没有输出数据"

    def stop(self) -> None:
        """删掉结果文件，子进程会在父进程消失后自行退出。"""
        try:
            self.json_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 便捷封装 --------------------------------------------------------
    def cpu_temp(self) -> float | None:
        snap = self.snapshot()
        if not snap:
            return None
        value = snap.get("cpu_temp")
        return float(value) if isinstance(value, (int, float)) else None

    def gpu_temp(self) -> float | None:
        snap = self.snapshot()
        if not snap:
            return None
        value = snap.get("gpu_temp")
        return float(value) if isinstance(value, (int, float)) else None


def helper_main(argv: list[str]) -> int:
    """`--sensors-helper` 的入口，供打包后的 exe 复用同一份代码。"""
    if argv and argv[0] == "--sensors-helper":
        argv = argv[1:]
    from sensors_helper import main as helper_run

    return helper_run(argv)
