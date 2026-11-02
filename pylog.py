"""极简日志：窗口版（PyInstaller --windowed）没有控制台，sys.stdout/stderr 是 None。

所有诊断信息都必须落到文件里，否则打包版出问题时用户拿不到任何线索。
日志放在 %LOCALAPPDATA%\\Tickline\\tickline.log，超过 1 MB 滚一份旧的。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

LOG_MAX_BYTES = 1_000_000
_lock = threading.Lock()


def log_path() -> Path:
    """日志位置；%LOCALAPPDATA% 不可用时退回用户目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Tickline" / "tickline.log"


def log(message: str) -> None:
    """写一行日志：先落盘，再尽量往 stderr 抄一份（窗口版没有 stderr 就是 no-op）。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with _lock:
        try:
            path = log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
                path.replace(path.with_name("tickline.1.log"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        stream = sys.stderr
        if stream is not None:
            try:
                print(line, file=stream, flush=True)
            except (AttributeError, OSError, ValueError):
                pass
