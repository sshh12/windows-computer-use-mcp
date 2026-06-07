"""The `window` tool: find / focus / read / click controls with no screenshot.

`list`/`focus` use plain Win32 (winfind). `get_text`/`ui_tree`/`click_element` use UI
Automation (the `uiautomation` package) so we can read and click controls by accessible
name — far cheaper and more reliable than screenshot + pixel-hunting. `get_text` falls
back to Windows OCR when a window exposes no UIA text.
"""
import sys
import time

from . import capture, ocr, winfind
from . import input as winput

_MAX_NODES = 600


def _auto():
    import uiautomation as auto  # lazy: COM init happens inside the lib
    return auto


def _control_from_query(query):
    matches = winfind.find_windows(query)
    if not matches:
        return None, None
    w = matches[0]
    try:
        ctrl = _auto().ControlFromHandle(w["hwnd"])
    except Exception as e:  # noqa: BLE001
        print(f"[wcu] UIA ControlFromHandle failed: {e}", file=sys.stderr)
        ctrl = None
    return ctrl, w


def list_windows(query=None, limit: int = 15, details: bool = False) -> dict:
    wins = winfind.find_windows(query) if query else winfind.list_windows()
    rows = []
    for w in wins[:limit]:
        row = {"title": w["title"], "process": w["process"],
               "monitor": w["monitor"], "foreground": w["foreground"]}
        if details:
            row["hwnd"] = w["hwnd"]
            row["rect"] = w["rect"]
        rows.append(row)
    return {"windows": rows, "shown": len(rows), "total": len(wins)}


def focus(query) -> dict:
    matches = winfind.find_windows(query)
    if not matches:
        return {"error": f"no window matching {query!r}"}
    w = matches[0]
    return {"focused": w["title"], "ok": winfind.foreground(w["hwnd"])}


def close(query) -> dict:
    matches = winfind.find_windows(query)
    if not matches:
        return {"error": f"no window matching {query!r}"}
    w = matches[0]
    return {"closed": w["title"], "ok": winfind.close_window(w["hwnd"])}


def _walk_text(ctrl, out: list, budget: list):
    if budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        name = (ctrl.Name or "").strip()
        if name:
            out.append(name)
        # ValuePattern (text boxes, address bars) carries content not in Name
        try:
            vp = ctrl.GetValuePattern()
            val = (vp.Value or "").strip()
            if val and val != name:
                out.append(val)
        except Exception:
            pass
        # TextPattern (Notepad/RichEdit/document bodies) — the main editable content
        try:
            tp = ctrl.GetTextPattern()
            body = (tp.DocumentRange.GetText(8000) or "").strip()
            if body and body != name:
                out.append(body)
        except Exception:
            pass
        for child in ctrl.GetChildren():
            _walk_text(child, out, budget)
    except Exception:
        pass


def get_text(query=None, max_chars: int = 20000) -> dict:
    q = query if query else str(winfind.foreground_window())
    ctrl, w = _control_from_query(q)
    title = w["title"] if w else ""
    if ctrl is not None:
        out: list[str] = []
        _walk_text(ctrl, out, [_MAX_NODES])
        text = "\n".join(dict.fromkeys(out))  # dedupe, preserve order
        if text.strip():
            return {"text": text[:max_chars], "source": "uia", "window": title,
                    "truncated": len(text) > max_chars}
    # OCR fallback
    matches = winfind.find_windows(q)
    if matches and ocr.available():
        try:
            img, _r, _ok = capture.grab_window(matches[0]["hwnd"])
            text = ocr.ocr_image(img)
            return {"text": text[:max_chars], "source": "ocr", "window": matches[0]["title"]}
        except Exception as e:  # noqa: BLE001
            print(f"[wcu] OCR fallback failed: {e}", file=sys.stderr)
    return {"text": "", "window": title, "error": "no readable text (UIA empty, OCR unavailable)"}


def _walk_tree(ctrl, depth: int, max_depth: int, budget: list) -> dict | None:
    if budget[0] <= 0 or depth > max_depth:
        return None
    budget[0] -= 1
    try:
        r = ctrl.BoundingRectangle
        node = {
            "name": (ctrl.Name or "")[:80],
            "role": ctrl.ControlTypeName.replace("Control", ""),
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
        }
        if depth < max_depth:
            kids = []
            for child in ctrl.GetChildren():
                cn = _walk_tree(child, depth + 1, max_depth, budget)
                if cn:
                    kids.append(cn)
            if kids:
                node["children"] = kids
        return node
    except Exception:
        return None


def ui_tree(query=None, depth: int = 4) -> dict:
    q = query if query else str(winfind.foreground_window())
    ctrl, w = _control_from_query(q)
    if ctrl is None:
        return {"error": f"no UIA tree for {query!r}"}
    tree = _walk_tree(ctrl, 0, depth, [_MAX_NODES])
    return {"window": w["title"] if w else "", "tree": tree}


def _find_elements(ctrl, name: str, role: str | None, found: list, budget: list):
    if budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        nm = ctrl.Name or ""
        if name.lower() in nm.lower():
            if not role or role.lower() in ctrl.ControlTypeName.lower():
                found.append(ctrl)
        for child in ctrl.GetChildren():
            _find_elements(child, name, role, found, budget)
    except Exception:
        pass


def click_element(query: str, role: str | None = None, nth: int = 0, window=None,
                  button: str = "left") -> dict:
    root_q = window if window else str(winfind.foreground_window())
    ctrl, w = _control_from_query(root_q)
    if ctrl is None:
        return {"error": "no foreground/UIA window to search"}
    found: list = []
    _find_elements(ctrl, query, role, found, [_MAX_NODES])
    if not found:
        return {"error": f"no control matching name~{query!r}"
                + (f" role~{role!r}" if role else "") + f" in {w['title']!r}"}
    if nth >= len(found):
        return {"error": f"nth={nth} out of range ({len(found)} match(es))"}
    target = found[nth]
    name, roletype = (target.Name or "")[:60], target.ControlTypeName
    # Prefer a programmatic invoke — works even if the window is NOT foreground (the common
    # case, since the controlling terminal usually holds focus). Falls back to a real click.
    for getter, fire in (("GetInvokePattern", lambda p: p.Invoke()),
                         ("GetTogglePattern", lambda p: p.Toggle()),
                         ("GetSelectionItemPattern", lambda p: p.Select())):
        try:
            pat = getattr(target, getter)()
            if pat:
                fire(pat)
                return {"invoked": name, "role": roletype, "via": getter[3:-7], "matches": len(found)}
        except Exception:
            continue
    try:
        if window:  # bring the target forward so the physical click lands on it
            winfind.foreground(w["hwnd"])
            time.sleep(0.15)
        r = target.BoundingRectangle
        cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        winput.click(button, x=cx, y=cy)
        return {"clicked": name, "role": roletype, "at": [cx, cy], "matches": len(found)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"click failed: {e}"}
