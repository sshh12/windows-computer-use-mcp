"""Key-name -> Windows virtual-key-code resolution.

Single-character names (``a``, ``5``, ``;``) resolve via VkKeyScanW so layout-aware
chars work in chords (e.g. ``ctrl+s``). Named keys (``enter``, ``f5``, ``up``) use an
explicit table. Used by input.py, which converts the VK to a hardware scan code
(MapVirtualKey) so injection works in games that read scan codes, not virtual keys.
"""
import ctypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.VkKeyScanW.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]

# Virtual-key codes that are "extended" keys and need KEYEVENTF_EXTENDEDKEY.
EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # pgup pgdn end home arrows
    0x2D, 0x2E,  # insert delete
    0x2C,        # print screen
    0x5B, 0x5C, 0x5D,  # lwin rwin apps
    0xA3, 0xA5,  # right ctrl, right alt
    0x90,        # numlock
    0x6F,        # numpad divide
}

NAMED: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "ret": 0x0D,
    "tab": 0x09, "space": 0x20, "spacebar": 0x20,
    "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "bksp": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "break": 0x13,
    "apps": 0x5D, "menu": 0x5D,
    # modifiers
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "super": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    "lwin": 0x5B, "rwin": 0x5C,
    # numpad
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
}
# Function keys F1..F24 -> 0x70..0x87
for _i in range(1, 25):
    NAMED[f"f{_i}"] = 0x6F + _i


class KeyError_(ValueError):
    pass


def resolve_vk(name: str) -> int:
    """Return the virtual-key code for a key name. Raises on unknown names."""
    if not name:
        raise KeyError_("empty key name")
    key = name.strip().lower()
    if key in NAMED:
        return NAMED[key]
    if len(name) == 1:
        res = user32.VkKeyScanW(name)
        if res != -1:
            return res & 0xFF
    raise KeyError_(f"unknown key: {name!r}")


def parse_combo(combo: str) -> list[int]:
    """``"ctrl+shift+s"`` -> [VK_CONTROL, VK_SHIFT, VK_S]. A literal ``+`` is ``plus``."""
    parts = [p for p in combo.replace("+", " ").split() if p] if combo.strip() != "+" else ["plus"]
    return [resolve_vk(p) for p in parts]
