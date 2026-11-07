"""Build the per-request training data: one request's logs + the Java code they map to -> what really happened.

Usage:
    uv run python monitoring/dataset/build_log_code_sft.py

Reads monitoring/dataset/runs/* (from record_requests.py) and writes monitoring/dataset/data/:
    train.jsonl         fault families used for training (plus normal requests)
    valid.jsonl         10% of the training runs' examples, for checking during training
    test_heldout.jsonl  fault families never trained on (n_plus_one, pool_exhausted, payment_down)
    stats.json          counts

Labels come from facts, not from an AI:
- really_succeeded: the HTTP status, plus for writes, whether the row is really in the database
- error_location: the first app frame in the stack trace, or the log statement that swallowed the error
- root_cause_location: the known guilty line of the injected fault
- missing_step: the first step the code should have logged but didn't
Log lines are redacted (personal data masked) before they go into an example.
"""

import json
import random
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from lab import APP_DIR  # noqa: E402
from redact import redact_text  # noqa: E402
from variants import VARIANTS_BY_ID  # noqa: E402

RUNS = HERE / "runs"
OUT = HERE / "data"
WRITES = {"POST /api/customers", "POST /api/orders", "PUT /api/customers/{id}/address"}
FRAME = re.compile(r"at com\.example\.shop\.([\w.$]+)\.(\w+)\((\w+\.java):(\d+)\)")
MAX_AFFECTED, MAX_NORMAL, MAX_NORMAL_ONLY_RUN = 22, 10, 30

SYSTEM_PROMPT = """You read the logs of ONE HTTP request to a Java Spring Boot service, together with the Java code those log lines come from. Work out what really happened to that request.
Answer with JSON only, in this shape:
{"http_status": number,
 "steps_completed": [step names, in order],
 "missing_step": "the step that should have happened but did not" or null,
 "really_succeeded": true or false (did the request do its job, e.g. was the data really saved),
 "problem": "none" | "failed" | "silent_failure" | "slow" | "too_many_queries",
 "error_location": "Class.method (File.java:line)" where the error happened or was swallowed, or null,
 "root_cause_location": "File:line" of the code or config line that causes it, or null if it is outside the code,
 "explanation": "one or two plain sentences"}"""


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def step_name(message: str) -> str:
    """'signup validated name=x email=y' -> 'signup validated'."""
    words = []
    for w in message.split():
        if "=" in w or w.endswith(":"):
            break
        words.append(w)
    return " ".join(words)


class CodeView:
    """Source and call paths at one commit, from that run's code_index.json."""

    def __init__(self, index: dict, commit: str):
        self.index, self.commit, self._files = index, commit, {}

    def source(self, file: str) -> list[str]:
        if file not in self._files:
            out = subprocess.run(["git", "show", f"{self.commit}:{file}"], cwd=APP_DIR, capture_output=True,
                                 text=True, encoding="utf-8")
            self._files[file] = out.stdout.splitlines()
        return self._files[file]

    def endpoint(self, http: str) -> dict | None:
        return next((e for e in self.index["endpoints"] if e["http"] == http), None)

    def methods_on_path(self, http: str) -> list[tuple[str, str]]:
        """(Class, method) pairs from the controller down, in call order, skipping inherited ones."""
        ep = self.endpoint(http)
        if not ep:
            return []
        seen, order = set(), []

        def walk(target: str, calls: list):
            if target in seen:
                return
            seen.add(target)
            order.append(tuple(target.split("#")))
            for c in calls:
                if c.get("line"):
                    walk(c["call"], c.get("calls", []))

        walk(ep["handler"], ep["calls"])
        return order

    def expected_steps(self, http: str) -> list[str]:
        """INFO step names the code logs along this endpoint's path, in code order."""
        steps = []

        def visit(cls: str, method: str, depth: int = 0):
            m = self.index["classes"].get(cls, {}).get("methods", {}).get(method)
            if not m or depth > 4:
                return
            events = [(l["line"], "log", l) for l in m.get("logs", []) if l["level"] == "INFO"]
            events += [(c["line"], "call", c) for c in m.get("calls", [])]
            for _, kind, item in sorted(events, key=lambda e: e[0]):
                if kind == "log":
                    steps.append(step_name(item["template"]))
                else:
                    visit(*item["target"].split("#"), depth + 1)

        ep = self.endpoint(http)
        if ep:
            visit(*ep["handler"].split("#"))
        return steps

    def log_location(self, message: str) -> str | None:
        for st in self.index["log_statements"]:
            if re.fullmatch(re.escape(st["template"]).replace(r"\{\}", ".*?"), message, flags=re.S):
                return f"{st['class']}.{st['method']} ({st['file'].split('/')[-1]}:{st['line']})"
        return None

    def code_text(self, http: str) -> str:
        parts = []
        for cls, method in self.methods_on_path(http):
            info = self.index["classes"].get(cls)
            m = info and info["methods"].get(method)
            # Repository interfaces add little next to their callers' code, unless they hold a custom query.
            if not m or (info["kind"] == "interface" and not any("@Query" in a for a in m.get("annotations", []))):
                continue
            lines = self.source(info["file"])
            start = max(1, m["line"] - len(m.get("annotations", [])))
            end = m.get("end_line", m["line"])
            numbered = [(i, lines[i - 1].rstrip()) for i in range(start, min(end, len(lines)) + 1) if lines[i - 1].strip()]
            indent = min(len(t) - len(t.lstrip()) for _, t in numbered)  # drop the shared indentation: fewer tokens
            parts.append(f"// {info['file'].split('/')[-1]}  {cls}.{method}\n" +
                         "\n".join(f"{i:4}  {t[indent:]}" for i, t in numbered))
        return "\n\n".join(parts)

    def shows_line(self, code: str, file: str, line: int) -> bool:
        """Is file:line part of the code (or config) shown in the example?"""
        name = file.split("/")[-1]
        if name == "application.properties":
            return f"{line:4}  " in self.config_text()
        sections = code.split("\n\n// ")
        return any(s.lstrip("/ ").startswith(name) and re.search(rf"(?m)^\s*{line}  ", s) for s in sections)

    def config_text(self) -> str:
        """Only the settings that can break a request: database, connection pool, outside services."""
        lines = self.source("src/main/resources/application.properties")
        keep = ("spring.datasource.url", "spring.datasource.hikari", "payment.")
        return "\n".join(f"{i:4}  {l}" for i, l in enumerate(lines, 1) if l.startswith(keep))


def load_run(run: Path):
    meta = json.loads((run / "meta.json").read_text())
    requests = {r["request_id"]: r for r in map(json.loads, (run / "requests.jsonl").read_text(encoding="utf-8").splitlines())
                if r.get("request_id")}
    db = json.loads((run / "db.json").read_text())
    logs = defaultdict(list)
    for line in (run / "logs.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("requestId"):
            row["_t"] = _utc(row["@timestamp"])
            logs[row["requestId"]].append(row)
    index = json.loads((run / "code_index.json").read_text(encoding="utf-8"))
    return meta, requests, db, logs, CodeView(index, meta["code_commit"])


def really_saved(http: str, req: dict, db: dict, last_update: dict) -> bool | None:
    if http == "POST /api/customers":
        return req["body"]["email"] in set(db["customer_emails"])
    if http == "POST /api/orders":
        m = re.search(r'"orderId"\s*:\s*(\d+)', req.get("response", ""))
        return bool(m) and m.group(1) in set(db["order_ids"])
    if http == "PUT /api/customers/{id}/address":
        cid = req["path"].split("/")[3]
        if last_update.get(cid) is not req:
            return None  # a later update to the same customer overwrote it; can't check
        return db["addresses"].get(cid) == [req["body"]["street"], req["body"]["city"]]
    return True


def label(meta, variant, rows, req, db, last_update, view, typical) -> dict | None:
    done = next((r for r in rows if r.get("message") == "request completed"), None)
    if not done:
        return None
    http = f"{done.get('httpMethod')} {done.get('endpoint')}"
    status, latency, queries = int(done["status"]), int(done.get("latencyMs", 0)), int(done.get("dbQueries", 0))
    steps = [step_name(r["message"]) for r in rows if r.get("level") == "INFO" and r.get("logger_name", "").startswith("com.example.shop")
             and not r.get("logger_name", "").startswith("com.example.shop.web") and not r["message"].startswith("service started")]
    saved = really_saved(http, req, db, last_update) if 200 <= status < 300 else False
    if saved is None:
        return None
    med_ms, med_q = typical.get(http, (latency, queries))

    error_location, exception = None, None
    for r in rows:
        trace = r.get("stack_trace", "")
        if trace and r.get("level") == "ERROR":
            exception = trace.splitlines()[0][:220]
            frame = next((f for f in FRAME.finditer(trace) if not f.group(1).startswith("web.")), None)
            if frame:
                cls = frame.group(1).split(".")[-1].split("$")[0]
                error_location = f"{cls}.{frame.group(2).split('$')[0]} ({frame.group(3)}:{frame.group(4)})"
            break
    warn = next((r for r in rows if r.get("level") == "WARN" and r.get("logger_name", "").startswith("com.example.shop")
                 and not r.get("logger_name", "").startswith("com.example.shop.web")), None)

    if status >= 500:
        problem = "failed"
    elif http in WRITES and not saved:
        problem = "silent_failure"
        error_location = view.log_location(warn["message"]) if warn else None
    elif queries > max(10, 5 * med_q):
        problem = "too_many_queries"
    elif latency >= max(40, 8 * med_ms):
        problem = "slow"
    else:
        problem = "none"

    after_fault = variant and "injected_at" in meta and rows[0]["_t"] >= _utc(meta["injected_at"])
    blame = problem != "none" and after_fault and http in variant.endpoints
    root = f"{meta['guilty_file'].split('/')[-1]}:{meta['guilty_line']}" if blame and meta.get("guilty_line") else None
    code = view.code_text(http)
    if root and not view.shows_line(code, meta["guilty_file"], meta["guilty_line"]):
        # The bug is in code this request never ran (e.g. another endpoint leaking DB connections):
        # it can't be pinned to a line from this request alone.
        root = None
    cause = variant.short_cause if blame else "it is not clear from this request alone"

    expected = view.expected_steps(http)
    missing = None
    if problem in ("failed", "silent_failure"):
        missing = next((s for s in expected if s not in steps), None)

    if problem == "none":
        explanation = (f"All steps completed ({', '.join(steps) or 'no step logs'}) and it returned {status}"
                       + (" and the data was saved." if http in WRITES else ".") + " Nothing went wrong.")
    elif problem == "silent_failure":
        explanation = (f"It returned {status}, so it looks successful, but the data was not saved: "
                       f"'{missing}' never happened. The error was caught and only logged as a warning at "
                       f"{error_location}. Cause: {cause}.")
    elif problem == "failed":
        explanation = (f"It failed with {status} after '{steps[-1] if steps else 'the start'}'"
                       + (f": {exception.split(':')[0]} at {error_location}" if exception else "") + f". Cause: {cause}.")
    elif problem == "slow":
        explanation = f"It worked but took {latency} ms, about {latency // max(med_ms, 1)}x slower than usual ({med_ms} ms). Cause: {cause}."
    else:
        explanation = f"It worked but ran {queries} database queries instead of the usual {med_q}. Cause: {cause}."

    answer = {"http_status": status, "steps_completed": steps, "missing_step": missing,
              "really_succeeded": bool(saved) and status < 400 and problem in ("none", "slow", "too_many_queries"),
              "problem": problem, "error_location": error_location, "root_cause_location": root,
              "explanation": explanation}

    log_lines = []
    for r in rows:
        extra = " ".join(f"{k}={r[k]}" for k in ("status", "latencyMs", "dbQueries") if k in r)
        who = r.get("handler") if r.get("logger_name", "").startswith("com.example.shop.web") else r.get("logger_name", "").split(".")[-1]
        log_lines.append(f"{r['_t'].strftime('%H:%M:%S.%f')[:-3]} {r.get('level', ''):5} [{who}] {redact_text(r.get('message', ''))} {extra}".rstrip())
        trace = r.get("stack_trace")
        if trace:
            keep = [trace.splitlines()[0]] + [l.strip() for l in trace.splitlines() if "com.example" in l][:4]
            log_lines += ["    " + redact_text(l)[:220] for l in keep]
    user = (f"Request: {http}  (requestId {rows[0]['requestId']})\n"
            f"Typical for this endpoint: about {med_ms} ms and {med_q} database queries.\n\n"
            "Log lines for this request:\n" + "\n".join(log_lines) +
            "\n\nCode on this request's path (as deployed):\n" + code +
            "\n\nConfig (application.properties):\n" + view.config_text())
    return {"messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user},
                         {"role": "assistant", "content": json.dumps(answer)}],
            "meta": {"run": meta["variant"], "family": meta["family"], "held_out": meta["held_out"], "http": http,
                     "problem": problem, "blamed": bool(blame)}}


def typical_values(meta, requests, logs) -> dict:
    """Median latency and query count per endpoint before the fault (or across a normal run)."""
    cutoff = _utc(meta["injected_at"]) if "injected_at" in meta else None
    acc = defaultdict(lambda: ([], []))
    for rid, rows in logs.items():
        done = next((r for r in rows if r.get("message") == "request completed"), None)
        if done and (cutoff is None or done["_t"] < cutoff):
            http = f"{done.get('httpMethod')} {done.get('endpoint')}"
            acc[http][0].append(int(done.get("latencyMs", 0)))
            acc[http][1].append(int(done.get("dbQueries", 0)))
    return {k: (int(statistics.median(a)), int(statistics.median(b))) for k, (a, b) in acc.items()}


def build_run(run: Path, rng: random.Random) -> list[dict]:
    meta, requests, db, logs, view = load_run(run)
    variant = VARIANTS_BY_ID.get(meta["variant"])
    typical = typical_values(meta, requests, logs)
    last_update = {}
    for r in sorted(requests.values(), key=lambda r: r["ts"]):
        if r["kind"] == "update_address":
            last_update[r["path"].split("/")[3]] = r
    cutoff = _utc(meta["injected_at"]) if "injected_at" in meta else None
    affected, normal = [], []
    for rid, rows in logs.items():
        req = requests.get(rid)
        if not req or (cutoff and rows[0]["_t"] < cutoff):
            continue
        ex = label(meta, variant, rows, req, db, last_update, view, typical)
        # A slow request the fault doesn't explain is usually the app warming up after a restart: too
        # ambiguous to teach from, so leave it out.
        if ex and ex["meta"]["problem"] in ("slow", "too_many_queries") and not ex["meta"]["blamed"]:
            continue
        if ex:
            (normal if ex["meta"]["problem"] == "none" else affected).append(ex)
    rng.shuffle(affected)
    rng.shuffle(normal)
    if not variant:
        return normal[:MAX_NORMAL_ONLY_RUN]
    # Spread the normal examples over different endpoints.
    by_ep = defaultdict(list)
    for ex in normal:
        by_ep[ex["meta"]["http"]].append(ex)
    picked_normal = []
    while len(picked_normal) < MAX_NORMAL and any(by_ep.values()):
        for ep in list(by_ep):
            if by_ep[ep] and len(picked_normal) < MAX_NORMAL:
                picked_normal.append(by_ep[ep].pop())
    return affected[:MAX_AFFECTED] + picked_normal


def main():
    rng = random.Random(42)
    train, test = [], []
    for run in sorted(RUNS.iterdir()):
        if not (run / "meta.json").exists():
            continue
        examples = build_run(run, rng)
        held = json.loads((run / "meta.json").read_text())["held_out"]
        (test if held else train).extend(examples)
        print(f"{run.name}: {len(examples)} examples ({sum(e['meta']['problem'] != 'none' for e in examples)} with a problem)")
    rng.shuffle(train)
    n_valid = max(1, len(train) // 10)
    valid, train = train[:n_valid], train[n_valid:]
    OUT.mkdir(exist_ok=True)
    for name, rows in (("train", train), ("valid", valid), ("test_heldout", test)):
        with (OUT / f"{name}.jsonl").open("w", encoding="utf-8") as f:
            for ex in rows:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    stats = {name: {"examples": len(rows),
                    "by_problem": dict(sorted(defaultdict(int, {p: sum(e["meta"]["problem"] == p for e in rows)
                                                                for p in {e["meta"]["problem"] for e in rows}}).items())),
                    "by_family": dict(sorted({f: sum(e["meta"]["family"] == f for e in rows)
                                              for f in {e["meta"]["family"] for e in rows}}.items())),
                    "avg_chars": int(sum(len(e["messages"][1]["content"]) for e in rows) / max(len(rows), 1))}
             for name, rows in (("train", train), ("valid", valid), ("test_heldout", test))}
    (OUT / "stats.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
