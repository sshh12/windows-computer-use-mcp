"""Windows computer-use MCP server.

A Model Context Protocol server that gives an agent full control of the local
Windows desktop: native screen capture (multi-monitor + per-window), low-level
input injection (scan-code keyboard, unicode text, absolute/relative mouse),
video recording with frame extraction, and a play-test loop for driving games.

DPI awareness is enabled at import time, before any capture/input module runs,
so all coordinates are consistent physical pixels across mixed-DPI monitors.
"""
from .dpi import enable_dpi_awareness

__version__ = "0.1.0"

# Must happen before pyautogui/mss/SendInput/GetWindowRect are exercised.
DPI_AWARENESS = enable_dpi_awareness()
