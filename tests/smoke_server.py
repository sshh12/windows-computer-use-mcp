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

    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
