"""Low-level input injection via SendInput (ctypes).

Why not pyautogui: pyautogui sends *virtual-key* events via the legacy keybd_event
path, which many games (DirectInput / raw input) ignore. We send hardware **scan
codes** (KEYEVENTF_SCANCODE), unicode text (KEYEVENTF_UNICODE), and support both
absolute and **relative** mouse motion (relative is required for in-game mouse-look).
All coordinates passed in are physical pixels in virtual-desktop space.
"""
import ctypes
import time
from ctypes import wintypes

from . import keymap

user32 = ctypes.WinDLL("user32", use_last_error=True)

# --- input type + flags ---------------------------------------------------
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
XBUTTON1, XBUTTON2 = 0x0001, 0x0002
WHEEL_DELTA = 120
MAPVK_VK_TO_VSC = 0

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ULONG_PTR))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ULONG_PTR))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL


def _send(*events: INPUT) -> None:
    n = len(events)
    if not n:
        return
    arr = (INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise ctypes.WinError(ctypes.get_last_error())


def _key_event(vk: int, keyup: bool) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    if scan:
        flags = KEYEVENTF_SCANCODE
        if vk in keymap.EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        w_vk, w_scan = 0, scan
    else:  # no scan code mapping -> fall back to virtual-key event
        flags, w_vk, w_scan = 0, vk, 0
    if keyup:
        flags |= KEYEVENTF_KEYUP
    ki = KEYBDINPUT(w_vk, w_scan, flags, 0, None)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki))


def _unicode_event(code_unit: int, keyup: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if keyup else 0)
    ki = KEYBDINPUT(0, code_unit, flags, 0, None)
    return INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=ki))


# --- public keyboard API --------------------------------------------------
def key_down(name: str) -> None:
    _send(_key_event(keymap.resolve_vk(name), False))


def key_up(name: str) -> None:
    _send(_key_event(keymap.resolve_vk(name), True))


def tap(name: str, presses: int = 1, interval: float = 0.0) -> None:
    vk = keymap.resolve_vk(name)
    for i in range(presses):
        _send(_key_event(vk, False))
        _send(_key_event(vk, True))
        if interval and i < presses - 1:
            time.sleep(interval)


def press(combo: str) -> None:
    """Press a chord like ``ctrl+shift+s`` (down in order, up in reverse)."""
    vks = keymap.parse_combo(combo)
    for vk in vks:
        _send(_key_event(vk, False))
    for vk in reversed(vks):
        _send(_key_event(vk, True))


def hold(combo: str, duration: float) -> None:
    """Hold a key/chord down for `duration` seconds (e.g. holding W to walk)."""
    vks = keymap.parse_combo(combo)
    for vk in vks:
        _send(_key_event(vk, False))
    try:
        time.sleep(max(0.0, duration))
    finally:
        for vk in reversed(vks):
            _send(_key_event(vk, True))


def type_text(text: str, chunk: int = 40, interval: float = 0.0, literal: bool = False) -> None:
    """Type arbitrary unicode text. Unless ``literal``, ``\\n`` -> Enter and ``\\t`` -> Tab
    so it behaves in real apps. Everything else is sent as a unicode code unit (no
    clipboard needed)."""
    batch: list[INPUT] = []

    def flush():
        nonlocal batch
        if batch:
            _send(*batch)
            batch = []

    for ch in text:
        if not literal and ch == "\n":
            flush()
            tap("enter")
            continue
        if not literal and ch == "\t":
            flush()
            tap("tab")
            continue
        for unit in _utf16_units(ch):
            batch.append(_unicode_event(unit, False))
            batch.append(_unicode_event(unit, True))
        if len(batch) >= chunk * 2:
            flush()
            if interval:
                time.sleep(interval)
    flush()


def _utf16_units(ch: str) -> list[int]:
    code = ord(ch)
    if code <= 0xFFFF:
        return [code]
    code -= 0x10000  # surrogate pair
    return [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]


# --- public mouse API -----------------------------------------------------
_BTN_DOWN = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN, "middle": MOUSEEVENTF_MIDDLEDOWN}
_BTN_UP = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP, "middle": MOUSEEVENTF_MIDDLEUP}


def move(x: int, y: int) -> None:
    """Move the cursor to an absolute physical pixel (multi-monitor aware)."""
    user32.SetCursorPos(int(x), int(y))


def move_relative(dx: int, dy: int) -> None:
    """Relative mouse motion (for in-game mouse-look / raw input)."""
    mi = MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, None)
    _send(INPUT(INPUT_MOUSE, _INPUTUNION(mi=mi)))


def _button(flag: int, data: int = 0) -> None:
    mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flag, 0, None)
    _send(INPUT(INPUT_MOUSE, _INPUTUNION(mi=mi)))


def button_down(button: str = "left") -> None:
    if button in ("x1", "x2"):
        _button(MOUSEEVENTF_XDOWN, XBUTTON1 if button == "x1" else XBUTTON2)
    else:
        _button(_BTN_DOWN[button])


def button_up(button: str = "left") -> None:
    if button in ("x1", "x2"):
        _button(MOUSEEVENTF_XUP, XBUTTON1 if button == "x1" else XBUTTON2)
    else:
        _button(_BTN_UP[button])


def click(button: str = "left", count: int = 1, x: int | None = None, y: int | None = None,
          interval: float = 0.0, modifiers: list[str] | None = None, press_dwell: float = 0.03) -> None:
    if x is not None and y is not None:
        move(x, y)
        time.sleep(0.02)
    mods = modifiers or []
    for m in mods:
        key_down(m)
    try:
        for i in range(count):
            button_down(button)
            if press_dwell:
                time.sleep(press_dwell)  # real mousedown->mouseup gap so canvases latch focus
            button_up(button)
            if interval and i < count - 1:
                time.sleep(interval)
    finally:
        for m in reversed(mods):
            key_up(m)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 20,
         duration: float = 0.2, press_dwell_ms: int = 60, release_dwell_ms: int = 0,
         path: list[tuple[int, int]] | None = None) -> None:
    """Press at (x1,y1), travel through `path` (or straight to x2,y2), release. Dwells let
    Explorer/desktop register the drag image and drop targets spring open."""
    move(x1, y1)
    time.sleep(0.02)
    button_down(button)
    if press_dwell_ms:
        time.sleep(press_dwell_ms / 1000.0)
    try:
        waypoints = path if path else [(x2, y2)]
        cur = (x1, y1)
        steps = max(1, steps)
        for wx, wy in waypoints:
            for i in range(1, steps + 1):
                ix = int(round(cur[0] + (wx - cur[0]) * i / steps))
                iy = int(round(cur[1] + (wy - cur[1]) * i / steps))
                move(ix, iy)
                if duration:
                    time.sleep(duration / steps)
            cur = (wx, wy)
    finally:
        if release_dwell_ms:
            time.sleep(release_dwell_ms / 1000.0)
        button_up(button)


def scroll(amount: int, horizontal: bool = False) -> None:
    """Scroll wheel. Positive = up / right. `amount` is in wheel notches."""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _button(flag, int(amount) * WHEEL_DELTA)
