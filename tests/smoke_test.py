"""Rooktest: start de MCP-server als claude en als gemini en test de tools.

Draai met een tijdelijke map, zodat je echte logboek leeg blijft:
    AI_BRIDGE_HOME=$(mktemp -d) uv run python tests/smoke_test.py
"""
import asyncio
import os
import subprocess
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent


def bridge(*args: str) -> None:
    subprocess.run(["uv", "run", "-q", "bridge", *args], cwd=ROOT, check=True)


async def as_agent(agent: str) -> None:
    params = StdioServerParameters(
        command="uv",
        args=["run", "-q", "--directory", str(ROOT), "ai-bridge-server"],
        env={**os.environ, "AI_BRIDGE_AGENT": agent},
    )
    async with Client(params) as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        print(f"[{agent}] tools: {tools}")
        for name, args in [
            ("bridge_status", {}),
            ("post_note", {"text": f"testbericht van {agent}", "thread": "test"}),
            ("read_notes", {}),
        ]:
            r = await c.call_tool(name, args)
            text = " ".join(getattr(x, "text", "") for x in r.content)
            print(f"[{agent}] {name} -> {'FOUT' if r.is_error else 'ok'}: {text.splitlines()[-1]}")


async def main() -> None:
    assert "AI_BRIDGE_HOME" in os.environ, "Zet AI_BRIDGE_HOME naar een tijdelijke map."
    bridge("on")
    await as_agent("claude")
    await as_agent("gemini")
    bridge("off")
    print("--- met de bridge UIT moet dit een FOUT geven:")
    await as_agent("claude")


asyncio.run(main())
