"""The only place DocSentinel talks to an LLM.

Every call is `claude -p` with all tools disabled and a JSON schema, so an
agent can only return data -- it can never read files, run commands or edit
anything. Writing is done by deterministic code elsewhere.

Modes (env DOCSENTINEL_LLM):
  live    always call claude; nothing is saved
  replay  never call claude, answer from recordings/ only
  record  use a recording if one exists, else call claude and save it
          (how recordings/ is made: `DOCSENTINEL_LLM=record python src/main.py eval`)
  off     no LLM at all (fork / Dependabot PRs without the secret, or --no-llm)
  auto    (default) live if the CLI is installed and authenticated, else replay
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

RECORDINGS_DIR = Path(__file__).parent / "recordings"
DEFAULT_MODEL = os.environ.get("DOCSENTINEL_MODEL", "sonnet")
TIMEOUT_S = 180


class LLMUnavailable(RuntimeError):
    """No live LLM and no recording for this prompt."""


@dataclass
class LLMResult:
    data: dict
    source: str  # "live" or "replay"
    cost_usd: float = 0.0
    duration_ms: int = 0


def claude_bin() -> str | None:
    return os.environ.get("DOCSENTINEL_CLAUDE_BIN") or shutil.which("claude")


KEY_VARS = ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
_key_var: str | None = None  # set by live_available() once a mapping is proven to work


def _key_var_order() -> list[str]:
    """Best guess first: sk-ant-oat... is a subscription token, anything else an API key."""
    key = os.environ.get("CLAUDE_KEY", "").strip()
    return KEY_VARS if key.startswith("sk-ant-oat") else KEY_VARS[::-1]


def _child_env(var: str | None = None) -> dict:
    """Map CLAUDE_KEY onto a variable the CLI actually reads (it ignores CLAUDE_KEY).

    With no CLAUDE_KEY, the CLI's own login is used (e.g. email login on a dev machine).
    """
    env = dict(os.environ)
    key = env.pop("CLAUDE_KEY", "").strip()
    if key:
        env[var or _key_var or _key_var_order()[0]] = key
    return env


def _recording_key(system: str, prompt: str, schema: dict) -> str:
    blob = json.dumps([system, prompt, schema], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _call_live(system: str, prompt: str, schema: dict, model: str, key_var: str | None = None,
               tools: str = "", cwd: Path | None = None, max_turns: int = 10) -> LLMResult:
    cmd = [
        claude_bin(), "-p", prompt,
        "--system-prompt", system,
        "--tools", tools,  # "" = no tools; the Concierge gets read-only "Read,Grep,Glob"
        *(["--allowedTools", tools] if tools else []),  # pre-approve them: -p mode can't ask
        "--setting-sources", "",  # ignore host settings, hooks and
        "--strict-mcp-config",  # MCP servers: same behaviour on every machine
        "--max-turns", str(max_turns),
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--model", model,
        "--no-session-persistence",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, env=_child_env(key_var), cwd=cwd)
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # stdout may carry the CLI's auth message; never echo env or key
        raise LLMUnavailable(f"claude -p failed (exit {proc.returncode}): {proc.stdout.strip()[:200]}")
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise LLMUnavailable(f"claude -p returned no structured output: {str(out.get('result'))[:200]}")
    return LLMResult(
        data=out["structured_output"],
        source="live",
        cost_usd=out.get("total_cost_usd", 0.0),
        duration_ms=out.get("duration_ms", 0),
    )


_live_ok: bool | None = None


def live_available() -> bool:
    """One cheap real call, cached for the process. Used by `auto` mode and `doctor`.

    If CLAUDE_KEY is set and the first variable mapping fails, the other one is
    tried, so a wrong prefix guess never silently drops us into replay.
    """
    global _live_ok, _key_var
    if _live_ok is None:
        _live_ok = False
        if claude_bin():
            candidates = _key_var_order() if os.environ.get("CLAUDE_KEY") else [None]
            for var in candidates:
                try:
                    r = _call_live("Answer briefly.", "Reply ok.", {
                        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                    }, DEFAULT_MODEL, key_var=var)
                except (LLMUnavailable, subprocess.TimeoutExpired, OSError):
                    continue
                if r.data.get("ok") is True:
                    _live_ok, _key_var = True, var
                    break
    return _live_ok


NO_CLI_BANNER = ("CLAUDE_KEY found but no claude CLI, running on recorded answers. To run live: "
                 "curl -fsSL https://claude.ai/install.sh | bash and add ~/.local/bin to PATH.")
_banner_shown = False


def key_without_cli() -> bool:
    return bool(os.environ.get("CLAUDE_KEY", "").strip()) and not claude_bin()


def mode() -> str:
    global _banner_shown
    m = os.environ.get("DOCSENTINEL_LLM", "auto")
    if m == "off":
        return "off"
    if m == "auto":
        live = live_available()
        if not live and key_without_cli() and not _banner_shown:
            _banner_shown = True  # once per run, on stderr so it never mixes into reports
            print(f"\n*** {NO_CLI_BANNER} ***\n", file=sys.stderr)
        return "live" if live else "replay"
    return m


STATS = {"calls": 0, "cost_usd": 0.0, "ms": 0}  # per process, read by the eval scorecard
_stats_lock = threading.Lock()


def ask(system: str, prompt: str, schema: dict, model: str = DEFAULT_MODEL,
        tools: str = "", cwd: Path | None = None, max_turns: int = 10) -> LLMResult:
    """Run one agent (single turn, or a read-only tool loop inside `cwd`) and return schema-shaped JSON."""
    key = _recording_key(system + tools, prompt, schema)  # cwd excluded: temp paths differ per run
    path = RECORDINGS_DIR / f"{key}.json"

    m = mode()
    if m == "off":
        raise LLMUnavailable("LLM is off")
    if m == "replay" or (m == "record" and path.exists()):
        if not path.exists():
            raise LLMUnavailable(f"replay mode: no recording {path.name}")
        with _stats_lock:
            STATS["calls"] += 1
        return LLMResult(data=json.loads(path.read_text())["data"], source="replay")

    try:
        result = _call_live(system, prompt, schema, model, tools=tools, cwd=cwd, max_turns=max_turns)
    except (LLMUnavailable, subprocess.TimeoutExpired):
        time.sleep(5)  # one retry: transient failures (rate limits) were seen under parallel load
        result = _call_live(system, prompt, schema, model, tools=tools, cwd=cwd, max_turns=max_turns)
    if m == "record":
        # Record mode reuses existing recordings, so each later step (fixer, reviewer,
        # concierge) is recorded against exactly the answers replay will return.
        RECORDINGS_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps({"data": result.data}, indent=2) + "\n")
    with _stats_lock:  # judge calls run in threads
        STATS["calls"] += 1
        STATS["cost_usd"] += result.cost_usd
        STATS["ms"] += result.duration_ms
    return result
