"""The live watcher: follow the app's JSON log, run the cheap checks, and hand flags to the investigator.

Usage:
    uv run python monitoring/watcher/watch.py                  # flags only
    uv run python monitoring/watcher/watch.py --investigate    # also run the local investigator on a flag

Flags are written to monitoring/run/alerts.jsonl (personal data masked). On a flag, --investigate waits a
few seconds for more evidence, saves a masked snapshot to monitoring/watcher/flagged/<id>/, and runs
gpt-oss-20b through the local llama.cpp server. The fine-tuned add-on is not used here: it only loads
through Unsloth so far.
"""

import argparse
import json
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from code_index import build_index  # noqa: E402
from lab import APP_DIR, APP_URL, LOG_DIR, RUN_DIR  # noqa: E402
from redact import redact_log_line  # noqa: E402
from record_incident import MetricsScraper, iso, now  # noqa: E402
from watcher.code_steps import StepMaps  # noqa: E402
from watcher.detector import Detector, parse_time  # noqa: E402
from watcher.models import load  # noqa: E402
from watcher.request_data import RequestGrouper  # noqa: E402

FLAGGED = HERE / "flagged"
KEEP_RAW_SECONDS = 300


class LogTail:
    """Reads new lines from a log file. It reopens the file each time so log rotation can rename it."""

    def __init__(self, path: Path, from_start: bool = False):
        self.path = path
        self.pos = 0 if from_start or not path.exists() else path.stat().st_size
        self.partial = b""

    def read(self) -> list[str]:
        try:
            size = self.path.stat().st_size
            if size < self.pos:  # rotated: a new, shorter file took its place
                self.pos, self.partial = 0, b""
            if size == self.pos:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
            self.pos += len(data)
        except OSError:
            return []
        lines = (self.partial + data).split(b"\n")
        self.partial = lines.pop()
        return [line.decode("utf-8", errors="replace") for line in lines]


class Investigations:
    """Runs one investigation at a time in the background, using a local model server started on first use."""

    def __init__(self, recent_rows: deque, scraper: MetricsScraper, collect_s: float, cooldown_s: float):
        self.recent, self.scraper = recent_rows, scraper
        self.collect_s, self.cooldown_s = collect_s, cooldown_s
        self.busy = threading.Event()
        self.last = 0.0
        self.server = None

    def trigger(self, alert: dict):
        if self.busy.is_set() or time.time() - self.last < self.cooldown_s:
            return
        self.busy.set()
        threading.Thread(target=self._run, args=(alert, time.time()), daemon=True).start()

    def _snapshot(self, alert: dict) -> Path:
        folder = FLAGGED / f"flag-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        folder.mkdir(parents=True)
        rows = list(self.recent)
        (folder / "logs.jsonl").write_text("\n".join(redact_log_line(r) for r in rows) + "\n", encoding="utf-8")
        (folder / "metrics.jsonl").write_text("\n".join(json.dumps(r) for r in list(self.scraper.rows)[-60:]) + "\n", encoding="utf-8")
        git = lambda *args: subprocess.run(["git", *args], cwd=APP_DIR, capture_output=True, text=True, encoding="utf-8").stdout.strip()
        (folder / "commits.txt").write_text(git("log", "-n", "10", "--date=iso", "--stat") + "\n", encoding="utf-8")
        (folder / "diffs.patch").write_text(git("log", "-p", "-n", "3", "--date=iso") + "\n", encoding="utf-8")
        (folder / "code_index.json").write_text(json.dumps(build_index(APP_DIR)), encoding="utf-8")
        first = json.loads(rows[0])["@timestamp"] if rows else iso(now())
        (folder / "incident.json").write_text(json.dumps({
            "id": folder.name, "service": "demo shop (Spring Boot)", "window_start": first, "window_end": iso(now()),
            "code_commit": git("rev-parse", "HEAD"), "files": [], "flag": alert}, indent=2), encoding="utf-8")
        return folder

    def _run(self, alert: dict, flagged_at: float):
        from investigator.agent import investigate
        from investigator.llm_server import PROFILES, LlamaServer
        try:
            time.sleep(self.collect_s)  # let more evidence arrive
            folder = self._snapshot(alert)
            profile = PROFILES["gpt-oss-20b"]
            if self.server is None:
                print("starting the local model server...", flush=True)
                self.server = LlamaServer(profile).__enter__()
            result = investigate(folder, profile.name, profile.sampling)
            out = {"flag": alert, "report": result["report"], "stats": result["stats"],
                   "model": "gpt-oss-20b (no fine-tuned add-on)", "seconds_from_flag_to_report": round(time.time() - flagged_at, 1)}
            (folder / "report.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            print(f"REPORT {folder.name} in {out['seconds_from_flag_to_report']} s: {json.dumps(result['report'])[:600]}", flush=True)
        except Exception as exc:  # the watcher must keep running
            print(f"investigation failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            self.last = time.time()
            self.busy.clear()

    def close(self):
        if self.server:
            self.server.__exit__(None, None, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, default=LOG_DIR / "app.log")
    parser.add_argument("--from-start", action="store_true")
    parser.add_argument("--investigate", action="store_true")
    parser.add_argument("--sequence-model", type=Path, help="also run this trained model; off by default, it caught nothing the rules missed")
    parser.add_argument("--collect-s", type=float, default=20, help="seconds to gather evidence after a flag")
    parser.add_argument("--investigate-cooldown-s", type=float, default=300)
    parser.add_argument("--seconds", type=float, help="stop after this long")
    args = parser.parse_args()

    detector = Detector(steps_for_commit=StepMaps(APP_DIR))
    sequence = load(args.sequence_model) if args.sequence_model else None
    grouper = RequestGrouper()
    tail = LogTail(args.log, args.from_start)
    recent: deque = deque()
    scraper = MetricsScraper()
    scraper.start()
    investigations = Investigations(recent, scraper, args.collect_s, args.investigate_cooldown_s) if args.investigate else None
    RUN_DIR.mkdir(exist_ok=True)
    out = open(RUN_DIR / "alerts.jsonl", "a", encoding="utf-8")
    seen_models: dict[str, float] = {}
    began = time.time()
    print(f"watching {args.log}; sequence model {'on' if sequence else 'off'}", flush=True)

    def emit(alert: dict):
        alert["seen_at"] = iso(now())
        out.write(json.dumps(alert) + "\n")
        out.flush()
        print(f"FLAG {alert['ts']} [{alert['check']}] {alert['summary'][:200]}", flush=True)
        if investigations:
            investigations.trigger(alert)

    try:
        while args.seconds is None or time.time() - began < args.seconds:
            for line in tail.read():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "@timestamp" not in row:
                    continue
                recent.append(line)
                cutoff = parse_time(row).timestamp() - KEEP_RAW_SECONDS
                while recent and parse_time(json.loads(recent[0])).timestamp() < cutoff:
                    recent.popleft()
                for alert in detector.feed(row):
                    emit(alert.to_dict())
                rec = grouper.feed(row)
                if rec and sequence:
                    why = sequence.explain(rec)
                    if why and time.time() - seen_models.get(rec.endpoint, 0) > 60:
                        seen_models[rec.endpoint] = time.time()
                        emit({"ts": rec.t.isoformat(timespec="milliseconds"), "check": "sequence_model", "endpoint": rec.endpoint,
                              "request_id": rec.request_id, "summary": f"{rec.endpoint}: {why}", "evidence": []})
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        scraper.stop()
        out.close()
        if investigations:
            while investigations.busy.is_set():
                time.sleep(1)
            investigations.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
