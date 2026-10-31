"""Docs Concierge (agent): answer "how do I...?" from docs checked against today's code.

Unlike the Judge and Fixer, this agent plans its own steps: it decides which
docs and source files to search and read (read-only tools, confined to the repo
by the CLI). It is told which doc sections are known to be stale, so it warns
instead of repeating them.

Post-checks on every answer (deterministic):
  citations  each cited file exists and the line is in range
  imports    names the snippet imports from the project exist in the current code
  stale      a citation into a known-stale doc section adds a warning
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gitio, llm
from .config import Config
from .verifier import public_names

SYSTEM = (Path(__file__).parent / "prompts" / "concierge.md").read_text()
TOOLS = "Read,Grep,Glob"

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "snippet": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "line": {"type": "integer"}},
                "required": ["file", "line"],
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "snippet", "citations", "warnings", "confidence"],
}


def drift_summary(result: dict | None) -> str:
    if not result:
        return "(no drift report available)"
    stale = [f for f in result["findings"] if f["status"] in ("stale", "review")]
    if not stale:
        return "No doc sections are known to be stale."
    lines = []
    for f in stale:
        reason = f["judge"]["reason"] if f.get("judge") else f["reason"]
        lines.append(f"- {f['doc']} -> \"{f['section']}\" (lines {f['section_start']}-{f['section_end']}): {reason}")
    return "\n".join(lines)


def _rel(path: str, repo: Path) -> str:
    """Citations may come back absolute; make them repo-relative."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(repo.resolve())) if p.is_absolute() else path
    except ValueError:
        return path  # outside the repo: will fail the existence check


def check(answer: dict, repo: Path, cfg: Config, result: dict | None) -> dict:
    problems, auto_warn = [], []
    for c in answer["citations"]:
        c["file"] = _rel(c["file"], repo)
        f = repo / c["file"]
        n_lines = len(f.read_text().splitlines()) if f.is_file() else 0
        c["valid"] = f.is_file() and 1 <= c["line"] <= n_lines
        if not c["valid"]:
            problems.append(f"citation {c['file']}:{c['line']} does not exist")
        for fd in (result or {}).get("findings", []):
            if (fd["status"] == "stale" and fd["doc"] == c["file"]
                    and fd["section_start"] <= c["line"] <= fd["section_end"]):
                auto_warn.append(f"{c['file']}:{c['line']} is in a section flagged stale (\"{fd['section']}\").")

    names, packages = public_names(repo, "HEAD", cfg)
    for m in re.finditer(r"from (\w+) import ([\w ,]+)", answer["snippet"]):
        if m.group(1) in packages:
            for name in (x.strip() for x in m.group(2).split(",")):
                if name and name not in names:
                    problems.append(f"snippet imports `{name}`, which does not exist in the current code")

    answer["warnings"] += sorted(set(auto_warn) - set(answer["warnings"]))
    answer["checks"] = {"passed": not problems, "problems": problems}
    return answer


def ask(question: str, repo: Path, cfg: Config, result: dict | None) -> dict:
    prompt = (f"Question: {question}\n\n"
              f"Known stale doc sections (drift report):\n{drift_summary(result)}\n")
    r = llm.ask(SYSTEM, prompt, SCHEMA, tools=TOOLS, cwd=repo, max_turns=15)
    answer = check(dict(r.data), repo, cfg, result)
    answer["source"] = r.source
    answer["verified_at"] = gitio.resolve(repo, "HEAD")
    answer["verified_sha"] = gitio.git(repo, "rev-parse", "HEAD").strip()
    return answer


def to_markdown(question: str, a: dict, link: str | None = None) -> str:
    """link: e.g. https://github.com/owner/repo -> citations become permalinks at the verified commit."""
    out = [f"### Q: {question}", "", a["answer"], "", "```python", a["snippet"].strip(), "```", ""]
    if a["warnings"]:
        out += ["**Warnings**", ""] + [f"- {w}" for w in a["warnings"]] + [""]
    def cite(c: dict) -> str:
        label = f"`{c['file']}:{c['line']}`"
        if link and c["valid"]:
            label = f"[{label}]({link.rstrip('/')}/blob/{a['verified_sha']}/{c['file']}#L{c['line']})"
        return label + ("" if c["valid"] else " (invalid)")
    cites = ", ".join(cite(c) for c in a["citations"])
    out.append(f"Sources: {cites or 'none'}")
    status = "all checks passed" if a["checks"]["passed"] else "; ".join(a["checks"]["problems"])
    out.append(f"Checked against code at `{a['verified_at']}`: {status}. "
               f"Confidence {a['confidence']:.2f}. LLM: {a['source']}.")
    return "\n".join(out) + "\n"
