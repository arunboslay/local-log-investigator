"""Replay recorded runs through the detector and measure it. No model, no GPU.

Usage:
    uv run python monitoring/watcher/evaluate.py --incidents monitoring/incidents --repo monitoring/demo_app
    uv run python monitoring/watcher/evaluate.py --runs monitoring/dataset/runs --repo monitoring/demo_app

For each recording: did an alert on an affected endpoint come after the fault went live, how long did it
take, and how many alerts were false (before the fault, or on endpoints the fault doesn't touch)?
A code or config fault goes live when the app restarts on the guilty commit.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "dataset"))
from watcher.code_steps import StepMaps  # noqa: E402
from watcher.detector import COMMIT, Detector, parse_time  # noqa: E402


def utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def load_rows(folder: Path) -> list[dict]:
    rows = []
    for line in (folder / "logs.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "@timestamp" in row:
            rows.append(row)
    return sorted(rows, key=parse_time)


def describe(folder: Path) -> dict:
    """Fault name, when it was injected, its guilty commit, and the endpoints it affects (None = all)."""
    truth = folder / "truth.json"
    if truth.exists():
        t = json.loads(truth.read_text(encoding="utf-8"))
        eps = t["affected_endpoints"]
        everything = any(e.startswith(("all", "eventually")) for e in eps)
        return {"fault": t["fault_id"], "injected": t["injected_at"], "commit": t["guilty_commit"],
                "endpoints": None if everything else set(eps)}
    from variants import VARIANTS_BY_ID
    m = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    variant = VARIANTS_BY_ID.get(m["variant"])
    return {"fault": m["variant"], "injected": m.get("injected_at"), "commit": m.get("guilty_commit"),
            "endpoints": set(variant.endpoints) if variant else set()}


def went_live(info: dict, rows: list[dict]) -> datetime | None:
    if not info["injected"]:
        return None
    if info["commit"]:
        for r in rows:
            found = COMMIT.search(r.get("message", ""))
            if found and r["message"].startswith("service started") and info["commit"].startswith(found.group(1)):
                return parse_time(r)
    return utc(info["injected"])


def evaluate(folder: Path, maps) -> dict:
    rows = load_rows(folder)
    info = describe(folder)
    live = went_live(info, rows)
    detector = Detector(steps_for_commit=maps)
    alerts = []
    start = time.perf_counter()
    for row in rows:
        alerts += detector.feed(row)
    seconds = time.perf_counter() - start

    false_alarms, hits = [], []
    for a in alerts:
        t = utc(a.ts)
        related = info["endpoints"] is None or a.endpoint is None or a.endpoint in info["endpoints"]
        if live and t >= live and related:
            hits.append(a)
        else:
            false_alarms.append(a)
    first = hits[0] if hits else None
    return {"name": folder.name, "fault": info["fault"], "detected": bool(first) if live else None,
            "delay_s": round((utc(first.ts) - live).total_seconds(), 1) if first else None,
            "first_check": first.check if first else None, "checks_fired": sorted({a.check for a in hits}),
            "false_alarms": [{"check": a.check, "summary": a.summary[:110]} for a in false_alarms],
            "minutes": round((parse_time(rows[-1]) - parse_time(rows[0])).total_seconds() / 60, 2),
            "lines": len(rows), "lines_per_s": round(len(rows) / seconds) if seconds else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incidents", type=Path)
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--repo", type=Path, required=True, help="the demo app's git repo that made these recordings")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    root = args.incidents or args.runs
    maps = StepMaps(args.repo)
    results = [evaluate(f, maps) for f in sorted(root.iterdir()) if (f / "logs.jsonl").exists()]
    results.sort(key=lambda r: r["fault"])
    print(f"{'fault':26} {'found':6} {'after':>7}  {'first check':18} {'false':>5}  all checks")
    for r in results:
        found = "-" if r["detected"] is None else "yes" if r["detected"] else "NO"
        delay = f"{r['delay_s']:.0f} s" if r["delay_s"] is not None else "-"
        print(f"{r['fault']:26} {found:6} {delay:>7}  {str(r['first_check']):18} {len(r['false_alarms']):5}  {','.join(r['checks_fired'])}")
    faulty = [r for r in results if r["detected"] is not None]
    delays = sorted(r["delay_s"] for r in faulty if r["delay_s"] is not None)
    minutes = sum(r["minutes"] for r in results)
    false_count = sum(len(r["false_alarms"]) for r in results)
    summary = {"recordings": len(results), "faults": len(faulty), "detected": sum(bool(r["detected"]) for r in faulty),
               "median_delay_s": delays[len(delays) // 2] if delays else None, "max_delay_s": delays[-1] if delays else None,
               "false_alarms": false_count, "hours_recorded": round(minutes / 60, 2),
               "slowest_lines_per_s": min(r["lines_per_s"] for r in results if r["lines_per_s"])}
    print(json.dumps(summary, indent=2))
    print("False alarms:")
    for r in results:
        for a in r["false_alarms"]:
            print(f"  {r['fault']:24} {a['check']:18} {a['summary']}")
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
