"""The investigator: a local model that uses tools over the incident evidence and writes a report.

Usage (with a llama.cpp server already running, see llm_server.py):
    uv run python -m investigator.agent monitoring/incidents/<id>      (run from the monitoring folder)

The loop: send the conversation and the tool list to the model; if it asks for tools, run them and
send the results back; stop when it calls submit_report. Only the local server is called.
"""

import json
import sys
import time
from pathlib import Path

import httpx

from investigator.evidence import Evidence
from investigator.llm_server import BASE_URL

CATEGORIES = ["code_change", "config_change", "database", "dependency", "traffic"]

SYSTEM_PROMPT = """You investigate production incidents in a Java Spring Boot web service (controller -> service -> repository, H2 database, an outside payment service).
Something went wrong during the incident window. Find what broke, when it started, and the root cause.

How to work:
1. Start with overview and summarize_logs to see what changed over time: errors, WARNINGS, slow endpoints, deploys. Repeated WARN lines that start at a certain time are as important as errors. Short DOWN gaps right before an app start are just deploy restarts.
2. Use get_metrics for rates, errors, latency, DB connection pool and JVM heap over time.
3. Pick a suspicious request and follow it step by step with search_logs(request_id=...). Compare its steps with the code (lookup_code, read_file). A missing step (for example no "saved" line) can mean a silent failure even when the status is 2xx.
4. Check which commits were deployed just before the problem started (list_commits, show_diff). Not every recent commit is guilty; some are harmless. Problems can also come from outside the code: database, outside services, traffic.
5. Base every claim on evidence you saw. Personal data is masked as <EMAIL>, <PHONE>, <NAME>, <CARD>, <ADDRESS>.

When done, call submit_report exactly once. For code_change or config_change, always fill guilty_commit (sha) and guilty_file (path). Category must be one of: code_change (a code commit caused it), config_change (a config/properties commit caused it), database (database down or broken, no code change), dependency (an outside service failed), traffic (load spike, no change)."""

TOOLS = [
    {"name": "overview", "description": "Incident window, app starts (deploys) seen in logs, and recent commits.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "summarize_logs", "description": "Counts per 30 s, per endpoint (status, latency, DB queries), and the most common WARN/ERROR messages. Times are UTC HH:MM:SS.",
     "parameters": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}},
    {"name": "search_logs", "description": "Log lines matching all given filters, oldest first. Use request_id to follow one request step by step.",
     "parameters": {"type": "object", "properties": {
         "request_id": {"type": "string"}, "text": {"type": "string", "description": "substring of message or stack trace"},
         "level": {"type": "string", "enum": ["INFO", "WARN", "ERROR"]},
         "endpoint": {"type": "string", "description": "e.g. /api/orders/{id}"},
         "start": {"type": "string"}, "end": {"type": "string"},
         "include_stack": {"type": "boolean"}, "limit": {"type": "integer"},
         "offset": {"type": "integer", "description": "skip this many matches (to page through results)"}}}},
    {"name": "get_metrics", "description": "Metrics over time: request rate, 5xx rate, latency, busiest endpoints, DB pool active/pending, JVM heap, GC time. DOWN rows mean the app was not answering.",
     "parameters": {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}}},
    {"name": "list_commits", "description": "Last 10 commits with UTC time and changed files.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "show_diff", "description": "Full diff of one commit.",
     "parameters": {"type": "object", "properties": {"sha": {"type": "string"}}, "required": ["sha"]}},
    {"name": "lookup_code", "description": "Find code for an endpoint ('POST /api/orders'), a log message (paste it), or Class#method. Returns files, lines and call chains.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Numbered source lines of a file (path like src/main/java/...) as deployed during the incident.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"},
                                                     "end_line": {"type": "integer"}}, "required": ["path"]}},
    {"name": "submit_report", "description": "Submit the final report. Call once, at the end.",
     "parameters": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "what happened, 2-4 sentences"},
         "category": {"type": "string", "enum": CATEGORIES},
         "likely_cause": {"type": "string"},
         "started_at": {"type": "string", "description": "UTC HH:MM:SS when the problem began"},
         "affected_endpoints": {"type": "array", "items": {"type": "string"}},
         "guilty_commit": {"type": ["string", "null"], "description": "sha of the commit that caused it, or null"},
         "guilty_file": {"type": ["string", "null"]},
         "guilty_line": {"type": ["integer", "null"]},
         "confidence": {"type": "number", "description": "0 to 1"},
         "evidence": {"type": "array", "items": {"type": "string"}, "description": "key log lines, metrics and code lines"}},
         "required": ["summary", "category", "likely_cause", "confidence", "evidence"]}},
]


def _run_tool(ev: Evidence, name: str, args: dict) -> str:
    fn = getattr(ev, name, None)
    if fn is None or name.startswith("_"):
        return f"unknown tool {name}"
    try:
        return fn(**args)
    except TypeError as exc:
        return f"bad arguments for {name}: {exc}"
    except Exception as exc:  # a tool error goes back to the model, not up the stack
        return f"tool error: {type(exc).__name__}: {exc}"


def _trim_history(messages: list[dict], budget_chars: int = 80_000):
    """Keep the conversation inside the context window: blank out the oldest tool results first."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    for m in messages:
        if total <= budget_chars:
            break
        if m["role"] == "tool" and len(m["content"]) > 200:
            total -= len(m["content"]) - 60
            m["content"] = "[older tool output removed to save space]"


def investigate(incident_dir: Path, model: str, sampling: dict, max_steps: int = 25) -> dict:
    ev = Evidence(incident_dir)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Incident window {ev.meta['window_start']} to {ev.meta['window_end']} "
                                            f"(UTC times in tools). Users reported problems. Investigate and submit a report."}]
    tools = [{"type": "function", "function": t} for t in TOOLS]
    stats = {"steps": 0, "tool_calls": [], "prompt_tokens": 0, "completion_tokens": 0, "gen_ms": 0.0}
    started = time.time()
    report = None

    for step in range(max_steps + 1):
        final_push = step == max_steps
        if final_push:
            messages.append({"role": "user", "content": "Stop investigating now and call submit_report with your best answer."})
        _trim_history(messages)
        body = {"model": model, "messages": messages, "tools": tools, "max_tokens": 4096, **sampling}
        if final_push:  # only the report tool is allowed now
            body["tools"] = [t for t in tools if t["function"]["name"] == "submit_report"]
            body["tool_choice"] = "required"
        for attempt in range(3):
            resp = httpx.post(f"{BASE_URL}/v1/chat/completions", json=body, timeout=900)
            # The server rejects a reply it can't parse into a tool call (gpt-oss sometimes uses the
            # wrong channel). Sampling is random, so asking again usually works.
            if resp.status_code != 500 or attempt == 2:
                break
            stats["retries"] = stats.get("retries", 0) + 1
        resp.raise_for_status()
        data = resp.json()
        stats["steps"] += 1
        usage, timings = data.get("usage", {}), data.get("timings", {})
        stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        stats["completion_tokens"] += usage.get("completion_tokens", 0)
        stats["gen_ms"] += timings.get("predicted_ms", 0.0)

        msg = data["choices"][0]["message"]
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("reasoning_content"):
            assistant["reasoning_content"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            assistant["tool_calls"] = msg["tool_calls"]
        messages.append(assistant)

        if not msg.get("tool_calls"):
            messages.append({"role": "user", "content": "Use the tools to check, then call submit_report."})
            continue
        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            stats["tool_calls"].append(name)
            if name == "submit_report":
                missing = [k for k in ("guilty_commit", "guilty_file") if not args.get(k)]
                if args.get("category") in ("code_change", "config_change") and missing and not stats.get("asked_fix"):
                    stats["asked_fix"] = True
                    result = (f"Report not accepted yet: for {args['category']} you must give {' and '.join(missing)} "
                              "(the commit sha and the file path you found). Call submit_report again with them.")
                else:
                    report = args
                    result = "report received"
            else:
                result = _run_tool(ev, name, args)
                left = max_steps - step - 1
                if left <= 8:
                    result += f"\n[{left} steps left. Wrap up and call submit_report soon.]"
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
        if report is not None:
            break

    stats["seconds"] = round(time.time() - started, 1)
    stats["tokens_per_s"] = round(stats["completion_tokens"] / (stats["gen_ms"] / 1000), 1) if stats["gen_ms"] else None
    return {"report": report, "stats": stats, "transcript": messages}


def main():
    from investigator.llm_server import PROFILES
    incident = Path(sys.argv[1])
    model = sys.argv[2] if len(sys.argv) > 2 else "gpt-oss-20b"
    result = investigate(incident, model, PROFILES[model].sampling)
    print(json.dumps({"report": result["report"], "stats": result["stats"]}, indent=2))


if __name__ == "__main__":
    main()
