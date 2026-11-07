"""Read-only tools over one incident's evidence. The investigator sees only what these return.

Sources: incident.json, logs.jsonl (already redacted), metrics.jsonl, commits.txt, diffs.patch,
code_index.json, and the demo app's git history (read-only, at the incident's code commit).
truth.json is never opened here.

All times shown to the model are UTC, as HH:MM:SS.
"""

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

MONITORING = Path(__file__).resolve().parent.parent
APP_REPO = MONITORING / "demo_app"
MAX_CHARS = 4000
LABELS = re.compile(r'(\w+)="([^"]*)"')
SHAPE_NUMBERS = re.compile(r"\b[0-9a-f]{8,}\b|\d+(?:\.\d+)?")


def _utc(ts: str) -> datetime:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _hms(t: datetime, ms: bool = False) -> str:
    return t.strftime("%H:%M:%S.%f")[:-3] if ms else t.strftime("%H:%M:%S")


def _shape(message: str) -> str:
    """Group similar messages: numbers and ids become N."""
    return SHAPE_NUMBERS.sub("N", message.split("\n")[0])[:170]


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [cut, {len(text) - limit} more chars; narrow the query]"


def _metric(key: str) -> tuple[str, dict]:
    name, _, rest = key.partition("{")
    return name, dict(LABELS.findall(rest))


def _parse_commits(text: str) -> list[dict]:
    commits = []
    for block in re.split(r"(?m)^commit ", text)[1:]:
        lines = block.splitlines()
        date = next((l[5:].strip() for l in lines if l.startswith("Date:")), "")
        subject = next((l.strip() for l in lines[1:] if l.startswith("    ")), "")
        files = [l.split("|")[0].strip() for l in lines if "|" in l]
        commits.append({"sha": lines[0].strip(), "time": _utc(datetime.strptime(date, "%Y-%m-%d %H:%M:%S %z").isoformat()),
                        "subject": subject, "files": files})
    return commits


class Evidence:
    def __init__(self, incident_dir: Path):
        self.dir = Path(incident_dir)
        self.meta = json.loads((self.dir / "incident.json").read_text(encoding="utf-8"))
        self.start, self.end = _utc(self.meta["window_start"]), _utc(self.meta["window_end"])
        self.code_commit = self.meta["code_commit"]
        self.logs = []
        for line in (self.dir / "logs.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and "@timestamp" in row:
                row["_t"] = _utc(row["@timestamp"])
                self.logs.append(row)
        self.logs.sort(key=lambda r: r["_t"])
        self.metrics = [json.loads(l) for l in (self.dir / "metrics.jsonl").read_text().splitlines() if l.strip()]
        self.index = json.loads((self.dir / "code_index.json").read_text(encoding="utf-8"))
        self.commits = _parse_commits((self.dir / "commits.txt").read_text(encoding="utf-8"))
        for c in self.commits:  # git --stat shortens long paths; ask git for the full list
            names = subprocess.run(["git", "show", "--name-only", "--format=", c["sha"]], cwd=APP_REPO,
                                   capture_output=True, text=True, encoding="utf-8").stdout.split()
            c["files"] = names or c["files"]
        self.diffs = (self.dir / "diffs.patch").read_text(encoding="utf-8")

    # ---- helpers ----

    def _time(self, value: str | None, default: datetime) -> datetime:
        """Accept 'HH:MM:SS' (UTC, same day as the window) or a full ISO timestamp."""
        if not value:
            return default
        try:
            if "T" in value or "-" in value:
                return _utc(value)
            t = time.fromisoformat(value)
            return datetime.combine(self.start.date(), t, timezone.utc)
        except ValueError:
            return default

    def _window(self, start, end):
        return self._time(start, self.start), self._time(end, self.end)

    # ---- tools ----

    def overview(self) -> str:
        """Incident window, deploys seen in the logs, and commits in or near the window."""
        out = [f"Service: {self.meta['service']}",
               f"Window (UTC): {_hms(self.start)} to {_hms(self.end)} on {self.start.date()}",
               "App starts (deploys) seen in logs. The app is briefly DOWN for a few seconds before each start; "
               "that restart gap is normal and is not the incident by itself:"]
        for r in self.logs:
            if r.get("message", "").startswith("service started"):
                out.append(f"  {_hms(r['_t'])}  {r['message']}")
        out.append("Commits from 1 hour before the window to its end:")
        for c in self.commits:
            if self.start - timedelta(hours=1) <= c["time"] <= self.end:
                out.append(f"  {c['sha'][:10]}  {_hms(c['time'])}  {c['subject']}  files: {_files(c['files'])}")
        return "\n".join(out)

    def summarize_logs(self, start: str | None = None, end: str | None = None) -> str:
        """Counts per 30 s, per endpoint, and the most common WARN/ERROR messages."""
        t0, t1 = self._window(start, end)
        rows = [r for r in self.logs if t0 <= r["_t"] <= t1]
        done = [r for r in rows if r.get("message") == "request completed"]
        out = [f"{len(rows)} log lines, {len(done)} completed requests, levels: "
               f"{dict(Counter(r.get('level') for r in rows))}", "",
               "Per 30 s:  time      reqs  4xx  5xx  WARN  ERROR  avg_ms"]
        buckets = defaultdict(list)
        for r in rows:
            buckets[r["_t"].replace(second=r["_t"].second // 30 * 30, microsecond=0)].append(r)
        for b in sorted(buckets):
            rs = buckets[b]
            req = [r for r in rs if r.get("message") == "request completed"]
            lat = [int(r.get("latencyMs", 0)) for r in req]
            out.append(f"           {_hms(b)}  {len(req):4}  {sum(r.get('status', '')[:1] == '4' for r in req):3}  "
                       f"{sum(r.get('status', '')[:1] == '5' for r in req):3}  "
                       f"{sum(r.get('level') == 'WARN' for r in rs):4}  {sum(r.get('level') == 'ERROR' for r in rs):5}  "
                       f"{(sum(lat) / len(lat) if lat else 0):6.0f}")
        out += ["", "Per endpoint:  method endpoint                      reqs  2xx  4xx  5xx  avg_ms  max_ms  avg_db_queries"]
        per = defaultdict(list)
        for r in done:
            per[(r.get("httpMethod"), r.get("endpoint", r.get("path")))].append(r)
        for (m, ep), rs in sorted(per.items(), key=lambda kv: -len(kv[1])):
            lat = [int(r.get("latencyMs", 0)) for r in rs]
            dbq = [int(r.get("dbQueries", 0)) for r in rs]
            st = Counter(r.get("status", "")[:1] for r in rs)
            out.append(f"              {m:6} {str(ep):30} {len(rs):4} {st['2']:4} {st['4']:4} {st['5']:4} "
                       f"{sum(lat) / len(lat):7.0f} {max(lat):7} {sum(dbq) / len(dbq):8.1f}")
        out += ["", "Top WARN/ERROR messages (numbers shown as N):"]
        groups = defaultdict(list)
        for r in rows:
            if r.get("level") in ("WARN", "ERROR") and r.get("message") != "request completed":
                groups[(r["level"], r.get("logger_name", "").split(".")[-1], _shape(r.get("message", "")))].append(r)
        for (lvl, logger, shape), rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:12]:
            rid = next((r["requestId"] for r in rs if r.get("requestId")), "-")
            out.append(f"  {len(rs):4}x {lvl:5} {logger}: {shape}\n"
                       f"        first {_hms(rs[0]['_t'])} last {_hms(rs[-1]['_t'])} example requestId={rid}")
        return _clip("\n".join(out), 6000)

    def search_logs(self, request_id: str | None = None, text: str | None = None, level: str | None = None,
                    endpoint: str | None = None, start: str | None = None, end: str | None = None,
                    include_stack: bool = False, limit: int = 30, offset: int = 0) -> str:
        """Log lines matching all given filters, oldest first."""
        t0, t1 = self._window(start, end)
        needle = text.lower() if text else None
        hits = []
        for r in self.logs:
            if not (t0 <= r["_t"] <= t1):
                continue
            if request_id and r.get("requestId") != request_id:
                continue
            if level and r.get("level") != level.upper():
                continue
            if endpoint and endpoint not in (r.get("endpoint") or r.get("path") or ""):
                continue
            if needle and needle not in (r.get("message", "") + r.get("stack_trace", "")).lower():
                continue
            hits.append(r)
        limit, offset = max(1, min(int(limit), 60)), max(0, int(offset))
        shown = hits[offset:offset + limit]
        out = [f"{len(hits)} matching lines" + (f", showing {offset + 1}-{offset + len(shown)} (use offset for more)"
                                                if len(hits) > len(shown) else "")]
        for r in shown:
            extra = " ".join(f"{k}={r[k]}" for k in ("status", "latencyMs", "dbQueries") if k in r)
            out.append(f"{_hms(r['_t'], ms=True)} {r.get('level', ''):5} [{r.get('handler') or r.get('logger_name', '').split('.')[-1]}] "
                       f"{r.get('message', '')[:300]} {extra} rid={r.get('requestId', '-')}")
            if include_stack and r.get("stack_trace"):
                lines = r["stack_trace"].splitlines()
                keep = [l for l in lines if "com.example" in l or not l.strip().startswith("at ")][:8]
                out += ["      " + l.strip()[:200] for l in keep]
        return _clip("\n".join(out))

    def get_metrics(self, start: str | None = None, end: str | None = None) -> str:
        """Metrics every ~15 s: request rate, errors, latency per endpoint; DB pool; JVM heap and GC."""
        t0, t1 = self._window(start, end)
        snaps = [m for m in self.metrics if t0 <= _utc(m["ts"]) <= t1]
        out = ["time      up  req/s  5xx/s  avg_ms  pool_active  pool_pending  heap_MB  gc_ms/s   busiest endpoints (req/s, err%, avg ms)"]
        prev = None
        for i, m in enumerate(snaps):
            if not m.get("up"):
                t = _utc(m["ts"])
                restart = any(r.get("message", "").startswith("service started") and t <= r["_t"] <= t + timedelta(seconds=20)
                              for r in self.logs)
                out.append(f"{_hms(t)}  DOWN ({m.get('error')})" + ("  <- restart for a deploy" if restart else ""))
                continue
            if prev is None:
                prev = m
                continue
            if i % 3 and i != len(snaps) - 1:
                continue
            dt = (_utc(m["ts"]) - _utc(prev["ts"])).total_seconds() or 1
            cur, old = _http(m["values"]), _http(prev["values"])
            total = {k: _delta(cur[k], old.get(k, (0, 0))) for k in cur}
            reqs = sum(c for c, _ in total.values())
            err = sum(c for (uri, status), (c, _) in total.items() if status.startswith("5"))
            secs = sum(s for c, s in total.values())
            v = m["values"]
            heap = sum(val for k, val in v.items() if k.startswith("jvm_memory_used_bytes") and 'area="heap"' in k)
            gc = _delta_scalar(_sum(v, "jvm_gc_pause_seconds_sum"), _sum(prev["values"], "jvm_gc_pause_seconds_sum"))
            by_uri = defaultdict(lambda: [0, 0, 0.0])
            for (uri, status), (c, s) in total.items():
                by_uri[uri][0] += c
                by_uri[uri][1] += c if status.startswith("5") else 0
                by_uri[uri][2] += s
            busiest = sorted(by_uri.items(), key=lambda kv: -kv[1][0])[:4]
            eps = ", ".join(f"{u} {c / dt:.1f}/{100 * e / c if c else 0:.0f}%/{1000 * s / c if c else 0:.0f}"
                            for u, (c, e, s) in busiest)
            out.append(f"{_hms(_utc(m['ts']))}  up  {reqs / dt:5.1f}  {err / dt:5.2f}  {1000 * secs / reqs if reqs else 0:6.0f}  "
                       f"{_sum(v, 'hikaricp_connections_active'):11.0f}  {_sum(v, 'hikaricp_connections_pending'):12.0f}  "
                       f"{heap / 2**20:7.0f}  {1000 * gc / dt:7.1f}   {eps}")
            prev = m
        return _clip("\n".join(out), 6000)

    def list_commits(self) -> str:
        """The last 10 commits (newest first) with time and changed files."""
        return "\n".join(f"{c['sha'][:10]}  {c['time'].date()} {_hms(c['time'])} UTC  {c['subject']}  files: {_files(c['files'])}"
                         for c in self.commits)

    def show_diff(self, sha: str) -> str:
        """Full diff of one commit from the list."""
        match = next((c for c in self.commits if c["sha"].startswith(sha.strip())), None)
        if match is None or len(sha.strip()) < 6:
            return f"unknown commit {sha!r}; use a sha from list_commits"
        out = subprocess.run(["git", "show", "--stat", "-p", "--date=iso", match["sha"]], cwd=APP_REPO,
                             capture_output=True, text=True, encoding="utf-8")
        return _clip(out.stdout or out.stderr)

    def lookup_code(self, query: str) -> str:
        """Find code for an endpoint ('POST /api/orders'), a log message, or a class/method ('OrderService#placeOrder')."""
        q = re.sub(r"^(class|interface|method)\s+", "", query.strip())
        if "#" not in q and q not in self.index["classes"]:
            owners = [c for c, info in self.index["classes"].items() if q in info["methods"]]
            if len(owners) >= 1 and not q.startswith(("/", "GET", "POST", "PUT", "DELETE")):
                return "\n".join(self.lookup_code(f"{c}#{q}") for c in owners)
        out = []
        for ep in self.index["endpoints"]:
            if q == ep["http"] or q == ep["http"].split(" ", 1)[1] or ep["handler"] == q:
                out.append(f"{ep['http']} -> {ep['handler']} ({ep['file']}:{ep['line']})")
                out += _tree(ep["calls"], 1)
        for st in self.index["log_statements"]:
            pattern = re.escape(st["template"]).replace(r"\{\}", ".*?")
            if re.search(pattern, q) or (len(q) > 6 and q.lower() in st["template"].lower()):
                out.append(f'log.{st["level"].lower()}("{st["template"]}") in {st["class"]}#{st["method"]} '
                           f'({st["file"]}:{st["line"]})')
        cls_name, _, method = q.partition("#")
        cls = self.index["classes"].get(cls_name)
        if cls:
            methods = {method: cls["methods"][method]} if method in cls["methods"] else cls["methods"]
            out.append(f"{cls['kind']} {cls_name} ({cls['file']})")
            for name, m in methods.items():
                calls = ", ".join(c["target"] for c in m.get("calls", []))
                out.append(f"  {name}: lines {m['line']}-{m.get('end_line', m['line'])}"
                           + (f"  annotations {m['annotations']}" if m.get("annotations") else "")
                           + (f"  calls {calls}" if calls else ""))
        return _clip("\n".join(out)) if out else f"nothing found for {q!r}; try an endpoint like 'POST /api/orders', a log message, or Class#method"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Source lines (numbered) as of the incident's code commit."""
        path = path.replace("\\", "/").lstrip("/")
        if ".." in path or not path.startswith(("src/", "pom.xml")):
            return "path must be inside the app source, e.g. src/main/java/..."
        out = subprocess.run(["git", "show", f"{self.code_commit}:{path}"], cwd=APP_REPO,
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            return f"cannot read {path}: {out.stderr.strip()}"
        lines = out.stdout.splitlines()
        start = max(1, int(start_line))
        stop = min(len(lines), int(end_line) if end_line else start + 119, start + 199)
        return "\n".join(f"{i:4}  {lines[i - 1]}" for i in range(start, stop + 1))


def _http(values: dict) -> dict:
    """(uri, status) -> (count, seconds) from Micrometer's http_server_requests_seconds."""
    acc = defaultdict(lambda: [0.0, 0.0])
    for key, val in values.items():
        name, labels = _metric(key)
        if name not in ("http_server_requests_seconds_count", "http_server_requests_seconds_sum"):
            continue
        uri = labels.get("uri", "")
        if uri.startswith("/actuator"):
            continue
        acc[(uri, labels.get("status", ""))][0 if name.endswith("_count") else 1] += val
    return {k: tuple(v) for k, v in acc.items()}


def _delta(cur: tuple, old: tuple) -> tuple:
    c, s = cur[0] - old[0], cur[1] - old[1]
    return cur if c < 0 else (c, s)  # counter reset after a restart


def _delta_scalar(cur: float, old: float) -> float:
    return cur if cur < old else cur - old


def _sum(values: dict, prefix: str) -> float:
    return sum(v for k, v in values.items() if k.split("{")[0] == prefix)


def _tree(calls: list, depth: int) -> list[str]:
    out = []
    for c in calls:
        where = f"{c['file'].split('/')[-1]}:{c['line']}" if c.get("line") else c.get("note", "")
        out.append("  " * depth + f"-> {c['call']} ({where}), called at line {c.get('called_at_line')}")
        out += _tree(c.get("calls", []), depth + 1)
    return out


def _files(files: list[str]) -> str:
    shown = ', '.join(files[:5])
    return shown + (f' (+{len(files) - 5} more)' if len(files) > 5 else '')
