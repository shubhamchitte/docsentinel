"""Render a flag result as Markdown: the text that becomes a PR comment or report.md."""

from __future__ import annotations

LABEL = {"stale": "STALE", "review": "REVIEW", "candidate": "CHECK", "ok": "ok", "gap": "GAP"}


def to_markdown(result: dict) -> str:
    findings = result["findings"]
    stale = [f for f in findings if f["status"] == "stale"]
    mode = result.get("llm_mode", "off")
    out = [
        f"## DocSentinel: {len(stale)} stale doc section(s)",
        "",
        f"Compared `{result['base']}` → `{result['head']}`: "
        f"{len(result['changes'])} public change(s), {len(findings)} finding(s). LLM: {mode}.",
        "",
    ]
    if not findings:
        out.append("No documentation affected.")
        return "\n".join(out) + "\n"
    out += ["| # | Status | Doc → section | Lines | Why |", "|---|---|---|---|---|"]
    # Findings judged together (same section, one verdict) are shown as one row: "F6+F10".
    rows: dict = {}
    for f in findings:
        key = (f["doc"], f["section_start"], "judged") if f.get("judge") else f["id"]
        if key in rows:
            rows[key]["id"] += f"+{f['id']}"
        else:
            rows[key] = dict(f)
    for f in rows.values():
        where = f"`{f['doc']}` → {f['section']}" if f["doc"] else "—"
        j = f.get("judge")
        lines = j["stale_lines"] if j and j["stale_lines"] else [n for n, _ in f["lines"]]
        why = f"{j['reason']} _(judge, {j['confidence']:.2f})_" if j else f["reason"]
        if f.get("judge_error"):
            why += " _(judge unavailable: left for review)_"
        out.append(f"| {f['id']} | {LABEL[f['status']]} | {where} | {', '.join(map(str, lines)) or '—'} "
                   f"| {why.replace('|', '/')} |")
    judged = len({(f["doc"], f["section_start"]) for f in findings if f.get("judge")})
    if result.get("capped"):
        out += ["", f"**{judged} section(s) judged, {result['capped']} skipped: cap reached** "
                    f"(`max_sections_per_run`). Skipped sections are listed as CHECK for a human."]
    if r := result.get("reuse"):
        out += ["", f"Verdicts reused from the PR check: {r['reused_sections']} section(s). {r['note']}".strip()]
    if u := result.get("usage"):
        out += ["", f"LLM usage this run: {u['llm_calls']} call(s), est. ${u['est_cost_usd']:.2f}."]
    out += ["", "_STALE = doc contradicts the code. REVIEW = judge unsure. CHECK = not judged. "
            "ok = judged still correct. GAP = new public API not documented._"]
    return "\n".join(out) + "\n"
