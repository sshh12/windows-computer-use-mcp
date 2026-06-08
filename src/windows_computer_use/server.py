"""FastMCP server: the 7-tool Windows computer-use surface.

stdout is the JSON-RPC channel — everything logs to stderr. Tools that produce images
return content blocks (text + inline downscaled image); large artifacts (video, full-res,
big text) are written to an MCP root and returned as a path / resource_link.
"""
import base64
import sys
import time

from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from PIL import ImageStat

from . import artifacts, capture, clipboard, coords, debughtml, images, playscript, targets, video, viewports, winfind
from . import process as process_mod
from . import system as system_mod
from . import windows as windows_tool

mcp = FastMCP("windows-computer-use")

# Remember the last screenshot target so `act`'s trailing screenshot re-captures the
# same view (keeping the agent's coordinate frame continuous).
_LAST_TARGET = {"target": "desktop", "region": None, "region_space": "image"}
_MOTION_ACTIONS = {"mouse_move", "move", "scroll", "mouse_move_relative",
                   "wait", "left_mouse_up", "mouse_up"}


# --------------------------------------------------------------------------- helpers
def _text(s: str) -> types.TextContent:
    return types.TextContent(type="text", text=s)


def _image(data: bytes, mime: str) -> types.ImageContent:
    return types.ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime)


def _link(path, name: str, mime: str, desc: str | None = None):
    try:
        return types.ResourceLink(type="resource_link", uri=artifacts.file_uri(path),
                                  name=name, mimeType=mime, description=desc)
    except Exception:
        return None  # older mcp without ResourceLink; the path is already in the text block


def _looks_black(img) -> bool:
    try:
        g = img.convert("L").resize((48, 48))
        return ImageStat.Stat(g).extrema[0][1] < 8
    except Exception:
        return False


def _capture_target(target, region, region_space, max_dim, capture_mode, foreground):
    """Capture a target, downscale, set the clickable frame. Returns a dict of pieces."""
    spec = targets.resolve(target, region, region_space)
    warning = None
    if spec["kind"] == "window":
        hwnd = spec["hwnd"]
        if foreground:
            winfind.foreground(hwnd)
            time.sleep(0.18)
        img, rect, ok = capture.grab_window(hwnd)
        if _looks_black(img) and capture_mode in ("auto", "bitblt"):
            winfind.foreground(hwnd)
            time.sleep(0.22)
            alt = capture.grab_rect(rect["left"], rect["top"], rect["width"], rect["height"])
            if not _looks_black(alt):
                img, warning = alt, "PrintWindow returned black; used on-screen BitBlt fallback"
            else:
                warning = "capture looks black (fullscreen-exclusive or DRM-protected surface?)"
    elif spec["kind"] == "window_region":  # a window-anchored viewport: PrintWindow + crop
        hwnd = spec["hwnd"]
        if foreground:
            winfind.foreground(hwnd)
            time.sleep(0.18)
        img, rect, ok = capture.grab_window_region(hwnd, spec["frac"])
        if _looks_black(img) and capture_mode in ("auto", "bitblt"):
            winfind.foreground(hwnd)
            time.sleep(0.22)
            alt = capture.grab_rect(rect["left"], rect["top"], rect["width"], rect["height"])
            if not _looks_black(alt):
                img, warning = alt, "PrintWindow returned black; used on-screen BitBlt fallback"
            else:
                warning = "viewport capture looks black (window occluded? bring it forward)"
    else:
        rect = spec["rect"]
        img = capture.grab_rect(rect["left"], rect["top"], rect["width"], rect["height"])
    small, scale = images.downscale(img, max_dim if max_dim else None)
    cid = coords.new_capture_id()
    coords.set_last_capture(coords.CaptureGeometry(
        origin_x=rect["left"], origin_y=rect["top"],
        actual_width=rect["width"], actual_height=rect["height"],
        rendered_width=small.size[0], rendered_height=small.size[1],
        label=spec["label"], capture_id=cid))
    return {"full": img, "small": small, "scale": scale, "rect": rect,
            "label": spec["label"], "capture_id": cid, "warning": warning}


# --------------------------------------------------------------------------- screenshot
def _define_viewport(name: str, target: str, region, region_space: str) -> str | None:
    """Store a named viewport from (target, region). Returns an optional warning string.
    Window targets -> fraction-anchored to the window; desktop/display -> screen-anchored rect."""
    base = targets.resolve(target)
    warn = None
    if base["kind"] in ("window", "window_region"):
        hwnd = base["hwnd"]
        win = next((w for w in winfind.list_windows() if w["hwnd"] == hwnd), None)
        if win is None:
            raise targets.TargetError(f"viewport define: window for {target!r} not found")
        wrect, title, process = win["rect"], win["title"], win.get("process", "")
        if region is None:
            frac = (0.0, 0.0, 1.0, 1.0)
        else:
            r = targets._resolve_region(region, region_space)["rect"]
            gm = coords.get_last_capture()
            if region_space == "image" and (abs(gm.origin_x - wrect["left"]) > 4
                                            or abs(gm.origin_y - wrect["top"]) > 4):
                warn = ("region was given in image space but the last screenshot wasn't this "
                        "window — screenshot the window first, or pass region_space='screen'")
            frac = ((r["left"] - wrect["left"]) / wrect["width"],
                    (r["top"] - wrect["top"]) / wrect["height"],
                    r["width"] / wrect["width"], r["height"] / wrect["height"])
        viewports.define_window_viewport(name, hwnd, title, process, frac)
    else:  # desktop / display -> screen-anchored absolute rect
        rect = targets._resolve_region(region, region_space)["rect"] if region else base["rect"]
        viewports.define_screen_viewport(name, rect)
    return warn


@mcp.tool()
async def screenshot(target: str = "desktop", region: dict | None = None,
                     region_space: str = "image", max_dim: int = 1568,
                     format: str = "png", capture_mode: str = "auto",
                     foreground: bool = False, save: bool = False,
                     define_viewport: str | None = None,
                     ctx: Context = None) -> list:
    """See the screen as an image. Targets: "desktop" (all monitors), "display:N" or
    "display:primary|left|right", "window:<title|process|hwnd>", a bare window title,
    "foreground", "region" (+region {x,y,w,h} in region_space image|screen), or
    "viewport:<name>" (a named crop — see define_viewport below).

    Do NOT use this just to read text — use `window` get_text (~10x fewer tokens). This
    sets the coordinate frame for later `act`/`play` clicks; any newer capture invalidates
    earlier image coordinates. Pass format="jpeg" for 3D/game/photo, max_dim=0 for tiny text.
    Window targets are captured in place (PrintWindow) WITHOUT bringing them to the front, so a
    screenshot never steals keyboard focus from a game/canvas; pass foreground=true only if a
    background window captures black.

    VIEWPORTS — focus the coordinate system on just the part of an app you care about (e.g. a
    game's canvas inside a browser, excluding ads/chrome): screenshot the window, then call again
    with the canvas as `region` plus define_viewport="game". That crops, names, and returns the
    cropped frame, which becomes the active coordinate space — `act` then clicks/keys inside it
    automatically, and screenshots stay zoomed-in (sharper clicks, fewer tokens). Re-capture later
    with target="viewport:game" (record/play accept it too); the viewport tracks the window as it
    moves/resizes. Manage with `system` action='viewports' / 'clear_viewport'."""
    vp_warn = None
    if define_viewport:
        vp_warn = _define_viewport(define_viewport, target, region, region_space)
        target, region, region_space = f"viewport:{define_viewport}", None, "image"
    _LAST_TARGET.update(target=target, region=region, region_space=region_space)
    c = _capture_target(target, region, region_space, max_dim, capture_mode, foreground)
    data, mime = images.encode(c["small"], "jpeg" if format == "jpeg" else "png")
    inv = (1 / c["scale"]) if c["scale"] else 1.0
    lines = [f"capture #{c['capture_id']} · {c['label']} · rendered {c['small'].size[0]}x"
             f"{c['small'].size[1]} (scale {c['scale']:.3f}; 1 image px ≈ {inv:.2f} screen px) · "
             f"origin ({c['rect']['left']},{c['rect']['top']}) size {c['rect']['width']}x{c['rect']['height']}"]
    if define_viewport:
        lines.append(f"✓ viewport '{define_viewport}' defined and now active — act/screenshot/"
                     f"record/play target=\"viewport:{define_viewport}\" all use this frame")
    if vp_warn:
        lines.append(f"⚠ {vp_warn}")
    if c["warning"]:
        lines.append(f"⚠ {c['warning']}")
    out = [_text("\n".join(lines)), _image(data, mime)]
    if save:
        outdir = await artifacts.resolve_output_dir(ctx)
        p = outdir / f"screenshot_{time.strftime('%Y%m%d-%H%M%S')}.png"
        c["full"].save(p)
        lines.append(f"full-res saved: {p}")
        out[0] = _text("\n".join(lines))
        lk = _link(p, p.name, "image/png", "full-resolution screenshot")
        if lk:
            out.append(lk)
    return out


# --------------------------------------------------------------------------- act
def _xy(a: dict):
    if a.get("coordinate"):
        return a["coordinate"][0], a["coordinate"][1]
    return a.get("x"), a.get("y")


def _screen_xy(a: dict, space: str, cid):
    x, y = _xy(a)
    if x is None or y is None:
        return None, None
    return coords.resolve_point(x, y, space, expected_capture_id=cid)


def _dispatch(a: dict, space: str, cid, kb_targets=None, kb_via: str = "scancode") -> str:
    from . import input as winput
    act = (a.get("action") or a.get("type") or "").lower()
    msg = kb_via == "message" and bool(kb_targets)
    px, py = _screen_xy(a, space, cid)
    mods = a.get("modifiers")
    if act in ("mouse_move", "move"):
        winput.move(px, py); return f"mouse_move ({px},{py})"
    if act in ("left_click", "right_click", "middle_click", "click"):
        btn = {"left_click": "left", "right_click": "right", "middle_click": "middle"}.get(act, a.get("button", "left"))
        winput.click(btn, x=px, y=py, modifiers=mods); return f"{btn}_click ({px},{py})" + (f"+{mods}" if mods else "")
    if act == "double_click":
        winput.click("left", count=2, x=px, y=py, interval=0.05); return f"double_click ({px},{py})"
    if act == "triple_click":
        winput.click("left", count=3, x=px, y=py, interval=0.05); return f"triple_click ({px},{py})"
    if act in ("left_mouse_down", "mouse_down"):
        if px is not None: winput.move(px, py)
        winput.button_down(a.get("button", "left")); return "mouse_down"
    if act in ("left_mouse_up", "mouse_up"):
        if px is not None: winput.move(px, py)
        winput.button_up(a.get("button", "left")); return "mouse_up"
    if act == "drag":
        to = a.get("to") or {}
        tx, ty = coords.resolve_point(to.get("x", to.get("coordinate", [0, 0])[0] if to.get("coordinate") else 0),
                                      to.get("y", to.get("coordinate", [0, 0])[1] if to.get("coordinate") else 0),
                                      space, expected_capture_id=cid)
        path = None
        if a.get("path"):
            path = [coords.resolve_point(p[0], p[1], space, expected_capture_id=cid) for p in a["path"]]
        winput.drag(px, py, tx, ty, button=a.get("button", "left"),
                    duration=a.get("duration", 0.2), press_dwell_ms=a.get("press_dwell_ms", 60),
                    path=path); return f"drag ({px},{py})->({tx},{ty})"
    if act == "scroll":
        amt = int(a.get("scroll_amount", a.get("amount", 3)))
        direction = (a.get("scroll_direction") or a.get("direction") or "down").lower()
        horiz = direction in ("left", "right")
        signed = amt if direction in ("up", "right") else -amt
        if px is not None: winput.move(px, py)
        winput.scroll(signed, horizontal=horiz); return f"scroll {direction} {amt}"
    if act == "mouse_move_relative":
        winput.move_relative(int(a.get("dx", 0)), int(a.get("dy", 0))); return f"mouse_move_relative ({a.get('dx')},{a.get('dy')})"
    if act == "key":
        combo = a.get("text") or a.get("keys")
        # message-mode: hold the key briefly between down/up — a 0ms press is invisible to games
        # that only register a key that is down across a frame (the SendInput path is naturally slower).
        winput.post_combo(kb_targets, combo, hold=0.05) if msg else winput.press(combo)
        return f"key {combo}" + (" [msg]" if msg else "")
    if act == "hold_key":
        combo = a.get("text") or a.get("keys"); dur = float(a.get("duration", 1.0))
        winput.post_hold(kb_targets, combo, dur) if msg else winput.hold(combo, dur)
        return f"hold_key {combo} {dur}s" + (" [msg]" if msg else "")
    if act == "type":
        text = a.get("text", ""); literal = bool(a.get("literal", False))
        winput.post_text(kb_targets, text, literal=literal) if msg else winput.type_text(text, literal=literal)
        return f"type {len(text)} chars" + (" [msg]" if msg else "")
    if act == "paste":
        # ctrl+v is a modifier chord; posted-message modifiers are unreliable, so paste always
        # uses SendInput (needs foreground). For a browser field, prefer `type` (WM_CHAR) instead.
        clipboard.set_text(a.get("text", "")); winput.press("ctrl+v"); return f"paste {len(a.get('text',''))} chars"
    if act == "wait":
        time.sleep(float(a.get("duration", 0.5))); return f"wait {a.get('duration')}s"
    if act == "click_element":
        r = windows_tool.click_element(a.get("query", ""), a.get("role"), int(a.get("nth", 0)),
                                       window=a.get("window"))
        return f"click_element {r}"
    return f"UNKNOWN action {act!r}"


def _foreground_title() -> str:
    for w in winfind.list_windows():
        if w["foreground"]:
            return w["title"]
    return "(unknown)"


_KEYBOARD_ACTIONS = {"type", "key", "hold_key", "paste"}
# Pointer actions that focus a web canvas. In message-mode, posted keys only reach a browser
# game if the canvas has DOM focus, and that focus needs a beat to register after the click —
# so we settle briefly after a click before posting keys batched behind it.
_POINTER_ACTIONS = {"left_click", "click", "double_click", "triple_click", "left_mouse_down", "mouse_down"}


def _resolve_keyboard(focus: str | None, key_method: str):
    """Decide how keyboard actions are injected. Returns (targets, via, title).
    - "scancode": SendInput hardware scan codes (foreground+focus required) — native apps, games.
    - "message":  PostMessage WM_KEY*/WM_CHAR to the target's render-widget child (no focus
      needed) — the only thing that reaches a browser web/canvas/Flash game, where the top-level
      frame holds focus instead of the render widget.
    "auto" picks message for browser windows, scancode otherwise."""
    if key_method == "scancode":
        return None, "scancode", None
    win = None
    if focus:
        hits = winfind.find_windows(focus)
        win = hits[0] if hits else None
    if win is None:
        win = next((w for w in winfind.list_windows() if w["foreground"]), None)
    if win is None:
        return None, "scancode", None
    want_msg = key_method == "message" or (key_method == "auto" and winfind.is_browser(win.get("process")))
    if want_msg:
        return winfind.keyboard_targets(win["hwnd"]), "message", win["title"]
    return None, "scancode", win["title"]


@mcp.tool()
async def act(actions: list[dict], coordinate_space: str = "image",
              capture_id: int | None = None, screenshot: bool | str = True,
              settle_ms: int = 80, focus: str | None = None,
              key_method: str = "auto", ctx: Context = None) -> list:
    """Perform input actions in order. Items: {"action": <name>, ...} with native names —
    left_click/right_click/double_click/mouse_move {x,y}, left_mouse_down/up, scroll
    {scroll_direction,scroll_amount}, drag {x,y,to:{x,y},path?}, key {text:"ctrl+s"},
    hold_key {text,duration}, type {text}, paste {text}, mouse_move_relative {dx,dy},
    click_element {query,role?,nth?,window?}, wait {duration}. Coords ([x,y] or {x,y}) are in
    the last screenshot's image space — if that was a viewport (a named app-sub-region; see
    `screenshot` define_viewport), clicks/keys operate inside that cropped frame automatically.
    Pass focus="<window>" to target a specific window first (this overrides any active viewport).
    key_method routes keyboard input: "auto" (default) sends scan codes to native apps/games but
    auto-switches to window-messages for BROWSER windows (the only thing that reaches a web/canvas/
    Flash game like coolmathgames — left_click the canvas once first to give it focus); force with
    "message" or "scancode".
    Batch related steps to save round-trips; for timed game input use `play`; to click a labeled
    control without vision use click_element. screenshot: true|false|"text" (UIA text)."""
    if focus:
        fr = windows_tool.focus(focus)
        if fr.get("error"):
            return [_text(f"focus({focus!r}) failed: {fr['error']}. Run `window list` for exact titles.")]
        time.sleep(0.18)
    # Keyboard routing is resolved LAZILY at the first key action — a click earlier in the same
    # batch (to focus a game canvas) changes the foreground window, and the route must reflect that.
    kbr = {"done": False, "targets": None, "via": "scancode", "title": None}

    def _kb():
        if not kbr["done"]:
            t, v, ti = _resolve_keyboard(focus, (key_method or "auto").lower())
            kbr.update(done=True, targets=t, via=v, title=ti)
        return kbr

    def _keys_follow(i):
        return any((b.get("action") or b.get("type") or "").lower() in _KEYBOARD_ACTIONS
                   for b in actions[i + 1:])

    log = []
    try:
        for i, a in enumerate(actions):
            name = (a.get("action") or a.get("type") or "").lower()
            if name in _KEYBOARD_ACTIONS:
                k = _kb()
                log.append(_dispatch(a, coordinate_space, capture_id, k["targets"], k["via"]))
            else:
                log.append(_dispatch(a, coordinate_space, capture_id))
                # settle after a click that precedes keys, so its DOM/canvas focus registers first
                if name in _POINTER_ACTIONS and _keys_follow(i):
                    time.sleep(0.2)
    except coords.CaptureMismatch as e:
        return [_text(f"ABORTED (stale frame): {e}\nExecuted before abort:\n" + "\n".join(log))]
    except Exception as e:  # noqa: BLE001
        return [_text(f"ERROR: {e}\nExecuted before error:\n" + "\n".join(log))]

    note = None
    if any((a.get("action") or a.get("type") or "").lower() in _KEYBOARD_ACTIONS for a in actions):
        kb_via, kb_title = kbr["via"], kbr["title"]
        if kb_via == "message":
            note = (f"keyboard posted to '{kb_title}' (browser render-widget; no focus theft). A web/"
                    "canvas/Flash game must have CANVAS focus to receive keys: left_click the game canvas"
                    " once (this batch or a prior call) — focus then persists until you click elsewhere."
                    " On-screen score/Moves is ground truth.")
        else:
            fg_win = next((w for w in winfind.list_windows() if w["foreground"]), None)
            fg = fg_win["title"] if fg_win else "(unknown)"
            note = (f"focused & sent input to '{fg}'." if focus
                    else f"keyboard input went to foreground '{fg}'. If that's not your target, pass focus=\"<title>\".")

    last = (actions[-1].get("action") or actions[-1].get("type") or "").lower() if actions else ""
    want_text = screenshot == "text"
    want_img = screenshot is True and last not in _MOTION_ACTIONS
    body = "ok:\n" + "\n".join(f"  {l}" for l in log) + (f"\n• {note}" if note else "")
    out = [_text(body)]
    if want_text:
        txt = windows_tool.get_text(focus or None)
        out.append(_text(f"text:\n{txt.get('text', '')[:4000]}"))
    elif want_img:
        time.sleep(settle_ms / 1000.0)
        tgt = f"window:{focus}" if focus else _LAST_TARGET["target"]
        try:
            c = _capture_target(tgt, None if focus else _LAST_TARGET["region"],
                                _LAST_TARGET["region_space"], 1568, "auto", False)
            data, mime = images.encode(c["small"], "png")
            out.append(_text(f"after · capture #{c['capture_id']} · {c['label']} · {c['small'].size[0]}x{c['small'].size[1]}"))
            out.append(_image(data, mime))
        except Exception as e:  # noqa: BLE001
            out.append(_text(f"(trailing screenshot failed: {e})"))
    return out


# --------------------------------------------------------------------------- record
@mcp.tool()
async def record(target: str = "desktop", seconds: float = 5.0, fps: int = 10,
                 region: dict | None = None, region_space: str = "image",
                 montage_frames: int = 6, foreground: bool = True,
                 keep_frames: bool = False, ctx: Context = None) -> list:
    """Record N seconds of a target and return a single timestamped frame montage (not N
    images) plus an mp4 path — use to judge motion/animation/stutter. For one moment use
    `screenshot`; to drive input while recording use `play`. Window targets are foregrounded
    first; a 'frames identical' warning means the target wasn't rendering. `target` may be a
    "viewport:<name>" to record just a defined app-sub-region (e.g. a game canvas)."""
    spec = targets.resolve(target, region, region_space)
    grab = video.grab_fn_for_spec(spec, foreground)
    frames, achieved = video.record(grab, seconds, fps)
    montage, warn = video.make_montage(frames, montage_frames)
    outdir = await artifacts.resolve_output_dir(ctx)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mp4 = outdir / f"record_{stamp}.mp4"
    t_enc = time.perf_counter()
    ok, msg = video.write_mp4(frames, fps, mp4)
    enc_s = time.perf_counter() - t_enc
    print(f"[wcu] record: {len(frames)} frames · encode {enc_s:.2f}s", file=sys.stderr)
    lines = [f"{spec['label']} · {len(frames)} frames · requested {fps}fps, achieved "
             f"{achieved:.1f}fps · encode {enc_s:.1f}s",
             f"mp4: {mp4}" if ok else f"mp4: FAILED ({msg})"]
    if warn:
        lines.append(f"⚠ {warn}")
    out = [_text("\n".join(lines))]
    if montage is not None:
        mdata, mmime = images.encode(montage, "jpeg", 80)
        out.append(_image(mdata, mmime))
    if ok:
        lk = _link(mp4, mp4.name, "video/mp4", f"{seconds}s screen recording")
        if lk:
            out.append(lk)
    return out


# --------------------------------------------------------------------------- play
@mcp.tool()
async def play(script: str, target: str | None = None, fps: int = 10,
               montage_frames: int = 6, probe: str | None = None,
               coordinate_space: str = "image", ctx: Context = None) -> list:
    """Drive a timed input script at a cadence while recording; returns a frame montage.
    Uses scan codes + relative mouse so games respond. Prefer `act` for ordinary UI. DSL
    (one cmd/line, +-only chords): hold <keys> <sec> | tap <keys> [n] | down/up <keys> |
    look <dx> <dy> (relative) | move <x> <y> (absolute image) | lmb|rmb|mmb [x y] |
    scroll <up|down|left|right> <amt> | type <text> | paste <text> | wait <sec> | probe |
    until <expr> | shot. With `probe` set (a shell command emitting JSON), `probe`/`until`
    read telemetry per sample and stop early (e.g. 'until dist < 1000') — the KSP loop.
    If `target` is a browser window (or a "viewport:<name>" anchored to one), keys auto-route to
    the page render-widget so canvas/Flash games (e.g. coolmathgames) receive them; mouse stays
    absolute. Screenshot the same viewport first so image-space move/click coords match it."""
    try:
        spec = targets.resolve(target or "foreground")
    except Exception:
        spec = targets.resolve("desktop")
    kb_targets = None
    if spec["kind"] in ("window", "window_region"):
        winfind.foreground(spec["hwnd"])
        time.sleep(0.2)
        # Browser windows: route keys to the page render-widget child so canvas/Flash games
        # actually receive them (SendInput keys miss — the frame, not the widget, holds focus).
        proc = spec.get("process")  # window_region carries it; plain window must look it up
        if proc is None:
            win = next((w for w in winfind.list_windows() if w["hwnd"] == spec["hwnd"]), None)
            proc = win.get("process") if win else None
        if winfind.is_browser(proc):
            kb_targets = winfind.keyboard_targets(spec["hwnd"])
    grab = video.grab_fn_for_spec(spec, foreground=True)
    t_run = time.perf_counter()
    result = playscript.run(script, grab, fps, probe_cmd=probe,
                            coordinate_space=coordinate_space, kb_targets=kb_targets)
    run_s = time.perf_counter() - t_run
    frames = result["frames"]
    montage, warn = video.make_montage(frames, montage_frames)
    outdir = await artifacts.resolve_output_dir(ctx)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mp4 = outdir / f"play_{stamp}.mp4"
    t_enc = time.perf_counter()
    ok, msg = video.write_mp4(frames, fps, mp4)
    enc_s = time.perf_counter() - t_enc
    print(f"[wcu] play: {len(frames)} frames · script+capture {run_s:.2f}s · encode {enc_s:.2f}s",
          file=sys.stderr)
    lines = [f"{spec['label']} · {len(frames)} frames · stopped_by: {result['stopped_by']} "
             f"· script+capture {run_s:.1f}s · encode {enc_s:.1f}s",
             f"mp4: {mp4}" if ok else f"mp4: FAILED ({msg})"]
    if result["samples"]:
        lines.append("probe samples: " + "; ".join(str(s) for s in result["samples"][-6:]))
    if warn:
        lines.append(f"⚠ {warn}")
    lines.append("log:\n" + "\n".join("  " + l for l in result["log"][-20:]))
    out = [_text("\n".join(lines))]
    if montage is not None:
        mdata, mmime = images.encode(montage, "jpeg", 80)
        out.append(_image(mdata, mmime))
    if ok:
        lk = _link(mp4, mp4.name, "video/mp4", "play-test recording")
        if lk:
            out.append(lk)
    return out


# --------------------------------------------------------------------------- window
@mcp.tool()
def window(action: str, query: str | None = None, limit: int = 15, details: bool = False,
           depth: int = 4, role: str | None = None, nth: int = 0, window: str | None = None) -> dict:
    """Find, focus, read, and click controls with NO screenshot (token-cheap). Actions:
    list {query?,limit,details} ; focus {query} (bring to front / activate — do this before
    typing into an app) ; close {query} (close a window via WM_CLOSE — no PID needed; the
    right way to close UWP/Store apps) ; get_text {query?} (read a window's/dialog's text via UIA, OCR
    fallback — prefer over screenshot when you only need text; works without focus) ; ui_tree
    {query?,depth} ; click_element {query=control name, role?, nth?, window?=which window}
    (invokes the control via UIA — works even when the window isn't foreground; searches the
    foreground window unless window= is given)."""
    if action == "list":
        return windows_tool.list_windows(query, limit, details)
    if action == "focus":
        return windows_tool.focus(query)
    if action == "close":
        return windows_tool.close(query)
    if action == "get_text":
        return windows_tool.get_text(query)
    if action == "ui_tree":
        return windows_tool.ui_tree(query, depth)
    if action == "click_element":
        return windows_tool.click_element(query, role, nth, window=window)
    return {"error": f"unknown window action {action!r}"}


# --------------------------------------------------------------------------- process
@mcp.tool()
async def process(action: str, exe: str | None = None, args: list | None = None,
                  cwd: str | None = None, shell: bool = False, command: str | None = None,
                  timeout: float = 60, target=None, pid=None, name: str | None = None,
                  all: bool = False, dir: str | None = None, glob: str | None = None,
                  query: str | None = None, ready: str = "input_idle",
                  shell_kind: str = "powershell", ctx: Context = None) -> dict:
    """Manage external processes and wait on readiness. Actions: launch {exe|verb|uri,args?,
    cwd?,shell?} (shell:true opens URLs / ms-settings: / documents / Store apps) ; kill
    {pid|name,all?} ; wait {pid|name,timeout} ; shell {command,timeout,shell_kind} ;
    wait_for_file {dir,glob,timeout} ; wait_for_window {query,timeout,ready}. After launch,
    use wait_for_window instead of guessing a sleep."""
    if action == "launch":
        if not exe:
            return {"error": "launch requires exe"}
        return process_mod.launch(exe, args, cwd, shell)
    if action == "kill":
        r = process_mod.kill(pid if pid is not None else (name or target), all)
        if isinstance(r, dict) and r.get("killed") == 0 and not r.get("error"):
            r["hint"] = ("0 matched. A UWP/Store app's window PID belongs to ApplicationFrameHost, "
                         "not the app — prefer `window close` by title, or `window list` for the real process.")
        return r
    if action == "wait":
        return process_mod.wait(pid if pid is not None else (name or target), timeout)
    if action == "shell":
        if not command:
            return {"error": "shell requires command"}
        r = process_mod.shell(command, timeout, shell_kind)
        for k in ("stdout", "stderr"):
            if len(r.get(k, "")) > 8000:
                outdir = await artifacts.resolve_output_dir(ctx)
                p = outdir / f"shell_{k}_{time.strftime('%H%M%S')}.txt"
                p.write_text(r[k], encoding="utf-8")
                r[k] = r[k][:1500] + f"\n…[truncated; full {k} -> {p}]"
        return r
    if action == "wait_for_file":
        if not dir or not glob:
            return {"error": "wait_for_file requires dir and glob"}
        return process_mod.wait_for_file(dir, glob, timeout)
    if action == "wait_for_window":
        q = query or name
        if not q:
            return {"error": "wait_for_window requires query"}
        return process_mod.wait_for_window(q, timeout, ready)
    return {"error": f"unknown process action {action!r}"}


# --------------------------------------------------------------------------- system
@mcp.tool()
def system(action: str, text: str | None = None) -> dict:
    """Read environment + clipboard, and manage viewports. Actions: displays (monitor
    layout/DPI/scale — call before reasoning about multi-monitor coords; displays are
    0-indexed, "monitor 2" = display:1) ; cursor ; get_clipboard ; set_clipboard {text} ;
    viewports (list named viewports defined via screenshot define_viewport) ;
    clear_viewport {text:name} (remove one)."""
    if action == "displays":
        return system_mod.displays()
    if action == "cursor":
        return system_mod.cursor()
    if action == "get_clipboard":
        return {"text": clipboard.get_text()}
    if action == "set_clipboard":
        return {"ok": clipboard.set_text(text or "")}
    if action == "viewports":
        return {"viewports": viewports.summaries()}
    if action == "clear_viewport":
        cleared = viewports.clear(text or "")
        if cleared and _LAST_TARGET["target"] == f"viewport:{text}":
            _LAST_TARGET.update(target="desktop", region=None, region_space="image")
        return {"cleared": cleared}
    return {"error": f"unknown system action {action!r}"}


# Arm the optional debug HTML dump (no-op unless WCU_DEBUG_HTML_DIR is set). Must run after all
# @mcp.tool() definitions so every registered tool gets wrapped.
debughtml.install(mcp)
