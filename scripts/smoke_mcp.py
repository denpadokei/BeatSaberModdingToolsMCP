import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PROJECT = ROOT / "_scratch" / "MyPlugin2"


async def main() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "bsmt_mcp.server"],
        env=env,
        cwd=str(ROOT),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print("server:", info.serverInfo.name, info.serverInfo.version)
            tools = await session.list_tools()
            print("tools:", len(tools.tools))
            result = await session.call_tool("compare_versions", {"manifest_version": "1.2.3-beta", "assembly_version": "1.2.3.0"})
            print("compare is_error:", result.isError)
            print(json.loads(result.content[0].text)["match"])
            result = await session.call_tool("inspect_project", {"project": str(PROJECT)})
            print("inspect is_error:", result.isError)
            data = json.loads(result.content[0].text)
            print("inspect ok:", data["ok"], "refs:", data["project"]["reference_count"], "warnings:", data["warnings"][:2])
            result = await session.call_tool("detect_game", {})
            data = json.loads(result.content[0].text)
            print("detect ok:", data["ok"], "chosen:", data["install"]["version"], "count:", len(data["installs"]))
            result = await session.call_tool("validate_manifest", {})
            print("expected error:", result.isError, result.content[0].text[:120])


asyncio.run(main())
