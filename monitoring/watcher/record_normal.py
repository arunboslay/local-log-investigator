"""Record a long stretch of healthy traffic, with varied load and the odd harmless deploy.

The anomaly models learn what normal looks like from this, and a separate recording is used to count
false alarms. No faults are injected.

Usage: uv run python monitoring/watcher/record_normal.py --name train_normal --minutes 75
Saves monitoring/watcher/data/<name>/logs.jsonl (personal data masked) and meta.json.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lab import Lab  # noqa: E402
from record_incident import decoy_commit, iso, now, read_logs  # noqa: E402
from redact import redact_log_line  # noqa: E402

RATES = [5, 8, 10, 10, 15, 20, 30]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--load-change-s", type=float, default=180)
    parser.add_argument("--deploy-every-s", type=float, default=1200)
    args = parser.parse_args()
    out = HERE / "data" / args.name
    out.mkdir(parents=True, exist_ok=False)
    lab = Lab()
    lab.ensure_clean()
    deploys = 0
    try:
        lab.reset_data()
        lab.write_control("load", {"rps": 10})
        lab.write_control("payment", {"delay_ms": 0})
        lab.build()
        start = now()
        lab.start_db()
        lab.start_payment()
        lab.start_app()
        lab.start_load()
        began = last_deploy = time.time()
        while time.time() - began < args.minutes * 60:
            rps = random.choice(RATES)
            lab.write_control("load", {"rps": rps})
            print(f"{(time.time() - began) / 60:5.1f} min: {rps} requests per second", flush=True)
            time.sleep(args.load_change_s)
            if time.time() - last_deploy >= args.deploy_every_s:
                decoy_commit(lab)
                lab.deploy()
                deploys += 1
                last_deploy = time.time()
        lab.stop("load")
        end = now()
    finally:
        lab.shutdown()
    logs = [redact_log_line(line) for line in read_logs(start, end)]
    (out / "logs.jsonl").write_text("\n".join(logs) + "\n", encoding="utf-8")
    (out / "meta.json").write_text(json.dumps({"name": args.name, "start": iso(start), "end": iso(end),
                                               "log_lines": len(logs), "deploys": deploys}, indent=2), encoding="utf-8")
    print(f"saved {out}: {len(logs)} log lines, {deploys} deploys", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
