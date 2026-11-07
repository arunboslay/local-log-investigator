"""Check that every code fault still applies to demo_app and compiles. Leaves the repo as it was.

Usage: uv run python monitoring/check_faults.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from faults.catalog import CODE_FAULTS  # noqa: E402
from lab import Lab  # noqa: E402


def main() -> int:
    lab = Lab()
    lab.ensure_clean()
    failures = 0
    for fault in CODE_FAULTS:
        start = lab.head()
        try:
            info = _commit_only(lab, fault)
            lab.build()
            print(f"ok    {fault.id}  ({', '.join(info)})")
        except Exception as exc:  # report and keep going
            failures += 1
            print(f"FAIL  {fault.id}: {exc}")
        finally:
            lab.git("reset", "-q", "--hard", start)
    lab.build()
    return 1 if failures else 0


def _commit_only(lab: Lab, fault) -> list[str]:
    """Run the fault's commit step without deploying."""
    deploy = lab.deploy
    lab.deploy = lambda: None
    try:
        return fault.inject(lab)["guilty_files"]
    finally:
        lab.deploy = deploy


if __name__ == "__main__":
    sys.exit(main())
