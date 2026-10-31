"""Inbound hook: DocSentinel as an MCP server, so any MCP client (Claude Code, Cursor,
VS Code, ...) can ask it questions from inside the editor.

Tools (all read-only -- an MCP client can query DocSentinel but never make it write):
  ask_docs(question)   Docs Concierge: answer checked against current code, with stale-doc warnings
  check_docs(base, head)  drift report: which doc sections a change made stale

Add to Claude Code:
  claude mcp add docsentinel -- python /path/to/src/main.py mcp --repo /path/to/repo
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import concierge, gitio, judge, report
from .config import Config
from .flag import flag

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def build(repo: Path, cfg: Config) -> MCPServer:
    server = MCPServer(
        name="docsentinel",
        instructions="Use ask_docs for how-to questions about this repository: answers are checked "
                     "against the current code and warn about stale docs. Use check_docs to see "
                     "which docs a code change made stale.",
    )
    drift_cache: dict[tuple, dict] = {}

    def drift(base: str, head: str) -> dict:
        key = (gitio.resolve(repo, base), gitio.resolve(repo, head))
        if key not in drift_cache:
            drift_cache[key] = judge.judge(flag(repo, base, head, cfg), repo, head, cfg)
        return drift_cache[key]

    @server.tool(annotations=READ_ONLY)
    def ask_docs(question: str, base: str = "HEAD~1", head: str = "HEAD") -> str:
        """Answer a how-to question about this repository. The answer is checked against the
        current code, cites file:line sources, and warns where the docs are known to be stale.
        base/head: the code change whose doc drift should be taken into account."""
        a = concierge.ask(question, repo, cfg, drift(base, head))
        return concierge.to_markdown(question, a)

    @server.tool(annotations=READ_ONLY)
    def check_docs(base: str = "HEAD~1", head: str = "HEAD") -> str:
        """Report which documentation sections the code change base..head made stale, with
        line numbers and reasons. Read-only: proposes nothing and writes nothing."""
        return report.to_markdown(drift(base, head))

    return server
