"""Screen and window capture.

Two backends:
  - mss (GDI BitBlt): fast capture of any physical rect — whole virtual desktop, a
    single monitor, or an arbitrary region. Multi-monitor aware (handles negative
    coordinates for monitors left of / above the primary).
  - PrintWindow(PW_RENDERFULLCONTENT): captures a *specific window's* pixels even when
    it is occluded or partially off-screen, and works for many DirectX/GPU-composited
    windows (e.g. the Unreal editor) where a plain BitBlt of the screen would be black.

All returned images are RGB PIL images in physical pixels.
"""
import ctypes
from ctypes import wintypes

import mss
from PIL import Image

from . import displays

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

# 64-bit-safe handle types (without these, ctypes truncates pointers to int).
user32.GetWindowDC.restype = wintypes.HDC
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def grab_rect(left: int, top: int, width: int, height: int) -> Image.Image:
    """Capture an arbitrary physical rect via mss (a fresh instance per call keeps it
    thread-safe under concurrent tool calls)."""
    with mss.mss() as sct:
        raw = sct.grab({"left": int(left), "top": int(top),
                        "width": int(width), "height": int(height)})
    return Image.frombytes("RGB", raw.size, raw.rgb)


def grab_virtual() -> tuple[Image.Image, dict]:
    vb = displays.virtual_bounds()
    return grab_rect(vb["left"], vb["top"], vb["width"], vb["height"]), vb


def grab_monitor(index: int) -> tuple[Image.Image, dict]:
    rect = displays.monitor_rect(index)
    return grab_rect(rect["left"], rect["top"], rect["width"], rect["height"]), rect


def grab_window(hwnd: int) -> tuple[Image.Image, dict, bool]:
    """Capture a window by handle using PrintWindow(PW_RENDERFULLCONTENT).

    Returns (image, rect, ok). `ok` is the PrintWindow return value; an all-black
    image with ok True can still happen for hardware-protected surfaces — callers may
    foreground + grab_rect as a fallback.
    """
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise ValueError("window has zero size (minimized?)")

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        ok = bool(user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT))
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # negative => top-down rows
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = BI_RGB
        buf = (ctypes.c_char * (w * h * 4))()
        gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        img = Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)
    return img, {"left": rect.left, "top": rect.top, "width": w, "height": h}, ok


def grab_window_region(hwnd: int, frac: tuple) -> tuple[Image.Image, dict, bool]:
    """Capture a window (PrintWindow, occluded-safe, no focus theft) then crop to a
    fractional sub-rect of it. `frac` is (fx, fy, fw, fh) as fractions of the window rect.

    Returns (cropped_image, abs_rect, ok) where abs_rect is the crop's absolute physical
    screen rect — so the caller can set the click-frame origin from it directly.
    """
    img, rect, ok = grab_window(hwnd)
    iw, ih = img.size
    fx, fy, fw, fh = frac
    cx = min(max(int(round(fx * iw)), 0), iw - 1)
    cy = min(max(int(round(fy * ih)), 0), ih - 1)
    cw = max(1, min(int(round(fw * iw)), iw - cx))
    ch = max(1, min(int(round(fh * ih)), ih - cy))
    crop = img.crop((cx, cy, cx + cw, cy + ch))
    abs_rect = {"left": rect["left"] + cx, "top": rect["top"] + cy, "width": cw, "height": ch}
    return crop, abs_rect, ok
