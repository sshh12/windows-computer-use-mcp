# End-to-end tests (`claude -p`)

These batch scripts spawn a **headless Claude agent** (`claude -p`) that drives the desktop
through this MCP server, so we test the real tool surface the way a model actually uses it.

Each script clears `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` so `claude -p` runs even from inside an
existing Claude session, and uses `--dangerously-skip-permissions` (full local control, by design).

| Script | What it does |
|---|---|
| `run-mcp.cmd "<task>"` | Runs a task against the project `.mcp.json` server (latest code via the editable install). |
| `run-plugin.cmd "<task>"` | Runs a task against the **plugin** (`--plugin-dir`), exercising the venv bootstrap. `WCU_SOURCE` points the install at the local checkout. |
| `test-calculator.cmd` | Smoke: open Calculator, compute, read result, close — with tool feedback. |
| `test-bloxorz.cmd` | Open a browser and play-test Bloxorz (turn-based block puzzle) on coolmathgames. |
| `test-unreal.cmd` | Drive a real Unreal Engine editor by sight: add a Cube, switch to wireframe, Play+Stop. **Requires the editor open with a project.** |

```bat
tests\e2e\test-calculator.cmd
tests\e2e\run-mcp.cmd "open Notepad, type a haiku, and tell me what you see"
tests\e2e\run-plugin.cmd "what monitors are connected and what's on each?"
```

The agent's transcript shows which tools it picked and any just-in-time feedback the tools returned —
the signal we use to refine tool descriptions and arguments.

Both runners (`run-mcp.cmd` / `run-plugin.cmd`) append two standing rules to **every** task, so the
signal is consistent no matter what the test does:
- **Bail-early + diagnose** — if a core interaction is clearly broken after 2-3 attempts, stop and
  report the failing call + hypothesis + the fix, rather than flailing.
- **Always give toolset feedback** — regardless of success or failure, the agent ends with a
  `Toolset feedback:` section: avoidable round-trips/screenshots, confusing or missing args/return
  fields, token/attempt costs, and one concrete fix per problem. This is what we mine to improve the
  toolset, so it runs on passing tests too — not just the ones that break.
