"""Fine-tune gpt-oss-20b with Unsloth 4-bit QLoRA on chat examples from a JSONL file.

Usage (from the repo root; uses the separate Unsloth environment):
    .venv-unsloth\\Scripts\\python.exe src/finetune_gptoss.py configs/qlora_gptoss20b.yaml
    .venv-unsloth\\Scripts\\python.exe src/finetune_gptoss.py configs/qlora_gptoss20b.yaml --max-steps 5   # fit check

What happens:
1. Load gpt-oss-20b in 4 bits (about 12 GB on the GPU) and freeze it.
2. Add a LoRA add-on: small extra weights, the only part that learns.
3. Show it the examples. Only the answer part counts toward the loss, not the logs and code it reads.
4. Print one line per step: the loss (how wrong its answers were; should go down) and time per step.
5. Save the add-on to outputs/<run_name>/adapter. The original model is untouched.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from unsloth import FastLanguageModel  # must be imported before transformers / trl
from unsloth.chat_templates import train_on_responses_only

import torch
import yaml
from datasets import Dataset
from transformers import TrainerCallback
from trl import SFTConfig, SFTTrainer

ROOT = Path(__file__).resolve().parent.parent


class PrintProgress(TrainerCallback):
    """One plain line per step: step, loss, seconds for the step, peak GPU memory so far."""

    def __init__(self):
        self.last = time.time()

    def on_step_end(self, args, state, control, **kwargs):
        now = time.time()
        loss = next((h["loss"] for h in reversed(state.log_history) if "loss" in h), None)
        left = (state.max_steps - state.global_step) * (now - self.last) / 60
        loss_text = f"{float(loss):.3f}" if loss is not None else "  -  "
        print(f"step {state.global_step:4}/{state.max_steps}  loss {loss_text}  {now - self.last:5.1f} s/step  "
              f"~{left:.0f} min left  GPU peak {torch.cuda.max_memory_reserved() / 2**30:.1f} GB", flush=True)
        self.last = now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--max-steps", type=int, help="stop after this many steps (e.g. 5 for a fit check)")
    parser.add_argument("--max-seq-length", type=int, help="override max_seq_length")
    parser.add_argument("--lora-r", type=int, help="override the LoRA size r (alpha follows as 2 x r)")
    args = parser.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.lora_r:
        cfg["lora"].update(r=args.lora_r, alpha=2 * args.lora_r)
    if args.max_steps:
        cfg["train"]["max_steps"] = args.max_steps
    if args.max_seq_length:
        cfg["max_seq_length"] = args.max_seq_length
    out_dir = ROOT / "outputs" / cfg["run_name"]

    print(f"Loading {cfg['model']} in 4 bits (the first time downloads ~12 GB)...", flush=True)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=cfg["model"], max_seq_length=cfg["max_seq_length"], load_in_4bit=True, dtype=None)
    lora = cfg["lora"]
    model = FastLanguageModel.get_peft_model(
        model, r=lora["r"], lora_alpha=lora["alpha"], lora_dropout=lora["dropout"],
        target_modules=lora["target_modules"], bias="none",
        use_gradient_checkpointing="unsloth",  # recompute some results instead of storing them: saves memory
        random_state=cfg["train"]["seed"])

    rows = [json.loads(l) for l in (ROOT / cfg["data"]["train"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    texts = [tokenizer.apply_chat_template(r["messages"], tokenize=False) for r in rows]
    lengths = [len(tokenizer(t)["input_ids"]) for t in texts]
    # Cutting an example short would cut off its answer, so drop the few that don't fit instead.
    texts = [t for t, n in zip(texts, lengths) if n <= cfg["max_seq_length"]]
    print(f"{len(rows)} examples; tokens per example: median {sorted(lengths)[len(lengths) // 2]}, max {max(lengths)}; "
          f"{len(rows) - len(texts)} longer than {cfg['max_seq_length']} left out; training on {len(texts)}", flush=True)

    trainer = SFTTrainer(
        model=model, processing_class=tokenizer, train_dataset=Dataset.from_dict({"text": texts}),
        args=SFTConfig(output_dir=str(out_dir), dataset_text_field="text", report_to="none",
                       max_length=cfg["max_seq_length"],
                       dataset_num_proc=1,  # extra worker processes crash on Windows
                       **cfg["train"]),
        callbacks=[PrintProgress()])
    # Learn only from the answers: the logs and code are input, not something to imitate.
    trainer = train_on_responses_only(trainer, instruction_part="<|start|>user<|message|>",
                                      response_part="<|start|>assistant")

    torch.cuda.reset_peak_memory_stats()
    start = time.time()
    result = trainer.train()
    minutes = (time.time() - start) / 60

    model.save_pretrained(str(out_dir / "adapter"))
    tokenizer.save_pretrained(str(out_dir / "adapter"))
    losses = [h["loss"] for h in trainer.state.log_history if "loss" in h]
    summary = {
        "steps": result.global_step, "minutes": round(minutes, 1),
        "seconds_per_step": round(minutes * 60 / max(result.global_step, 1), 1),
        "first_loss": losses[0] if losses else None, "last_loss": losses[-1] if losses else None,
        "peak_gpu_memory_gb": round(torch.cuda.max_memory_reserved() / 2**30, 2),
        "max_seq_length": cfg["max_seq_length"], "adapter": str(out_dir / "adapter"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\nDone.", json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
