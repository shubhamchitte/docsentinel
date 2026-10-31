"""Outbound hooks: send each run's result to any system over a signed webhook.

Configure in .docsentinel.yml (or --webhook URL on the command line):

  webhooks:
    - url: https://hooks.example.com/docsentinel
      secret_env: DOCSENTINEL_WEBHOOK_SECRET   # name of the env var, never the secret itself
      format: json                             # json (default) | slack

Slack: the incoming-webhook URL *is* the credential (anyone holding it can post),
so it is passed only as a secret (SLACK_WEBHOOK_URL), never put in config, and logs
show only the host. The Slack message carries counts and links, never doc text or code.

Security:
  - HTTPS only (plain http allowed for localhost, for testing)
  - X-DocSentinel-Signature: sha256=HMAC(secret, "<timestamp>.<body>")
  - X-DocSentinel-Timestamp: receivers reject anything older than 5 minutes (replay guard)
  - payload carries findings and fix summaries, not whole documents
Receivers can use verify() below, or the same 4 lines in any language.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
from urllib.parse import urlparse

MAX_AGE_S = 300


def payload(event: str, result: dict, fix_summary: dict | None = None, links: dict | None = None) -> dict:
    counts: dict[str, int] = {}
    for f in result["findings"]:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return {
        "event": f"docsentinel.{event}",
        "base": result["base"],
        "head": result["head"],
        "llm": result.get("llm_mode", "off"),
        "counts": counts,
        "findings": [
            {"id": f["id"], "status": f["status"], "doc": f["doc"], "section": f["section"],
             "lines": [n for n, _ in f["lines"]],
             "reason": (f["judge"] or {}).get("reason") if f.get("judge") else f["reason"]}
            for f in result["findings"] if f["status"] != "ok"
        ],
        "fix": fix_summary,
        "links": links or {},  # repo / pr / fix_pr / run URLs, when known
    }


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, headers: dict, body: bytes, now: float | None = None) -> tuple[bool, str]:
    """For receivers: (ok, reason). Checks signature and age."""
    h = {k.lower(): v for k, v in headers.items()}  # HTTP header names are case-insensitive
    ts = h.get("x-docsentinel-timestamp", "")
    sig = h.get("x-docsentinel-signature", "")
    if not ts.isdigit():
        return False, "missing timestamp"
    if abs((now or time.time()) - int(ts)) > MAX_AGE_S:
        return False, "too old (possible replay)"
    if not hmac.compare_digest(sig, sign(secret, ts, body)):
        return False, "bad signature"
    return True, "verified"


def _slack(p: dict) -> dict:
    """Slack adapter: counts and links only. No doc text, reasons or code leave the repo."""
    c, links = p["counts"], p.get("links") or {}
    if links.get("pr"):
        where = f"<{links['pr']}|PR #{links['pr'].rstrip('/').split('/')[-1]}>"
    elif links.get("commit"):
        where = f"<{links['commit']}|commit {links['commit'].rstrip('/').split('/')[-1][:7]}>"
    else:
        where = f"commit `{p['head']}`"
    repo = f" · {links['repo']}" if links.get("repo") else ""
    line = (f"*DocSentinel*{repo} · {where}: *{c.get('stale', 0)} stale* doc section(s), "
            f"{c.get('review', 0)} need review, {c.get('gap', 0)} undocumented")
    extra = []
    if links.get("fix_pr"):
        extra.append(f"<{links['fix_pr']}|draft docs fix PR>")
    if links.get("run"):
        extra.append(f"<{links['run']}|details>")
    return {"text": line + (" · " + " · ".join(extra) if extra else "")}


def send(url: str, p: dict, secret: str | None, fmt: str = "json", timeout: int = 10) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError(f"refusing non-HTTPS webhook: {url}")
    body = json.dumps(_slack(p) if fmt == "slack" else p).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "docsentinel",
               "X-DocSentinel-Event": p["event"], "X-DocSentinel-Timestamp": ts}
    if secret:
        headers["X-DocSentinel-Signature"] = sign(secret, ts, body)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def fire(targets: list[dict], p: dict) -> list[str]:
    """Send to every configured target. A failing hook never fails the run; it is reported."""
    log = []
    for t in targets:
        if not t.get("url"):
            continue
        secret = os.environ.get(t.get("secret_env", "DOCSENTINEL_WEBHOOK_SECRET"))
        host = urlparse(t["url"]).hostname
        try:
            status = send(t["url"], p, secret, t.get("format", "json"))
            signed = "" if secret or t.get("format") == "slack" else " (unsigned: no secret set)"
            log.append(f"{p['event']} -> {host}: HTTP {status}{signed}")
        except Exception as e:  # noqa: BLE001 - report, don't crash the pipeline
            log.append(f"{p['event']} -> {host}: FAILED ({str(e)[:100]})")
    return log
