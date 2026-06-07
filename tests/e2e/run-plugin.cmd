@echo off
setlocal EnableExtensions
rem Run a `claude -p` headless task against the windows-computer-use PLUGIN (end-to-end plugin test).
rem --plugin-dir loads the plugin; its .mcp.json bootstraps a venv and installs the server.
rem WCU_SOURCE points the bootstrap at the local checkout so we test current code without a GitHub push.
rem Usage:  run-plugin.cmd "task prompt for the agent"

set "TASK=%~1"
if "%TASK%"=="" (
  echo Usage: run-plugin.cmd "task prompt" 1>&2
  exit /b 2
)

set "TASK=%TASK%  [E2E HARNESS RULE: this is a tool-evaluation run. If after at most 2-3 attempts a core interaction is clearly not working or you are not making real progress (an action reports ok but the screen does not change, repeated no-ops, or the same retry twice), STOP IMMEDIATELY and do not try more variations. Report concisely: the exact tool call that failed, what you observed, your best hypothesis why, and what tool change would fix it. Bailing early with a crisp diagnosis is the success condition.]"

set "CLAUDECODE="
set "CLAUDE_CODE_ENTRYPOINT="
set "WCU_SOURCE=C:\dev\windows-computer-use-mcp"
set "PLUGIN=C:\dev\claude-plugins\plugins\windows-computer-use"

rem Run from a NEUTRAL temp dir (not the repo) so the project .mcp.json isn't picked up — this
rem tests the plugin's own bootstrap/pathing, not a hard-coded dev server in the repo folder.
set "RUNDIR=%TEMP%\wcu-plugin-test"
if not exist "%RUNDIR%" mkdir "%RUNDIR%"
pushd "%RUNDIR%"
echo === claude -p (plugin, cwd=%RUNDIR%) :: %TASK% === 1>&2
claude -p "%TASK%" --plugin-dir "%PLUGIN%" --dangerously-skip-permissions --verbose --output-format stream-json
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
