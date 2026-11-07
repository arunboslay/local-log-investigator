"""Record one run for the per-request training data: traffic, a fault, and the database truth.

Usage:
    uv run python monitoring/dataset/record_requests.py --variant silent_save_phone [--before 15 --after 40]
    uv run python monitoring/dataset/record_requests.py --variant none          # normal traffic only
    uv run python monitoring/dataset/record_requests.py --all [--repeat 2]       # every variant

Each run saves monitoring/dataset/runs/<variant>-<time>/ (kept out of git; it holds raw logs):
    logs.jsonl       raw app logs for the run (redacted later, when examples are built)
    requests.jsonl   every request the traffic generator sent: request id, status, what was sent
    db.json          what really reached the database (new customers, new orders, addresses)
    code_index.json  code map at the commit that was running during the fault
    meta.json        variant, family, guilty commit/file/line, timings
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from code_index import build_index  # noqa: E402
from lab import APP_DIR, BASE_RPS, Lab  # noqa: E402
from record_incident import iso, now, read_logs  # noqa: E402
from variants import HELD_OUT_FAMILIES, VARIANTS, VARIANTS_BY_ID  # noqa: E402

RUNS = HERE / "runs"


def guilty_line(lab: Lab, sha: str, file: str, marker: str) -> int | None:
    text = lab.git("show", f"{sha}:{file}")
    return next((i + 1 for i, line in enumerate(text.splitlines()) if marker in line), None)


def db_truth(lab: Lab, requests: list[dict]) -> dict:
    """Read back what really reached the database."""
    emails = {r[0] for r in lab.query("SELECT email FROM customer WHERE id > 5000")}
    orders = {r[0] for r in lab.query("SELECT id FROM orders WHERE id > 300000")}
    updated = sorted({int(r["path"].split("/")[3]) for r in requests if r["kind"] == "update_address"})
    addresses = {}
    for i in range(0, len(updated), 200):
        ids = ",".join(map(str, updated[i:i + 200]))
        for cid, street, city in lab.query(f"SELECT id, street, city FROM customer WHERE id IN ({ids})"):
            addresses[cid] = [street, city]
    return {"customer_emails": sorted(emails), "order_ids": sorted(orders), "addresses": addresses}


def record(variant_id: str, before_s: float, after_s: float) -> Path:
    variant = VARIANTS_BY_ID.get(variant_id)
    lab = Lab()
    lab.ensure_clean()
    out = RUNS / f"{variant_id}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    requests_file = out / "requests.jsonl"
    info, meta = None, {"variant": variant_id, "family": variant.family if variant else "none",
                        "held_out": bool(variant and variant.family in HELD_OUT_FAMILIES)}
    try:
        print(f"[{out.name}] starting", flush=True)
        lab.reset_data()
        lab.write_control("load", {"rps": BASE_RPS})
        lab.write_control("payment", {"delay_ms": 0})
        lab.build()
        start = now()
        lab.start_db()
        lab.start_payment()
        lab.start_app()
        lab.start_load(record_to=requests_file)
        time.sleep(before_s)
        if variant:
            meta["injected_at"] = iso(now())
            info = variant.fault.inject(lab)
            meta.update(guilty_commit=info["guilty_commit"], guilty_files=info["guilty_files"])
            if info["guilty_commit"] and variant.marker:
                meta["guilty_file"] = variant.marker_file
                meta["guilty_line"] = guilty_line(lab, info["guilty_commit"], variant.marker_file, variant.marker)
        time.sleep(after_s)
        lab.stop("load")
        end = now()
        meta["code_commit"] = lab.head()
        (out / "code_index.json").write_text(json.dumps(build_index(APP_DIR), indent=1), encoding="utf-8")
        if variant:
            variant.fault.revert(lab, info)
            info = None
        lab.wait_healthy(timeout_s=60)
        requests = [json.loads(l) for l in requests_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        (out / "db.json").write_text(json.dumps(db_truth(lab, requests)), encoding="utf-8")
    finally:
        if info is not None:
            variant.fault.revert(lab, info)
        lab.shutdown()
    logs = read_logs(start, end)
    (out / "logs.jsonl").write_text("\n".join(logs) + "\n", encoding="utf-8")
    meta.update(start=iso(start), end=iso(end), requests=len(requests), log_lines=len(logs))
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  saved {out.name}: {len(requests)} requests, {len(logs)} log lines", flush=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS_BY_ID) + ["none"])
    parser.add_argument("--all", action="store_true", help="every variant once (plus one normal run) per repeat")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--before", type=float, default=15)
    parser.add_argument("--after", type=float, default=40)
    args = parser.parse_args()
    if args.all:
        for _ in range(args.repeat):
            for v in ["none"] + [v.id for v in VARIANTS]:
                try:
                    record(v, args.before, args.after)
                except Exception as exc:  # keep going with the next variant
                    print(f"  FAILED {v}: {type(exc).__name__}: {exc}", flush=True)
    else:
        record(args.variant, args.before, args.after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
