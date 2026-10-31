"""Fix Reviewer (reasoning): a second LLM reads each LLM-written fix against the code change.

The Verifier checks structure (scope, names, syntax); it cannot tell whether a
sentence is true. This agent can. Rule-made fixes (renames) skip it: they are certain.
A rejected fix is dropped and its findings stay as flags for a human.
"""

from __future__ import annotations

from pathlib import Path

from . import llm
from .fixer import SectionFix
from .judge import entity_source

SYSTEM = (Path(__file__).parent / "prompts" / "reviewer.md").read_text()

SCHEMA = {
    "type": "object",
    "properties": {"approve": {"type": "boolean"}, "problem": {"type": "string"}},
    "required": ["approve", "problem"],
}


def review(fix: SectionFix, findings: list[dict], repo: Path, head: str) -> tuple[bool, str]:
    changes = []
    for f in findings:
        ch = f["change"]
        name = ch.get("new_name") or ch["name"]
        src = entity_source(repo, head, ch["file"], name, max_lines=40) if ch["kind"] == "py" else ""
        changes.append(f"- {ch['name']}: {'; '.join(ch['details'])}\n```python\n{src or ch['after']}\n```")
    original = "\n".join(f"{fix.start + i:>4} | {line}" for i, line in enumerate(fix.old_lines))
    edits = "\n".join(f"{n:>4} | - {fix.old_lines[n - fix.start]}\n     | + {new}"
                      for n, new in sorted(fix.edits.items()))
    prompt = (f"## Code changes\n" + "\n".join(changes) +
              f"\n\n## Original section: {fix.doc} -> {fix.section}\n{original}\n\n## Proposed edits\n{edits}\n")
    try:
        r = llm.ask(SYSTEM, prompt, SCHEMA)
    except Exception as e:
        return False, f"reviewer unavailable: {str(e)[:120]}"  # unsure -> don't auto-fix
    return bool(r.data["approve"]), r.data.get("problem", "")
