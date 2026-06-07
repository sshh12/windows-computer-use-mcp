"""Coordinate-space mapping + capture identity.

The agent sees a screenshot (possibly downscaled, possibly of a single monitor or
window whose top-left is not the desktop origin) and clicks in *that image's* pixel
space. This module remembers the geometry of the last *clickable* capture (only
``screenshot`` sets one — never ``record``/``play`` montages) and maps image
coordinates back to absolute physical desktop pixels for input injection.

Each clickable capture has a monotonic ``capture_id``. ``act`` may pass the id it is
clicking against; if it no longer matches the current frame we raise instead of
silently mis-clicking a stale layout — the single most common computer-use failure.

Spaces:
  - "image"  (default): coordinates are in the last screenshot's pixel space.
  - "screen": coordinates are already absolute physical desktop pixels.
"""
import itertools
import sys
from dataclasses import dataclass
from threading import Lock

_counter = itertools.count(1)


class CaptureMismatch(Exception):
    """Raised when act() coordinates reference a capture that is no longer current."""


@dataclass
class CaptureGeometry:
    origin_x: int = 0          # physical x of the captured region's top-left
    origin_y: int = 0          # physical y of the captured region's top-left
    actual_width: int = 0      # physical width of the captured region
    actual_height: int = 0     # physical height of the captured region
    rendered_width: int = 0    # width of the image actually returned to the agent
    rendered_height: int = 0   # height of the image actually returned to the agent
    label: str = ""            # human description, e.g. "monitor 0" / "window: Notepad"
    capture_id: int = 0        # monotonic id; 0 = no clickable capture yet

    @property
    def valid(self) -> bool:
        return self.rendered_width > 0 and self.rendered_height > 0


_state = CaptureGeometry()
_lock = Lock()


def new_capture_id() -> int:
    return next(_counter)


def set_last_capture(geom: CaptureGeometry) -> None:
    """Record the geometry of the latest clickable capture (screenshot only)."""
    global _state
    with _lock:
        _state = geom


def get_last_capture() -> CaptureGeometry:
    with _lock:
        return _state


def resolve_point(x: float, y: float, space: str = "image",
                  expected_capture_id: int | None = None) -> tuple[int, int]:
    """Map (x, y) in the given space to absolute physical desktop pixels.

    Raises CaptureMismatch if expected_capture_id is given and no longer current.
    """
    g = get_last_capture()
    if expected_capture_id is not None and g.capture_id != expected_capture_id:
        raise CaptureMismatch(
            f"coordinates reference capture #{expected_capture_id}, but the current frame "
            f"is #{g.capture_id or 'none'} ({g.label or 'unset'}). Take a fresh screenshot "
            f"before clicking, or pass coordinate_space='screen' for absolute pixels.")
    if space == "screen":
        return int(round(x)), int(round(y))
    if not g.valid:
        # No capture taken yet; assume caller already passed screen coordinates.
        return int(round(x)), int(round(y))
    sx = g.actual_width / g.rendered_width
    sy = g.actual_height / g.rendered_height
    px = g.origin_x + x * sx
    py = g.origin_y + y * sy
    cpx = min(max(px, g.origin_x), g.origin_x + g.actual_width - 1)
    cpy = min(max(py, g.origin_y), g.origin_y + g.actual_height - 1)
    if abs(cpx - px) > 1 or abs(cpy - py) > 1:
        print(f"[wcu] warning: image point ({x:.0f},{y:.0f}) falls outside the captured "
              f"region; clamped to screen ({int(cpx)},{int(cpy)})", file=sys.stderr)
    return int(round(cpx)), int(round(cpy))
