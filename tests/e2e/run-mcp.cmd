@echo off
setlocal EnableExtensions
rem Run a `claude -p` headless task against the project-local windows-computer-use MCP server.
rem The project .mcp.json points at the editable-installed venv, so this always tests the LATEST code.
rem Usage:  run-mcp.cmd "task prompt for the agent"

set "TASK=%~1"
if "%TASK%"=="" (
  echo Usage: run-mcp.cmd "task prompt" 1>&2
  exit /b 2
)

rem Append a standing "bail early + diagnose" rule so tool-evaluation runs stop flailing.
set "TASK=%TASK%  [E2E HARNESS RULE: this is a tool-evaluation run. If after at most 2-3 attempts a core interaction is clearly not working or you are not making real progress (an action reports ok but the screen does not change, repeated no-ops, or the same retry twice), STOP IMMEDIATELY and do not try more variations. Report concisely: the exact tool call that failed, what you observed, your best hypothesis why, and what tool change would fix it. Bailing early with a crisp diagnosis is the success condition.]"

rem Clear the nested-session guard so `claude -p` can run inside an existing Claude session.
set "CLAUDECODE="
set "CLAUDE_CODE_ENTRYPOINT="

set "REPO=%~dp0..\.."
pushd "%REPO%"
echo === claude -p (project MCP) :: %TASK% === 1>&2
rem stream-json + verbose emits one JSON event per turn/tool-call so progress is visible live
rem (text mode only flushes at the very end, which looks like a hang on long runs).
claude -p "%TASK%" --mcp-config ".mcp.json" --strict-mcp-config --dangerously-skip-permissions --verbose --output-format stream-json
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
