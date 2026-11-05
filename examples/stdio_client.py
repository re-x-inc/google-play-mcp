#!/usr/bin/env python3
"""Minimal stdio JSON-RPC smoke client for google-play-mcp.

Usage:
  GOOGLE_PLAY_SA_KEY_FILE=/path/to/key.json \
    python examples/stdio_client.py <packageName> [productId]

Spawns server.py, performs initialize -> tools/list -> tools/call and prints the results.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server.py")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    package = sys.argv[1]
    product = sys.argv[2] if len(sys.argv) > 2 else None
    proc = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def rpc(msg: dict) -> dict | None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if "id" not in msg:
            return None
        line = proc.stdout.readline()
        return json.loads(line)

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "stdio_client", "version": "0"}}})
    print("initialize:", json.dumps(init["result"]["serverInfo"]), init["result"]["protocolVersion"])
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    print("tools:", [t["name"] for t in tools["result"]["tools"]])
    if product:
        call = {"name": "play_get_subscription", "arguments": {"packageName": package, "productId": product}}
    else:
        call = {"name": "play_list_subscriptions", "arguments": {"packageName": package}}
    res = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
    r = res["result"]
    print("isError:", r.get("isError", False))
    print(r["content"][0]["text"][:1500])
    proc.stdin.close()
    return proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
