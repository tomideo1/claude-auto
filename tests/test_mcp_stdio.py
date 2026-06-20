"""MCP server stdio smoke test.

Speaks raw JSON-RPC at the hive-mcp-server subprocess and verifies that:
  - The server initializes (capabilities exchange).
  - All 7 hive tools are registered.
  - A read-only tool (pool_status) returns sane content.

Run with:  ~/projects/hive/.venv/bin/python tests/test_mcp_stdio.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HIVE_ROOT = Path(__file__).resolve().parent.parent
SERVER_CMD = [str(HIVE_ROOT / "bin" / "hive-mcp-server")]
EXPECTED_TOOLS = {
    "delegate_worker", "spawn_orchestrator", "await_workers",
    "worker_status", "abort_worker", "pool_status", "cost_status",
}


def send(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen, timeout: float = 10.0) -> dict:
    """Read one JSON line, skipping any non-JSON noise (e.g. log banners)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise TimeoutError(f"no response within {timeout}s")


def main() -> int:
    env = os.environ.copy()
    env["HIVE_STATE_DIR"] = str(HIVE_ROOT / "state")
    proc = subprocess.Popen(
        SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    try:
        # 1. initialize
        send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hive-test", "version": "0.1"},
            },
        })
        resp = recv(proc)
        assert resp.get("id") == 1, f"initialize id mismatch: {resp}"
        assert "result" in resp, f"initialize errored: {resp}"
        server_name = resp["result"]["serverInfo"]["name"]
        print(f"  initialized: server={server_name}, "
              f"protocol={resp['result']['protocolVersion']}")

        # 2. initialized notification
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. tools/list
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = recv(proc)
        assert resp.get("id") == 2, f"tools/list id mismatch: {resp}"
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        assert not missing, f"missing tools: {missing}"
        print(f"  tools listed: {sorted(names)}")
        if extra:
            print(f"  extra tools (informational): {sorted(extra)}")

        # 4. tools/call pool_status (read-only, safe)
        send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "pool_status", "arguments": {}},
        })
        resp = recv(proc)
        assert resp.get("id") == 3, f"pool_status id mismatch: {resp}"
        if "error" in resp:
            raise AssertionError(f"pool_status errored: {resp['error']}")
        content = resp["result"]["content"]
        # Content is a list of text blocks; first one is the tool's JSON.
        payload = json.loads(content[0]["text"])
        assert "queen" in payload, f"pool_status payload missing 'queen': {payload}"
        assert "accounts" in payload, f"pool_status payload missing 'accounts'"
        print(f"  pool_status: queen={payload['queen']}, "
              f"accounts={[a['name'] for a in payload['accounts']]}")

        # 5. tools/call cost_status (read-only, safe)
        send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "cost_status", "arguments": {}},
        })
        resp = recv(proc)
        assert resp.get("id") == 4, f"cost_status id mismatch: {resp}"
        cost = json.loads(resp["result"]["content"][0]["text"])
        print(f"  cost_status: {len(cost['accounts'])} accounts tracked")

        print("\nMCP STDIO TEST: PASSED")
        return 0
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        # surface server stderr on failure
        stderr = proc.stderr.read() if proc.stderr else ""
        if stderr.strip():
            print(f"\n--- server stderr ---\n{stderr}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
