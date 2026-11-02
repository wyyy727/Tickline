"""快捷键的解析与格式化。

统一用一种可读的字符串表示：`Ctrl+Shift+M`。三个地方都要用到它 ——
配置文件的存储、设置窗口里的录入、以及注册到系统时的键码换算。
这里只做纯字符串 ↔ 键码转换，不碰 Win32，方便单独测试。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_MODIFIER_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt": MOD_ALT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
}

# 具名键 → 虚拟键码。单字母/数字走另一条分支。
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "minus": 0xBD,
    "plus": 0xBB,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "semicolon": 0xBA,
    "quote": 0xDE,
    "bracketleft": 0xDB,
    "bracketright": 0xDD,
    "backslash": 0xDC,
    "grave": 0xC0,
}

_VK_TO_NAME = {v: k for k, v in _NAMED_KEYS.items() if k not in ("escape", "del")}

# 显示用的名称（上面的表都是小写，直接 title() 会把 PageUp 显示成 Pageup）
_VK_DISPLAY = {
    0x21: "PageUp",
    0x22: "PageDown",
    0x08: "Backspace",
    0x2D: "Insert",
    0x2E: "Delete",
    0x24: "Home",
    0x23: "End",
    0x20: "Space",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
}

_MODIFIER_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_SHIFT, "Shift"), (MOD_ALT, "Alt"), (MOD_WIN, "Win"))

# Qt 里这些键与 Windows 虚拟键码不同，需要单独映射。
_QT_SPECIAL = {
    Qt.Key_Space: 0x20,
    Qt.Key_Tab: 0x09,
    Qt.Key_Return: 0x0D,
    Qt.Key_Enter: 0x0D,
    Qt.Key_Insert: 0x2D,
    Qt.Key_Delete: 0x2E,
    Qt.Key_Home: 0x24,
    Qt.Key_End: 0x23,
    Qt.Key_PageUp: 0x21,
    Qt.Key_PageDown: 0x22,
    Qt.Key_Left: 0x25,
    Qt.Key_Up: 0x26,
    Qt.Key_Right: 0x27,
    Qt.Key_Down: 0x28,
}


def parse(spec: str) -> tuple[int, int] | None:
    """把 "Ctrl+Shift+M" 解析成 (修饰键位掩码, 虚拟键码)；无法解析返回 None。

    强制要求至少带一个修饰键：裸按键注册成全局热键会抢走游戏里的正常输入。
    """
    if not spec or not spec.strip():
        return None
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if len(parts) < 2:
        return None
    modifiers = 0
    key_part = ""
    for part in parts:
        modifier = _MODIFIER_ALIASES.get(part)
        if modifier is not None:
            modifiers |= modifier
        else:
            key_part = part
    if modifiers == 0 or not key_part:
        return None

    if len(key_part) == 1 and key_part.isalnum():
        return modifiers, ord(key_part.upper())
    if key_part.startswith("f") and key_part[1:].isdigit():
        index = int(key_part[1:])
        if 1 <= index <= 24:
            return modifiers, 0x70 + index - 1
    vk = _NAMED_KEYS.get(key_part)
    if vk is not None:
        return modifiers, vk
    return None


def to_text(modifiers: int, vk: int) -> str:
    """把 (修饰键, 虚拟键码) 还原成显示用的字符串。"""
    names = [label for flag, label in _MODIFIER_ORDER if modifiers & flag]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        key = chr(vk)
    elif 0x70 <= vk <= 0x87:
        key = f"F{vk - 0x70 + 1}"
    else:
        key = _VK_DISPLAY.get(vk) or _VK_TO_NAME.get(vk, f"0x{vk:02X}").title()
    names.append(key)
    return "+".join(names)


def from_qt(modifiers, key) -> str | None:
    """把 Qt 的按键事件转成同一个字符串表示，用于设置窗口里的录入。"""
    mods = 0
    if modifiers & Qt.ControlModifier:
        mods |= MOD_CONTROL
    if modifiers & Qt.ShiftModifier:
        mods |= MOD_SHIFT
    if modifiers & Qt.AltModifier:
        mods |= MOD_ALT
    if modifiers & Qt.MetaModifier:
        mods |= MOD_WIN
    if mods == 0:
        # 只按修饰键或裸按键都不算，继续等用户按下完整组合。
        return None

    if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
        vk = key  # Qt 的 A-Z / 0-9 数值与虚拟键码一致
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = 0x70 + (key - Qt.Key_F1)
    else:
        vk = _QT_SPECIAL.get(key)
        if vk is None:
            return None
    return to_text(mods, vk)
