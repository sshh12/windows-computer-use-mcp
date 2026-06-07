"""Recording: paced frame capture, ffmpeg mp4 encode, and frame montage.

The agent gets a single timestamped **contact-sheet montage** to reason over (cheap),
plus an mp4 written to disk for the human. A "frames identical" liveness check warns when
the target wasn't actually rendering (a classic background-window / stale-frame failure).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import ImageChops, ImageStat

from . import capture, images, winfind


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def grab_fn_for_spec(spec: dict, foreground: bool = True):
    """Build a zero-arg callable that returns a fresh PIL frame for a target spec."""
    if spec["kind"] == "window":
        hwnd = spec["hwnd"]
        if foreground:
            winfind.foreground(hwnd)
            time.sleep(0.15)

        def grab():
            img, _rect, _ok = capture.grab_window(hwnd)
            return img
        return grab

    r = spec["rect"]

    def grab_rect():
        return capture.grab_rect(r["left"], r["top"], r["width"], r["height"])
    return grab_rect


def record(grab, seconds: float, fps: int) -> tuple[list, float]:
    """Capture frames at a paced cadence. Returns ([(t_seconds, PIL.Image)], achieved_fps)."""
    frames: list[tuple[float, object]] = []
    interval = 1.0 / max(1, fps)
    n = max(1, int(round(seconds * fps)))
    t0 = time.perf_counter()
    for i in range(n):
        target = t0 + i * interval
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        ts = time.perf_counter() - t0
        try:
            frames.append((ts, grab()))
        except Exception as e:  # noqa: BLE001
            print(f"[wcu] record: frame {i} failed: {e}", file=sys.stderr)
    elapsed = max(1e-6, time.perf_counter() - t0)
    return frames, len(frames) / elapsed


def _even_indices(count: int, want: int) -> list[int]:
    if count <= want:
        return list(range(count))
    return [round(i * (count - 1) / (want - 1)) for i in range(want)]


def make_montage(frames: list, montage_frames: int = 6):
    """Sample frames into a labeled contact sheet. Returns (PIL montage, warning|None)."""
    if not frames:
        return None, "no frames captured"
    idxs = _even_indices(len(frames), montage_frames)
    sel = [frames[i] for i in idxs]
    labels = [f"t={t:.2f}s" for t, _ in sel]
    sheet = images.contact_sheet([img for _, img in sel], labels, cols=3)
    warning = None
    if len(sel) >= 2:
        a, b = sel[0][1].convert("RGB"), sel[-1][1].convert("RGB")
        if a.size == b.size:
            diff = ImageStat.Stat(ImageChops.difference(a, b)).mean
            if max(diff) < 1.5:
                warning = ("frames look identical — the target may not be rendering "
                           "(for a window try foreground=true, or it may be a fullscreen/DRM surface)")
    return sheet, warning


def write_mp4(frames: list, fps: int, out_path) -> tuple[bool, str]:
    """Encode frames to an H.264 mp4. Returns (ok, message)."""
    fp = ffmpeg_path()
    if not fp:
        return False, "ffmpeg not found on PATH (frames captured but no mp4)"
    if not frames:
        return False, "no frames to encode"
    tmp = tempfile.mkdtemp(prefix="wcu_frames_")
    try:
        for i, (_t, img) in enumerate(frames):
            img.convert("RGB").save(os.path.join(tmp, f"f{i:05d}.png"))
        cmd = [
            fp, "-y", "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%05d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-loglevel", "error",
            str(out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return False, f"ffmpeg failed: {r.stderr.strip()[:300]}"
        return True, "ok"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
