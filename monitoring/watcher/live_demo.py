"""Live demo: run the app and the watcher, inject a fault, and time flag and report.

Usage: uv run python monitoring/watcher/live_demo.py [--fault silent_save_failure] [--baseline 90]
Saves monitoring/watcher/flagged/live_demo_result.json.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import psutil  # noqa: E402

from faults.catalog import FAULTS  # noqa: E402
from investigator.llm_server import PORT  # noqa: E402
from lab import BASE_RPS, RUN_DIR, Lab  # noqa: E402

FLAGGED = HERE / "flagged"


def service_started_at(lab_log: Path, commit: str) -> float | None:
    """When the app reported ready on this commit (seconds since the epoch), from its log."""
    for line in lab_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "service started" in line and commit[:12] in line:
            row = json.loads(line)
            return datetime.fromisoformat(row["@timestamp"]).timestamp()
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault", default="silent_save_failure")
    parser.add_argument("--baseline", type=float, default=90)
    parser.add_argument("--max-wait", type=float, default=300)
    args = parser.parse_args()
    fault = FAULTS[args.fault]
    alerts_file = RUN_DIR / "alerts.jsonl"
    alerts_file.unlink(missing_ok=True)
    before = {p.name for p in FLAGGED.iterdir()} if FLAGGED.exists() else set()
    lab = Lab()
    lab.ensure_clean()
    info = None
    result = {"fault": args.fault}
    try:
        lab.reset_data()
        lab.write_control("load", {"rps": BASE_RPS})
        lab.write_control("payment", {"delay_ms": 0})
        lab.build()
        lab.start_db()
        lab.start_payment()
        lab.start_app()
        lab.start_load()
        lab.start("watcher", [sys.executable, HERE / "watch.py", "--investigate"])
        print(f"normal traffic for {args.baseline:.0f} s, watcher on", flush=True)
        time.sleep(args.baseline)
        early = alerts_file.read_text(encoding="utf-8").splitlines() if alerts_file.exists() else []
        result["flags_before_fault"] = len(early)

        injected = time.time()
        info = fault.inject(lab)
        live = service_started_at(Path(lab.git("rev-parse", "--show-toplevel")) / "logs" / "app.log", info["guilty_commit"]) if info["guilty_commit"] else injected
        print("fault live; waiting for a flag and a report", flush=True)
        report_path = None
        deadline = time.time() + args.max_wait
        while time.time() < deadline and report_path is None:
            for folder in (FLAGGED.iterdir() if FLAGGED.exists() else []):
                if folder.name not in before and (folder / "report.json").exists():
                    report_path = folder / "report.json"
            time.sleep(1)
        alerts = [json.loads(l) for l in alerts_file.read_text(encoding="utf-8").splitlines()][len(early):]
        result["flags_after_fault"] = [{"check": a["check"], "summary": a["summary"][:160], "seconds_after_live": round(
            datetime.fromisoformat(a["ts"]).timestamp() - live, 1)} for a in alerts]
        result["guilty_commit"] = info["guilty_commit"]
        if report_path:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            result.update(report=data["report"], investigator_stats={k: data["stats"].get(k) for k in ("steps", "seconds", "tokens_per_s")},
                          flag_to_report_s=data["seconds_from_flag_to_report"], model=data["model"], report_folder=report_path.parent.name)
    finally:
        lab.stop("watcher")
        # Stopping the watcher this way doesn't stop the model server it started.
        for proc in psutil.process_iter(["name", "cmdline"]):
            if proc.info["name"] == "llama-server.exe" and str(PORT) in (proc.info["cmdline"] or []):
                proc.terminate()
        if info is not None:
            fault.revert(lab, info)
        lab.shutdown()
    FLAGGED.mkdir(exist_ok=True)
    (FLAGGED / "live_demo_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
