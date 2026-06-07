"""Process and shell orchestration for the Windows computer-use MCP server.

This module is self-contained: it implements its own lightweight ``EnumWindows``
search rather than importing a sibling windows module. Everything here is a pure
function that returns a plain ``dict``/``str``/``bool`` and never raises for an
expected failure -- instead it returns a dict carrying an ``"error"`` key (or a
``"timed_out"`` flag).

Capabilities:
  * launch          -- start a program (subprocess or ShellExecuteW)
  * kill            -- terminate by pid or by image name
  * wait            -- wait for a pid (or first name match) to exit
  * shell           -- run a powershell/cmd command and capture stdout/stderr
  * wait_for_file   -- block until a *new* file matching a glob appears
  * wait_for_window -- block until a matching top-level window is ready

HARD CONSTRAINTS honoured here:
  * stdio MCP server -- NEVER write to stdout; log only to stderr.
  * 64-bit-safe ctypes -- explicit .argtypes/.restype on every Win32 call that
    returns or consumes a HANDLE / pointer so values are not truncated.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import fnmatch
import logging
import os
import subprocess
import time
from typing import Any

logger = logging.getLogger("computer-use-windows.process")

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

SW_SHOWNORMAL = 1

# OpenProcess access rights
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

# WaitForSingleObject / WaitForInputIdle return codes
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF

# subprocess flag: don't pop a console window when capturing output
CREATE_NO_WINDOW = 0x08000000

# Poll cadence used by the wait_for_* helpers (seconds)
_POLL_INTERVAL = 0.25


# ---------------------------------------------------------------------------
# 64-bit-safe ctypes setup
# ---------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
# ShellExecuteW lives in shell32; use_last_error not required for it.
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# HANDLE-returning / pointer-consuming signatures. Declaring these explicitly is
# essential on 64-bit Windows: without restype set, ctypes assumes c_int and
# truncates the returned 64-bit handle.
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE

_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD

# GetExitCodeProcess(HANDLE, LPDWORD)
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL

# ShellExecuteW returns an HINSTANCE-sized value (treated as a pointer/handle).
_shell32.ShellExecuteW.argtypes = [
    wintypes.HWND,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_int,
]
_shell32.ShellExecuteW.restype = wintypes.HINSTANCE  # pointer-sized

# Window enumeration / inspection
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL

_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL

_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int

_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int

# GetWindowThreadProcessId(HWND, LPDWORD) -> thread id
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

# WaitForInputIdle(HANDLE, DWORD) -> DWORD
_user32.WaitForInputIdle.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_user32.WaitForInputIdle.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# Path / helper utilities
# ---------------------------------------------------------------------------

def _expand(path: str | None) -> str | None:
    """Expand ``~`` and environment variables in a path string."""
    if path is None:
        return None
    return os.path.expandvars(os.path.expanduser(path))


def _norm_image(name: str) -> str:
    """Normalise a process image name to lower-case with a trailing ``.exe``."""
    name = name.strip().lower()
    if not name.endswith(".exe"):
        name = name + ".exe"
    return name


def _get_window_title(hwnd: int) -> str:
    """Return the title of *hwnd* (empty string if it has none)."""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_pid(hwnd: int) -> int:
    """Return the owning process id of *hwnd* (0 on failure)."""
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_image_name(pid: int) -> str | None:
    """Return the image (executable) name for *pid*, or ``None`` if unknown.

    Uses ``QueryFullProcessImageNameW`` via a limited-information handle so it
    works for most processes without elevation.
    """
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        QueryFullProcessImageNameW = _kernel32.QueryFullProcessImageNameW
        QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        QueryFullProcessImageNameW.restype = wintypes.BOOL
        if QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return None
    except Exception:  # pragma: no cover - defensive
        return None
    finally:
        _kernel32.CloseHandle(handle)


def _enum_windows() -> list[int]:
    """Return a list of all top-level window handles (visible or not)."""
    handles: list[int] = []

    def _callback(hwnd: int, _lparam: Any) -> bool:
        handles.append(int(hwnd))
        return True  # keep enumerating

    # Keep a reference to the callback for the duration of the call so it is not
    # garbage-collected while EnumWindows is iterating.
    proc = _WNDENUMPROC(_callback)
    _user32.EnumWindows(proc, 0)
    return handles


def _list_pids_by_image(image: str) -> list[int]:
    """Return pids whose image name equals *image* (already normalised .exe).

    Implemented via ``tasklist`` CSV output so we do not need the Toolhelp
    snapshot ctypes dance; ``tasklist`` is always present on Windows.
    """
    pids: list[int] = []
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tasklist failed for %s: %s", image, exc)
        return pids

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # CSV: "image","pid","session","sess#","mem"
        fields = [f.strip().strip('"') for f in line.split('","')]
        if len(fields) < 2:
            continue
        # Confirm image actually matches (the filter can return "INFO: No tasks")
        if fields[0].lower() != image:
            continue
        try:
            pids.append(int(fields[1]))
        except ValueError:
            continue
    return pids


def _terminate_pid(pid: int, exit_code: int = 1) -> bool:
    """Open *pid* and terminate it. Returns True on success."""
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        # Fall back to taskkill (handles access-denied / elevated targets).
        try:
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
            return res.returncode == 0
        except Exception:
            return False
    try:
        return bool(_kernel32.TerminateProcess(handle, exit_code))
    finally:
        _kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def launch(
    exe: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    shell: bool = False,
) -> dict:
    """Launch a program.

    Parameters
    ----------
    exe:
        Executable path, document, URL, or shell URI (``ms-settings:`` etc).
        ``~`` and environment variables are expanded.
    args:
        Argument list (used as-is for the direct path; joined into a parameter
        string for the ShellExecute path).
    cwd:
        Working directory; ``~``/env vars expanded.
    shell:
        When ``False`` (default) the program is started with
        ``subprocess.Popen([exe, *args], cwd=...)`` and a concrete pid is
        returned. When ``True`` the launch is routed through ``ShellExecuteW``
        so URLs, ``ms-settings:`` URIs, documents-by-association,
        ``shell:Downloads`` and Store/UWP apps all resolve correctly. In that
        case a pid is usually not available.

    Returns
    -------
    dict
        ``{"pid": int, "exe": exe}`` for direct launches, or
        ``{"pid": None, "launched": exe, "shell": True}`` for shell launches.
        On failure: ``{"error": "..."}``.
    """
    args = list(args) if args else []
    exe_expanded = _expand(exe) or exe
    cwd_expanded = _expand(cwd)

    if not shell:
        try:
            proc = subprocess.Popen([exe_expanded, *args], cwd=cwd_expanded)
            return {"pid": proc.pid, "exe": exe_expanded}
        except Exception as exc:
            logger.warning("launch(direct) failed for %s: %s", exe_expanded, exc)
            return {"error": str(exc), "exe": exe_expanded}

    # shell=True -> ShellExecuteW. Note: for protocol/URI launches the path must
    # be left intact (do not basename it). We still expand ~/env vars for normal
    # file paths; URIs are unaffected because they contain no ~ or %VARS%.
    params = subprocess.list2cmdline(args) if args else None
    try:
        rc = _shell32.ShellExecuteW(
            None,
            "open",
            exe_expanded,
            params,
            cwd_expanded,
            SW_SHOWNORMAL,
        )
        # ShellExecuteW returns a value > 32 on success.
        rc_int = ctypes.cast(rc, ctypes.c_void_p).value or 0
        if rc_int <= 32:
            return {
                "error": f"ShellExecuteW failed (code {rc_int})",
                "launched": exe_expanded,
                "shell": True,
            }
        return {"pid": None, "launched": exe_expanded, "shell": True}
    except Exception as exc:
        logger.warning("launch(shell) failed for %s: %s", exe_expanded, exc)
        return {"error": str(exc), "launched": exe_expanded, "shell": True}


def kill(target: Any, all: bool = False) -> dict:
    """Terminate process(es).

    Parameters
    ----------
    target:
        ``int`` -> kill that exact pid. ``str`` -> match running image names
        case-insensitively, with or without the ``.exe`` suffix.
    all:
        Only relevant for name targets. If a name matches more than one process
        and ``all`` is ``False``, nothing is killed and
        ``{"error": "ambiguous", "matches": n}`` is returned. If ``all`` is
        ``True`` (or there is exactly one match) every match is terminated.

    Returns
    -------
    dict
        ``{"killed": n}`` on success, or a dict with an ``"error"`` key.
    """
    if isinstance(target, bool):
        # bool is a subclass of int; reject it to avoid surprising behaviour.
        return {"error": "invalid target"}

    if isinstance(target, int):
        ok = _terminate_pid(target)
        if not ok:
            return {"error": f"could not terminate pid {target}", "killed": 0}
        return {"killed": 1}

    if isinstance(target, str):
        image = _norm_image(target)
        pids = _list_pids_by_image(image)
        if not pids:
            return {"killed": 0}
        if len(pids) > 1 and not all:
            return {"error": "ambiguous", "matches": len(pids)}
        killed = 0
        for pid in pids:
            if _terminate_pid(pid):
                killed += 1
        return {"killed": killed}

    return {"error": "invalid target type"}


def wait(target: Any, timeout: float) -> dict:
    """Wait for a process to exit.

    Parameters
    ----------
    target:
        ``int`` pid, or a ``str`` image name (the first matching pid is used).
    timeout:
        Maximum seconds to wait.

    Returns
    -------
    dict
        ``{"exit_code": int|None, "timed_out": bool}``. ``exit_code`` is the
        process exit code when it could be retrieved, otherwise ``None``.
        If the target was a name with no running match, returns
        ``{"exit_code": None, "timed_out": False}`` (already gone).
        On bad input: ``{"error": "..."}``.
    """
    if isinstance(target, bool):
        return {"error": "invalid target"}

    if isinstance(target, str):
        pids = _list_pids_by_image(_norm_image(target))
        if not pids:
            # Nothing matching is running -> treat as already exited.
            return {"exit_code": None, "timed_out": False}
        pid = pids[0]
    elif isinstance(target, int):
        pid = target
    else:
        return {"error": "invalid target type"}

    handle = _kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        # Could not open -> most likely the process is already gone.
        return {"exit_code": None, "timed_out": False}

    try:
        ms = INFINITE if timeout is None else max(0, int(timeout * 1000))
        res = _kernel32.WaitForSingleObject(handle, ms)
        if res == WAIT_TIMEOUT:
            return {"exit_code": None, "timed_out": True}
        if res == WAIT_FAILED:
            return {"exit_code": None, "timed_out": False}
        # WAIT_OBJECT_0 -> process signalled (exited). Read its exit code.
        code = wintypes.DWORD(0)
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return {"exit_code": int(code.value), "timed_out": False}
        return {"exit_code": None, "timed_out": False}
    finally:
        _kernel32.CloseHandle(handle)


def shell(command: str, timeout: float = 60, shell: str = "powershell") -> dict:
    """Run a shell command and capture its output.

    The command is executed through PowerShell or cmd with stdout and stderr
    captured, no console window shown (``CREATE_NO_WINDOW``), and decoded as
    UTF-8 with ``errors="replace"``. Output is **not** truncated -- the caller
    is responsible for any size limiting.

    Parameters
    ----------
    command:
        The command text to execute.
    timeout:
        Maximum seconds before the command is killed.
    shell:
        Either ``"powershell"`` (default) or ``"cmd"``.

    Returns
    -------
    dict
        ``{"exit": int, "stdout": str, "stderr": str, "timed_out": bool}``.
        On bad input / spawn failure: a dict with an ``"error"`` key.
    """
    shell_kind = (shell or "powershell").lower()
    if shell_kind == "powershell":
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
    elif shell_kind == "cmd":
        argv = ["cmd.exe", "/d", "/c", command]
    else:
        return {"error": f"unsupported shell '{shell}'"}

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {"exit": -1, "stdout": out, "stderr": err, "timed_out": True}
    except Exception as exc:
        logger.warning("shell spawn failed: %s", exc)
        return {"error": str(exc)}

    return {
        "exit": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "timed_out": False,
    }


def wait_for_file(directory: str, glob: str, timeout: float, stable: bool = True) -> dict:
    """Wait for a *new* file matching *glob* to appear in *directory*.

    Existing matches are snapshotted up-front and ignored; only a file that
    appears after the call begins counts. When *stable* is ``True`` the new
    file's size must be unchanged across two consecutive polls before it is
    reported (helps avoid catching a file mid-download/mid-write).

    Parameters
    ----------
    directory:
        Directory to watch (``~``/env vars expanded).
    glob:
        ``fnmatch``-style pattern, e.g. ``"*.pdf"``.
    timeout:
        Maximum seconds to wait.
    stable:
        Require two equal size readings before reporting (default ``True``).

    Returns
    -------
    dict
        ``{"path": str}`` for the newly seen file, or ``{"timed_out": True}``.
        On bad input: ``{"error": "..."}``.
    """
    directory = _expand(directory) or directory
    if not os.path.isdir(directory):
        return {"error": f"not a directory: {directory}"}

    def _matches() -> set[str]:
        try:
            names = os.listdir(directory)
        except OSError:
            return set()
        return {n for n in names if fnmatch.fnmatch(n, glob)}

    baseline = _matches()
    deadline = time.monotonic() + max(0.0, timeout)

    while True:
        current = _matches()
        new_names = current - baseline
        if new_names:
            # Deterministic pick: newest by mtime, then lexical.
            candidates = sorted(new_names)
            chosen = None
            newest_mtime = -1.0
            for n in candidates:
                p = os.path.join(directory, n)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > newest_mtime:
                    newest_mtime = mt
                    chosen = n
            if chosen is None:
                chosen = candidates[0]
            full = os.path.join(directory, chosen)

            if not stable:
                return {"path": full}

            # Stability check: size unchanged across two polls.
            try:
                size1 = os.path.getsize(full)
            except OSError:
                # File vanished; drop it from consideration and keep polling.
                baseline.add(chosen)
                if time.monotonic() >= deadline:
                    return {"timed_out": True}
                time.sleep(_POLL_INTERVAL)
                continue
            time.sleep(_POLL_INTERVAL)
            try:
                size2 = os.path.getsize(full)
            except OSError:
                baseline.add(chosen)
                if time.monotonic() >= deadline:
                    return {"timed_out": True}
                continue
            if size1 == size2:
                return {"path": full}
            # Not stable yet; loop again (do not re-baseline).

        if time.monotonic() >= deadline:
            return {"timed_out": True}
        time.sleep(_POLL_INTERVAL)


def wait_for_window(query: str, timeout: float, ready: str = "input_idle") -> dict:
    """Wait for a top-level window matching *query* to become ready.

    A window matches when *query* (case-insensitive) appears in either its
    title or the image name of its owning process.

    Ready levels (each implies the previous):
      * ``"exists"``     -- the window handle was found.
      * ``"visible"``    -- ``IsWindowVisible`` is true.
      * ``"input_idle"`` -- the owning process has finished initialising; this
        opens the process via its pid (from ``GetWindowThreadProcessId``) and
        calls ``WaitForInputIdle``. If that cannot be done (e.g. console app, or
        the handle cannot be opened) it falls back to the ``exists`` guarantee.

    Parameters
    ----------
    query:
        Substring to match against window title or process image name.
    timeout:
        Maximum seconds to wait.
    ready:
        One of ``"exists"``, ``"visible"``, ``"input_idle"`` (default).

    Returns
    -------
    dict
        ``{"title": str, "pid": int, "ready": str}`` on success, or
        ``{"timed_out": True}``. On bad input: ``{"error": "..."}``.
    """
    if ready not in ("exists", "visible", "input_idle"):
        return {"error": f"invalid ready level '{ready}'"}

    q = (query or "").lower()
    deadline = time.monotonic() + max(0.0, timeout)

    def _find() -> tuple[int, str, int] | None:
        """Return (hwnd, title, pid) of the first matching window, else None."""
        for hwnd in _enum_windows():
            title = _get_window_title(hwnd)
            pid = _window_pid(hwnd)
            hay_title = title.lower()
            image = _process_image_name(pid) or ""
            if q in hay_title or q in image.lower():
                # Prefer windows with a non-empty title when matching, but accept
                # any match so process-name-only matches still work.
                return hwnd, title, pid
        return None

    while True:
        match = _find()
        if match is not None:
            hwnd, title, pid = match

            if ready == "exists":
                return {"title": title, "pid": pid, "ready": "exists"}

            visible = bool(_user32.IsWindowVisible(hwnd))
            if ready == "visible":
                if visible:
                    return {"title": title, "pid": pid, "ready": "visible"}
            else:  # input_idle
                if visible or True:  # visibility not required for input_idle
                    handle = _kernel32.OpenProcess(
                        PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, pid
                    )
                    if handle:
                        try:
                            res = _user32.WaitForInputIdle(handle, 0)
                            # 0 -> idle/ready. WAIT_TIMEOUT -> still busy.
                            # WAIT_FAILED (e.g. non-GUI) -> fall back to exists.
                            if res == 0:
                                return {
                                    "title": title,
                                    "pid": pid,
                                    "ready": "input_idle",
                                }
                            if res == WAIT_FAILED:
                                return {
                                    "title": title,
                                    "pid": pid,
                                    "ready": "exists",
                                }
                            # else still initialising -> keep polling
                        finally:
                            _kernel32.CloseHandle(handle)
                    else:
                        # Cannot inspect the process -> fall back to exists.
                        return {"title": title, "pid": pid, "ready": "exists"}

        if time.monotonic() >= deadline:
            return {"timed_out": True}
        time.sleep(_POLL_INTERVAL)
