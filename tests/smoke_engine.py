"""On-machine smoke test for the capture/input/display engine.

Run with the dev venv:  .venv\\Scripts\\python.exe tests\\smoke_engine.py
Writes a few artifacts to %TEMP%\\wcu-smoke and prints a PASS/FAIL report.
Non-destructive: only moves the mouse (and restores it).
"""
import ctypes
import os
import sys
from ctypes import wintypes

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import windows_computer_use  # noqa: E402  (sets DPI awareness on import)
from windows_computer_use import capture, coords, displays, images  # noqa: E402
from windows_computer_use import input as winput

OUT = os.path.join(os.environ.get("TEMP", "."), "wcu-smoke")
os.makedirs(OUT, exist_ok=True)
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((True, name, detail))
        print(f"[PASS] {name}: {detail}")
    except Exception as e:  # noqa: BLE001
        results.append((False, name, repr(e)))
        print(f"[FAIL] {name}: {e!r}")


def _find_titled_window():
    user32 = ctypes.WinDLL("user32")
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                if (rect.right - rect.left) > 50 and (rect.bottom - rect.top) > 50:
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return found


print(f"DPI awareness: {windows_computer_use.DPI_AWARENESS}")
print(f"artifacts -> {OUT}\n")

check("virtual_bounds", lambda: displays.virtual_bounds())
check("list_monitors", lambda: [
    f"{m['name']} {m['width']}x{m['height']}@({m['left']},{m['top']}) "
    f"scale={m['scale']} primary={m['primary']}" for m in displays.list_monitors()])
check("cursor_pos", lambda: displays.cursor_pos())


def _cap_virtual():
    img, vb = capture.grab_virtual()
    small, scale = images.downscale(img)
    data, mime = images.encode(small, "png")
    p = os.path.join(OUT, "virtual.png")
    small.save(p)
    return f"{img.size} -> rendered {small.size} scale={scale:.3f} {len(data)//1024}KB {mime} ({p})"


check("grab_virtual+downscale+encode", _cap_virtual)


def _cap_primary():
    img, rect = capture.grab_monitor(0)
    img.save(os.path.join(OUT, "monitor0.png"))
    return f"monitor0 {img.size} rect={rect}"


check("grab_monitor(0)", _cap_primary)

wins = _find_titled_window()
check("enum_windows", lambda: f"{len(wins)} titled windows; e.g. {[w[1] for w in wins[:3]]}")

if wins:
    def _cap_window():
        hwnd, title = wins[0]
        img, rect, ok = capture.grab_window(hwnd)
        img.save(os.path.join(OUT, "window0.png"))
        return f"'{title[:40]}' {img.size} ok={ok} rect={rect}"
    check("grab_window(PrintWindow)", _cap_window)


def _contact():
    img, _ = capture.grab_monitor(0)
    small, _ = images.downscale(img, 800)
    sheet = images.contact_sheet([small, small, small, small], ["t=0.0", "t=0.3", "t=0.6", "t=0.9"], cols=2)
    p = os.path.join(OUT, "contact.png")
    sheet.save(p)
    return f"{sheet.size} ({p})"


check("contact_sheet", _contact)


def _input_roundtrip():
    start = displays.cursor_pos()
    rect = displays.primary_rect()
    cx, cy = rect["left"] + rect["width"] // 2, rect["top"] + rect["height"] // 2
    winput.move(cx, cy)
    got = displays.cursor_pos()
    winput.move(*start)  # restore
    dx, dy = abs(got[0] - cx), abs(got[1] - cy)
    if dx > 2 or dy > 2:
        raise AssertionError(f"cursor move off: wanted {(cx, cy)} got {got}")
    return f"moved to {(cx, cy)} read {got} (restored to {start})"


check("input.move roundtrip", _input_roundtrip)


def _coord_mapping():
    # Simulate a downscaled monitor-1 capture and verify image->screen mapping.
    rect = displays.primary_rect()
    coords.set_last_capture(coords.CaptureGeometry(
        origin_x=rect["left"], origin_y=rect["top"],
        actual_width=rect["width"], actual_height=rect["height"],
        rendered_width=rect["width"] // 2, rendered_height=rect["height"] // 2,
        label="test"))
    px, py = coords.resolve_point(10, 10, "image")
    expect = (rect["left"] + 20, rect["top"] + 20)
    if abs(px - expect[0]) > 1 or abs(py - expect[1]) > 1:
        raise AssertionError(f"map wrong: got {(px, py)} expect ~{expect}")
    return f"image(10,10) -> screen {(px, py)} (expect ~{expect})"


check("coords.resolve_point", _coord_mapping)

ok = sum(1 for r in results if r[0])
print(f"\n{ok}/{len(results)} checks passed")
sys.exit(0 if ok == len(results) else 1)
