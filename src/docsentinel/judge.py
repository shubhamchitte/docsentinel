"""Drift Judge (reasoning): is a candidate doc section actually stale?

One `claude -p` call per doc section (all changes touching it together), no tools,
strict JSON. Certain
findings (rule R1) are not sent: the rules already proved them.
Guardrails on the answer:
  - stale_lines outside the section are dropped
  - stale with confidence below the threshold is kept as "needs review", never fixed
  - an LLM failure leaves the finding as a candidate (flag, never edit)
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitio, llm
from .config import Config

SYSTEM = (Path(__file__).parent / "prompts" / "judge.md").read_text()

SCHEMA = {
    "type": "object",
    "properties": {
        "stale": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "stale_lines": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["stale", "confidence", "reason", "stale_lines"],
}


def entity_source(repo: Path, ref: str, file: str, name: str, max_lines: int = 60) -> str:
    """Source of a top-level function/class (or Class.method) at a revision, trimmed."""
    src = gitio.read_at(repo, ref, file) or ""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    parts = name.split(".")
    nodes = tree.body
    for i, part in enumerate(parts):
        node = next((n for n in nodes if getattr(n, "name", None) == part), None)
        if node is None:
            return ""
        if i == len(parts) - 1:
            seg = ast.get_source_segment(src, node) or ""
            lines = seg.splitlines()
            return "\n".join(lines[:max_lines]) + ("\n    ..." if len(lines) > max_lines else "")
        nodes = node.body
    return ""


def source_at_line(repo: Path, ref: str, file: str, line: int) -> str:
    """Source of the function defined at `line` (works for CLI commands, whose function
    name differs from the command name)."""
    src = gitio.read_at(repo, ref, file) or ""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == line:
            return ast.get_source_segment(src, node) or ""
    return ""


def code_diff(repo: Path, base: str, head: str, ch: dict, max_lines: int = 80) -> str:
    if not ch.get("old_line"):
        return ""
    old = source_at_line(repo, base, ch["file"], ch["old_line"]).splitlines()
    new = source_at_line(repo, head, ch["file"], ch["line"]).splitlines()
    diff = list(difflib.unified_diff(old, new, "before", "after", lineterm="", n=2))[2:]
    return "\n".join(diff[:max_lines]) + ("\n..." if len(diff) > max_lines else "")


def test_diff(repo: Path, base: str, head: str, cfg: Config, max_lines: int = 120) -> str:
    """Test changes show the new behaviour clearly; shared by every section in the run."""
    specs = [f":(glob){p}" for p in cfg.tests]
    try:
        lines = gitio.git(repo, "diff", "-U2", f"{base}..{head}", "--", *specs).splitlines()
    except Exception:
        return ""
    lines = [ln for ln in lines if not ln.startswith(("diff --git", "index "))]
    return "\n".join(lines[:max_lines]) + ("\n..." if len(lines) > max_lines else "")


def build_prompt(findings: list[dict], sources: dict[str, str], diffs: dict[str, str] | None = None,
                 tests: str = "") -> str:
    f0 = findings[0]
    numbered = "\n".join(f"{f0['section_start'] + i:>4} | {line}"
                         for i, line in enumerate(f0["section_text"].splitlines()))
    changes = []
    for f in findings:
        ch = f["change"]
        name = ch.get("new_name") or ch["name"]
        changes.append(f"""### {ch['name']} ({ch['kind']}) in {ch['file']}
Change: {ch['change']}
Old signature: {ch['before'] or '-'}
New signature: {ch['after'] or '-'}
Details: {'; '.join(ch['details'])}
New source:
```python
{sources.get(name) or '(not available)'}
```""" + (f"""
Code diff:
```diff
{(diffs or {})[name]}
```""" if (diffs or {}).get(name) else ""))
    test_part = f"\n\n## Test changes in this commit\n```diff\n{tests}\n```" if tests else ""
    return ("## Code changes\n" + "\n\n".join(changes) + test_part +
            f"\n\n## Documentation section: {f0['doc']} -> {f0['section']}\n{numbered}\n")


def _judge_section(group: list[dict], repo: Path, base: str, head: str, cfg: Config, tests: str) -> None:
    """One call per doc section, with every change that touches it: one consistent verdict."""
    sources, diffs = {}, {}
    for f in group:
        ch = f["change"]
        name = ch.get("new_name") or ch["name"]
        sources[name] = (entity_source(repo, head, ch["file"], name)
                         or source_at_line(repo, head, ch["file"], ch["line"]))
        if ch["change"] == "behavior":
            diffs[name] = code_diff(repo, base, head, ch)
    has_behavior = any(f["change"]["change"] == "behavior" for f in group)
    try:
        r = llm.ask(SYSTEM, build_prompt(group, sources, diffs, tests if has_behavior else ""), SCHEMA)
    except Exception as e:  # LLM down / no recording: stay a candidate
        for f in group:
            f["judge"], f["judge_error"] = None, str(e)[:200]
        return
    v = dict(r.data)
    v["source"] = r.source
    _apply(group, v, cfg)


def _apply(group: list[dict], v: dict, cfg: Config) -> None:
    f0 = group[0]
    valid = set(range(f0["section_start"], f0["section_end"] + 1))
    v["stale_lines"] = [n for n in v.get("stale_lines", []) if n in valid]
    v["confidence"] = max(0.0, min(1.0, float(v.get("confidence", 0))))
    if v["stale"] and v["confidence"] >= cfg.min_confidence_to_fix:
        status = "stale"
    elif v["stale"]:
        status = "review"  # LLM thinks stale but isn't sure: human looks, nothing auto-fixed
    else:
        status = "ok"
    for f in group:
        f["judge"], f["status"] = v, status


def verdict_key(group: list[dict]) -> str:
    """Identity of one judgement: the section's exact text plus the exact changes shown to the Judge."""
    f0 = group[0]
    changes = sorted(json.dumps([f["change"]["name"], f["change"]["change"], f["change"]["details"]])
                     for f in group)
    blob = json.dumps([f0["doc"], f0["section"], f0["section_text"], changes])
    return hashlib.sha256(blob.encode()).hexdigest()


def prior_verdicts(prior: dict | None, repo: Path, base: str, head: str) -> tuple[dict, str]:
    """Verdicts from an earlier run (the PR check), usable only if it judged the same change.
    Returns ({verdict_key: verdict}, note)."""
    if not prior:
        return {}, ""
    try:
        same = _patch_id(repo, prior["base_sha"], prior["head_sha"]) == _patch_id(repo, base, head)
    except Exception:
        same = False
    if not same:
        return {}, "PR verdicts not reused: merged change differs from what was judged; re-judged."
    groups: dict[tuple, list[dict]] = {}
    for f in prior.get("findings", []):
        if f.get("judge"):
            groups.setdefault((f["doc"], f["section_start"]), []).append(f)
    return {verdict_key(g): g[0]["judge"] for g in groups.values()}, ""


def _patch_id(repo: Path, base: str, head: str) -> str:
    diff = gitio.git(repo, "diff", f"{base}..{head}")
    out = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=diff,
                         capture_output=True, text=True, check=True).stdout
    return out.split()[0] if out.strip() else ""


def judge(result: dict, repo: Path, head: str, cfg: Config, prior: dict | None = None) -> dict:
    groups: dict[tuple, list[dict]] = {}
    for f in result["findings"]:
        if f["status"] == "candidate":
            groups.setdefault((f["doc"], f["section_start"]), []).append(f)
    known, note = prior_verdicts(prior, repo, result["base"], head)
    reused = 0
    for key in list(groups):
        v = known.get(verdict_key(groups[key]))
        if v:  # same section text, same changes, same merged diff: one decision per change
            _apply(groups.pop(key), {**v, "source": "reused from PR check"}, cfg)
            reused += 1
    if prior:
        result["reuse"] = {"reused_sections": reused, "note": note}
    todo = list(groups.values())[: cfg.max_sections_per_run]
    result["llm_mode"] = llm.mode()  # resolve once, before threads start
    if len(groups) > len(todo):
        result["capped"] = len(groups) - len(todo)  # over max_sections_per_run: left unjudged
    base = result["base"]
    tests = test_diff(repo, base, head, cfg) if any(
        f["change"]["change"] == "behavior" for g in todo for f in g) else ""
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda g: _judge_section(g, repo, base, head, cfg, tests), todo))
    return result
