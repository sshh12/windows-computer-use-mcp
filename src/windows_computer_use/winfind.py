"""Top-level window enumeration, search, and foreground/focus helpers (shared).

Used by target resolution (`targets.py`) and the `window` tool. Coordinates/rects are
physical pixels in virtual-desktop space.
"""
import ctypes
import sys
from ctypes import wintypes

from . import displays

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_RESTORE, SW_SHOW = 9, 5
WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL


def _resolve_uwp(hwnd, frame_pid: int, frame_name: str):
    """UWP/Store apps (Calculator, Settings…) present their window under
    ApplicationFrameHost.exe; the real, killable process owns a child CoreWindow with a
    different PID. Resolve it so callers see the process they can actually act on."""
    if (frame_name or "").lower() != "applicationframehost.exe":
        return frame_pid, frame_name
    found = [frame_pid, frame_name]

    def _child(child, _lparam):
        cpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(child, ctypes.byref(cpid))
        if cpid.value and cpid.value != frame_pid:
            found[0] = int(cpid.value)
            found[1] = _proc_name(cpid.value)
            return False
        return True

    user32.EnumChildWindows(wintypes.HWND(int(hwnd)), _WNDENUMPROC(_child), 0)
    return found[0], found[1]


def close_window(hwnd) -> bool:
    """Politely close a window (WM_CLOSE) — like clicking its X. No PID needed."""
    return bool(user32.PostMessageW(wintypes.HWND(int(hwnd)), WM_CLOSE, 0, 0))


def _proc_name(pid: int) -> str:
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1]
    finally:
        kernel32.CloseHandle(h)
    return ""


def _monitor_for(rect: dict, monitors: list[dict]) -> int | None:
    cx = rect["left"] + rect["width"] // 2
    cy = rect["top"] + rect["height"] // 2
    for m in monitors:
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            return m["index"]
    return None


def list_windows(visible_only: bool = True) -> list[dict]:
    """All top-level windows with a title and non-zero size (foreground first)."""
    out: list[dict] = []
    monitors = displays.list_monitors()
    fg = int(user32.GetForegroundWindow() or 0)

    def _cb(hwnd, _lparam):
        h = int(hwnd)
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        ln = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if ln > 0:
            buf = ctypes.create_unicode_buffer(ln + 1)
            user32.GetWindowTextW(hwnd, buf, ln + 1)
            title = buf.value
        if visible_only and not title:
            return True
        rc = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rc))
        w, hgt = rc.right - rc.left, rc.bottom - rc.top
        if w <= 0 or hgt <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        real_pid, real_proc = _resolve_uwp(hwnd, int(pid.value), _proc_name(pid.value))
        rect = {"left": rc.left, "top": rc.top, "width": w, "height": hgt}
        out.append({
            "hwnd": h, "title": title, "pid": real_pid,
            "process": real_proc, "rect": rect,
            "monitor": _monitor_for(rect, monitors), "foreground": h == fg,
        })
        return True

    user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    out.sort(key=lambda w: (not w["foreground"], w["title"].lower()))
    return out


def find_windows(query: str) -> list[dict]:
    """Match windows by hwnd (exact), or case-insensitive substring of title/process.
    Accepts (and ignores) a leading ``window:`` prefix for consistency with the target grammar."""
    q = str(query).strip()
    if q.lower().startswith("window:"):
        q = q.split(":", 1)[1].strip()
    wins = list_windows()
    if q.isdigit():
        hit = [w for w in wins if w["hwnd"] == int(q)]
        if hit:
            return hit
    ql = q.lower()
    return [w for w in wins if ql in w["title"].lower() or ql in (w["process"] or "").lower()]


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def foreground(hwnd: int) -> bool:
    """Bring a window to the front, defeating the foreground lock via AttachThreadInput
    (the trick the ksp-spike capture relied on). Restores if minimized.

    Crucially: if the window is ALREADY foreground, this is a no-op. Re-activating an
    already-front window (SetForegroundWindow/AttachThreadInput) makes apps like Chrome reset
    their internal keyboard focus back to the page/document — which silently knocks focus off a
    game canvas/iframe before injected keys arrive. So only activate when actually needed."""
    hwnd_i = int(hwnd)
    if int(user32.GetForegroundWindow() or 0) == hwnd_i and not user32.IsIconic(wintypes.HWND(hwnd_i)):
        return True
    hwnd = wintypes.HWND(hwnd_i)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = []
    try:
        for tid in (fg_tid, tgt_tid):
            if tid and tid != cur_tid and tid not in attached:
                if user32.AttachThreadInput(cur_tid, tid, True):
                    attached.append(tid)
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, SW_SHOW)
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)
    if not ok:
        print(f"[wcu] warning: SetForegroundWindow failed for hwnd {int(hwnd.value)}", file=sys.stderr)
    return ok
