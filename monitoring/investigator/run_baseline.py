"""Run each local model on every recorded incident, score the reports, and write a results table.

Usage (from the repo root):
    uv run python monitoring/investigator/run_baseline.py [--models gpt-oss-20b] [--incidents <id> ...]

Writes:
    monitoring/investigator_runs/<model>/<incident>.json   report, stats and score
    monitoring/investigator_runs/<model>/<incident>.transcript.json   full conversation
    reports/investigator_baseline.md   results table
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MONITORING = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MONITORING))
from investigator.agent import investigate  # noqa: E402
from investigator.llm_server import PROFILES, LlamaServer, VramMonitor  # noqa: E402
from investigator.score import score_incident  # noqa: E402

INCIDENTS = MONITORING / "incidents"
RUNS = MONITORING / "investigator_runs"
REPORT_MD = MONITORING.parent / "reports" / "investigator_baseline.md"


def gpu_used_mib() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True)
    return int(out.stdout.strip().splitlines()[0])


def run_model(name: str, incidents: list[Path]) -> dict:
    profile = PROFILES[name]
    out_dir = RUNS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    idle = gpu_used_mib()
    vram = VramMonitor()
    vram.start()
    rows = []
    with LlamaServer(profile):
        loaded = gpu_used_mib()
        for inc in incidents:
            print(f"[{name}] {inc.name} ...", flush=True)
            try:
                result = investigate(inc, name, profile.sampling)
            except Exception as exc:  # keep going; record the failure
                result = {"report": None, "stats": {"error": f"{type(exc).__name__}: {exc}"}, "transcript": []}
            scored = score_incident(inc, result["report"])
            (out_dir / f"{inc.name}.json").write_text(json.dumps(
                {"incident": inc.name, "model": name, "report": result["report"], "stats": result["stats"],
                 "score": scored}, indent=2), encoding="utf-8")
            (out_dir / f"{inc.name}.transcript.json").write_text(json.dumps(result["transcript"], indent=1),
                                                                 encoding="utf-8")
            print(f"    {scored}  {result['stats'].get('seconds')}s", flush=True)
            rows.append({"incident": inc.name, **scored, **result["stats"]})
    peak, ram_peak = vram.stop()
    return {"model": name, "rows": rows, "vram_idle_mib": idle, "vram_loaded_mib": loaded, "vram_peak_mib": peak,
            "ram_peak_mib": ram_peak}


def write_markdown(results: list[dict]):
    lines = ["# Investigator baseline", "",
             f"Run on {datetime.now():%Y-%m-%d %H:%M}, RTX 5070 Ti 16 GB, llama.cpp server, 32k context, fully local.",
             "Each model investigates every recorded incident with the same tools and prompt. No fine-tuning yet.", "",
             "Scores: **category** = right kind of cause; **commit/file** = right guilty commit and file (for "
             "non-code faults: correctly blames no commit); **top-1** = all three right.", ""]
    lines += ["## Summary", "", "| Model | Top-1 | Category | Commit | File | Avg time / incident | Gen speed | VRAM loaded / peak (whole card) | RAM peak (server) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rows, n = r["rows"], len(r["rows"])
        pct = lambda k: f"{sum(x[k] for x in rows)}/{n}"
        secs = [x.get("seconds") or 0 for x in rows]
        tps = [x["tokens_per_s"] for x in rows if x.get("tokens_per_s")]
        lines.append(f"| {r['model']} | **{pct('top1')}** | {pct('category')} | {pct('commit')} | {pct('file')} | "
                     f"{sum(secs) / n:.0f} s | {sum(tps) / len(tps) if tps else 0:.0f} tok/s | "
                     f"{r['vram_loaded_mib'] / 1024:.1f} / {r['vram_peak_mib'] / 1024:.1f} GB (idle {r['vram_idle_mib'] / 1024:.1f}) | "
                     f"{r.get('ram_peak_mib', 0) / 1024:.1f} GB |")
    lines += ["", "## Per incident", "", "| Fault | " + " | ".join(r["model"] for r in results) + " |",
              "|---|" + "---|" * len(results)]
    by_fault = {}
    for r in results:
        for x in r["rows"]:
            by_fault.setdefault((x["fault_id"], x["incident"]), {})[r["model"]] = x
    mark = lambda ok: "✅" if ok else "❌"
    for (fault, inc), per in sorted(by_fault.items()):
        cells = []
        for r in results:
            x = per.get(r["model"])
            cells.append("-" if x is None else
                         f"{mark(x['top1'])} cat {mark(x['category'])} commit {mark(x['commit'])} file {mark(x['file'])} "
                         f"({x.get('seconds', 0):.0f} s, {len(x.get('tool_calls', []))} tools)")
        lines.append(f"| {fault} | " + " | ".join(cells) + " |")
    REPORT_MD.parent.mkdir(exist_ok=True)
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_MD}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=list(PROFILES))
    parser.add_argument("--incidents", nargs="+")
    args = parser.parse_args()
    incidents = sorted(p for p in INCIDENTS.iterdir() if (p / "incident.json").exists())
    if args.incidents:
        incidents = [p for p in incidents if p.name in args.incidents]
    summary = RUNS / "summary.json"
    results = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else []
    for m in args.models:  # results for other models from earlier runs are kept
        new = run_model(m, incidents)
        results = [r for r in results if r["model"] != m] + [new]
        summary.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        write_markdown(results)


if __name__ == "__main__":
    main()
