"""Environment reads: monitor layout and cursor position."""
from . import displays as _displays


def displays() -> dict:
    mons = _displays.list_monitors()
    vb = _displays.virtual_bounds()
    leftmost = min(mons, key=lambda m: m["left"])["index"] if mons else None
    rightmost = max(mons, key=lambda m: m["left"])["index"] if mons else None
    rows = []
    for m in mons:
        roles = []
        if m["primary"]:
            roles.append("primary")
        if len(mons) > 1 and m["index"] == leftmost:
            roles.append("left")
        if len(mons) > 1 and m["index"] == rightmost:
            roles.append("right")
        rows.append({
            "index": m["index"],
            "role": "/".join(roles) or "secondary",
            "resolution": f"{m['width']}x{m['height']}",
            "scale": f"{int(round(m['scale'] * 100))}%",
            "origin": [m["left"], m["top"]],
        })
    return {"monitors": rows, "virtual_bounds": vb, "count": len(mons),
            "note": "displays are 0-indexed; target them as display:N or display:primary|left|right"}


def cursor() -> dict:
    x, y = _displays.cursor_pos()
    mon = None
    for m in _displays.list_monitors():
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            mon = m["index"]
            break
    return {"x": x, "y": y, "monitor": mon}
