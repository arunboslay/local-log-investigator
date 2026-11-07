"""Train the Isolation Forest and the sequence model on normal traffic only.

Usage: uv run python monitoring/watcher/train_models.py --data monitoring/watcher/data/train_normal
Saves monitoring/watcher/models/forest.pkl and sequence.pkl.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from watcher.models import ForestModel, SequenceModel, choose_top_k, save  # noqa: E402
from watcher.request_data import read_requests  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE / "models")
    args = parser.parse_args()
    recs = read_requests(args.data)
    print(f"{len(recs)} normal requests")

    start = time.perf_counter()
    forest = ForestModel().fit(recs)
    forest_seconds = time.perf_counter() - start
    save(forest, args.out / "forest.pkl")

    sequence = SequenceModel().fit(recs)
    sequence.top_k = choose_top_k(sequence, recs)
    save(sequence, args.out / "sequence.pkl")

    # Speed on one CPU thread, one request at a time, as the live watcher runs them.
    torch.set_num_threads(1)
    sample = recs[:500]
    start = time.perf_counter()
    for r in sample:
        sequence.flag(r)
    seq_ms = (time.perf_counter() - start) / len(sample) * 1000
    start = time.perf_counter()
    for r in sample[:200]:
        forest.flags([r])
    forest_ms = (time.perf_counter() - start) / 200 * 1000
    info = {"requests": len(recs), "forest_train_seconds": round(forest_seconds, 1), "forest_cpu_ms_per_request": round(forest_ms, 2),
            "sequence": sequence.info, "sequence_top_k": sequence.top_k, "sequence_cpu_ms_per_request": round(seq_ms, 2),
            "sequence_flag_rate_on_training_traffic": round(sequence.train_flag_rate(recs[:5000]), 5)}
    (args.out / "training_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
