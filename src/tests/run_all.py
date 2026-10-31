"""Run every test suite with one command: python src/tests/run_all.py  (no network, no LLM)."""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SUITES = ["test_guardrails.py", "test_workflows.py"]


def main() -> int:
    failed = 0
    for suite in SUITES:
        print(f"== {suite}")
        failed += subprocess.run([sys.executable, str(HERE / suite)]).returncode != 0
        print()
    print("ALL SUITES PASSED" if not failed else f"{failed} suite(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
