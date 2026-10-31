"""Doc Fixer: propose minimal line edits for stale sections.

Two paths:
  action     renames (rule R1, renamed) are fixed by plain search/replace in code
             contexts -- certain, free, no LLM
  reasoning  everything else goes to `claude -p`, which may only return
             {line, new_text} replacements inside the section
Nothing here writes files. Output is a list of proposed edits for the Verifier.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import llm
from .judge import entity_source
from pathlib import Path

SYSTEM = (Path(__file__).parent / "prompts" / "fixer.md").read_text()

SCHEMA = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"line": {"type": "integer"}, "new_text": {"type": "string"}},
                "required": ["line", "new_text"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["replacements", "summary"],
}


@dataclass
class SectionFix:
    doc: str
    section: str
    start: int
    end: int
    old_lines: list[str]
    finding_ids: list[str]
    edits: dict[int, str] = field(default_factory=dict)  # line number -> new text
    summaries: list[str] = field(default_factory=list)
    by: set[str] = field(default_factory=set)  # "rule" / "llm"
    error: str = ""
    low_confidence: bool = False  # the section was REVIEW (Judge unsure): a suggestion for a human


def _group(findings: list[dict]) -> list[SectionFix]:
    groups: dict[tuple, SectionFix] = {}
    for f in findings:
        if f["status"] not in ("stale", "review") or not f["doc"]:
            continue
        key = (f["doc"], f["section_start"])
        if key not in groups:
            groups[key] = SectionFix(f["doc"], f["section"], f["section_start"], f["section_end"],
                                     f["section_text"].splitlines(), [])
        groups[key].finding_ids.append(f["id"])
        groups[key].low_confidence |= f["status"] == "review"
    return list(groups.values())


def _rename_in_code(line: str, old: str, new: str) -> str:
    """Replace whole-word `old` inside inline code spans and on fenced lines (caller decides)."""
    return re.sub(rf"(?<![\w.-]){re.escape(old)}(?![\w-])", new, line)


def _apply_renames(fix: SectionFix, findings: list[dict]) -> list[dict]:
    """Rule fixes for renames. Returns findings that still need the LLM."""
    rest = []
    in_fence = False
    fence_state = []
    for line in fix.old_lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        fence_state.append(in_fence)
    for f in findings:
        ch = f["change"]
        if not (f["rule"] == "R1" and ch["change"] == "renamed"):
            rest.append(f)
            continue
        old, new = ch["name"].split()[-1], ch["new_name"].split()[-1]  # cli_opt "list --format" -> "--format"
        for i, line in enumerate(fix.old_lines):
            n = fix.start + i
            cur = fix.edits.get(n, line)
            if fence_state[i]:
                updated = _rename_in_code(cur, old, new)
            else:
                updated = re.sub(r"`[^`]*`", lambda m: _rename_in_code(m.group(0), old, new), cur)
            if updated != cur:
                fix.edits[n] = updated
        fix.summaries.append(f"Rename `{old}` to `{new}`.")
        fix.by.add("rule")
    return rest


def _llm_fix(fix: SectionFix, findings: list[dict], repo: Path, head: str) -> None:
    current = [fix.edits.get(fix.start + i, line) for i, line in enumerate(fix.old_lines)]
    numbered = "\n".join(f"{fix.start + i:>4} | {line}" for i, line in enumerate(current))
    problems, sources = [], []
    for f in findings:
        ch = f["change"]
        reason = f["judge"]["reason"] if f.get("judge") else f["reason"]
        problems.append(f"- {reason}")
        name = ch.get("new_name") or ch["name"]
        if ch["kind"] == "py" and (src := entity_source(repo, head, ch["file"], name, max_lines=25)):
            sources.append(src)
        elif ch["after"]:
            sources.append(f"{name}: {ch['after']}")
    prompt = (f"## Problems\n" + "\n".join(problems) +
              "\n\n## New code\n```python\n" + "\n\n".join(sources) + "\n```\n\n"
              f"## Section: {fix.doc} -> {fix.section}\n{numbered}\n")
    try:
        r = llm.ask(SYSTEM, prompt, SCHEMA)
    except Exception as e:
        fix.error = f"fixer unavailable: {str(e)[:150]}"
        return
    for rep in r.data["replacements"]:
        n, text = rep["line"], rep["new_text"]
        if fix.start <= n <= fix.end and "\n" not in text:
            fix.edits[n] = text
    fix.summaries.append(r.data["summary"])
    fix.by.add("llm")


def propose(result: dict, repo: Path, head: str) -> list[SectionFix]:
    by_id = {f["id"]: f for f in result["findings"]}
    fixes = _group(result["findings"])
    for fix in fixes:
        rest = _apply_renames(fix, [by_id[i] for i in fix.finding_ids])
        if rest:
            _llm_fix(fix, rest, repo, head)
    return fixes
