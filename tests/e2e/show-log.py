"""Pretty-print a `claude -p --output-format stream-json` log as a readable transcript.

Usage:  python tests/e2e/show-log.py <path-to-.output>
Shows agent text, each tool call + its input, and tool-result text (image blobs elided).
"""
import json
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def short(s, n=320):
    s = str(s).replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + " …"


path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    print("AGENT:", short(b["text"], 500))
                elif b.get("type") == "tool_use":
                    print("  CALL", b.get("name"), short(json.dumps(b.get("input", {})), 240))
        elif t == "user":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        for cc in c:
                            if cc.get("type") == "text":
                                print("    ->", short(cc.get("text", ""), 320))
                            elif cc.get("type") == "image":
                                print("    -> [image]")
                    else:
                        print("    ->", short(c, 320))
        elif t == "result":
            print("RESULT:", short(ev.get("result", ev.get("subtype", "")), 1000))
