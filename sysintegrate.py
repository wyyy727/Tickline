"""开机自启。

写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，不需要管理员权限。
优先用 pythonw.exe 启动，避免每次开机弹一个黑色控制台窗口。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# 不声明 argtypes 时 ctypes 会把 HKEY 当 int 传，64 位下句柄会被截断。
advapi32.RegOpenKeyExW.argtypes = [wt.HKEY, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.HKEY)]
advapi32.RegOpenKeyExW.restype = wt.LONG
advapi32.RegCreateKeyExW.argtypes = [
    wt.HKEY,
    wt.LPCWSTR,
    wt.DWORD,
    wt.LPWSTR,
    wt.DWORD,
    wt.DWORD,
    ctypes.c_void_p,
    ctypes.POINTER(wt.HKEY),
    ctypes.POINTER(wt.DWORD),
]
advapi32.RegCreateKeyExW.restype = wt.LONG
advapi32.RegQueryValueExW.argtypes = [
    wt.HKEY,
    wt.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(wt.DWORD),
    ctypes.c_void_p,
    ctypes.POINTER(wt.DWORD),
]
advapi32.RegQueryValueExW.restype = wt.LONG
advapi32.RegSetValueExW.argtypes = [wt.HKEY, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD]
advapi32.RegSetValueExW.restype = wt.LONG
advapi32.RegDeleteValueW.argtypes = [wt.HKEY, wt.LPCWSTR]
advapi32.RegDeleteValueW.restype = wt.LONG
advapi32.RegCloseKey.argtypes = [wt.HKEY]
advapi32.RegCloseKey.restype = wt.LONG

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Tickline"
# 更名前（Pacelite）写的自启项。留着会在开机时把旧版本也拉起来，启动时清掉。
LEGACY_RUN_VALUE = "Pacelite"

HKEY_CURRENT_USER = 0x80000001
KEY_SET_VALUE = 0x0002
KEY_QUERY_VALUE = 0x0001
REG_SZ = 1


def pythonw_command() -> str:
    """自启命令。

    打包成 exe 后 sys.executable 就是 Tickline.exe 自己，不能再拼一次入口脚本 ——
    以前会写出 "Tickline.exe" "Tickline.exe"，开机启动时 argparse 直接报参数错误退出。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    entry = Path(sys.argv[0]).resolve()
    python = Path(sys.executable)
    interpreter = python.with_name("pythonw.exe")
    if not interpreter.is_file():
        interpreter = python
    return f'"{interpreter}" "{entry}"'


def autostart_enabled() -> bool:
    """注册表里有没有这项；界面用它来和配置对账。"""
    key = wt.HKEY()
    if advapi32.RegOpenKeyExW(
        HKEY_CURRENT_USER, RUN_KEY, 0, KEY_QUERY_VALUE, ctypes.byref(key)
    ) != 0:
        return False
    try:
        size = wt.DWORD(0)
        kind = wt.DWORD(0)
        if advapi32.RegQueryValueExW(
            key, RUN_VALUE, None, ctypes.byref(kind), None, ctypes.byref(size)
        ) != 0:
            return False
        return size.value > 0
    finally:
        advapi32.RegCloseKey(key)


def purge_legacy_autostart() -> None:
    """删掉更名前的自启项（如果存在）。每次启动调一次，幂等。"""
    key = wt.HKEY()
    if advapi32.RegOpenKeyExW(
        HKEY_CURRENT_USER, RUN_KEY, 0, KEY_SET_VALUE, ctypes.byref(key)
    ) != 0:
        return
    try:
        advapi32.RegDeleteValueW(key, LEGACY_RUN_VALUE)
    finally:
        advapi32.RegCloseKey(key)


def set_autostart(enabled: bool) -> bool:
    """写入或删除自启项；成功返回 True。"""
    key = wt.HKEY()
    if advapi32.RegCreateKeyExW(
        HKEY_CURRENT_USER, RUN_KEY, 0, None, 0, KEY_SET_VALUE, None, ctypes.byref(key), None
    ) != 0:
        return False
    try:
        if enabled:
            command = pythonw_command()
            data = (ctypes.c_wchar * (len(command) + 1))(*command)
            return (
                advapi32.RegSetValueExW(
                    key,
                    RUN_VALUE,
                    0,
                    REG_SZ,
                    ctypes.cast(data, ctypes.c_void_p),
                    ctypes.sizeof(data),
                )
                == 0
            )
        # 删除一个本来就不存在的值会返回非 0，这里视为成功。
        advapi32.RegDeleteValueW(key, RUN_VALUE)
        return True
    finally:
        advapi32.RegCloseKey(key)
