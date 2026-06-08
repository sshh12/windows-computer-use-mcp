@echo off
rem Smoke e2e: a headless agent drives Calculator via the MCP and reports tool feedback.
call "%~dp0run-mcp.cmd" "Using ONLY the windows-computer-use MCP tools (do not use Bash): open the Windows Calculator, compute 137 + 246, read the on-screen result, then close Calculator. State the result."
