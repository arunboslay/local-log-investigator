"""Score a model on the per-request task: read one request's logs + code, say what really happened.

Usage:
    # untrained gpt-oss-20b through the local llama.cpp server (main uv environment)
    uv run python monitoring/dataset/score_requests.py --backend llama --name base_llama

    # gpt-oss-20b with or without your trained add-on, through Unsloth (Unsloth environment)
    .venv-unsloth\\Scripts\\python.exe monitoring/dataset/score_requests.py --backend unsloth --name base_unsloth
    .venv-unsloth\\Scripts\\python.exe monitoring/dataset/score_requests.py --backend unsloth --adapter outputs/gptoss_logcode/adapter --name tuned

Scores each answer field against the truth label and writes monitoring/dataset/results/<name>.json.
"all right" = really_succeeded, problem, error_location and root_cause_location all correct.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RESULTS = HERE / "results"
LOCATION = re.compile(r"(\w+\.(?:java|properties)):(\d+)")


def parse_answer(text: str) -> dict | None:
    """The JSON object in the model's reply (ignores any text around it)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += text[i] == "{"
            depth -= text[i] == "}"
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    break
        start = text.find("{", start + 1)
    return None


def _loc(value) -> tuple | None:
    m = LOCATION.search(str(value or ""))
    return (m.group(1), m.group(2)) if m else None


def compare(pred: dict | None, gold: dict) -> dict:
    if not pred:
        return {k: False for k in ("parsed", "really_succeeded", "problem", "error_location", "root_cause_location",
                                   "missing_step", "all_right")}
    checks = {
        "parsed": True,
        "really_succeeded": pred.get("really_succeeded") == gold["really_succeeded"],
        "problem": pred.get("problem") == gold["problem"],
        "error_location": _loc(pred.get("error_location")) == _loc(gold["error_location"]),
        "root_cause_location": _loc(pred.get("root_cause_location")) == _loc(gold["root_cause_location"]),
        "missing_step": (str(pred.get("missing_step") or "").lower().strip() == str(gold["missing_step"] or "").lower().strip()),
    }
    checks["all_right"] = all(checks[k] for k in ("really_succeeded", "problem", "error_location", "root_cause_location"))
    return checks


def llama_backend(max_tokens: int):
    import httpx

    sys.path.insert(0, str(HERE.parent))
    from investigator.llm_server import BASE_URL, PROFILES, LlamaServer
    server = LlamaServer(PROFILES["gpt-oss-20b"], ctx=8192)
    server.__enter__()

    def generate(messages):
        body = {"model": "gpt-oss-20b", "messages": messages, "max_tokens": max_tokens, **PROFILES["gpt-oss-20b"].sampling}
        for _ in range(3):
            r = httpx.post(f"{BASE_URL}/v1/chat/completions", json=body, timeout=600)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"].get("content") or ""
        return ""

    return generate, lambda: server.__exit__(None, None, None)


def unsloth_backend(adapter: str | None, max_tokens: int):
    from unsloth import FastLanguageModel  # noqa: F401  (must come before transformers)
    import torch

    name = adapter or "unsloth/gpt-oss-20b-unsloth-bnb-4bit"
    model, tokenizer = FastLanguageModel.from_pretrained(model_name=name, max_seq_length=4096, load_in_4bit=True, dtype=None)
    FastLanguageModel.for_inference(model)

    def generate(messages):
        inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                               return_dict=True, reasoning_effort="low").to("cuda")
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        text = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=False)
        # gpt-oss writes its thinking first; the answer is in the "final" channel.
        return text.split("<|channel|>final<|message|>")[-1].replace("<|return|>", "").replace("<|end|>", "")

    return generate, lambda: None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["llama", "unsloth"], required=True)
    parser.add_argument("--adapter", help="trained add-on folder (unsloth backend)")
    parser.add_argument("--split", default="test_heldout", help="test_heldout or valid")
    parser.add_argument("--name", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()

    examples = [json.loads(l) for l in (DATA / f"{args.split}.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.limit and args.limit < len(examples):  # an evenly spread sample, so every fault type is in it
        step = len(examples) / args.limit
        examples = [examples[int(i * step)] for i in range(args.limit)]
    generate, close = (llama_backend(args.max_tokens) if args.backend == "llama"
                       else unsloth_backend(args.adapter, args.max_tokens))
    rows, start = [], time.time()
    try:
        for i, ex in enumerate(examples, 1):
            gold = json.loads(ex["messages"][2]["content"])
            reply = generate(ex["messages"][:2])
            pred = parse_answer(reply)
            checks = compare(pred, gold)
            rid = re.search(r"requestId (\S+)\)", ex["messages"][1]["content"]).group(1)
            rows.append({"request_id": rid, "meta": ex["meta"], "gold": gold, "pred": pred, "raw": reply[-1500:],
                         "checks": checks})
            print(f"{i:3}/{len(examples)} {ex['meta']['family']:15} {ex['meta']['problem']:16} "
                  f"all_right={checks['all_right']}", flush=True)
    finally:
        close()
    keys = ["parsed", "really_succeeded", "problem", "error_location", "root_cause_location", "missing_step", "all_right"]
    summary = {k: round(sum(r["checks"][k] for r in rows) / len(rows), 3) for k in keys}
    by_family = {}
    for fam in sorted({r["meta"]["family"] for r in rows}):
        sub = [r for r in rows if r["meta"]["family"] == fam]
        by_family[fam] = {"n": len(sub), "all_right": round(sum(r["checks"]["all_right"] for r in sub) / len(sub), 3),
                          "really_succeeded": round(sum(r["checks"]["really_succeeded"] for r in sub) / len(sub), 3)}
    result = {"name": args.name, "backend": args.backend, "adapter": args.adapter, "split": args.split,
              "examples": len(rows), "minutes": round((time.time() - start) / 60, 1), "accuracy": summary,
              "by_family": by_family, "rows": rows}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{args.name}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
