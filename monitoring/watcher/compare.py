"""Compare the detectors on recorded faults and on clean traffic. Writes reports/watcher_eval.md.

Usage: uv run python monitoring/watcher/compare.py

Detectors: rules; Isolation Forest; sequence model; rules + sequence model.
- Rules were adjusted while looking at monitoring/incidents, so those results are optimistic. The dataset
  runs were not used for that and are the fairer test.
- Both models trained on normal traffic only, so every fault type is unseen by them. Their flag
  thresholds came from the training traffic.
- False alarms per hour come from a separate clean recording used for nothing else.
"""

import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MONITORING = HERE.parent
sys.path.insert(0, str(MONITORING))
sys.path.insert(0, str(MONITORING / "dataset"))
from watcher.code_steps import StepMaps  # noqa: E402
from watcher.detector import Detector  # noqa: E402
from watcher.evaluate import describe, utc, went_live  # noqa: E402
from watcher.models import load  # noqa: E402
from watcher.request_data import RequestGrouper, read_rows  # noqa: E402

FAMILY = {"silent_save_failure": "silent save", "silent_save_phone": "silent save", "silent_save_phone_suffix": "silent save",
          "silent_save_order": "silent save", "rare_npe": "null pointer", "npe_gift_city": "null pointer",
          "npe_customer_city": "null pointer", "npe_signup_phone": "null pointer", "bad_config_property": "bad config",
          "bad_timeout": "bad config", "bad_payment_url": "bad config", "slow_query": "slow query",
          "slow_order_query": "slow query", "slow_product_query": "slow query", "n_plus_one": "N+1 queries",
          "memory_leak": "memory leak", "pool_exhausted": "pool exhausted", "payment_timeout": "payment slow",
          "payment_down": "payment down", "db_down": "database down", "traffic_spike": "traffic spike"}
DETECTORS = ["rules", "rules, already running", "forest", "sequence", "rules+sequence"]


def fix_old_mask(rows):
    """Recordings from before the phone-mask fix show stock counts as <PHONE>; undo that one artifact."""
    for row in rows:
        if "available=<PHONE>" in row.get("message", ""):
            row["message"] = row["message"].replace("available=<PHONE>", "available=1000000000")
    return rows


def run_all(rows, maps, forest, sequence, warmup_rows=()) -> dict[str, list[tuple]]:
    """Every detector's flags for a recording, as (time, endpoint) pairs."""
    detector = Detector(steps_for_commit=maps)
    warmed = Detector(steps_for_commit=maps)  # as if the watcher had already been running on normal traffic
    for row in warmup_rows:
        warmed.feed(row)
    grouper = RequestGrouper()
    flags = defaultdict(list)
    last = defaultdict(dict)
    for row in rows:
        for a in detector.feed(row):
            flags["rules"].append((utc(a.ts), a.endpoint))
        for a in warmed.feed(row):
            flags["rules, already running"].append((utc(a.ts), a.endpoint))
        rec = grouper.feed(row)
        if rec is None:
            continue
        for name, hit in (("forest", forest.flags([rec])[0]), ("sequence", sequence.flag(rec))):
            if hit:
                previous = last[name].get(rec.endpoint)  # same 60 s repeat limit as the rules
                if previous is None or (rec.t - previous).total_seconds() >= 60:
                    last[name][rec.endpoint] = rec.t
                    flags[name].append((rec.t, rec.endpoint))
    flags["rules+sequence"] = sorted(flags["rules"] + flags["sequence"], key=lambda f: f[0])
    return flags


def score(flags, info, live):
    related = lambda ep: info["endpoints"] is None or ep is None or ep in info["endpoints"]
    hits = [t for t, ep in flags if t >= live and related(ep)]
    false = sum(1 for t, ep in flags if not (t >= live and related(ep)))
    return ((hits[0] - live).total_seconds() if hits else None), false


def table(results: list[dict], title: str) -> list[str]:
    families = sorted({r["family"] for r in results})
    out = [f"### {title}", "", "| Fault type | Recordings | " + " | ".join(DETECTORS) + " |", "|---|---|" + "---|" * len(DETECTORS)]
    for fam in families + ["**All**"]:
        sub = [r for r in results if fam == "**All**" or r["family"] == fam]
        cells = []
        for d in DETECTORS:
            delays = [r[d]["delay"] for r in sub if r[d]["delay"] is not None]
            med = f" ({statistics.median(delays):.0f} s)" if delays else ""
            cells.append(f"{len(delays)}/{len(sub)}{med}")
        out.append(f"| {fam} | {len(sub)} | " + " | ".join(cells) + " |")
    out.append("| False alarms in these recordings | | " + " | ".join(str(sum(r[d]["false"] for r in results)) for d in DETECTORS) + " |")
    return out + [""]


def main() -> int:
    forest, sequence = load(HERE / "models/forest.pkl"), load(HERE / "models/sequence.pkl")
    info = json.loads((HERE / "models/training_info.json").read_text())
    maps = StepMaps(MONITORING / "demo_app")
    train = read_rows(HERE / "data/train_normal")
    warmup = [r for r in train if (utc(train[-1]["@timestamp"]) - utc(r["@timestamp"])).total_seconds() < 1200]
    groups = {"incidents": sorted((MONITORING / "incidents").iterdir()),
              "runs": sorted(p for p in (MONITORING / "dataset/runs").iterdir() if (p / "logs.jsonl").exists())}
    results = defaultdict(list)
    for group, folders in groups.items():
        for folder in folders:
            described = describe(folder)
            rows = fix_old_mask(read_rows(folder))
            live = went_live(described, rows)
            flags = run_all(rows, maps, forest, sequence, warmup)
            if live is None:  # a normal run within the dataset: everything flagged is false
                print(f"{folder.name}: normal", {d: len(flags[d]) for d in DETECTORS}, flush=True)
                results["normal_runs"].append({"minutes": (utc(rows[-1]["@timestamp"]) - utc(rows[0]["@timestamp"])).total_seconds() / 60,
                                               **{d: len(flags[d]) for d in DETECTORS}})
                continue
            row = {"name": folder.name, "fault": described["fault"], "family": FAMILY[described["fault"]]}
            for d in DETECTORS:
                delay, false = score(flags[d], described, live)
                row[d] = {"delay": delay, "false": false}
            results[group].append(row)
            print(f"{row['fault']:26}", {d: (None if row[d]['delay'] is None else round(row[d]['delay'])) for d in DETECTORS}, flush=True)

    clean_rows = read_rows(HERE / "data/clean_normal")
    hours = (utc(clean_rows[-1]["@timestamp"]) - utc(clean_rows[0]["@timestamp"])).total_seconds() / 3600
    start = time.perf_counter()
    clean_flags = run_all(clean_rows, maps, forest, sequence, warmup)
    clean = {d: {"flags": len(clean_flags[d]), "per_hour": round(len(clean_flags[d]) / hours, 1)} for d in DETECTORS}
    print(json.dumps(clean, indent=2))

    rule_only = Detector(steps_for_commit=maps)
    start = time.perf_counter()
    for row in clean_rows:
        rule_only.feed(row)
    lines_per_s = round(len(clean_rows) / (time.perf_counter() - start))

    md = ["# Watcher evaluation", "", f"Run on {datetime.now():%Y-%m-%d %H:%M}. No GPU is used by any of these once trained.", "",
          "Each cell: faults flagged / recordings, with the median seconds from the fault going live to the first flag on an",
          "affected endpoint. A code or config fault goes live when the app restarts on the guilty commit. A flag before that,",
          "or on an endpoint the fault doesn't touch, counts as a false alarm.", "",
          "Rules were adjusted while looking at the incident recordings, so that table is optimistic. The dataset runs were not",
          "used for that. Both models trained on normal traffic only, so every fault type is new to them.",
          "\"Rules, already running\" first reads 20 minutes of normal traffic, as a watcher that has been up a while would. Plain",
          "\"rules\" starts from nothing, so it may not have learned normal speed yet.", ""]
    md += table(results["runs"], "Dataset runs (fairer test; short normal period before each fault)")
    md += table(results["incidents"], "Incident recordings (rules were adjusted on these)")
    md += [f"### False alarms on clean traffic ({hours * 60:.0f} minutes, varied load, never used for training or adjusting)", "",
           "| Detector | Flags | Per hour |", "|---|---|---|"] + [f"| {d} | {clean[d]['flags']} | {clean[d]['per_hour']} |" for d in DETECTORS] + [""]
    seq = info["sequence"]
    md += ["### Models and speed", "",
           f"- Training traffic: {info['requests']} normal requests (75 minutes, varied load, harmless deploys).",
           f"- Sequence model: tiny LSTM, {seq['params']:,} weights, {seq['vocab']} log message types, top-{info['sequence_top_k']}, "
           f"trained in {seq['train_seconds']} s on {seq['device']}; {info['sequence_cpu_ms_per_request']} ms per request on one CPU thread.",
           f"- Isolation Forest: trained in {info['forest_train_seconds']} s; {info['forest_cpu_ms_per_request']} ms per request on CPU.",
           f"- Rules: about {lines_per_s:,} log lines per second on one CPU thread.", ""]
    report = MONITORING.parent / "reports" / "watcher_eval.md"
    report.write_text("\n".join(md), encoding="utf-8")
    (HERE / "models" / "compare_results.json").write_text(json.dumps({**results, "clean": clean, "lines_per_s": lines_per_s}, indent=1), encoding="utf-8")
    print(f"wrote {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
