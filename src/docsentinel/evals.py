"""Eval harness: run every case, compare with its expected answer, print a scorecard.

Per case we score two pipelines on the same change:
  rules     every section the rules touch (stale + candidate) counts as flagged
  rules+llm only sections that end up "stale" after the Drift Judge
and then run the Fixer + Verifier on the rules+llm result.

Metrics (section level, summed over cases):
  precision  flagged sections that were really stale
  recall     really stale sections that were flagged
  traps      cases expecting no flags that stayed clean
  fixes      stale sections with a fix that passed every Verifier check
"""

from __future__ import annotations

import copy
import re
import time
from pathlib import Path

from . import fixer, judge, llm, scenario, verifier
from .config import Config
from .flag import flag


def _sections(findings: list[dict], statuses: set[str]) -> set[str]:
    return {f"{f['doc']}#{f['section']}" for f in findings if f["status"] in statuses and f["doc"]}


def _prf(pred: set[str], gold: set[str]) -> tuple[int, int, int]:
    return len(pred & gold), len(pred - gold), len(gold - pred)


def run_case(path: Path) -> dict:
    case = scenario.load_case(path)
    gold = set(case.get("expect", {}).get("stale", []))
    gold_gaps = set(case.get("expect", {}).get("gaps", []))
    repo = scenario.build(case)
    cfg = Config.load(repo)

    t0, c0 = time.time(), dict(llm.STATS)
    rules = flag(repo, "HEAD~1", "HEAD", cfg)
    rules_pred = _sections(rules["findings"], {"stale", "candidate"})
    judged = judge.judge(copy.deepcopy(rules), repo, "HEAD", cfg)
    llm_pred = _sections(judged["findings"], {"stale"})

    fixes = fixer.propose(judged, repo, "HEAD")
    applied, rejected = verifier.check_all(fixes, judged, repo, "HEAD", cfg)
    passed = [fx for fx, _ in applied]
    declined = [fx for fx in fixes if not fx.edits]  # fixer chose not to edit: left as a flag
    reviewer_rejected = [fx for fx, v in rejected if v.checks.get("review", "pass") != "pass"]
    gaps = {f["change"]["name"] for f in judged["findings"] if f["status"] == "gap"}

    return {
        "case": case["name"],
        "gold": sorted(gold),
        "rules": _prf(rules_pred, gold),
        "llm": _prf(llm_pred, gold),
        "missed": sorted(gold - llm_pred),
        "false_alarms": sorted(llm_pred - gold),
        "fixes_ok": len(passed),
        "fixes_total": len(fixes),
        "fixes_declined": len(declined),
        "fixes_reviewer_rejected": len(reviewer_rejected),
        "gaps_expected": len(gold_gaps),
        "gaps_found": len(gold_gaps & gaps),
        "llm_errors": sum(1 for f in judged["findings"] if f.get("judge_error"))
                      + sum(1 for fx in fixes if fx.error),
        "missing_recordings": sorted({m.group(0) for e in
                                      [f.get("judge_error") or "" for f in judged["findings"]] + [fx.error for fx in fixes]
                                      for m in [re.search(r"no recording \S+", e or "")] if m}),
        "llm_calls": llm.STATS["calls"] - c0["calls"],
        "cost_usd": llm.STATS["cost_usd"] - c0["cost_usd"],
        "seconds": time.time() - t0,
    }


def _ratio(a: int, b: int) -> str:
    return f"{a / b:.0%} ({a}/{b})" if b else "n/a"


def scorecard(rows: list[dict], mode: str) -> str:
    def total(key, i):
        return sum(r[key][i] for r in rows)

    out = [f"## DocSentinel eval scorecard ({len(rows)} cases, LLM: {mode})", "",
           "| Case | Expected stale | Rules TP/FP/FN | Rules+LLM TP/FP/FN | Fixes passed | LLM calls | s |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['case']} | {len(r['gold'])} | {'/'.join(map(str, r['rules']))} | "
                   f"{'/'.join(map(str, r['llm']))} | {r['fixes_ok']}/{r['fixes_total']} | "
                   f"{r['llm_calls']} | {r['seconds']:.0f} |")
    traps = [r for r in rows if not r["gold"]]
    out += ["", "| Metric | Rules only | Rules + LLM judge |", "|---|---|---|"]
    for name, (i, j) in {"Precision": (0, 1), "Recall": (0, 2)}.items():
        vals = []
        for key in ("rules", "llm"):
            tp, other = total(key, 0), total(key, j)
            vals.append(_ratio(tp, tp + other))
        out.append(f"| {name} | {vals[0]} | {vals[1]} |")
    out.append(f"| Trap cases clean (no false flags) | "
               f"{_ratio(sum(1 for r in traps if r['rules'][1] == 0), len(traps))} | "
               f"{_ratio(sum(1 for r in traps if r['llm'][1] == 0), len(traps))} |")
    fx_total = sum(r["fixes_total"] for r in rows)
    fx_ok = sum(r["fixes_ok"] for r in rows)
    fx_declined = sum(r["fixes_declined"] for r in rows)
    out += ["",
            f"- Stale or REVIEW sections with a verified fix (Verifier + Reviewer passed): {_ratio(fx_ok, fx_total)}",
            f"- Fixer declined, left as flag for a human: {fx_declined}",
            f"- Fix rejected by the Fix Reviewer (LLM, meaning check): {sum(r['fixes_reviewer_rejected'] for r in rows)}",
            f"- Fix rejected by Verifier (structure checks): "
            f"{fx_total - fx_ok - fx_declined - sum(r['fixes_reviewer_rejected'] for r in rows)}",
            f"- Expected GAPs found: {_ratio(sum(r['gaps_found'] for r in rows), sum(r['gaps_expected'] for r in rows))}",
            f"- LLM call failures (finding left unjudged/unfixed): {sum(r['llm_errors'] for r in rows)}",
            f"- LLM calls: {sum(r['llm_calls'] for r in rows)}, cost ${sum(r['cost_usd'] for r in rows):.2f}, "
            f"wall time {sum(r['seconds'] for r in rows):.0f}s",
            ""]
    misses = [(r["case"], s, "missed") for r in rows for s in r["missed"]]
    misses += [(r["case"], s, "false alarm") for r in rows for s in r["false_alarms"]]
    if misses:
        out += ["Errors (rules + LLM):", ""] + [f"- {c}: {kind} — {s}" for c, s, kind in misses] + [""]
    out.append("_Small hand-made suite: a health check, not a benchmark. Cases live in src/evals/cases/._")
    return "\n".join(out) + "\n"


# Sections for the consistency check: two clear-cut, two borderline (see docs/README.md).
CONSISTENCY = [
    ("default_change", "Getting Started"),          # clearly stale (states the old default)
    ("default_change", "Parse configuration as a stream"),  # clearly fine
    ("behavior_defaults", "Variable expansion"),    # borderline: "can interpolate"
    ("remove_param", "Disable load_dotenv"),         # borderline: "files or streams"
]


def consistency(cases_dir: Path, runs: int) -> str:
    """Ask the Judge `runs` times about the same sections (always live, never recordings)
    and report how often it agrees with itself. Confidence is the model's self-report."""
    import os
    os.environ["DOCSENTINEL_LLM"] = "live"
    rows, agree_total = [], 0
    for case_name, section in CONSISTENCY:
        case = scenario.load_case(cases_dir / f"{case_name}.yaml")
        repo = scenario.build(case)
        cfg = Config.load(repo)
        result = flag(repo, "HEAD~1", "HEAD", cfg)
        group = [f for f in result["findings"] if f["section"] == section and f["status"] == "candidate"]
        verdicts, confs = [], []
        for _ in range(runs):
            g = copy.deepcopy(group)
            judge._judge_section(g, repo, result["base"], "HEAD", cfg, "")
            verdicts.append(g[0]["status"])
            confs.append((g[0].get("judge") or {}).get("confidence", 0.0))
        majority = max(set(verdicts), key=verdicts.count)
        agree = verdicts.count(majority) / runs
        agree_total += agree
        rows.append(f"| {case_name} | {section} | {', '.join(verdicts)} | {agree:.0%} | "
                    f"{min(confs):.2f}–{max(confs):.2f} |")
    return "\n".join([
        f"## Judge consistency ({runs} runs per section, live)", "",
        "| Case | Section | Verdicts | Agreement with majority | Self-reported confidence |",
        "|---|---|---|---|---|", *rows, "",
        f"Mean agreement: {agree_total / len(CONSISTENCY):.0%}. Confidence is what the model reports about "
        f"itself, not a calibrated probability; the 0.7 fix threshold uses it.",
        f"LLM usage: {llm.STATS['calls']} call(s), est. ${llm.STATS['cost_usd']:.2f}.", ""])
