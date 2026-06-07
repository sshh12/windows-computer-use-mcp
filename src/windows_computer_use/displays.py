"""Multi-monitor enumeration and virtual-desktop geometry.

Everything here is in *physical* pixels in virtual-desktop space, where the
top-left of the primary monitor is (0, 0) and monitors to the left/above have
negative coordinates. This is the coordinate space mss grabs in and SendInput
(with SetCursorPos / VIRTUALDESK) operates in.
"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore")
except OSError:  # pragma: no cover - shcore missing on very old Windows
    shcore = None

# GetSystemMetrics indices for the virtual desktop (all monitors combined).
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, ctypes.c_void_p, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]


def virtual_bounds() -> dict:
    """Bounding box of all monitors combined (physical px)."""
    g = user32.GetSystemMetrics
    return {
        "left": g(SM_XVIRTUALSCREEN),
        "top": g(SM_YVIRTUALSCREEN),
        "width": g(SM_CXVIRTUALSCREEN),
        "height": g(SM_CYVIRTUALSCREEN),
        "count": g(SM_CMONITORS),
    }


def _dpi_for_monitor(hmon) -> int:
    if shcore is None:
        return 96
    dpi_x = ctypes.c_uint(96)
    dpi_y = ctypes.c_uint(96)
    try:
        shcore.GetDpiForMonitor(ctypes.c_void_p(hmon), MDT_EFFECTIVE_DPI,
                                ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    except Exception:
        return 96
    return int(dpi_x.value)


def list_monitors() -> list[dict]:
    """All monitors, index 0.. in enumeration order. Each entry has physical
    bounds, primary flag, DPI and the scale factor (dpi/96)."""
    out: list[dict] = []

    def _cb(hmon, _hdc, _lprc, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            rc, wk = info.rcMonitor, info.rcWork
            dpi = _dpi_for_monitor(hmon)
            out.append({
                "index": len(out),
                "name": info.szDevice,
                "left": rc.left, "top": rc.top,
                "width": rc.right - rc.left, "height": rc.bottom - rc.top,
                "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                "dpi": dpi,
                "scale": round(dpi / 96.0, 4),
                "work_area": {
                    "left": wk.left, "top": wk.top,
                    "width": wk.right - wk.left, "height": wk.bottom - wk.top,
                },
            })
        return True

    user32.EnumDisplayMonitors(0, None, _MONITORENUMPROC(_cb), 0)
    return out


def monitor_rect(index: int) -> dict:
    """Physical rect of monitor `index` as an mss-style {left,top,width,height}."""
    mons = list_monitors()
    if not mons:
        return virtual_bounds()
    if index < 0 or index >= len(mons):
        raise ValueError(f"monitor index {index} out of range (have {len(mons)})")
    m = mons[index]
    return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}


def primary_rect() -> dict:
    for m in list_monitors():
        if m["primary"]:
            return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
    return monitor_rect(0)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)
