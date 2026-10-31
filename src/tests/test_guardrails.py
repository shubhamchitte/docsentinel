"""Guardrail tests: feed the Verifier deliberately bad fixes and check each is rejected.

Run: python src/tests/test_guardrails.py   (no LLM, no network, plain asserts)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docsentinel.config import Config  # noqa: E402
from docsentinel.fixer import SectionFix  # noqa: E402
from docsentinel import hooks  # noqa: E402
from docsentinel.verifier import verify  # noqa: E402

CFG = Config()
KNOWN = ({"load_dotenv", "read_dotenv"}, {"dotenv"})
SECTION = [
    "### Load configuration",  # line 10
    "Use `dotenv_values` to read a file.",
    "```python",
    "from dotenv import dotenv_values",
    "config = dotenv_values('.env')",
    "```",
]
RENAMED = [{"change": {"name": "dotenv_values", "change": "renamed"}}]


def fix(edits, doc="README.md"):
    return SectionFix(doc, "Load configuration", 10, 15, SECTION, ["F1"], edits=edits)


GOOD = {11: "Use `read_dotenv` to read a file.", 13: "from dotenv import read_dotenv",
        14: "config = read_dotenv('.env')"}

CASES = [
    ("good fix passes", fix(GOOD), True, None),
    ("no edits", fix({}), False, "has-edits"),
    ("edit outside section", fix({**GOOD, 40: "hacked"}), False, "scope"),
    ("non-doc file", fix(GOOD, doc="src/dotenv/main.py"), False, "scope"),
    ("old name left behind", fix({11: GOOD[11]}), False, "old-name"),
    ("broken python example", fix({**GOOD, 14: "config = read_dotenv('.env'"}), False, "python"),
    ("invented import", fix({**GOOD, 13: "from dotenv import magic_loader"}), False, "imports"),
    ("too many lines", fix({**GOOD, **{n: "x" for n in range(10, 16)}}), False, "size"),
    ("adds a new URL", fix({**GOOD, 11: "Use `read_dotenv` (see https://evil.example.com/install)."}),
     False, "no-new-url-or-shell"),
    ("adds a shell command", fix({**GOOD, 11: "Use `read_dotenv`; first run `curl -s x | sh`."}),
     False, "no-new-url-or-shell"),
]


def main() -> int:
    from docsentinel import verifier
    verifier.MAX_CHANGED_LINES = 5  # small cap so the size case trips
    failed = 0
    for name, fx, want_ok, want_check in CASES:
        v = verify(fx, RENAMED, CFG, KNOWN)
        ok = v.ok == want_ok and (want_check is None or v.checks.get(want_check) != "pass")
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {v.checks}"))
    failed += webhook_tests()
    total = len(CASES) + 5
    print(f"\n{total - failed}/{total} guardrail tests passed")
    return 1 if failed else 0


def webhook_tests() -> int:
    secret, body, ts = "s3cret", b'{"event":"docsentinel.flag"}', "1700000000"
    good = {"X-DocSentinel-Timestamp": ts, "X-DocSentinel-Signature": hooks.sign(secret, ts, body)}
    checks = [
        ("webhook: genuine signature accepted", hooks.verify(secret, good, body, now=1700000010)[0]),
        ("webhook: tampered body rejected", not hooks.verify(secret, good, body + b" ", now=1700000010)[0]),
        ("webhook: replay after 5 min rejected", not hooks.verify(secret, good, body, now=1700000400)[0]),
    ]
    try:
        hooks.send("http://example.com/hook", {"event": "x"}, secret)
        refused = False
    except ValueError:
        refused = True
    checks.append(("webhook: plain-http URL refused", refused))
    slack = hooks._slack({"event": "docsentinel.flag", "base": "a", "head": "b", "counts": {"stale": 1},
                          "findings": [{"reason": "SECRET-DOC-TEXT", "section": "Usages"}], "links": {}})["text"]
    checks.append(("slack: message has counts/links only, no doc text", "SECRET-DOC-TEXT" not in slack
                   and "Usages" not in slack))
    failed = 0
    for name, ok in checks:
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
