"""Where to write artifacts (screenshots/video) so the MCP client can find them.

Priority: client filesystem roots (roots/list) -> MCP_OUTPUT_DIR env ->
~/Pictures/windows-computer-use -> %TEMP%/windows-computer-use. Writing inside a client
root lets Claude Code open the file; the absolute path is always returned in text too.
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

SUBDIR = "windows-computer-use"


def _uri_to_path(uri: str) -> pathlib.Path | None:
    try:
        pr = urlparse(uri)
        if pr.scheme and pr.scheme != "file":
            return None
        return pathlib.Path(url2pathname(unquote(pr.path)))
    except Exception:
        return None


def _fallback_dir() -> pathlib.Path:
    env = os.environ.get("MCP_OUTPUT_DIR")
    if env:
        p = pathlib.Path(os.path.expandvars(os.path.expanduser(env)))
    else:
        pics = pathlib.Path(os.path.expanduser("~")) / "Pictures"
        base = pics if pics.exists() else pathlib.Path(tempfile.gettempdir())
        p = base / SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


async def resolve_output_dir(ctx=None) -> pathlib.Path:
    """Best writable artifact directory for this session."""
    if ctx is not None:
        try:
            result = await ctx.session.list_roots()
            for root in (getattr(result, "roots", None) or []):
                local = _uri_to_path(str(root.uri))
                if local and local.exists():
                    out = local / SUBDIR
                    out.mkdir(parents=True, exist_ok=True)
                    return out
        except Exception as e:  # client may not support roots
            print(f"[wcu] roots/list unavailable ({e}); using fallback dir", file=sys.stderr)
    return _fallback_dir()


def file_uri(path) -> str:
    return pathlib.Path(path).absolute().as_uri()


def unique_path(directory: pathlib.Path, prefix: str, ext: str, stamp: str) -> pathlib.Path:
    """A collision-resistant artifact path. `stamp` is supplied by the caller (the server
    stamps wall-clock time; this module stays time-source-free)."""
    name = f"{prefix}_{stamp}.{ext.lstrip('.')}"
    return directory / name
