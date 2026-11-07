"""Record one incident: run the shop with traffic, break it, collect the evidence, undo the fault.

Usage:
    uv run python monitoring/record_incident.py --list
    uv run python monitoring/record_incident.py --fault silent_save_failure [--baseline 45 --after 75]

Output: monitoring/incidents/<id>/
    incident.json    time window, code commit, file list (no answer in here)
    logs.jsonl       app log lines in the window, with personal data masked
    metrics.jsonl    /actuator/prometheus every 5 s ("up": false while the app was down or restarting)
    commits.txt      last 10 commits in demo_app, with changed files
    diffs.patch      full diffs of commits made during the run
    code_index.json  endpoint -> controller -> service -> repository map of the code at incident time
    truth.json       the true cause (keep this away from the investigator)
"""

import argparse
import gzip
import json
import random
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from prometheus_client.parser import text_string_to_metric_families

sys.path.insert(0, str(Path(__file__).resolve().parent))
from code_index import build_index  # noqa: E402
from faults.catalog import FAULTS  # noqa: E402
from lab import APP_DIR, APP_URL, BASE_RPS, LOG_DIR, ROOT, Lab  # noqa: E402
from redact import redact_log_line  # noqa: E402

DECOY_CHANGES = [
    "Document the order endpoints",
    "Changelog: mention the address form",
    "Changelog: note payment provider",
    "Tidy changelog wording",
]


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds")


class MetricsScraper(threading.Thread):
    """Reads /actuator/prometheus every few seconds and keeps the values."""

    def __init__(self, every_s: float = 5):
        super().__init__(daemon=True)
        self.every_s = every_s
        self.rows: list[dict] = []
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            row = {"ts": iso(now())}
            try:
                text = httpx.get(f"{APP_URL}/actuator/prometheus", timeout=3).text
                row["up"] = True
                row["values"] = {_sample_key(s): s.value
                                 for family in text_string_to_metric_families(text) for s in family.samples}
            except httpx.HTTPError as exc:
                row.update(up=False, error=type(exc).__name__)
            self.rows.append(row)
            self.stopped.wait(self.every_s)

    def stop(self):
        self.stopped.set()
        if self.is_alive():
            self.join(timeout=10)


def _sample_key(s) -> str:
    if not s.labels:
        return s.name
    return s.name + "{" + ",".join(f'{k}="{v}"' for k, v in sorted(s.labels.items())) + "}"


def read_logs(start: datetime, end: datetime) -> list[str]:
    """App log lines in the window, including rotated files (Spring gzips them: app.log.<date>.<n>.gz)."""
    lines = []
    for path in sorted(LOG_DIR.glob("app*.log*"), key=lambda p: p.stat().st_mtime):  # oldest file first
        if path.suffix == ".gz":
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < start:
                continue  # rotated before the window started
            text = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            try:
                ts = datetime.fromisoformat(json.loads(line)["@timestamp"])
            except (ValueError, KeyError, TypeError):
                continue  # not a full JSON log line (e.g. two app processes writing at once during a deploy)
            if start <= ts <= end:
                lines.append(line)
    return lines


def decoy_commit(lab: Lab) -> str:
    """A harmless commit deployed before the baseline, so 'the latest commit' isn't always guilty."""
    changelog = APP_DIR / "CHANGELOG.md"
    note = random.choice(DECOY_CHANGES)
    changelog.write_bytes(changelog.read_bytes() + f"- {note}.\n".encode("utf-8"))
    lab.git("commit", "-q", "-am", note)
    return lab.head()


def record(fault_id: str, baseline_s: float, after_s: float) -> Path:
    fault = FAULTS[fault_id]
    lab = Lab()
    lab.ensure_clean()
    incident_id = f"inc-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    out = ROOT / "incidents" / incident_id
    scraper = MetricsScraper()
    info, injected_at, reverted_at, recovered = None, None, None, False

    try:
        print(f"[{incident_id}] fault={fault.id}: starting database, payment service, shop and traffic")
        lab.reset_data()
        lab.write_control("load", {"rps": BASE_RPS})
        lab.write_control("payment", {"delay_ms": 0})
        lab.build()
        run_start = now()
        base_sha = lab.head()
        lab.start_db()
        lab.start_payment()
        lab.start_app()
        lab.start_load()
        scraper.start()

        decoy_sha = decoy_commit(lab)
        lab.deploy()
        print(f"  decoy commit {decoy_sha[:8]} deployed; baseline for {baseline_s:.0f}s")
        time.sleep(baseline_s)

        print(f"  injecting {fault.id}")
        injected_at = now()
        info = fault.inject(lab)
        time.sleep(after_s)
        window_end = now()

        code_commit = lab.head()
        commits = lab.git("log", "-n", "10", "--date=iso", "--stat")
        diffs = lab.git("log", "-p", "--date=iso", f"{base_sha}..HEAD")
        index = build_index(APP_DIR)
        scraper.stop()

        print("  reverting")
        fault.revert(lab, info)
        reverted_at = now()
        recovered = lab.wait_healthy(timeout_s=60)
    finally:
        if info is not None and reverted_at is None:
            print("  error during run; reverting before exit")
            fault.revert(lab, info)
        scraper.stop()
        lab.shutdown()

    out.mkdir(parents=True)
    logs = [redact_log_line(line) for line in read_logs(run_start, window_end)]
    files = ["logs.jsonl", "metrics.jsonl", "commits.txt", "diffs.patch", "code_index.json"]
    (out / "logs.jsonl").write_text("\n".join(logs) + "\n", encoding="utf-8")
    (out / "metrics.jsonl").write_text("\n".join(json.dumps(r) for r in scraper.rows) + "\n", encoding="utf-8")
    (out / "commits.txt").write_text(commits + "\n", encoding="utf-8")
    (out / "diffs.patch").write_text(diffs + "\n", encoding="utf-8")
    (out / "code_index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (out / "incident.json").write_text(json.dumps({
        "id": incident_id,
        "service": "demo shop (Spring Boot, monitoring/demo_app)",
        "window_start": iso(run_start),
        "window_end": iso(window_end),
        "code_commit": code_commit,
        "files": files,
        "note": "Evidence only. Something went wrong in this window; find what and why.",
    }, indent=2), encoding="utf-8")
    (out / "truth.json").write_text(json.dumps({
        "fault_id": fault.id,
        "category": fault.category,
        "cause": fault.cause,
        "code_location": fault.code_location,
        "affected_endpoints": fault.endpoints,
        "expected_signals": fault.signals,
        "guilty_commit": info["guilty_commit"],
        "guilty_files": info["guilty_files"],
        "decoy_commit": decoy_sha,
        "injected_at": iso(injected_at),
        "reverted_at": iso(reverted_at),
        "recovered_after_revert": recovered,
    }, indent=2), encoding="utf-8")
    print(f"  saved {out}  ({len(logs)} log lines, {len(scraper.rows)} metric snapshots, recovered={recovered})")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault", choices=sorted(FAULTS))
    parser.add_argument("--baseline", type=float, default=45, help="seconds of normal traffic before the fault")
    parser.add_argument("--after", type=float, default=75, help="seconds to keep running after the fault")
    parser.add_argument("--list", action="store_true", help="list the faults and exit")
    args = parser.parse_args()

    if args.list or not args.fault:
        for f in FAULTS.values():
            print(f"{f.id:22} {f.category:15} {f.cause[:110]}")
        return 0
    record(args.fault, args.baseline, args.after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
