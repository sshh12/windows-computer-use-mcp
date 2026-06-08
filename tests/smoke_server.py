"""Import + tool smoke for the assembled MCP server (run with the dev venv)."""
import asyncio
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from mcp import types  # noqa: E402

import windows_computer_use.server as S  # noqa: E402


def block_kinds(blocks):
    return [type(b).__name__ for b in blocks]


def has_image(blocks):
    return any(isinstance(b, types.ImageContent) for b in blocks)


async def main():
    tools = await S.mcp.list_tools()
    print("TOOLS:", [t.name for t in tools])
    assert {"screenshot", "act", "record", "play", "window", "process", "system"} <= {t.name for t in tools}

    print("\n[system displays]", S.system("displays"))
    print("[system cursor]", S.system("cursor"))

    wl = S.window("list", limit=4)
    print("\n[window list] shown", wl["shown"], "of", wl["total"], "e.g.",
          [w["title"][:30] for w in wl["windows"][:3]])

    sh = await S.process("shell", command="echo hello-from-mcp")
    print("\n[process shell] exit", sh["exit"], "stdout", repr(sh["stdout"].strip()))

    print("[system get_clipboard]", repr((S.system("get_clipboard")["text"] or "")[:40]))

    act_r = await S.act(actions=[{"action": "wait", "duration": 0.1}], screenshot=False)
    print("\n[act wait] ->", block_kinds(act_r))

    shot = await S.screenshot(target="desktop")
    print("\n[screenshot desktop] ->", block_kinds(shot), "image?", has_image(shot))
    print("  text:", shot[0].text.splitlines()[0])

    fg = await S.screenshot(target="foreground")
    print("[screenshot foreground] ->", block_kinds(fg), "image?", has_image(fg))

    rec = await S.record(target="desktop", seconds=1.0, fps=5, montage_frames=4)
    print("\n[record 1s] ->", block_kinds(rec), "image?", has_image(rec))
    print("  ", rec[0].text.splitlines()[0])

    # viewports: screen-anchored crop with exact coordinate assertions (max_dim=0 -> no downscale)
    disp = S.system("displays")["monitors"][0]
    ox, oy = disp["origin"]
    vp_rect = {"left": ox + 100, "top": oy + 100, "width": 400, "height": 300}
    S.viewports.define_screen_viewport("smoke", vp_rect)
    await S.screenshot(target="viewport:smoke", max_dim=0)
    g = S.coords.get_last_capture()
    assert (g.origin_x, g.origin_y) == (vp_rect["left"], vp_rect["top"]), (g.origin_x, g.origin_y)
    assert (g.actual_width, g.actual_height) == (400, 300), (g.actual_width, g.actual_height)
    cx, cy = S.coords.resolve_point(200, 150, "image")  # viewport center -> physical center
    assert (cx, cy) == (vp_rect["left"] + 200, vp_rect["top"] + 150), (cx, cy)
    assert any(v["name"] == "smoke" for v in S.viewports.summaries())
    print("\n[viewport screen] origin", (g.origin_x, g.origin_y), "size",
          (g.actual_width, g.actual_height), "center->", (cx, cy))

    # viewports: window-anchored crop (center quarter of the foreground window) -> PrintWindow+crop
    fgw = next((w for w in S.winfind.list_windows() if w["foreground"]), None)
    if fgw:
        S.viewports.define_window_viewport("smokewin", fgw["hwnd"], fgw["title"],
                                           fgw.get("process", ""), (0.25, 0.25, 0.5, 0.5))
        await S.screenshot(target="viewport:smokewin", max_dim=0)
        g2 = S.coords.get_last_capture()
        exp_w = round(0.5 * fgw["rect"]["width"])
        assert abs(g2.actual_width - exp_w) <= 2, (g2.actual_width, exp_w)
        print("[viewport window] cropped", (g2.actual_width, g2.actual_height),
              "of window", (fgw["rect"]["width"], fgw["rect"]["height"]))
        S.system("clear_viewport", text="smokewin")
    S.system("clear_viewport", text="smoke")

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
