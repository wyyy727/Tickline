r"""打包脚本：用 PyInstaller 生成可分发的程序。

两种形态：

  目录版（默认，推荐）
    python build.py
    产物：dist/Pacelite/Pacelite.exe + 依赖目录，启动快（约 0.5 秒），可以只更新单个文件。

  单文件版（体积大、启动慢，但确实只有一个 exe）
    python build.py --onefile
    产物：dist/Pacelite.exe，约 60-80 MB，首次启动要解压到临时目录（2-4 秒）。

两种形态都会把 PresentMon.exe 一起带上，程序启动时会先找同目录下的 tools\PresentMon。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT.parent / "tools"
PRESENTMON = TOOLS / "PresentMon" / "PresentMon.exe"
LHM_DIR = TOOLS / "lhm"
PAWNIO_SETUP = TOOLS / "pawnio" / "PawnIO_setup.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Pacelite")
    parser.add_argument("--onefile", action="store_true", help="生成单文件 exe（启动更慢）")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排错用）")
    parser.add_argument("--no-fetch", action="store_true", help="缺依赖时不自动下载")
    args = parser.parse_args()

    # 依赖缺失就自动下载，省掉"先去下 PresentMon / LHM"这一步。
    missing = [
        not PRESENTMON.is_file(),
        not (LHM_DIR / "LibreHardwareMonitorLib.dll").is_file(),
        not PAWNIO_SETUP.is_file(),
    ]
    if any(missing):
        if args.no_fetch:
            print("[build] 缺少依赖且指定了 --no-fetch，无法继续", file=sys.stderr)
            return 1
        print("[build] 检测到依赖缺失，尝试自动下载…", flush=True)
        try:
            from fetch_deps import ensure_dependencies

            ensure_dependencies()
        except Exception as exc:  # noqa: BLE001 - 下载失败要给出明确原因
            print(f"[build] 依赖下载失败：{exc}", file=sys.stderr)
            return 1

    if not PRESENTMON.is_file():
        print(f"[build] 找不到 PresentMon：{PRESENTMON}", file=sys.stderr)
        return 1
    if not (LHM_DIR / "LibreHardwareMonitorLib.dll").is_file():
        print(f"[build] 找不到 LHM 库：{LHM_DIR}", file=sys.stderr)
        return 1
    if not PAWNIO_SETUP.is_file():
        print(f"[build] 提示：找不到 {PAWNIO_SETUP}，CPU 温度将无法启用（仍可打包）")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        "Pacelite",
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.console else "--windowed",
        # Qt 里用不到的模块先剔掉，产物能小一大截
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtQml",
        "--exclude-module",
        "PySide6.QtQuick",
        "--exclude-module",
        "PySide6.Qt3DCore",
        "--exclude-module",
        "tkinter",
        # PresentMon 跟着程序走，运行期按同级目录查找
        "--add-binary",
        f"{PRESENTMON};tools/PresentMon",
        # 精确传感器：LHM 的 dll 与 PawnIO 安装包（首次开启时弹 UAC 安装）
        "--add-data",
        f"{LHM_DIR};libs/lhm",
        str(ROOT / "main.py"),
    ]
    if PAWNIO_SETUP.is_file():
        command[-1:-1] = ["--add-data", f"{PAWNIO_SETUP};libs/pawnio"]
    print("[build] " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    target = ROOT / "dist" / ("Pacelite.exe" if args.onefile else "Pacelite")
    if target.exists():
        if args.onefile:
            shutil.copy2(ROOT / "README.md", ROOT / "dist" / "README.md")
        else:
            shutil.copy2(ROOT / "README.md", target / "README.md")
        print(f"[build] 完成：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
