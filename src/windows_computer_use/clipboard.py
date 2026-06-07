"""Windows clipboard access for CF_UNICODETEXT (ctypes, 64-bit safe).

This module reads and writes unicode text on the Windows clipboard using the
raw Win32 API via ctypes. All HANDLE / HGLOBAL / pointer-returning functions
have explicit ``argtypes`` / ``restype`` declarations so that 64-bit pointers
are not silently truncated to 32 bits (the classic cause of clipboard crashes
on amd64 Python builds).

Design notes:
  * ``OpenClipboard`` can fail transiently when another process holds the
    clipboard open, so ``set_text`` retries a handful of times with a tiny
    backoff.
  * ``EmptyClipboard`` is always called before writing new data.
  * ``CloseClipboard`` is always called in a ``finally`` block so we never
    leave the clipboard locked.
  * Allocation size is ``(len(text) + 1) * 2`` bytes to leave room for the
    UTF-16 null terminator.

This is a stdio MCP server component: nothing here writes to stdout. Any
diagnostics go to the module logger (stderr).
"""
from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

logger = logging.getLogger(__name__)

# CF_UNICODETEXT standard clipboard format id.
CF_UNICODETEXT = 13
# GlobalAlloc flag: movable memory (required for clipboard ownership transfer).
GMEM_MOVEABLE = 0x0002

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


def _setup_ctypes() -> None:
    """Declare 64-bit-safe argtypes/restypes for clipboard Win32 APIs.

    Functions returning a HANDLE/HGLOBAL/pointer must use ``c_void_p`` (or an
    explicit pointer type) so the return value occupies a full pointer width;
    leaving them as the default ``c_int`` truncates the high 32 bits on 64-bit
    builds and yields invalid handles.
    """
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p

    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL

    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p

    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD


_setup_ctypes()


def _open_clipboard(retries: int = 5, backoff: float = 0.02) -> bool:
    """Open the clipboard, retrying when another process holds it.

    Returns True if the clipboard was opened (caller is responsible for
    calling CloseClipboard), False if all attempts failed.
    """
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            return True
        err = ctypes.get_last_error()
        logger.debug("OpenClipboard failed (attempt %d/%d, err=%d)",
                     attempt + 1, retries, err)
        time.sleep(backoff)
    return False


def get_text() -> str | None:
    """Return the current clipboard unicode text (CF_UNICODETEXT).

    Returns the clipboard text as a ``str``, or ``None`` if the clipboard is
    empty, holds no unicode-text format, or cannot be opened/locked.
    """
    if not _open_clipboard():
        logger.warning("get_text: could not open clipboard")
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            logger.debug("get_text: GlobalLock returned NULL")
            return None
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
        return text
    finally:
        user32.CloseClipboard()


def set_text(text: str, verify: bool = True) -> bool:
    """Set the clipboard to ``text`` as CF_UNICODETEXT.

    Allocates a movable global buffer of ``(len(text) + 1) * 2`` bytes (UTF-16
    plus null terminator), copies the text in, and hands ownership to the
    clipboard via SetClipboardData. OpenClipboard is retried a few times to
    cope with transient contention.

    If ``verify`` is True (default), the value is read back and compared; the
    function returns True only on an exact match. If ``verify`` is False, it
    returns True as soon as SetClipboardData succeeds.

    Returns False on any failure (allocation, open, or verification mismatch).
    """
    if text is None:
        text = ""

    # UTF-16-LE encoding; reserve room for the 2-byte null terminator.
    # Size is based on the encoded byte length (not the Python code-point
    # count) so that astral-plane characters encoded as surrogate pairs
    # (two UTF-16 code units) are sized correctly.
    encoded = text.encode("utf-16-le")
    buf_size = len(encoded) + 2  # + 2-byte UTF-16 null terminator

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, buf_size)
    if not handle:
        logger.error("set_text: GlobalAlloc failed (err=%d)",
                     ctypes.get_last_error())
        return False

    # Fill the buffer. If anything fails before SetClipboardData succeeds, we
    # must free the buffer ourselves; once SetClipboardData succeeds the system
    # owns it and we must NOT free it.
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        logger.error("set_text: GlobalLock failed (err=%d)",
                     ctypes.get_last_error())
        kernel32.GlobalFree(handle)
        return False
    try:
        if encoded:
            ctypes.memmove(ptr, encoded, len(encoded))
        # Null terminator (2 bytes) right after the encoded text.
        ctypes.memset(ptr + len(encoded), 0, 2)
    finally:
        kernel32.GlobalUnlock(handle)

    if not _open_clipboard():
        logger.warning("set_text: could not open clipboard")
        return False

    transferred = False
    try:
        user32.EmptyClipboard()
        if user32.SetClipboardData(CF_UNICODETEXT, handle):
            transferred = True
        else:
            logger.error("set_text: SetClipboardData failed (err=%d)",
                         ctypes.get_last_error())
    finally:
        user32.CloseClipboard()

    if not transferred:
        return False

    if not verify:
        return True

    read_back = get_text()
    if read_back == text:
        return True
    logger.warning("set_text: verification mismatch (set %d chars, read %r)",
                   len(text), None if read_back is None else len(read_back))
    return False


def sequence_number() -> int:
    """Return the clipboard sequence number (GetClipboardSequenceNumber).

    The value increments each time the clipboard contents change, which is a
    cheap way to detect modifications without opening the clipboard. Returns 0
    if the API is unavailable or reports no value.
    """
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception as exc:  # pragma: no cover - defensive
        print(f"sequence_number failed: {exc}", file=sys.stderr)
        return 0
