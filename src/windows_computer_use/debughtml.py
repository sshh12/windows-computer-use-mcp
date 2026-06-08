"""Optional debug dump: write a per-session HTML log of every MCP tool call.

Off by default. Set WCU_DEBUG_HTML_DIR to a directory and the server writes one timestamped
`session_<stamp>.html` per process, appending a pretty-printed entry for every tool call: the
arguments, the result text, and any returned images embedded inline. Open it in a browser after
reloading the server and driving some actions.

Implementation: after FastMCP has registered the tools (schema/async/context already computed from
the original functions), we swap each `tool.fn` for a thin tracing wrapper. `Tool.run` calls
`self.fn(**kwargs)` dynamically, so this leaves the advertised surface untouched while giving us the
validated args and the native (pre-conversion) result blocks. Logging never raises and never writes
to stdout (the JSON-RPC channel).
"""
import functools
import html
import inspect
import json
import os
import pathlib
import sys
import threading
import time

_DIR_ENV = "WCU_DEBUG_HTML_DIR"
_lock = threading.Lock()
_session_file: pathlib.Path | None = None
_count = 0
_init_failed = False


def _dir() -> pathlib.Path | None:
    raw = os.environ.get(_DIR_ENV)
    if not raw:
        return None
    return pathlib.Path(os.path.expandvars(os.path.expanduser(raw)))


def enabled() -> bool:
    return _dir() is not None


_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>wcu debug — {stamp}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 13px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace; margin: 0; }}
  header.pinned {{ position: sticky; top: 0; z-index: 9; padding: 10px 16px;
    background: #1f2430; color: #e6e6e6; border-bottom: 2px solid #3a4150; }}
  header.pinned b {{ color: #8ec07c; }}
  main {{ padding: 8px 16px 64px; }}
  section.call {{ border: 1px solid #3a4150; border-left-width: 5px; border-radius: 6px;
    margin: 12px 0; padding: 8px 12px; }}
  section.ok {{ border-left-color: #8ec07c; }}
  section.err {{ border-left-color: #fb4934; }}
  .row {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }}
  .idx {{ color: #83a598; font-weight: 700; }}
  .tool {{ font-weight: 700; font-size: 15px; }}
  .meta {{ color: #928374; }}
  .err .tool {{ color: #fb4934; }}
  h4 {{ margin: 8px 0 2px; color: #928374; font-weight: 600; }}
  pre {{ margin: 2px 0; padding: 8px; background: #00000014; border-radius: 4px;
    white-space: pre-wrap; word-break: break-word; overflow-x: auto; }}
  pre.error {{ color: #fb4934; }}
  img.shot {{ max-width: 680px; height: auto; border: 1px solid #3a4150; border-radius: 4px;
    display: block; margin: 4px 0; }}
  a {{ color: #83a598; }}
</style></head>
<body>
<header class="pinned">windows-computer-use debug &mdash; session <b>{stamp}</b>
&middot; pid {pid} &middot; {path}</header>
<main id="log">
"""


def _ensure_file() -> pathlib.Path | None:
    """Create (once) the session HTML file with its header. Returns None if disabled/failed."""
    global _session_file, _init_failed
    if _session_file is not None:
        return _session_file
    if _init_failed:
        return None
    d = _dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = d / f"session_{stamp}.html"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_HEAD.format(stamp=stamp, pid=os.getpid(), path=html.escape(str(path))))
        _session_file = path
        print(f"[wcu] debug HTML dump -> {path}", file=sys.stderr)
        return path
    except Exception as e:
        _init_failed = True
        print(f"[wcu] debug HTML dump disabled (init failed: {e})", file=sys.stderr)
        return None


def _render_block(b) -> str:
    """One mcp.types content block -> HTML."""
    kind = getattr(b, "type", None)
    if kind == "text":
        return f'<pre class="text">{html.escape(getattr(b, "text", "") or "")}</pre>'
    if kind == "image":
        mime = getattr(b, "mimeType", "image/png")
        data = getattr(b, "data", "") or ""
        return f'<img class="shot" src="data:{html.escape(mime)};base64,{data}">'
    if kind == "resource_link":
        uri = html.escape(str(getattr(b, "uri", "") or ""))
        name = html.escape(str(getattr(b, "name", "") or uri))
        desc = getattr(b, "description", None)
        tail = f' <span class="meta">{html.escape(desc)}</span>' if desc else ""
        return f'<div>&#128206; <a href="{uri}">{name}</a>{tail}</div>'
    # Unknown block type: dump its repr.
    return f'<pre>{html.escape(repr(b))}</pre>'


def _render_result(result) -> str:
    if isinstance(result, list):
        return "".join(_render_block(b) for b in result)
    if isinstance(result, dict):
        return f'<pre>{html.escape(json.dumps(result, indent=2, default=str))}</pre>'
    return f'<pre>{html.escape(repr(result))}</pre>'


def log_call(name: str, args: dict, result, error: str | None, duration_ms: float) -> None:
    """Append one entry for a completed (or failed) tool call. Never raises."""
    global _count
    try:
        with _lock:
            path = _ensure_file()
            if path is None:
                return
            _count += 1
            idx = _count
            args_clean = {k: v for k, v in (args or {}).items() if k != "ctx"}
            args_html = html.escape(json.dumps(args_clean, indent=2, default=str))
            cls = "err" if error else "ok"
            status = "ERROR" if error else "ok"
            parts = [
                f'<section class="call {cls}">',
                '<div class="row">',
                f'<span class="idx">#{idx}</span>',
                f'<span class="tool">{html.escape(name)}</span>',
                f'<span class="meta">{time.strftime("%H:%M:%S")} '
                f'&middot; {duration_ms:.0f} ms &middot; {status}</span>',
                '</div>',
                '<h4>args</h4>',
                f'<pre>{args_html}</pre>',
            ]
            if error:
                parts += ['<h4>error</h4>', f'<pre class="error">{html.escape(error)}</pre>']
            else:
                parts += ['<h4>result</h4>', _render_result(result)]
            parts.append('</section>\n')
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(parts))
    except Exception as e:  # logging must never break a tool call
        print(f"[wcu] debug HTML dump: log failed ({e})", file=sys.stderr)


def _wrap(name: str, fn):
    """Return a tracing wrapper around `fn` (async if fn is async, else sync)."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **kw):
            t0 = time.perf_counter()
            try:
                result = await fn(*a, **kw)
            except Exception as e:
                log_call(name, kw, None, repr(e), (time.perf_counter() - t0) * 1000)
                raise
            log_call(name, kw, result, None, (time.perf_counter() - t0) * 1000)
            return result
        return awrapper

    @functools.wraps(fn)
    def swrapper(*a, **kw):
        t0 = time.perf_counter()
        try:
            result = fn(*a, **kw)
        except Exception as e:
            log_call(name, kw, None, repr(e), (time.perf_counter() - t0) * 1000)
            raise
        log_call(name, kw, result, None, (time.perf_counter() - t0) * 1000)
        return result
    return swrapper


def install(mcp) -> None:
    """Swap each registered tool's fn for a tracing wrapper. No-op when disabled.

    Safe to call once after all tools are registered: FastMCP has already built each tool's
    schema/is_async/context_kwarg from the original fn, and Tool.run resolves self.fn dynamically.
    """
    if not enabled():
        return
    try:
        tools = mcp._tool_manager.list_tools()
    except Exception as e:
        print(f"[wcu] debug HTML dump: cannot enumerate tools ({e})", file=sys.stderr)
        return
    for tool in tools:
        tool.fn = _wrap(tool.name, tool.fn)
    print(f"[wcu] debug HTML dump armed for {len(tools)} tools "
          f"(set {_DIR_ENV} to a dir to capture)", file=sys.stderr)
