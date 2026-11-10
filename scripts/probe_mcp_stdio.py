"""Probe the stdio MCP server without asyncio subprocess pipes (sandbox-friendly)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bsmt_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin and proc.stdout

    def send(payload: dict) -> None:
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict:
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
    )
    init = receive(1)
    print("initialize:", init["result"]["serverInfo"])
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = receive(2)["result"]["tools"]
    print("tools:", len(tools), [tool["name"] for tool in tools][:5], "...")
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "compare_versions", "arguments": {"manifest_version": "1.2.3", "assembly_version": "1.2.3.0"}},
        }
    )
    result = receive(3)["result"]
    print("compare isError:", result.get("isError"), "structured:", result.get("structuredContent"))
    send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "inspect_project", "arguments": {"project": "missing"}}})
    error_result = receive(4)["result"]
    print("error isError:", error_result.get("isError"), "text:", error_result["content"][0]["text"].splitlines()[0])
    proc.stdin.close()
    proc.wait(timeout=20)
    stderr = proc.stderr.read() if proc.stderr else ""
    if stderr.strip():
        print("stderr:", stderr.strip()[:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
