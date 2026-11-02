"""下载构建所需的第三方组件。

`build.py` 发现依赖缺失时会自动调用它，也可以手动跑：

    python fetch_deps.py           # 缺什么下什么
    python fetch_deps.py --force   # 全部重新下载

三个组件都固定到具体版本，保证任何人任何时候打出来的包一致；升级版本时改这里的常量即可。
下载支持断点续传：网络中断后会从已下载的字节继续，而不是从头再来。
"""

from __future__ import annotations

import argparse
import http.client
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT.parent / "tools"

PRESENTMON_VERSION = "2.6.0"
LHM_VERSION = "0.9.6"

PRESENTMON_URL = (
    "https://github.com/GameTechDev/PresentMon/releases/download/"
    f"v{PRESENTMON_VERSION}/PresentMon-{PRESENTMON_VERSION}-x64.exe"
)
LHM_URL = (
    "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/"
    f"v{LHM_VERSION}/LibreHardwareMonitor.zip"
)
PAWNIO_URL = (
    "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/raw/master/"
    "LibreHardwareMonitor.Windows.Forms/Resources/PawnIO_setup.exe"
)

PRESENTMON_DEST = TOOLS / "PresentMon" / "PresentMon.exe"
LHM_DIR = TOOLS / "lhm"
PAWNIO_DEST = TOOLS / "pawnio" / "PawnIO_setup.exe"

# 只解这些：LHM 的 exe、绘图库 OxyPlot、WinForms 控件、语言资源包都用不上。
LHM_SKIP_PREFIX = ("OxyPlot", "Aga.Controls", "LibreHardwareMonitor.exe")

USER_AGENT = "Tickline-FetchDeps/1.0"

# 运行时需要的 pip 包（模块名, 发行版名）。缺了不会崩，但对应功能会静默消失：
# 没 PySide6 起不来，没 psutil 采不了硬件，没 pynvml 没有 N 卡温度，没 pythonnet 没精确传感器。
REQUIRED_PYTHON = (
    ("PySide6", "PySide6-Essentials"),
    ("psutil", "psutil"),
    ("pynvml", "nvidia-ml-py"),
    ("clr", "pythonnet"),
)


def missing_python_packages() -> list[str]:
    """返回缺失的 pip 发行版名（可直接喂给 pip install）。"""
    import importlib.util

    missing: list[str] = []
    for module, package in REQUIRED_PYTHON:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(package)
    return missing


def ensure_python_packages(install: bool = False) -> bool:
    """检查（可选：安装）运行时的 pip 包；返回是否齐全。"""
    missing = missing_python_packages()
    if not missing:
        return True
    print(f"[提示] 缺少 Python 依赖：{', '.join(missing)}")
    if not install:
        print("       安装：pip install -r requirements.txt")
        print("       （或加 --install-python 让本脚本代劳）")
        return False
    print(f"[安装] {sys.executable} -m pip install " + " ".join(missing))
    result = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
    return result.returncode == 0 and not missing_python_packages()


def _ssl_context() -> ssl.SSLContext:
    """默认严格校验证书；只有显式设置 TICKLINE_INSECURE_TLS=1 时才跳过。

    留这个开关是为了在证书链异常的开发环境里能跑通，正常使用绝不该打开。
    旧名 PACELITE_INSECURE_TLS 仍然认，免得已经把变量写进脚本/环境的人突然失效。
    """
    for name in ("TICKLINE_INSECURE_TLS", "PACELITE_INSECURE_TLS"):
        if os.environ.get(name) == "1":
            return ssl._create_unverified_context()
    return ssl.create_default_context()


def download(url: str, dest: Path, attempts: int = 8) -> Path:
    """下载到 dest，失败自动续传重试。

    GitHub 的 release 会 302 到 CDN，中途断流是常事，所以这里做的不是"重下载"而是
    "接着下"：先看 .part 已有多少字节，用 Range 请求续上。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last_error = ""
    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response:
                # 服务器不认 Range 就只能从头写，否则会拼出坏文件。
                if have and response.status != 206:
                    have = 0
                    part.unlink(missing_ok=True)
                total = have + int(response.headers.get("Content-Length") or 0)
                mode = "ab" if have else "wb"
                with open(part, mode) as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            last_error = str(exc)
            print(f"  第 {attempt} 次中断：{last_error}", flush=True)
            continue

        size = part.stat().st_size
        if total and size < total:
            print(f"  已下载 {size}/{total} 字节，继续续传", flush=True)
            continue
        if size == 0:
            last_error = "下载到 0 字节"
            continue
        part.replace(dest)
        return dest
    raise RuntimeError(f"下载失败：{url}（最后一次错误：{last_error}）")


def fetch_presentmon(force: bool = False) -> bool:
    if PRESENTMON_DEST.is_file() and not force:
        print(f"[跳过] PresentMon 已存在：{PRESENTMON_DEST}")
        return False
    print(f"[下载] PresentMon {PRESENTMON_VERSION}")
    download(PRESENTMON_URL, PRESENTMON_DEST)
    print(f"[完成] {PRESENTMON_DEST}  {PRESENTMON_DEST.stat().st_size / 1024:.0f} KB")
    return True


def fetch_lhm(force: bool = False) -> bool:
    marker = LHM_DIR / "LibreHardwareMonitorLib.dll"
    if marker.is_file() and not force:
        print(f"[跳过] LHM 已存在：{LHM_DIR}")
        return False
    print(f"[下载] LibreHardwareMonitor {LHM_VERSION}（约 6.3 MB）")
    archive = TOOLS / "lhm" / f"LibreHardwareMonitor-{LHM_VERSION}.zip"
    download(LHM_URL, archive)
    LHM_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename
            if "/" in name or not name.lower().endswith(".dll"):
                continue
            if name.startswith(LHM_SKIP_PREFIX):
                continue
            (LHM_DIR / name).write_bytes(bundle.read(info))
            count += 1
    archive.unlink(missing_ok=True)
    print(f"[完成] 解出 {count} 个 dll 到 {LHM_DIR}")
    return True


def fetch_pawnio(force: bool = False) -> bool:
    if PAWNIO_DEST.is_file() and not force:
        print(f"[跳过] PawnIO 安装包已存在：{PAWNIO_DEST}")
        return False
    print("[下载] PawnIO 安装包（约 3.1 MB，用于读 CPU 温度）")
    download(PAWNIO_URL, PAWNIO_DEST)
    print(f"[完成] {PAWNIO_DEST}  {PAWNIO_DEST.stat().st_size / 1024:.0f} KB")
    return True


def ensure_dependencies(force: bool = False, with_pawnio: bool = True) -> bool:
    """补齐所有依赖；返回是否有文件被下载（供 build.py 打日志）。"""
    # pip 包下载**下载不下来**，这里只做检查和提示，绝不自动安装（用户可能用的是别的解释器）。
    ensure_python_packages(install=False)
    changed = False
    changed |= fetch_presentmon(force)
    changed |= fetch_lhm(force)
    if with_pawnio:
        changed |= fetch_pawnio(force)
    return changed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="下载 Tickline 构建所需的第三方组件")
    parser.add_argument("--force", action="store_true", help="忽略已有文件，全部重新下载")
    parser.add_argument("--no-pawnio", action="store_true", help="跳过 PawnIO（不需要 CPU 温度时）")
    parser.add_argument(
        "--install-python", action="store_true", help="顺便用 pip 安装 requirements.txt 里的包"
    )
    args = parser.parse_args(argv)
    ensure_python_packages(install=args.install_python)
    try:
        ensure_dependencies(force=args.force, with_pawnio=not args.no_pawnio)
    except (RuntimeError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    print("\n依赖就绪，可以运行 build.py 或 main.py 了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
