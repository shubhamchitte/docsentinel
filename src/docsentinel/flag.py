"""Rule-based flagger: turn (change, mention) pairs into findings.

Rules, strongest first. Only R1 is certain without an LLM; the rest are
candidates the Drift Judge decides on.
  R1 stale      doc names something that was removed or renamed
  R2 candidate  doc names a function AND one of the parameters that changed
  R3 candidate  doc names a symbol whose signature or options changed
  R4 gap        new public thing that no doc mentions (info only)
  R5 candidate  doc names a function or CLI command whose behaviour changed (same signature);
                the Judge gets the code diff and the test diff
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import docmap, gitio
from .config import Config
from .scout import Change, scout


@dataclass
class Finding:
    id: str
    status: str  # stale | candidate | gap
    rule: str
    change: dict
    doc: str = ""
    section: str = ""
    lines: list[list] = field(default_factory=list)  # [[line_no, text], ...]
    reason: str = ""
    section_text: str = ""
    section_start: int = 0
    section_end: int = 0
    judge: dict | None = None  # filled by the Drift Judge


def flag(repo: Path, base: str, head: str, cfg: Config, docs_ref: str | None = None) -> dict:
    """docs_ref: which version of the docs to check (default: head, i.e. as the change leaves them)."""
    changes = scout(repo, base, head, cfg)
    sections = docmap.load_sections(repo, docs_ref or head, cfg)
    findings: list[Finding] = []

    def add(status, rule, ch: Change, reason, m: docmap.Mention | None = None):
        f = Finding(f"F{len(findings) + 1}", status, rule, ch.to_dict(), reason=reason)
        if m:
            f.doc, f.section = m.section.doc, m.section.heading
            f.lines = [list(x) for x in m.lines]
            f.section_text, f.section_start, f.section_end = m.section.text, m.section.start, m.section.end
        findings.append(f)

    for ch in changes:
        mentions = docmap.find_mentions(ch, sections, cfg)
        if ch.change in ("removed", "renamed"):
            for m in mentions:
                what = f"renamed to `{ch.new_name}`" if ch.change == "renamed" else "removed"
                add("stale", "R1", ch, f"Doc uses `{ch.name}`, which was {what}.", m)
        elif ch.change == "changed":
            for m in mentions:
                if m.params_mentioned:
                    add("candidate", "R2", ch, f"Doc uses `{ch.name}` and mentions changed parameter(s) "
                        f"{', '.join(m.params_mentioned)}: {'; '.join(ch.details)}.", m)
                else:
                    add("candidate", "R3", ch, f"`{ch.name}` changed ({'; '.join(ch.details)}); doc uses it.", m)
        elif ch.change == "behavior":
            for m in mentions:
                add("candidate", "R5", ch, f"Behaviour of `{ch.name}` changed (same signature); doc uses it.", m)
        elif ch.change == "added" and not mentions and "." not in ch.name:
            add("gap", "R4", ch, f"New public `{ch.name}` is not mentioned in any doc.")

    return {
        "repo": str(repo),
        "base": gitio.resolve(repo, base),
        "head": gitio.resolve(repo, head),
        "base_sha": gitio.git(repo, "rev-parse", base).strip(),  # full SHAs: used to reuse verdicts
        "head_sha": gitio.git(repo, "rev-parse", head).strip(),
        "changes": [c.to_dict() for c in changes],
        "findings": [f.__dict__ for f in findings],
    }
