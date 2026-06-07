"""The `play` DSL: a timed input script executed at a cadence while a background thread
captures frames. Supports per-sample `probe` (run a command, capture JSON) and `until`
(early-stop on a condition over the last probe) — the KSP/Unreal closed loop.

Grammar (one command per line; `#` comments; chords use `+` with no spaces):
  hold <keys> <sec>        tap <keys> [count]      down <keys> | up <keys>
  look <dx> <dy>           move <x> <y>            lmb|rmb|mmb [x y]
  scroll <up|down|left|right> <amount>            type <text...>
  paste <text...>          wait <sec>             probe        until <expr>        shot
"""
import json
import sys
import threading
import time

from . import coords
from . import input as winput


class PlayError(ValueError):
    pass


def parse(script: str) -> list[tuple]:
    cmds = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        op, _, rest = line.partition(" ")
        cmds.append((op.lower(), rest.strip(), line))
    return cmds


def _keys(s: str) -> str:
    if not s:
        raise PlayError("missing key(s)")
    return s.split()[0]


def run(script: str, grab, fps: int, probe_cmd: str | None = None,
        coordinate_space: str = "image", max_seconds: float = 180.0):
    """Execute the script. Returns dict {frames, samples, log, stopped_by}."""
    cmds = parse(script)
    frames: list[tuple[float, object]] = []
    samples: list[dict] = []
    log: list[str] = []
    held_keys: list[str] = []
    held_btns: list[str] = []
    stop = threading.Event()
    t0 = time.perf_counter()
    last_sample: dict = {}
    stopped_by = "end"

    def now() -> float:
        return time.perf_counter() - t0

    def capture_loop():
        interval = 1.0 / max(1, fps)
        i = 0
        while not stop.is_set():
            target = i * interval
            wait = target - now()
            if wait > 0 and stop.wait(wait):
                break
            try:
                frames.append((now(), grab()))
            except Exception as e:  # noqa: BLE001
                print(f"[wcu] play: capture failed: {e}", file=sys.stderr)
            i += 1

    def run_probe() -> dict:
        from . import process
        if not probe_cmd:
            return {}
        res = process.shell(probe_cmd, timeout=10)
        out = (res.get("stdout") or "").strip()
        data = {}
        try:
            data = json.loads(out)
        except Exception:
            data = {"output": out}
        sample = {"t": round(now(), 3), **(data if isinstance(data, dict) else {"value": data})}
        samples.append(sample)
        return sample if isinstance(data, dict) else {}

    ct = threading.Thread(target=capture_loop, daemon=True)
    ct.start()
    try:
        for op, rest, line in cmds:
            if now() > max_seconds:
                stopped_by = "max_seconds"
                break
            try:
                if op == "hold":
                    parts = rest.split()
                    winput.hold(parts[0], float(parts[1]))
                elif op == "tap":
                    parts = rest.split()
                    count = int(parts[1]) if len(parts) > 1 else 1
                    for _ in range(count):
                        winput.press(parts[0])
                elif op == "down":
                    k = _keys(rest)
                    winput.key_down(k)
                    held_keys.append(k)
                elif op == "up":
                    k = _keys(rest)
                    winput.key_up(k)
                    if k in held_keys:
                        held_keys.remove(k)
                elif op == "look":
                    dx, dy = rest.split()
                    winput.move_relative(int(dx), int(dy))
                elif op == "move":
                    x, y = rest.split()
                    px, py = coords.resolve_point(float(x), float(y), coordinate_space)
                    winput.move(px, py)
                elif op in ("lmb", "rmb", "mmb"):
                    btn = {"lmb": "left", "rmb": "right", "mmb": "middle"}[op]
                    parts = rest.split()
                    if len(parts) >= 2:
                        px, py = coords.resolve_point(float(parts[0]), float(parts[1]), coordinate_space)
                        winput.click(btn, x=px, y=py)
                    else:
                        winput.click(btn)
                elif op == "scroll":
                    parts = rest.split()
                    direction, amount = parts[0].lower(), int(parts[1])
                    horiz = direction in ("left", "right")
                    signed = amount if direction in ("up", "right") else -amount
                    winput.scroll(signed, horizontal=horiz)
                elif op == "type":
                    winput.type_text(rest)
                elif op == "paste":
                    from . import clipboard
                    clipboard.set_text(rest)
                    winput.press("ctrl+v")
                elif op == "wait":
                    time.sleep(float(rest))
                elif op == "probe":
                    last_sample = run_probe() or last_sample
                elif op == "until":
                    if not last_sample:
                        last_sample = run_probe() or {}
                    try:
                        if eval(rest, {"__builtins__": {}}, dict(last_sample)):  # noqa: S307
                            stopped_by = f"until: {rest}"
                            log.append(f"{now():.2f}s until {rest} -> TRUE (stop)")
                            break
                    except Exception as e:  # noqa: BLE001
                        log.append(f"{now():.2f}s until {rest} -> error {e}")
                elif op == "shot":
                    frames.append((now(), grab()))
                else:
                    log.append(f"{now():.2f}s UNKNOWN: {line}")
                    continue
                log.append(f"{now():.2f}s {line}")
            except Exception as e:  # noqa: BLE001
                log.append(f"{now():.2f}s ERROR {line} -> {e}")
    finally:
        stop.set()
        ct.join(timeout=1.5)
        for k in list(held_keys):
            try:
                winput.key_up(k)
            except Exception:
                pass
        for b in list(held_btns):
            try:
                winput.button_up(b)
            except Exception:
                pass
    return {"frames": frames, "samples": samples, "log": log, "stopped_by": stopped_by}
