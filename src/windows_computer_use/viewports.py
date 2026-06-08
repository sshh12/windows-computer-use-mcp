"""Named viewports: persistent, reusable sub-regions of an app.

A *viewport* is a named crop of a target (e.g. a game's canvas inside a browser window,
excluding the ads/chrome around it). Once defined it gives the agent a stable, zoomed-in
coordinate system focused on just the region it cares about, shared by every capture/input
tool via the target string ``viewport:<name>``.

A window-anchored viewport stores the crop as FRACTIONS of the window's rect, so re-resolving
the live window each use makes it track the window as it moves/resizes (and absorbs per-monitor
DPI changes — fractions are dimensionless). A screen-anchored viewport stores an absolute rect.

``resolve_spec`` returns a capture spec consumed by ``targets.resolve``:
  window-anchored -> {"kind":"window_region","hwnd","frac":(fx,fy,fw,fh),"process","label"}
  screen-anchored -> {"kind":"rect","rect":{left,top,width,height},"label"}
"""
from dataclasses import dataclass
from threading import Lock

from . import winfind


class ViewportError(ValueError):
    pass


@dataclass
class Viewport:
    name: str
    anchor: str  # "window" | "screen"
    # window-anchored:
    hwnd: int | None = None
    title: str = ""
    process: str = ""
    frac: tuple[float, float, float, float] | None = None  # (fx, fy, fw, fh) of the window rect
    # screen-anchored:
    rect: dict | None = None  # {left, top, width, height} absolute physical px


_registry: dict[str, Viewport] = {}
_lock = Lock()


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def define_window_viewport(name: str, hwnd: int, title: str, process: str,
                           frac: tuple[float, float, float, float]) -> Viewport:
    fx, fy, fw, fh = (_clamp01(v) for v in frac)
    # keep width/height within the window from the (clamped) origin
    fw = min(fw, 1.0 - fx)
    fh = min(fh, 1.0 - fy)
    vp = Viewport(name=name, anchor="window", hwnd=int(hwnd), title=title or "",
                  process=process or "", frac=(fx, fy, fw, fh))
    with _lock:
        _registry[name] = vp
    return vp


def define_screen_viewport(name: str, rect: dict) -> Viewport:
    r = {"left": int(rect["left"]), "top": int(rect["top"]),
         "width": int(rect["width"]), "height": int(rect["height"])}
    vp = Viewport(name=name, anchor="screen", rect=r)
    with _lock:
        _registry[name] = vp
    return vp


def get(name: str) -> Viewport | None:
    with _lock:
        return _registry.get(name)


def clear(name: str) -> bool:
    with _lock:
        return _registry.pop(name, None) is not None


def summaries() -> list[dict]:
    """Serializable summaries of all defined viewports (for the `system` tool)."""
    with _lock:
        vps = [v for v in _registry.values()]
    out: list[dict] = []
    for vp in vps:
        if vp.anchor == "window":
            fx, fy, fw, fh = vp.frac or (0, 0, 1, 1)
            out.append({"name": vp.name, "anchor": "window", "window": vp.title[:50],
                        "process": vp.process,
                        "frac": [round(fx, 4), round(fy, 4), round(fw, 4), round(fh, 4)]})
        else:
            out.append({"name": vp.name, "anchor": "screen", "rect": vp.rect})
    return out


def resolve_spec(name: str) -> dict:
    """Resolve a named viewport to a live capture spec (re-finding the window each time)."""
    vp = get(name)
    if vp is None:
        raise ViewportError(f"no viewport named {name!r} (define one via screenshot "
                            f"define_viewport=, or list with system action='viewports')")
    if vp.anchor == "screen":
        return {"kind": "rect", "rect": vp.rect, "label": f"viewport: {name}"}
    # window-anchored: re-find the live window (hwnd first, then title/process)
    live = None
    for w in winfind.list_windows():
        if w["hwnd"] == vp.hwnd:
            live = w
            break
    if live is None:
        hits = winfind.find_windows(vp.title or vp.process)
        live = hits[0] if hits else None
        if live is not None:
            vp.hwnd = live["hwnd"]  # adopt the new handle for next time
    if live is None:
        raise ViewportError(f"viewport {name!r}: its window ({vp.title or vp.process!r}) is no "
                            f"longer open — redefine it")
    return {"kind": "window_region", "hwnd": live["hwnd"], "frac": vp.frac,
            "process": live.get("process"), "label": f"viewport: {name}"}
