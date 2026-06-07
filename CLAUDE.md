# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Windows computer-use MCP server** (Python + FastMCP, stdio) that gives an agent full control of
the **local** Windows desktop: multi-monitor screen capture, native input injection, video recording,
and a play-test loop. It runs ON the machine it controls (no resolution requests) and is built to work
universally across native apps, games, and browsers. No security gating — full agent control by design.

## Design principles (the decisions behind the surface — preserve these)

- **Universal input is a hard requirement.** Capture and input must work for native apps, games, AND
  browser/canvas games — no fullscreen workarounds, no per-app special-casing. If something works only
  for one class of app, it isn't done.
- **The agent is the user; tokens are the scarce resource.** Map tools to workflows (7 tools, not 35),
  batch input to cut round-trips, and **prefer non-image reads** (`window get_text`/`ui_tree`, OCR) over
  screenshots whenever text is enough. Return a single contact-sheet montage, never N frames. Downscale
  to 1568; write video/large output to a file, never inline.
- **Mirror the native `computer_20251124` vocabulary** (`left_click`, `type`, `key`, `scroll`, `drag`…)
  so the model is fluent on the first try.
- **Correctness beats cleverness in the capture/coordinate path.** `capture_id` makes a stale-frame
  click a loud error, not a silent mis-click; only `screenshot` sets the click frame; never steal
  keyboard focus to take a picture (PrintWindow captures in place).
- **Just-in-time feedback in tool results.** When something looks wrong, the result should teach the
  agent the fix (which window received keyboard input; the browser-canvas hint; black-frame warnings) —
  don't rely on the static description alone.
- **Read the real machine; never request a resolution.** Multi-monitor + per-monitor DPI are first-class
  (the server controls the local desktop, unlike the sandboxed cloud tool).
- **Validate against reality.** Iterate via headless `claude -p` agents running the latest code, and make
  tool-eval runs **bail early and diagnose** rather than flail.

## Commands

```bat
:: Dev setup (Python 3.10+; ffmpeg on PATH for video)
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

:: Run the server directly (stdio)
.venv\Scripts\python.exe -m windows_computer_use

:: Tests (drive the REAL machine — they move the mouse / capture the screen)
.venv\Scripts\python.exe tests\smoke_engine.py     :: dpi/displays/capture/input engine
.venv\Scripts\python.exe tests\smoke_server.py      :: assembled 7-tool surface (direct calls)
.venv\Scripts\python.exe tests\smoke_stdio.py       :: full MCP client<->server over stdio
.venv\Scripts\python.exe tests\integration_notepad.py  :: live input into a real app

:: Lint / type-check (must pass; pre-commit runs these)
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy

:: End-to-end: a headless `claude -p` agent drives the MCP and reports tool feedback
tests\e2e\test-calculator.cmd
tests\e2e\run-mcp.cmd "your task prompt"          :: project .mcp.json server (latest code)
tests\e2e\run-plugin.cmd "your task prompt"        :: the plugin via --plugin-dir
.venv\Scripts\python.exe tests\e2e\show-log.py <task.output>   :: pretty-print a stream-json run
```

There is no single-test runner; the `tests\*.py` scripts are standalone — run one directly.

## Architecture

**Entry:** `windows_computer_use.__main__:main` → `server.mcp.run()`. Importing the package
(`__init__.py`) enables **per-monitor-v2 DPI awareness** — this MUST happen before any capture/input,
so everything operates in true physical pixels (a 150%-scaled 4K panel reports 3840×2160, not 2560×1440).

**Two layers:**
- **Engine modules** (no MCP types; pure functions over ctypes/mss/PIL): `dpi`, `displays`,
  `winfind` (top-level window enum, focus, UWP real-process resolution, WM_CLOSE), `capture`,
  `input` (SendInput), `keymap`, `coords`, `images`, `targets`, `artifacts`, `video`, `playscript`,
  `clipboard`, `ocr`, `process`, `system`, `windows` (UI Automation).
- **`server.py`** wires the engine into **7 MCP tools**: `screenshot`, `act`, `record`, `play`,
  `window`, `process`, `system`. It builds `mcp.types` content blocks (text + inline image), and writes
  large artifacts (video/full-res/>8KB text) to disk returning a `resource_link` + path.

**Load-bearing concepts (read these files together to understand the system):**

- **Coordinate model** (`coords.py`, `targets.py`, `server.py`): physical px in virtual-desktop space
  (primary's top-left = 0,0; left/above monitors negative). The agent clicks in the **image space of
  the last `screenshot`**; `coords.resolve_point` maps image→physical using stored geometry (handles
  downscale + per-monitor offset + DPI). Each screenshot returns a monotonic `capture_id`; `act` errors
  loudly on a stale `capture_id` instead of mis-clicking. **Only `screenshot` sets the click frame —
  `record`/`play` montages deliberately do not.**

- **Foreground discipline** (`winfind.foreground`, `server._capture_target`): re-activating an
  already-front window (SetForegroundWindow/AttachThreadInput) makes apps like Chrome reset their
  internal keyboard focus — so `foreground()` is a **no-op when the window is already frontmost**, and
  window `screenshot` defaults `foreground=False` (PrintWindow captures occluded windows in place, no
  focus theft). Keyboard input goes to the foreground window; `act`'s `focus=` param focuses atomically
  before the batch and the result reports which window actually received input.

- **Input** (`input.py`, `keymap.py`): ctypes `SendInput` with **hardware scan codes**
  (`KEYEVENTF_SCANCODE`, extended-key flags for arrows) so games respond; unicode typing
  (`KEYEVENTF_UNICODE`); absolute + **relative** mouse (relative for in-game look). `act`'s `key` and
  `play`'s `tap` use the **same** scan-code path.

- **Capture** (`capture.py`): `mss` (GDI BitBlt) for desktop/monitor/region/virtual; `PrintWindow`
  (`PW_RENDERFULLCONTENT`) for a specific window even if occluded/GPU-composited; black-frame →
  foreground + BitBlt fallback. All 64-bit-safe ctypes (explicit `argtypes`/`restype` on HANDLE-
  returning calls — required or pointers truncate).

- **Artifacts / roots** (`artifacts.py`): output dir = first writable client root (MCP `roots/list`)
  → `MCP_OUTPUT_DIR` → `~/Pictures/windows-computer-use` → `%TEMP%`. Screenshots inline+downscaled
  (default long edge **1568**; Anthropic downsamples above this); video → mp4 + inline contact-sheet
  montage (`video.py`, `images.contact_sheet`).

## Critical gotchas

- **stdout is the JSON-RPC channel.** Never `print()` to stdout from the server — log to **stderr** only.
- **The interactive session's MCP server is pinned for the session.** Code edits are picked up only on
  restart. But each `tests\e2e\run-mcp.cmd` / `claude -p` spawns a **fresh** server from the editable
  install, so e2e runs always test the **latest** code — use them to iterate without restarting.
- The e2e `.cmd` scripts clear `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT` so `claude -p` runs nested, and use
  `--verbose --output-format stream-json` (text mode only flushes at the end and looks hung). They also
  append a "bail-early + diagnose" rule so tool-eval runs stop flailing.
- The repo's `.mcp.json` (gitignored — machine-specific) points Claude Code at the dev venv. UWP/Store
  apps (Calculator, Settings) present their window under `ApplicationFrameHost.exe`; `window list`
  resolves the real process and `window close` (WM_CLOSE) is the way to close them.

## Distribution

Standalone repo `sshh12/windows-computer-use-mcp` (pip-installable). The Claude plugin lives in
`sshh12/claude-plugins` under `plugins/windows-computer-use/` — its `.mcp.json` runs `launch.cmd`, which
bootstraps a venv and `pip install`s this package from GitHub on first run (`WCU_SOURCE` overrides the
source for local testing). See `README.md` for install paths.
