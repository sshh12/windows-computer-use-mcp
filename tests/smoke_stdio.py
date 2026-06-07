"""End-to-end MCP stdio test: launch the server as a subprocess and drive it as a client,
exactly as Claude Code will. Validates the wire protocol, tool schemas, and a real call."""
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "windows_computer_use"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS over stdio:", names)
            assert "screenshot" in names and "act" in names

            # a non-destructive structured call
            res = await session.call_tool("system", {"action": "displays"})
            print("system displays isError:", res.isError)
            print("  structured:", res.structuredContent)

            # an image-returning call -> verify an image content block crosses the wire
            shot = await session.call_tool("screenshot", {"target": "desktop", "max_dim": 768})
            kinds = [c.type for c in shot.content]
            print("screenshot content kinds:", kinds)
            assert "image" in kinds, "no image block returned over stdio"
            print("\nSTDIO E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
