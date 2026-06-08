"""Resolve a `target` string (+ optional region) into a concrete capture spec.

Grammar (shared by screenshot / record / play):
  "desktop"                     -> whole virtual desktop (all monitors)
  "display:N" | "display:0"     -> monitor N (0-indexed)
  "display:primary|left|right"  -> monitor by spatial role
  "window:<title|process|hwnd>" -> a specific window (PrintWindow capture)
  "foreground"                  -> the current foreground window
  "<bare title>"                -> treated as window:<bare title>
  "region"                      -> sub-rect given by `region` in `region_space`
  "viewport:<name>"             -> a previously-defined named viewport (see viewports.py)

Returns one of:
  {"kind": "rect",          "rect": {left,top,width,height}, "label": str}
  {"kind": "window",        "hwnd": int, "rect": {...}, "label": str}
  {"kind": "window_region", "hwnd": int, "frac": (fx,fy,fw,fh), "process": str, "label": str}
    (a window-anchored viewport: capture PrintWindow's the window, then crops to the fractions)
"""
from . import coords, displays, viewports, winfind


class TargetError(ValueError):
    pass


def _rect_of_monitor(sel: str) -> tuple[dict, str]:
    mons = displays.list_monitors()
    if not mons:
        raise TargetError("no monitors found")
    sel = sel.strip().lower()
    if sel in ("primary", "main"):
        m = next((x for x in mons if x["primary"]), mons[0])
    elif sel == "left":
        m = min(mons, key=lambda x: x["left"])
    elif sel == "right":
        m = max(mons, key=lambda x: x["left"])
    else:
        try:
            idx = int(sel)
        except ValueError:
            raise TargetError(f"bad display selector {sel!r} (use N, primary, left, right)")
        if idx < 0 or idx >= len(mons):
            raise TargetError(f"display:{idx} out of range (have {len(mons)} monitor(s))")
        m = mons[idx]
    rect = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
    return rect, f"display:{m['index']}{' (primary)' if m['primary'] else ''}"


def _norm_region(region) -> tuple[int, int, int, int]:
    if region is None:
        raise TargetError("target='region' requires a region {x,y,w,h}")
    def g(*ks):
        return next((region[k] for k in ks if k in region), None)
    x, y = g("x", "left"), g("y", "top")
    w, h = g("w", "width"), g("h", "height")
    if None in (x, y, w, h):
        raise TargetError("region must have x,y,w,h (or left,top,width,height)")
    return int(x), int(y), int(w), int(h)


def _resolve_region(region, region_space: str) -> dict:
    x, y, w, h = _norm_region(region)
    if region_space == "screen":
        rect = {"left": x, "top": y, "width": w, "height": h}
    else:  # "image": relative to the last screenshot
        gm = coords.get_last_capture()
        if not gm.valid:
            raise TargetError("region_space='image' but no prior screenshot to anchor to; "
                              "take a screenshot first or use region_space='screen'")
        sx = gm.actual_width / gm.rendered_width
        sy = gm.actual_height / gm.rendered_height
        rect = {"left": int(round(gm.origin_x + x * sx)), "top": int(round(gm.origin_y + y * sy)),
                "width": int(round(w * sx)), "height": int(round(h * sy))}
    return {"kind": "rect", "rect": rect, "label": "region"}


def _resolve_window(query: str) -> dict:
    matches = winfind.find_windows(query)
    if not matches:
        raise TargetError(f"no visible window matching {query!r} "
                          f"(use the window tool's list action to see options)")
    w = matches[0]
    return {"kind": "window", "hwnd": w["hwnd"], "rect": w["rect"],
            "label": f"window: {w['title'][:60]}"}


def resolve(target: str = "desktop", region=None, region_space: str = "image") -> dict:
    t = (target or "desktop").strip()
    low = t.lower()
    if low in ("desktop", "all", "virtual", "screen"):
        vb = displays.virtual_bounds()
        rect = {"left": vb["left"], "top": vb["top"], "width": vb["width"], "height": vb["height"]}
        return {"kind": "rect", "rect": rect, "label": f"desktop ({vb['count']} monitor(s))"}
    if low == "region":
        return _resolve_region(region, region_space)
    if low.startswith("viewport:"):
        # the viewport already IS the region; region/region_space are ignored here
        return viewports.resolve_spec(t.split(":", 1)[1].strip())
    if low == "foreground":
        hwnd = winfind.foreground_window()
        if not hwnd:
            raise TargetError("no foreground window")
        wins = [w for w in winfind.list_windows() if w["hwnd"] == hwnd]
        rect = wins[0]["rect"] if wins else None
        label = f"foreground: {wins[0]['title'][:60]}" if wins else "foreground"
        return {"kind": "window", "hwnd": hwnd, "rect": rect, "label": label}
    if low.startswith("display:"):
        rect, label = _rect_of_monitor(t.split(":", 1)[1])
        return {"kind": "rect", "rect": rect, "label": label}
    if low.startswith("window:"):
        return _resolve_window(t.split(":", 1)[1])
    # bare string -> window query
    return _resolve_window(t)
