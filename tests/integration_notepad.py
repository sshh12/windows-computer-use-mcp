"""Live input-injection integration test: launch Notepad, type via SendInput, read the
text back via UI Automation, screenshot the window, then force-close. Validates the real
process -> focus -> act(type) -> window(get_text) -> screenshot -> kill loop."""
import asyncio
import sys
import time

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import windows_computer_use.server as S  # noqa: E402

MARKER = "WCU_OK_42 hello computer use"


async def main():
    print("launch:", await S.process("launch", exe="notepad.exe"))
    r = await S.process("wait_for_window", query="Notepad", timeout=12, ready="input_idle")
    print("wait_for_window:", r)
    if r.get("timed_out"):
        print("RESULT: FAIL (notepad window not found)")
        return
    time.sleep(0.6)
    print("focus:", S.window("focus", query="Notepad"))
    time.sleep(0.3)
    await S.act(actions=[{"action": "type", "text": MARKER}], screenshot=False)
    time.sleep(0.4)

    txt = S.window("get_text", query="Notepad")
    got = (txt.get("text") or "")
    found = MARKER.replace(" ", "") in got.replace(" ", "").replace("\r", "").replace("\n", "")
    print(f"get_text source={txt.get('source')} marker_found={found}")
    print("  read-back text:", repr(got[:160]))

    shot = await S.screenshot(target="window:Notepad", save=True)
    print("  screenshot:", shot[0].text.splitlines()[-1])

    print("kill:", await S.process("kill", name="notepad", all=True))
    print("RESULT:", "PASS" if found else "FAIL (typed text not read back via UIA)")


if __name__ == "__main__":
    asyncio.run(main())
