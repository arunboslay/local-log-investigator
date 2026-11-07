"""Re-check saved predictions against the current labels (after the labels were rebuilt), without re-running the model.

Usage: uv run python monitoring/dataset/rescore.py <results name> [--split test_heldout]
The dataset build is deterministic (fixed seed), so examples line up by position; the request id is
checked to make sure.
"""

import argparse
import json
import re

from score_requests import DATA, RESULTS, compare

parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("--split", default="test_heldout")
args = parser.parse_args()

examples = [json.loads(l) for l in (DATA / f"{args.split}.jsonl").read_text(encoding="utf-8").splitlines()]
if len(examples) > len(json.loads((RESULTS / f"{args.name}.json").read_text(encoding="utf-8"))["rows"]):
    raise SystemExit("this result used a sample (--limit); re-run the scoring instead")
path = RESULTS / f"{args.name}.json"
result = json.loads(path.read_text(encoding="utf-8"))
for ex, row in zip(examples, result["rows"]):
    rid = re.search(r"requestId (\S+)\)", ex["messages"][1]["content"]).group(1)
    if row.get("request_id", rid) != rid:
        raise SystemExit(f"examples don't line up ({rid} vs {row.get('request_id')}); re-run the scoring instead")
    row["request_id"] = rid
    row["gold"] = json.loads(ex["messages"][2]["content"])
    row["checks"] = compare(row["pred"], row["gold"])
rows = result["rows"]
result["accuracy"] = {k: round(sum(r["checks"][k] for r in rows) / len(rows), 3) for k in result["accuracy"]}
for fam in result["by_family"]:
    sub = [r for r in rows if r["meta"]["family"] == fam]
    result["by_family"][fam]["all_right"] = round(sum(r["checks"]["all_right"] for r in sub) / len(sub), 3)
path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"accuracy": result["accuracy"], "by_family": result["by_family"]}, indent=2))
