"""Workflow tests: run the REAL shell steps from the reusable workflows against a local git
remote and a stub `gh`, plus the Python pieces the workflows rely on. No network, no LLM.

Covers: idempotent re-runs (one draft PR per merge, update not duplicate), human-edit
protection, merged/closed drafts left alone, fork/Dependabot PRs running rules-only,
cap reporting, Slack skip-when-empty and commit labels.

Run: python src/tests/test_workflows.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

BOT = "docsentinel@users.noreply.github.com"
SHA = "abc1234def5678abc1234def5678abc1234def56"
BRANCH = "docs-sentinel/abc1234"

STUB_GH = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
state = json.load(open(os.environ["STUB_STATE"]))
open(os.environ["STUB_LOG"], "a").write(" ".join(a) + "\n")
pr = state.get("pr")
if a[:2] == ["pr", "list"]:
    if "--state" in a:                       # lookup: "<num> <state> <url>"
        print(f"{pr['num']} {pr['state']} {pr['url']}" if pr else "")
    else:                                    # final url lookup
        print(pr["url"] if pr else "https://github.com/o/r/pull/99")
elif a[:1] == ["api"] and "/commits" in a[1]:
    print("\n".join(state.get("authors", [])))
elif a[:2] == ["pr", "create"]:
    print("https://github.com/o/r/pull/99")
'''


def step_script(workflow: str, job: str, name_prefix: str) -> str:
    wf = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    for step in wf["jobs"][job]["steps"]:
        if step.get("name", "").startswith(name_prefix):
            return step["run"]
    raise KeyError(name_prefix)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def make_world(tmp: Path, draft: str | None):
    """Bare remote with main; optionally an existing draft branch: 'bot', 'human' or 'same'."""
    remote, work = tmp / "remote.git", tmp / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
    for k, v in (("user.name", "dev"), ("user.email", "dev@example.com")):
        git(work, "config", k, v)
    (work / "README.md").write_text("line one\nold text\n")
    git(work, "add", "-A"); git(work, "commit", "-qm", "init"); git(work, "push", "-q", "origin", "main")
    (work / "out").mkdir()
    (work / "out/head_sha").write_text(SHA)
    (work / "out/fix.md").write_text("fix body")
    patch = "--- a/README.md\n+++ b/README.md\n@@ -1,2 +1,2 @@\n line one\n-old text\n+new text\n"
    (work / "out/fix.patch").write_text(patch)
    if draft:
        git(work, "switch", "-qc", BRANCH)
        (work / "README.md").write_text("line one\n" + ("new text\n" if draft == "same" else "earlier bot text\n"))
        git(work, "-c", f"user.email={BOT}", "-c", "user.name=bot", "commit", "-qam", "bot draft")
        if draft == "human":
            (work / "README.md").write_text("line one\nhuman edited text\n")
            git(work, "commit", "-qam", "human edit")
        git(work, "push", "-q", "origin", BRANCH)
        git(work, "switch", "-q", "main"); git(work, "branch", "-qD", BRANCH)
    # out/ is untracked; the step must not commit it
    (work / ".git/info/exclude").write_text("out/\n")
    return remote, work


def run_open_pr(tmp: Path, work: Path, pr: dict | None, authors: list[str], event="push") -> dict:
    bin_dir = tmp / "bin"; bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(STUB_GH); (bin_dir / "gh").chmod(0o755)
    state, log, genv, summary = tmp / "state.json", tmp / "gh.log", tmp / "env", tmp / "summary"
    state.write_text(json.dumps({"pr": pr, "authors": authors}))
    for f in (log, genv, summary):
        f.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_STATE": str(state), "STUB_LOG": str(log),
           "GITHUB_ENV": str(genv), "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_EVENT_NAME": event,
           "GITHUB_REPOSITORY": "o/r", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "1",
           "GH_TOKEN": "x", "TARGET": "main", "BOT_EMAIL": BOT}
    script = step_script("docsentinel-fix.yml", "open-pr", "Open or update the draft PR")
    r = subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, capture_output=True, text=True)
    return {"rc": r.returncode, "err": r.stderr, "log": log.read_text(), "env": genv.read_text(),
            "summary": summary.read_text()}


def notify(env_text: str) -> str:
    """Last NOTIFY value written to GITHUB_ENV (later lines win, as in Actions)."""
    vals = [ln.split("=", 1)[1] for ln in env_text.splitlines() if ln.startswith("NOTIFY=")]
    return vals[-1] if vals else "unset"


def remote_sha(remote: Path, branch: str) -> str:
    out = subprocess.run(["git", "-C", str(remote), "rev-parse", "--verify", "-q", branch],
                         capture_output=True, text=True).stdout.strip()
    return out


def case(name, draft, pr, authors, check, event="push"):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        remote, work = make_world(tmp, draft)
        before = remote_sha(remote, BRANCH)
        res = run_open_pr(tmp, work, pr, authors, event)
        res["before"], res["after"] = before, remote_sha(remote, BRANCH)
        ok = res["rc"] == 0 and check(res)
        return name, ok, ("" if ok else f"rc={res['rc']} err={res['err'][-300:]} log={res['log']!r}")


OPEN = {"num": 8, "state": "OPEN", "url": "https://github.com/o/r/pull/8"}

CASES = [
    ("re-run: no draft yet -> opens one draft PR", None, None, [],
     lambda r: "pr create" in r["log"] and r["after"] and notify(r["env"]) == "true"),
    ("re-run: open draft, bot-only, new fix -> updates it, no new PR", "bot", OPEN, [BOT],
     lambda r: "pr create" not in r["log"] and "pr edit 8" in r["log"] and r["after"] != r["before"]
     and notify(r["env"]) == "true"),
    ("re-run: open draft, same fix -> no push, no new PR, no notify", "same", OPEN, [BOT],
     lambda r: "pr create" not in r["log"] and r["after"] == r["before"] and notify(r["env"]) == "false"),
    ("human edits on draft -> not overwritten, one comment, no notify", "human", OPEN, [BOT, "dev@example.com"],
     lambda r: r["after"] == r["before"] and "pr comment 8" in r["log"] and "--edit-last" in r["log"]
     and "pr create" not in r["log"] and notify(r["env"]) == "false"),
    ("draft already merged -> nothing done", "bot", {**OPEN, "state": "MERGED"}, [BOT],
     lambda r: r["after"] == r["before"] and "pr create" not in r["log"] and "pr edit" not in r["log"]),
    ("manual re-run that changes nothing -> no notify", "same", OPEN, [BOT],
     lambda r: notify(r["env"]) == "false" and r["after"] == r["before"], "workflow_dispatch"),
]


def fork_pr_rules_only() -> tuple[str, bool, str]:
    """No CLAUDE_KEY (fork / Dependabot PR): the step must choose rules-only and not fail."""
    script = step_script("docsentinel-check.yml", "analyze", "Install Claude CLI")
    with tempfile.TemporaryDirectory() as t:
        out, summ = Path(t) / "out", Path(t) / "sum"
        out.write_text(""); summ.write_text("")
        r = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                           env={**os.environ, "HAS_KEY": "false", "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ)})
        ok = r.returncode == 0 and "flags=--no-llm" in out.read_text() and "LLM skipped" in summ.read_text()
    wf = yaml.safe_load((ROOT / ".github/workflows/docsentinel-check.yml").read_text())
    cond = wf["jobs"]["comment"]["if"]
    ok &= "head.repo.full_name == github.repository" in cond and "dependabot[bot]" in cond
    return "fork/Dependabot PR: no secret -> rules-only, no write-token comment", ok, ""


def cap_reported() -> tuple[str, bool, str]:
    from docsentinel import report
    result = {"base": "a", "head": "b", "changes": [], "capped": 5, "llm_mode": "live",
              "findings": [{"id": "F1", "status": "ok", "doc": "R.md", "section": "S", "section_start": 1,
                            "lines": [[1, "x"]], "reason": "r", "change": {},
                            "judge": {"stale": False, "confidence": 0.9, "reason": "fine", "stale_lines": []}}]}
    md = report.to_markdown(result)
    return "cap reached is reported, never silent", "1 section(s) judged, 5 skipped: cap reached" in md, md[-200:]


def slack_rules() -> tuple[str, bool, str]:
    from docsentinel import hooks
    commit = hooks._slack({"counts": {}, "head": "abc", "links": {"commit": f"https://github.com/o/r/commit/{SHA}"}})["text"]
    r = subprocess.run([sys.executable, str(SRC / "main.py"), "notify", "--report", "/dev/stdin"],
                       input=json.dumps({"findings": [{"status": "ok"}], "base": "a", "head": "b"}),
                       capture_output=True, text=True, env={**os.environ, "SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"})
    ok = "commit abc1234" in commit and "PR #" not in commit and "not posting" in r.stdout
    return "slack: direct push labelled 'commit <sha>'; empty runs not posted", ok, r.stdout + r.stderr


def key_without_cli_banner() -> tuple[str, bool, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "CLAUDE_KEY": "sk-ant-oat01-fake"}
    r = subprocess.run([sys.executable, str(SRC / "main.py"), "doctor"], capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    ok = r.returncode == 0 and "CLAUDE_KEY found but no claude CLI" in out and "claude.ai/install.sh" in out
    return "CLAUDE_KEY set but no claude CLI -> clear banner, replay", ok, out[-300:]


def main() -> int:
    results = [case(*c) for c in CASES] + [fork_pr_rules_only(), cap_reported(), slack_rules(),
                                           key_without_cli_banner()]
    failed = 0
    for name, ok, why in results:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {why}"))
    print(f"\n{len(results) - failed}/{len(results)} workflow tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
