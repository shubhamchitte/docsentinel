"""Publisher: the only component that writes.

  patch    always: out/fix.patch + out/fix.md (PR description). Nothing in the repo changes.
  branch   --branch: commit the verified fixes on a new docs-sentinel/<sha> branch, locally
  pr       --pr: push that branch and open a DRAFT pull request with `gh`

Never touches the default branch and never merges. A human reviews and merges.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from . import gitio
from .fixer import SectionFix


def apply_edits(text: str, fixes: list[SectionFix]) -> str:
    lines = text.splitlines(keepends=True)
    for fx in fixes:
        for n, new in fx.edits.items():
            end = "\n" if lines[n - 1].endswith("\n") else ""
            lines[n - 1] = new + end
    return "".join(lines)


def build_patch(repo: Path, head: str, fixes: list[SectionFix]) -> tuple[str, dict[str, str]]:
    """(unified diff, {doc path: new content})."""
    by_doc: dict[str, list[SectionFix]] = {}
    for fx in fixes:
        by_doc.setdefault(fx.doc, []).append(fx)
    patch, new_files = [], {}
    for doc, fxs in sorted(by_doc.items()):
        old = gitio.read_at(repo, head, doc) or ""
        new = apply_edits(old, fxs)
        new_files[doc] = new
        patch += difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                      f"a/{doc}", f"b/{doc}")
    return "".join(patch), new_files


def pr_body(result: dict, applied: list[tuple], rejected: list[tuple], review_in_draft: bool = True) -> str:
    """Draft PR description (also shown in the PR comment). Low-confidence (REVIEW) fixes come first."""
    def line(fx) -> str:
        tag = " — *suggestion, low confidence*" if fx.low_confidence else ""
        return (f"- `{fx.doc}` → **{fx.section}** ({'+'.join(sorted(fx.by))}){tag}: {' '.join(fx.summaries)} "
                f"_(findings {', '.join(fx.finding_ids)})_")
    low = [fx for fx, _ in applied if fx.low_confidence]
    sure = [fx for fx, _ in applied if not fx.low_confidence]
    out = [f"Docs updated for code change `{result['base']}..{result['head']}`, proposed by DocSentinel.", "",
           "**A human approves this.** Review the edits, change them if needed, mark the draft "
           "*Ready for review*, and merge. DocSentinel never merges and never commits to the base branch.", ""]
    if low:
        where = "included below" if review_in_draft else "shown here only, not included in the draft (`review_sections: comment_only`)"
        out += [f"### ⚠️ Low confidence, check these first ({where})", "", *[line(fx) for fx in low], ""]
    if sure:
        out += ["### Stale sections (checks passed)", "", *[line(fx) for fx in sure], ""]
    if rejected:
        out += ["", "Not fixed automatically (flagged only):"]
        for fx, v in rejected:
            failed = "; ".join(f"{k}: {m}" for k, m in v.checks.items() if m != "pass")
            out.append(f"- `{fx.doc}` → **{fx.section}**: {failed}")
    return "\n".join(out) + "\n"


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(args, cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def to_branch(repo: Path, head: str, new_files: dict[str, str], message: str) -> str:
    branch = f"docs-sentinel/{gitio.resolve(repo, head)}"
    default = _run(repo, "git", "symbolic-ref", "--short", "HEAD")
    if branch == default:
        raise RuntimeError("refusing to write to the current branch")
    _run(repo, "git", "switch", "-q", "-c", branch, head)
    try:
        for doc, content in new_files.items():
            (repo / doc).write_text(content)
        _run(repo, "git", "add", *new_files)
        _run(repo, "git", "commit", "-q", "-m", message)
    finally:
        _run(repo, "git", "switch", "-q", default)
    return branch


def open_draft_pr(repo: Path, branch: str, base_branch: str, title: str, body: str) -> str:
    _run(repo, "git", "push", "-q", "-u", "origin", branch)
    return _run(repo, "gh", "pr", "create", "--draft", "--base", base_branch, "--head", branch,
                "--title", title, "--body", body, "--label", "documentation")
