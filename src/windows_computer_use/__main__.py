"""Entry point: ``python -m windows_computer_use`` (stdio MCP server)."""
import sys


def main() -> None:
    # Importing the package sets DPI awareness first (see __init__).
    from . import DPI_AWARENESS

    print(f"[windows-computer-use-mcp] starting (dpi={DPI_AWARENESS})", file=sys.stderr)
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
