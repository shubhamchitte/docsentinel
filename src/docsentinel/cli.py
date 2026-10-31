"""DocSentinel command line: `docsentinel --help` (installed) or `python src/main.py --help` (checkout)."""

import argparse
import json
import os
import sys
from pathlib import Path

from docsentinel import flag as flagger
from docsentinel import concierge, evals, fixer, hooks, judge, llm, publish, report, scenario, verifier
from docsentinel.config import Config

SRC = Path(__file__).resolve().parents[1]  # repo checkout: src/ (evals and demos live here)


def cmd_doctor(_args) -> int:
    print(f"claude CLI : {llm.claude_bin() or 'NOT FOUND'}")
    print(f"CLAUDE_KEY : {'set' if llm.os.environ.get('CLAUDE_KEY') else 'not set (using CLI login)'}")
    live = llm.live_available()
    print(f"live LLM   : {'ok' if live else 'unavailable'}" + (f" (key via {llm._key_var})" if llm._key_var else ""))
    print(f"mode       : {llm.mode()}")
    if not live and llm.key_without_cli():
        print(f"-> {llm.NO_CLI_BANNER}")
    elif not live:
        print("-> running on recorded LLM responses (replay). Results match the recorded demo run.")
    return 0


def _target(args) -> tuple[Path, str, str]:
    """Either a real repo (--repo/--base/--head) or a demo case built into a throwaway repo."""
    if args.case:
        repo = scenario.build(scenario.load_case(Path(args.case)))
        print(f"[case] built {args.case} at {repo}", file=sys.stderr)
        return repo, "HEAD~1", "HEAD"
    return Path(args.repo).resolve(), args.base, args.head


def _write(out_dir: Path, name: str, text: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(text)


def _flag(args):
    if args.no_llm:
        os.environ["DOCSENTINEL_LLM"] = "off"  # rules only: also stops the fixer's LLM path
    repo, base, head = _target(args)
    cfg = Config.load(repo, args.config)
    if args.max_sections is not None:
        cfg.max_sections_per_run = args.max_sections
    result = flagger.flag(repo, base, head, cfg)
    prior = None
    if getattr(args, "reuse", None) and Path(args.reuse).is_file():
        prior = json.loads(Path(args.reuse).read_text())
    result = judge.judge(result, repo, head, cfg, prior) if not args.no_llm else {**result, "llm_mode": "off"}
    out = Path(args.out)
    result["notify_slack"] = cfg.notify_slack  # read by `notify` in the write job (no checkout there)
    _save(out, result)
    return repo, head, cfg, result, out


def _save(out: Path, result: dict) -> None:
    result["usage"] = {"llm_calls": llm.STATS["calls"], "est_cost_usd": round(llm.STATS["cost_usd"], 4)}
    _write(out, "report.json", json.dumps(result, indent=2))
    _write(out, "report.md", report.to_markdown(result))


def _hooks(args, cfg, event: str, result: dict, fix_summary: dict | None = None) -> None:
    targets = list(cfg.webhooks) + ([{"url": args.webhook}] if args.webhook else [])
    for line in hooks.fire(targets, hooks.payload(event, result, fix_summary)):
        print(f"[hook] {line}", file=sys.stderr)


def cmd_flag(args) -> int:
    repo, head, cfg, result, out = _flag(args)
    print(report.to_markdown(result))
    print(f"[out] {out}/report.md, {out}/report.json", file=sys.stderr)
    _hooks(args, cfg, "flag", result)
    return 0


def cmd_fix(args) -> int:
    repo, head, cfg, result, out = _flag(args)
    applied, rejected = verifier.check_all(fixer.propose(result, repo, head), result, repo, head, cfg)
    in_draft = cfg.review_sections != "comment_only"
    draft = [fx for fx, _ in applied if in_draft or not fx.low_confidence]

    patch, new_files = publish.build_patch(repo, head, draft)           # what the draft PR will contain
    suggest, _ = publish.build_patch(repo, head, [fx for fx, _ in applied])  # every suggestion, for the PR comment
    body = publish.pr_body(result, applied, rejected, in_draft)
    _write(out, "fix.patch", patch)
    _write(out, "suggest.patch", suggest)
    _write(out, "fix.md", body)
    _save(out, result)  # refresh usage now that fixer/reviewer calls are counted
    print(body)
    print(patch or "(no verified fixes)")
    print(f"[out] {out}/fix.patch, {out}/fix.md", file=sys.stderr)

    branch = url = None
    if (args.branch or args.pr) and new_files:
        branch = publish.to_branch(repo, head, new_files, f"docs: sync with {result['head']} (DocSentinel)")
        print(f"Auto-update: committed {len(draft)} verified doc fix(es) on branch `{branch}`. "
              f"Default branch untouched; a human merges (with --pr: as a draft PR).")
        if args.pr:
            url = publish.open_draft_pr(repo, branch, args.pr_base, f"docs: sync with {result['head']}", body)
            print(f"[pr] draft PR: {url}", file=sys.stderr)
    _hooks(args, cfg, "fix", result, {
        "applied": [{"doc": fx.doc, "section": fx.section, "by": sorted(fx.by)} for fx, _ in applied],
        "left_for_human": [{"doc": fx.doc, "section": fx.section} for fx, _ in rejected],
        "branch": branch, "pr": url,
    })
    return 0


def cmd_hooks_demo(args) -> int:
    """Local receiver + signed webhook: genuine event accepted, tampered and replayed ones rejected."""
    import secrets
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    secret = secrets.token_hex(16)  # in real use: a shared secret in an env var on both sides
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            ok, why = hooks.verify(secret, dict(self.headers), body)
            received.append((ok, why, json.loads(body) if ok else None))
            self.send_response(200 if ok else 401)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/docsentinel"

    repo = scenario.build(scenario.load_case(Path(args.case)))
    cfg = Config.load(repo)
    result = judge.judge(flagger.flag(repo, "HEAD~1", "HEAD", cfg), repo, "HEAD", cfg)
    p = hooks.payload("flag", result)
    body = json.dumps(p).encode()

    def post(ts: str, data: bytes, sig_body: bytes) -> int:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "X-DocSentinel-Timestamp": ts,
            "X-DocSentinel-Signature": hooks.sign(secret, ts, sig_body)})
        try:
            return urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    now = str(int(time.time()))
    old = str(int(time.time()) - 600)
    tampered = body.replace(b'"stale"', b'"ok"', 1)
    trials = [
        ("genuine event", hooks.send(url, p, secret)),
        ("tampered body (stale -> ok)", post(now, tampered, body)),
        ("replayed 10 min later", post(old, body, body)),
    ]
    server.shutdown()
    print("Webhook demo: DocSentinel -> local receiver (HMAC-SHA256 signed)\n")
    for (name, status), (ok, why, _) in zip(trials, received):
        print(f"  {name:<30} HTTP {status}  {why}")
    got = next((r[2] for r in received if r[0]), None)
    if got:
        print(f"\nReceiver got: {got['event']}, counts {got['counts']}, {len(got['findings'])} findings, "
              f"e.g. {got['findings'][0]['status']} {got['findings'][0]['section']!r}")
    expected = [True, False, False]
    return 0 if [r[0] for r in received] == expected else 1


def cmd_ask(args) -> int:
    repo, base, head = _target(args)
    cfg = Config.load(repo, args.config)
    result = None
    if not args.no_drift:  # the concierge knows which docs are stale
        result = judge.judge(flagger.flag(repo, base, head, cfg), repo, head, cfg)
    try:
        a = concierge.ask(args.question, repo, cfg, result)
    except llm.LLMUnavailable as e:
        print(f"Docs Concierge needs a live LLM for this question ({e}). Run `python src/main.py doctor`.")
        return 1
    md = concierge.to_markdown(args.question, a, args.link)
    _write(Path(args.out), "answer.md", md)
    _write(Path(args.out), "answer.json", json.dumps(a, indent=2))
    print(md)
    return 0


def cmd_trial(args) -> int:
    """Dry run on history: what would DocSentinel have flagged on the last N commits?
    Rules only by default ($0). With --llm, the Judge decides candidates until --max-calls
    is used up. Nothing is written to the repo."""
    from docsentinel import gitio
    repo = Path(args.repo).resolve()
    cfg = Config.load(repo, args.config)
    if not args.llm:
        os.environ["DOCSENTINEL_LLM"] = "off"
    budget = args.max_calls
    commits = (args.commits.split(",") if args.commits else
               gitio.git(repo, "rev-list", "--first-parent", "--no-merges", f"-n{args.last}", "HEAD").split())
    keys = ["stale", "candidate", "gap", "review", "ok"]
    rows, total = [], dict.fromkeys(keys, 0)
    llm_judged = llm_ok = 0
    for sha in commits:
        parents = gitio.git(repo, "rev-list", "--parents", "-n1", sha).split()[1:]
        if not parents:
            continue
        # --docs-at base: judge the change against the docs BEFORE it, so doc fixes the
        # author made in the same commit don't hide the drift.
        r = flagger.flag(repo, parents[0], sha, cfg, docs_ref=parents[0] if args.docs_at == "base" else None)
        if not r["changes"]:
            continue
        sections = len({(f["doc"], f["section_start"]) for f in r["findings"] if f["status"] == "candidate"})
        if args.llm and sections and budget > 0:
            cfg.max_sections_per_run = budget
            r = judge.judge(r, repo, sha, cfg)
            budget -= min(sections, budget)
        counts = {k: len({(f["doc"], f["section_start"]) for f in r["findings"] if f["status"] == k}) for k in keys}
        judged_secs = {(f["doc"], f["section_start"], f["status"]) for f in r["findings"] if f.get("judge")}
        llm_judged += len(judged_secs)
        llm_ok += sum(1 for *_, st in judged_secs if st == "ok")
        for k in keys:
            total[k] += counts[k]
        kinds = sorted({c["change"] for c in r["changes"]})
        subject = gitio.git(repo, "log", "-1", "--format=%s", sha).strip()[:55]
        rows.append(f"| `{sha[:7]}` | {subject.replace('|', '/')} | {', '.join(kinds)} | {counts['stale']} | "
                    f"{counts['review']} | {counts['candidate']} | {counts['ok']} | {counts['gap']} |")
    judged = llm_judged
    mode = (f"LLM Judge on up to {args.max_calls} sections" if args.llm else "rules only, no LLM, $0")
    mode += f"; docs as of {'the parent commit' if args.docs_at == 'base' else 'the commit'}"
    md = [f"## DocSentinel trial: last {args.last} commits of `{args.name or repo.name}` ({mode})", "",
          f"{len(rows)} commit(s) changed the public API or the behaviour of documented code. "
          "Counts are doc sections. STALE = wrong. REVIEW = judge unsure. CHECK = would go to the LLM "
          "Judge (≈1 call each). ok = judged still correct. GAP = new public API with no doc.", "",
          "| Commit | Subject | Change kinds | STALE | REVIEW | CHECK | ok | GAP |",
          "|---|---|---|---|---|---|---|---|", *rows, "",
          f"Totals: {total['stale']} stale, {total['review']} review, {total['candidate']} unjudged, "
          f"{total['ok']} ok, {total['gap']} gaps."]
    if judged:
        md.append(f"Judge 'ok' rate: {llm_ok / judged:.0%} of {judged} LLM-judged sections "
                  f"(rule candidates the LLM cleared as still correct; rule-certain STALE not counted).")
    md.append(f"LLM usage: {llm.STATS['calls']} call(s), est. ${llm.STATS['cost_usd']:.2f}.")
    text = "\n".join(md) + "\n"
    _write(Path(args.out), "trial.md", text)
    print(text)
    return 0


def cmd_notify(args) -> int:
    """Send an existing run's result to outbound hooks (used by the no-LLM write jobs).
    Slack: SLACK_WEBHOOK_URL from the environment; generic: --webhook / config."""
    result = json.loads(Path(args.report).read_text())
    links = {k: v for k, v in {"repo": args.repo_name, "pr": args.pr_url, "commit": args.commit_url,
                                "fix_pr": args.fix_pr_url, "run": args.run_url}.items() if v}
    reportable = [f for f in result["findings"] if f["status"] in ("stale", "review", "candidate", "gap")]
    if not reportable and result.get("notify_slack", "changes") != "always":
        print("[hook] nothing to report (all counts 0); not posting. Set notify_slack: always to post every run.")
        return 0
    targets = []
    if os.environ.get("SLACK_WEBHOOK_URL"):
        targets.append({"url": os.environ["SLACK_WEBHOOK_URL"], "format": "slack"})
    if args.webhook:
        targets.append({"url": args.webhook})
    for line in hooks.fire(targets, hooks.payload(args.event, result, None, links)):
        print(f"[hook] {line}")
    if not targets:
        print("[hook] no targets configured; nothing sent")
    return 0


def cmd_mcp(args) -> int:
    from docsentinel import mcp_server  # optional dependency, only needed here
    repo, _, _ = _target(args)
    mcp_server.build(repo, Config.load(repo, args.config)).run("stdio")
    return 0


def cmd_mcp_demo(args) -> int:
    """Act as an MCP client (like Claude Code would): start the server, list tools, call them."""
    import asyncio
    import os

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    question = "How do I read a .env file into a dict without changing os.environ?"
    # stdio servers only inherit a few env vars by default: pass ours explicitly.
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "CLAUDE_KEY") or k.startswith("DOCSENTINEL_")}
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(SRC / "main.py"), "mcp", "--case", args.case], env=env)

    async def run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print("MCP client connected to DocSentinel. Tools offered:")
                for t in tools.tools:
                    ro = " (read-only)" if t.annotations and t.annotations.read_only_hint else ""
                    print(f"  - {t.name}{ro}: {t.description.splitlines()[0]}")
                print(f"\n> check_docs()\n")
                r = await session.call_tool("check_docs", {})
                print(r.content[0].text.split("\n|")[0].strip())
                print(f"\n> ask_docs({question!r})\n")
                r = await session.call_tool("ask_docs", {"question": question})
                print(r.content[0].text)

    asyncio.run(run())
    return 0


def cmd_eval(args) -> int:
    if args.consistency:
        md = evals.consistency(Path(args.cases), args.consistency)
        _write(Path(args.out), "consistency.md", md)
        print(md)
        return 0
    rows = []
    for path in sorted(Path(args.cases).glob("*.yaml")):
        print(f"[eval] {path.stem} ...", file=sys.stderr)
        rows.append(evals.run_case(path))
    md = evals.scorecard(rows, llm.mode())
    out = Path(args.out)
    _write(out, "scorecard.md", md)
    _write(out, "scorecard.json", json.dumps(rows, indent=2))
    print(md)
    missing = sorted({m for r in rows for m in r.get("missing_recordings", [])})
    if missing:
        for m in missing:
            print(f"missing recording: {m}", file=sys.stderr)
        print(f"docsentinel: error: {len(missing)} recording(s) missing in replay mode; scores above are not "
              "the recorded run. Fix: DOCSENTINEL_LLM=record python src/main.py eval", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docsentinel", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check claude CLI, auth and LLM mode").set_defaults(func=cmd_doctor)

    target = argparse.ArgumentParser(add_help=False)
    src = target.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="path to a git repo")
    src.add_argument("--case", help="demo/eval case YAML; builds a throwaway repo")
    target.add_argument("--base", default="HEAD~1")
    target.add_argument("--head", default="HEAD")
    target.add_argument("--out", default="out")
    target.add_argument("--config", help="settings file (default: <repo>/.docsentinel.yml)")
    target.add_argument("--webhook", help="also POST the result to this URL (signed if DOCSENTINEL_WEBHOOK_SECRET is set)")
    target.add_argument("--no-llm", action="store_true", help="rules only: no LLM calls at all")
    target.add_argument("--max-sections", type=int, help="cap on doc sections sent to the LLM per run")

    f = sub.add_parser("flag", parents=[target], help="find doc sections made stale by a code change (no edits)")
    f.set_defaults(func=cmd_flag)

    x = sub.add_parser("fix", parents=[target], help="flag, then propose verified doc fixes as a patch")
    x.add_argument("--branch", action="store_true", help="also commit fixes on a new docs-sentinel/* branch")
    x.add_argument("--pr", action="store_true", help="also push that branch and open a DRAFT PR (needs gh)")
    x.add_argument("--pr-base", default="main", help="branch the draft PR targets")
    x.add_argument("--reuse", help="report.json from the PR check: reuse its verdicts if the merged change is identical")
    x.set_defaults(func=cmd_fix)

    a = sub.add_parser("ask", parents=[target], help="Docs Concierge: answer a how-to question, checked against code")
    a.add_argument("question")
    a.add_argument("--no-drift", action="store_true", help="don't tell the agent which docs are stale")
    a.add_argument("--link", help="repo URL (https://github.com/owner/repo): cite with permalinks at the checked commit")
    a.set_defaults(func=cmd_ask)

    m = sub.add_parser("mcp", parents=[target], help="run DocSentinel as an MCP server (stdio) for AI editors")
    m.set_defaults(func=cmd_mcp)
    md = sub.add_parser("mcp-demo", help="connect as an MCP client and call the tools")
    md.add_argument("--case", default=str(SRC / "evals" / "cases" / "demo.yaml"))
    md.set_defaults(func=cmd_mcp_demo)

    n = sub.add_parser("notify", help="send a report.json to Slack / webhooks (counts and links only for Slack)")
    n.add_argument("--report", required=True)
    n.add_argument("--event", choices=["flag", "fix"], default="flag")
    n.add_argument("--repo-name")
    n.add_argument("--pr-url")
    n.add_argument("--commit-url", help="direct push (no PR): link the commit instead")
    n.add_argument("--fix-pr-url")
    n.add_argument("--run-url")
    n.add_argument("--webhook")
    n.set_defaults(func=cmd_notify)

    t = sub.add_parser("trial", help="dry run on the last N commits of a repo (rules only, $0, read-only)")
    t.add_argument("--repo", default=".")
    t.add_argument("--config")
    t.add_argument("--last", type=int, default=20)
    t.add_argument("--out", default="out/trial")
    t.add_argument("--name", help="repo name to show in the report")
    t.add_argument("--llm", action="store_true", help="let the Judge decide candidates (costs LLM calls)")
    t.add_argument("--max-calls", type=int, default=5, help="with --llm: most doc sections judged in total")
    t.add_argument("--docs-at", choices=["head", "base"], default="base",
                   help="check against docs after the commit (head) or before it (base)")
    t.add_argument("--commits", help="comma-separated SHAs instead of --last")
    t.set_defaults(func=cmd_trial)

    h = sub.add_parser("hooks-demo", help="show a signed webhook being verified, and tampered/replayed ones rejected")
    h.add_argument("--case", default=str(SRC / "evals" / "cases" / "demo.yaml"))
    h.set_defaults(func=cmd_hooks_demo)

    e = sub.add_parser("eval", help="run all eval cases and print the scorecard")
    e.add_argument("--cases", default=str(SRC / "evals" / "cases"))
    e.add_argument("--out", default="out/eval")
    e.add_argument("--consistency", type=int, metavar="N",
                   help="instead: ask the Judge N times per borderline/clear section and report agreement (live)")
    e.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


def _hint(e: BaseException) -> str:
    """One line: what went wrong, and how to fix it."""
    import subprocess
    if isinstance(e, FileNotFoundError) and "git" in str(e):
        return "git is not installed. Fix: install git and re-run."
    if isinstance(e, subprocess.CalledProcessError) and e.cmd and "git" in str(e.cmd[0]):
        return f"git failed ({' '.join(map(str, e.cmd[:4]))}…). Fix: run inside a git repo with the given --base/--head commits."
    if isinstance(e, ImportError):
        return f"missing Python package '{e.name}'. Fix: pip install -r requirements.txt (inside your venv)."
    if isinstance(e, llm.LLMUnavailable):
        return f"no LLM available ({e}). Fix: set CLAUDE_KEY and install the claude CLI, or unset DOCSENTINEL_LLM to use replay."
    if isinstance(e, FileNotFoundError):
        return f"file not found: {e.filename}. Fix: check the path; run commands from the repo root."
    return f"{type(e).__name__}: {e}. Fix: re-run with DOCSENTINEL_DEBUG=1 for details."


def run(argv=None) -> int:
    """Entry point: main() with one-line errors instead of tracebacks."""
    if os.environ.get("PYTHONHASHSEED") is None and argv is None:  # same fixed seed as src/main.py
        os.environ["PYTHONHASHSEED"] = "1"
        os.execv(sys.executable, [sys.executable, "-c", "import sys; from docsentinel.cli import run; sys.exit(run())",
                                  *sys.argv[1:]])
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - every failure gets a readable reason
        if os.environ.get("DOCSENTINEL_DEBUG"):
            raise
        print(f"docsentinel: error: {_hint(e)}", file=sys.stderr)
        return 1
