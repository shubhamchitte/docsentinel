"""DocSentinel command line (repo checkout entry point). Installed, the same CLI is `docsentinel`.

Any failure prints one line: what went wrong and how to fix it. Set DOCSENTINEL_DEBUG=1 for a traceback.
"""

import os
import sys

# Deterministic replay: prompts (and so recording keys) must not depend on Python's per-process
# hash randomisation. If no seed is set, re-run this process once with a fixed one.
if os.environ.get("PYTHONHASHSEED") is None:
    os.environ["PYTHONHASHSEED"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])

if sys.version_info < (3, 10):
    sys.exit(f"docsentinel: error: Python {sys.version.split()[0]} is too old. "
             "Fix: use Python 3.10 or newer (python3 -m venv .venv && source .venv/bin/activate).")

try:
    from docsentinel.cli import main
except ImportError as e:  # usually: requirements not installed in this Python
    sys.exit(f"docsentinel: error: missing Python package '{e.name}'. "
             "Fix: pip install -r requirements.txt (inside your venv).")

if __name__ == "__main__":
    from docsentinel.cli import run
    sys.exit(run())
