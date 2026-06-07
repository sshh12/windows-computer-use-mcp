"""Process DPI awareness.

Must run before any screen capture or input injection so that mss, GetWindowRect,
SendInput and SetCursorPos all agree on *physical* pixels. Without this, a display
scaled to 125%/150% returns virtualized (logical) coordinates and clicks land off.

Tiered fallback: per-monitor-v2 (best, Win10 1703+) -> per-monitor -> system.
"""
import ctypes


def enable_dpi_awareness() -> str:
    """Make the process DPI aware. Returns the level achieved."""
    # PER_MONITOR_AWARE_V2 = DPI_AWARENESS_CONTEXT handle value -4.
    try:
        ctx = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:
        pass
    # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win8.1+, shcore).
    try:
        # Returns S_OK (0) or E_ACCESSDENIED if already set.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"
