"""Token lengths of the built examples with gpt-oss's own tokenizer (run in the Unsloth environment).

Usage: .venv-unsloth\\Scripts\\python.exe monitoring/dataset/measure_tokens.py [--limit 1536]
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from transformers import AutoTokenizer

DATA = Path(__file__).resolve().parent / "data"

parser = argparse.ArgumentParser()
parser.add_argument("--limit", type=int, default=1536)
args = parser.parse_args()
tok = AutoTokenizer.from_pretrained("unsloth/gpt-oss-20b-unsloth-bnb-4bit")
for split in ("train", "valid", "test_heldout"):
    rows = [json.loads(l) for l in (DATA / f"{split}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        continue
    lengths = [len(tok(tok.apply_chat_template(r["messages"], tokenize=False))["input_ids"]) for r in rows]
    by_ep = defaultdict(list)
    for r, n in zip(rows, lengths):
        by_ep[r["meta"]["http"]].append(n)
    print(f"{split}: {len(rows)} examples, median {sorted(lengths)[len(lengths) // 2]}, max {max(lengths)}, "
          f"over {args.limit}: {sum(n > args.limit for n in lengths)}")
    for ep, ns in sorted(by_ep.items()):
        print(f"   {ep:36} max {max(ns):5}  over: {sum(n > args.limit for n in ns)}/{len(ns)}")
