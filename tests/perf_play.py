"""Profile the `play` capture/encode path to find why it is pathologically slow on a
large GPU window (Unreal). Times each stage independently against a live window.

Usage: .venv\\Scripts\\python.exe tests\\perf_play.py [window-query]
"""
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import windows_computer_use  # noqa: E402,F401  (enables DPI awareness)
from windows_computer_use import capture, video, winfind  # noqa: E402


def stats(ts):
    return f"median {statistics.median(ts):6.0f}ms   min {min(ts):5.0f}   max {max(ts):6.0f}   (n={len(ts)})"


def timeit(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


query = sys.argv[1] if len(sys.argv) > 1 else "Unreal"
hits = winfind.find_windows(query)
if not hits:
    print(f"no window matching {query!r}")
    sys.exit(1)
w = hits[0]
hwnd = w["hwnd"]
rect = w["rect"]
print(f"target: {w['title'][:50]!r}  {rect['width']}x{rect['height']}  hwnd={hwnd}\n")

# 1. Full-window PrintWindow (what grab_window_region calls under the hood)
print("[1] grab_window  (full PrintWindow PW_RENDERFULLCONTENT):")
ts = timeit(lambda: capture.grab_window(hwnd), 12)
print("    " + stats(ts))

# 2. grab_window_region (PrintWindow + crop) — the per-frame call the viewport play uses
frac = (0.0, 0.13, 0.71, 0.74)
print("\n[2] grab_window_region  (PrintWindow + crop, the viewport play per-frame grab):")
ts = timeit(lambda: capture.grab_window_region(hwnd, frac), 12)
print("    " + stats(ts))

# 3. Realistic capture burst (mirrors playscript capture_loop) for ~3s
spec = {"kind": "window_region", "hwnd": hwnd, "frac": frac}
grab = video.grab_fn_for_spec(spec, foreground=False)
print("\n[3] capture burst (as fast as possible, ~30 frames):")
N = 30
t0 = time.perf_counter()
frames = [(time.perf_counter(), grab()) for _ in range(N)]
cap = time.perf_counter() - t0
print(f"    {N} frames in {cap:.2f}s  ->  {N / cap:.1f} fps   frame size {frames[0][1].size}")

# 4. PNG-save only (the loop inside write_mp4 before ffmpeg)
print("\n[4] PNG save (write_mp4 step 1):")
tmp = tempfile.mkdtemp(prefix="wcu_perf_")
t0 = time.perf_counter()
for i, (_t, img) in enumerate(frames):
    img.convert("RGB").save(os.path.join(tmp, f"f{i:05d}.png"))
print(f"    saved {N} PNGs in {time.perf_counter() - t0:.2f}s  ->  {(time.perf_counter() - t0) / N * 1000:.0f}ms/frame")

# 5. Full write_mp4 (PNG save + ffmpeg encode)
print("\n[5] write_mp4  (PNG save + ffmpeg libx264):")
out = os.path.join(tempfile.gettempdir(), "wcu_perf.mp4")
t0 = time.perf_counter()
ok, msg = video.write_mp4(frames, 15, out)
print(f"    {time.perf_counter() - t0:.2f}s   ok={ok} {msg}")

# 6. make_montage
print("\n[6] make_montage:")
t0 = time.perf_counter()
video.make_montage(frames, 9)
print(f"    {time.perf_counter() - t0:.2f}s")

print("\nDONE")
