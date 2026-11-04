"""MCP server tests: both tools are registered, read-only, and annotated correctly.

Run: python src/tests/test_mcp_server.py   (no LLM, no network, plain asserts; needs `mcp`)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docsentinel.config import Config  # noqa: E402
from docsentinel.mcp_server import build  # noqa: E402

EXPECTED = {"ask_docs", "check_docs"}


def main() -> int:
    server = build(Path(__file__).resolve().parents[2], Config())
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    failed = 0

    def check(name, ok):
        nonlocal failed
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        failed += not ok

    check("exposes exactly ask_docs and check_docs", set(tools) == EXPECTED)
    for name in sorted(EXPECTED & set(tools)):
        a = tools[name].annotations
        check(f"{name}: read-only, non-destructive", a.read_only_hint is True and a.destructive_hint is False)
        check(f"{name}: idempotent, closed-world", a.idempotent_hint is True and a.open_world_hint is False)
        check(f"{name}: has a description", bool((tools[name].description or "").strip()))
    print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
