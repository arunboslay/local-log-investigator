# local-log-investigator

A local, offline AI that reads a Spring Boot service's logs next to its Java code and works out
what really happened to a request. It runs on one consumer GPU. No log line leaves the machine.

**Status: a working experiment, not a finished product.** The honest results are below, including
what it did not learn.

## The problem

In production support I saw a form submit that looked successful but was never saved, because of a
database error. An AI could work this out from the logs: which steps completed, which endpoint maps
to which controller and service, and which step never happened.

A failure like that is easy to miss, because normal monitoring sees nothing wrong:

```
INFO request received            CustomerController#signup
INFO signup validated name=<NAME> email=<EMAIL> phone=<PHONE>
WARN could not save customer email=<EMAIL>: Value too long for column "PHONE ..." (19)
INFO signup completed email=<EMAIL>
INFO request completed           status=201
```

The user sees "Signup complete". There are no 5xx errors, and "customer saved" never appears.

Production logs contain personal data, so sending them to a cloud AI wasn't an option. Everything
here runs locally.

## Design

```
Spring Boot app -- live logs --> watcher --> flag --> investigator (one local model + tools) --> report --> human
                                 (plain rules,            |
                                  no AI, no GPU)          +-- search logs, read metrics, list commits,
                                                              show diffs, look up code, read files
```

- **Watcher:** follows the log file and runs plain checks, with no model and no GPU: new error patterns,
  jumps in error rate, p95 latency, query count and traffic, and **missing steps** (a 2xx request that
  skipped a step the code should have logged, which is how it catches silent saves).
- **Investigator:** one local model with read-only tools, served by llama.cpp. It ends with a report:
  what broke, the suspect commit, file and line, how sure it is, and the evidence.
- **Code map:** links each endpoint to its controller, service and repository methods, and each log
  message to the line that wrote it. It's a plain-text scan, not a full Java parser.
- **Personal data masking:** emails, phones, card numbers, names and street addresses are masked
  before anything is saved or used for training. Name matching uses a list, so a real system would
  load names from its own customer table.
- **Test lab:** a demo Spring Boot shop that I break on purpose, so the right answer is always known.
  All its customer data is fake.

## Evaluation

Every test case is a fault I injected, so the true cause is recorded. A script compares the model's
JSON answer with it, field by field. No AI grades the answers.

**Task 1: read one request.** The input is one request's masked logs plus the Java code on its path.
The labels come from facts: whether the row is really in the database, the first app frame in the
stack trace or the log line that hid the error, and the known guilty line.

- 15 fault variants in 9 families (silent saves, null pointers, bad config, slow queries, and more),
  plus normal traffic.
- 570 training examples, 63 for checking, 192 held out.
- **Three fault families are never trained on:** N+1 queries, connection pool exhausted, payment
  service down. Scoring on those tests the skill, not memory.

**Task 2: investigate a whole incident** with tools, over 11 faults. It's scored on the kind of
cause, the guilty commit and the guilty file.

## Results

### Watcher: do you need a trained anomaly model?

On this app, no. I compared plain rules with two small models trained only on normal traffic: an
Isolation Forest and a tiny LSTM that predicts the next log line (DeepLog style). Test: 31 recorded
fault runs the rules were not adjusted on, plus 30 minutes of clean traffic. Details are in
[reports/watcher_eval.md](reports/watcher_eval.md).

| Detector | Faults flagged | Flags per hour on clean traffic |
|---|---|---|
| Rules, already running | **29 of 31** (median 1 s) | 4.0 |
| Rules, just started | 27 of 31 | 4.0 |
| LSTM sequence model | 25 of 31 | 2.0 |
| Rules + LSTM | 27 of 31 (just-started rules) | 5.9 |
| Isolation Forest | 5 of 31 | 25.7 |

The LSTM caught nothing the rules missed, so the watcher uses rules only by default. The "clean" flags
for rules and LSTM all came from one real payment timeout during that recording. The rules read about
127,000 log lines per second on one CPU thread when no error text needs masking.

**Live demo** (one run, silent-save fault): flagged 0.3 s after the bad deploy went live, with no flags
in the 90 s before it. The investigator's report came 37 s after the flag, with the right category,
commit and file. It named the wrong line (38, not 41) and wrongly said the phone field was left empty,
when the customer wasn't saved at all. A second run flagged in 0.2 s and reported in 31 s, this time
describing the failure correctly, so answers vary between runs. Both use gpt-oss-20b without the
fine-tuned add-on.

**Limits:** the memory leak isn't found, because it doesn't show in logs. The slow product query is
missed even with a baseline. Short-running watchers can't judge speed yet. The missing-step check only
works for steps the code logs, and a request step inside an `if` could cause false alarms. The rules
were written knowing these faults, on one small app I wrote, so real traffic will be harder.

### Fine-tuning: before and after

Model: OpenAI gpt-oss-20b, 4-bit QLoRA with Unsloth (r=8, 2 passes, 144 steps). Training took
18.7 minutes on an RTX 5070 Ti and peaked at 14.8 GB of its 16 GB. Tested on 60 examples from the
fault families it never saw, through the same engine before and after:

| Check | Before | After |
|---|---|---|
| Did it really succeed? | 95% | 100% |
| Problem type | 80% | 75% |
| Where it broke (file and line) | 53% | 100% |
| Root-cause line | 48% | 52% |
| **All four right** | **35%** | **48%** |

All four right, by fault family (out of 20 each):

| Fault family | Before | After |
|---|---|---|
| Connection pool exhausted | 6 | 17 |
| N+1 queries | 6 | 6 |
| Payment service down | 9 | **6 (worse)** |

### What it did not learn

The score looks better than the skill is. The model got better at reading logs precisely, but it
didn't clearly get better at diagnosing:

- **It reuses causes from training.** Of the 17 pool-exhausted answers counted as right, 12 are failed
  requests. All 12 explanations blame a slow database, and none mentions leaked connections, which
  is the real cause. **The score doesn't check the explanation text**, so they still count. That
  6 to 17 jump is the least trustworthy number here.
- **Payment down got worse.** It now blames a config line, using a sentence from a similar training
  fault, when the service was simply down.
- **N+1 queries:** it answers "slow", because "too many queries" never appears in training.
- **The training loss went flat** at about 3.69 from around step 30, with very small updates after
  that. I haven't explained this. It may have stopped learning early.
- **The sample is small and alike:** 60 examples from 3 families, many from the same failure. Treat
  these numbers as rough.
- **The trained add-on isn't connected to the investigator yet.** The two tasks are measured separately.
- **Everything so far is on my own demo app,** not on a real production system.

### Whole-incident investigation (no fine-tuning)

gpt-oss-20b through llama.cpp: **6 of 11 fully right** on the run in
[reports/investigator_baseline.md](reports/investigator_baseline.md). An earlier run scored 5 of 11,
but two of its incidents failed on a server error that I then fixed. It's strong on crashes and
outages, and weak on slow queries, N+1, memory leaks and traffic spikes. That's one run per incident,
so the score moves with chance.

**Known issue with this number:** that run used a line-ending bug that made every guilty commit's
diff look like a whole-file change. This code has the fix, but the 6 of 11 hasn't been re-measured.
The per-request results above came from recorded runs with the same bug, though their inputs use
source code rather than diffs.

## Run it

Needs: Windows, an NVIDIA GPU with 16 GB, JDK 21, [uv](https://docs.astral.sh/uv/), and git. The
scripts use `cmd` and `mvnw.cmd`, so other systems would need small changes.

**Model weights aren't included.** gpt-oss-20b (Apache-2.0) is downloaded separately. Neither are
recorded incidents or generated data; you create your own.

```powershell
uv sync
uv run pytest -q monitoring/tests                # masking tests
uv run python env_check.py                       # GPU check
uv run python monitoring/setup_demo_app.py       # once: creates the demo app as its own git repo
```

Record an incident (about 3 minutes). It starts the app and traffic, injects the fault, saves the
evidence and undoes the fault:

```powershell
uv run python monitoring/record_incident.py --list
uv run python monitoring/record_incident.py --fault silent_save_failure
```

Run the investigator. Put `llama-server.exe` from a
[llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) in `~/llm/llama.cpp` and
`gpt-oss-20b-MXFP4.gguf` from
[ggml-org/gpt-oss-20b-GGUF](https://huggingface.co/ggml-org/gpt-oss-20b-GGUF) in `~/llm/models`
(or set `LLAMA_DIR` and `LLM_MODELS`):

```powershell
uv run python monitoring/investigator/run_baseline.py
```

Watch live, and run the investigator on the first flag (flags go to `monitoring/run/alerts.jsonl`):

```powershell
uv run python monitoring/watcher/watch.py --investigate
uv run python monitoring/watcher/live_demo.py     # or: app + traffic + watcher + a fault, timed end to end
```

Repeat the detector comparison (about 2 hours of recording; it also needs the dataset runs below):

```powershell
uv run python monitoring/watcher/record_normal.py --name train_normal --minutes 75
uv run python monitoring/watcher/record_normal.py --name clean_normal --minutes 30
uv run python monitoring/watcher/train_models.py --data monitoring/watcher/data/train_normal
uv run python monitoring/watcher/compare.py
```

Build the training data (about an hour) and score the untrained model:

```powershell
uv run python monitoring/dataset/record_requests.py --all --repeat 2
uv run python monitoring/dataset/build_log_code_sft.py
uv run python monitoring/dataset/score_requests.py --backend llama --name base_llama
```

Fine-tune in a separate environment, because Unsloth needs older library versions:

```powershell
uv venv .venv-unsloth --python 3.12
uv pip install --python .venv-unsloth\Scripts\python.exe torch torchvision --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-unsloth\Scripts\python.exe unsloth==2026.10.11 unsloth_zoo pyyaml
.venv-unsloth\Scripts\python.exe src/finetune_gptoss.py configs/qlora_gptoss20b.yaml
.venv-unsloth\Scripts\python.exe monitoring/dataset/score_requests.py --backend unsloth --limit 60 --name before
.venv-unsloth\Scripts\python.exe monitoring/dataset/score_requests.py --backend unsloth --adapter outputs/gptoss_logcode/adapter --limit 60 --name after
```

After the one-time downloads, it's designed to work offline: the lab builds with Maven's offline mode, all services
are on localhost, and models can load with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. To check,
turn off the network and record an incident. I documented this check but have not run it yet.

## What's where

```
monitoring/
  demo_app_template/   the Spring Boot demo shop (Java 21, Spring Boot 4.1)
  faults/catalog.py    11 faults, each with inject, undo and true cause
  record_incident.py   records one incident
  redact.py            personal data masking (tests in tests/)
  code_index.py        endpoint -> controller -> service -> repository map
  watcher/             live log follower, rule checks, the two compared models, evaluation
  investigator/        tools, agent loop, llama.cpp wrapper, scorer
  dataset/             fault variants, request recorder, example builder, scorer
    data/sample_5.jsonl   5 example training records, so you can see the format
src/finetune_gptoss.py Unsloth 4-bit QLoRA training
configs/               training settings
reports/               result numbers
```

## Next steps

1. Find out why the training loss went flat.
2. Score the explanation too, so a wrong cause can't count as right.
3. Add more variety of causes to the training data, including "too many queries", while still holding
   out specific bugs.
4. Re-measure the investigator with the diff fix, several runs per incident.
5. Search past incidents for similar failures, and measure whether it helps.
6. Give the watcher a saved baseline, so a restart doesn't lose what normal looks like, and group one
   fault's several flags into one alert.
7. Test on a second Spring Boot app I didn't write.

## License

MIT, see [LICENSE](LICENSE). The Maven wrapper files in the demo app come from Apache Maven and
Spring Initializr under their own licenses. gpt-oss-20b is Apache-2.0 and isn't included here.
