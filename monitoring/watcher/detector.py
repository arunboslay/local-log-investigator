"""The cheap anomaly detector: plain code that reads log lines one at a time. No model, no GPU.

Checks:
- new_error_pattern: a WARN/ERROR message shape not seen before (numbers become N, personal data masked)
- missing_step: a 2xx request that skipped a step the code should have logged (silent failures)
- error_rate: an endpoint's share of 5xx responses jumped
- latency: an endpoint's median response time jumped
- query_count: an endpoint's median database queries per request jumped
- traffic: the overall request rate jumped

It uses the timestamps in the logs, not the clock, so replaying a recording gives the same result.
It can only see what is in the logs: a memory leak or a dead app that logs nothing gets past it.
"""

import re
import statistics
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redact import redact_text  # noqa: E402

NUMBERS = re.compile(r"\b[0-9a-f]{8,}\b|\d+(?:\.\d+)?")
VALUES = re.compile(r"\b(\w+)=(?!<)[^\s,:;]+")  # city=Pune -> city=*, but keep masked values like email=<EMAIL>
APP_READY = re.compile(r"^(service started|Started \w+ in )")
APP_STARTING = re.compile(r"^Starting \w+")
COMMIT = re.compile(r"commit=(\w+)")


@dataclass
class Settings:
    window_s: float = 30  # recent samples are compared with older, healthy ones
    warmup_s: float = 15  # the JVM is slow right after a start, so don't judge latency yet
    cooldown_s: float = 60  # repeat an alert for the same thing at most this often
    min_window: int = 5
    min_baseline: int = 10
    latency_factor: float = 3
    latency_min_rise_ms: float = 25
    query_factor: float = 3
    query_min_rise: float = 5
    error_min_count: int = 3
    error_min_share: float = 0.10
    traffic_factor: float = 3
    traffic_bucket_s: float = 15
    traffic_min_buckets: int = 2


@dataclass
class Alert:
    ts: str
    check: str
    summary: str
    endpoint: str | None = None
    request_id: str | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def step_name(message: str) -> str:
    """'signup validated name=x email=y' -> 'signup validated'."""
    words = []
    for word in message.split():
        if "=" in word or word.endswith(":"):
            break
        words.append(word)
    return " ".join(words)


def shape(message: str) -> str:
    """Group similar messages: mask personal data, drop key=value values, numbers and ids become N."""
    masked = VALUES.sub(r"\1=*", redact_text(message.split("\n")[0]))
    return NUMBERS.sub("N", masked)[:170]


def parse_time(row: dict) -> datetime:
    return datetime.fromisoformat(row["@timestamp"]).astimezone(timezone.utc)


def _is_step(row: dict) -> bool:
    logger = row.get("logger_name", "")
    return row.get("level") == "INFO" and logger.startswith("com.example.shop") and not logger.startswith("com.example.shop.web")


class _Endpoint:
    def __init__(self):
        self.window = deque()  # (time, latency, queries, failed, judged)
        self.latency_base = deque(maxlen=500)
        self.query_base = deque(maxlen=500)


class Detector:
    def __init__(self, steps_for_commit: Callable[[str], dict[str, list[str]]] | None = None,
                 settings: Settings | None = None):
        self.steps_for_commit = steps_for_commit
        self.s = settings or Settings()
        self.known_patterns: set[str] = set()
        self.open: dict[str, list[dict]] = {}
        self.endpoints = defaultdict(_Endpoint)
        self.expected: dict[str, list[str]] = {}
        self.starting = False
        self.started_at: datetime | None = None
        self.last_alert: dict[tuple, datetime] = {}
        self.bucket_start: datetime | None = None
        self.bucket_count = 0
        self.rates = deque(maxlen=20)
        self.lines = 0

    def feed(self, row: dict) -> list[Alert]:
        """Take one parsed JSON log line; return any alerts it causes."""
        if "@timestamp" not in row:
            return []
        self.lines += 1
        t = parse_time(row)
        message = row.get("message", "")
        alerts = []
        if APP_STARTING.match(message):
            self.starting = True
        elif APP_READY.match(message):
            self.starting = False
            self.started_at = t
            found = COMMIT.search(message)
            if found and self.steps_for_commit:
                self.expected = self.steps_for_commit(found.group(1))
        alerts += self._check_pattern(row, t, message)
        request_id = row.get("requestId")
        if request_id:
            self.open.setdefault(request_id, []).append(row)
            if message == "request completed":
                alerts += self._finish_request(self.open.pop(request_id), row, t)
        return alerts

    def _alert(self, key: tuple, t: datetime, **fields) -> list[Alert]:
        last = self.last_alert.get(key)
        if last and (t - last).total_seconds() < self.s.cooldown_s:
            return []
        self.last_alert[key] = t
        return [Alert(ts=t.isoformat(timespec="milliseconds"), **fields)]

    def _check_pattern(self, row: dict, t: datetime, message: str) -> list[Alert]:
        if row.get("level") not in ("WARN", "ERROR") or message == "request completed" or self.starting:
            return []
        if row.get("httpMethod") and not row.get("endpoint"):
            return []  # a URL the app doesn't have: scanners and local tools, not our code failing
        pattern = shape(message)
        if pattern in self.known_patterns:
            return []
        self.known_patterns.add(pattern)
        return [Alert(ts=t.isoformat(timespec="milliseconds"), check="new_error_pattern", summary=pattern,
                      endpoint=_endpoint_of(row), request_id=row.get("requestId"), evidence=[pattern])]

    def _finish_request(self, rows: list[dict], done: dict, t: datetime) -> list[Alert]:
        http = _endpoint_of(done)
        alerts = self._check_traffic(t)
        if not http:
            return alerts
        status = int(done.get("status", 0))
        latency, queries = float(done.get("latencyMs", 0)), float(done.get("dbQueries", 0))
        alerts += self._check_missing_step(http, rows, status, done, t)
        ep = self.endpoints[http]
        warm = self.started_at is None or (t - self.started_at).total_seconds() >= self.s.warmup_s
        ep.window.append((t, latency, queries, status >= 500, warm))
        while ep.window and (t - ep.window[0][0]).total_seconds() > self.s.window_s:
            old = ep.window.popleft()
            # Only healthy samples teach what normal looks like.
            if old[4] and not old[3] and not self._alerting(http, t):
                ep.latency_base.append(old[1])
                ep.query_base.append(old[2])
        return alerts + self._check_errors(http, ep, t) + self._check_speed(http, ep, t)

    def _alerting(self, http: str, t: datetime) -> bool:
        return any((check, http) in self.last_alert and (t - self.last_alert[(check, http)]).total_seconds() < self.s.cooldown_s
                   for check in ("latency", "query_count", "error_rate"))

    def _check_missing_step(self, http, rows, status, done, t) -> list[Alert]:
        if not 200 <= status < 300:
            return []
        seen = {step_name(r["message"]) for r in rows if _is_step(r)}
        missing = [s for s in self.expected.get(http, []) if s not in seen]
        if not missing:
            return []
        steps = ", ".join(s for s in self.expected[http] if s in seen) or "none"
        return self._alert(("missing_step", http, missing[0]), t, check="missing_step", endpoint=http,
                           request_id=done.get("requestId"),
                           summary=f"{http} returned {status} but step '{missing[0]}' never happened (steps seen: {steps})",
                           evidence=[redact_text(r["message"].split("\n")[0])[:200] for r in rows
                                     if r.get("level") in ("WARN", "ERROR")][:3])

    def _check_errors(self, http, ep, t) -> list[Alert]:
        failed = sum(1 for sample in ep.window if sample[3])
        if failed < self.s.error_min_count or failed / len(ep.window) < self.s.error_min_share:
            return []
        return self._alert(("error_rate", http), t, check="error_rate", endpoint=http,
                           summary=f"{http}: {failed} of {len(ep.window)} requests failed in the last {self.s.window_s:.0f} s")

    def _check_speed(self, http, ep, t) -> list[Alert]:
        recent = [sample for sample in ep.window if sample[4]]
        if len(recent) < self.s.min_window or len(ep.latency_base) < self.s.min_baseline:
            return []
        alerts = []
        # p95 (the slowest 5%) reacts sooner than the median when only new requests are slow.
        now_ms = _p95([sample[1] for sample in recent])
        base_ms = _p95(ep.latency_base)
        if now_ms >= max(self.s.latency_factor * base_ms, base_ms + self.s.latency_min_rise_ms):
            alerts += self._alert(("latency", http), t, check="latency", endpoint=http,
                                  summary=f"{http}: p95 {now_ms:.0f} ms, normally {base_ms:.0f} ms")
        now_q = _p95([sample[2] for sample in recent])
        base_q = _p95(ep.query_base)
        if now_q >= max(self.s.query_factor * base_q, base_q + self.s.query_min_rise):
            alerts += self._alert(("query_count", http), t, check="query_count", endpoint=http,
                                  summary=f"{http}: p95 {now_q:.0f} database queries per request, normally {base_q:.0f}")
        return alerts

    def _check_traffic(self, t) -> list[Alert]:
        if self.bucket_start is None:
            self.bucket_start = t
        self.bucket_count += 1
        age = (t - self.bucket_start).total_seconds()
        if age < self.s.traffic_bucket_s:
            return []
        rate = self.bucket_count / age
        self.bucket_start, self.bucket_count = t, 0
        alerts = []
        if len(self.rates) >= self.s.traffic_min_buckets:
            normal = statistics.median(self.rates)
            if rate >= self.s.traffic_factor * normal:
                alerts = self._alert(("traffic",), t, check="traffic",
                                     summary=f"{rate:.0f} requests per second, normally {normal:.0f}")
                return alerts  # a spike doesn't count as the new normal
        self.rates.append(rate)
        return alerts


def _p95(values) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def _endpoint_of(row: dict) -> str | None:
    if row.get("httpMethod") and row.get("endpoint"):
        return f"{row['httpMethod']} {row['endpoint']}"
    return None


def expected_steps(index: dict) -> dict[str, list[str]]:
    """For each endpoint in a code map, the INFO steps its code path logs, in code order."""
    classes = index["classes"]

    def visit(cls: str, method: str, depth: int, steps: list[str]):
        m = classes.get(cls, {}).get("methods", {}).get(method)
        if not m or depth > 4:
            return
        events = [(l["line"], "log", l) for l in m.get("logs", []) if l["level"] == "INFO"]
        events += [(c["line"], "call", c) for c in m.get("calls", [])]
        for _, kind, item in sorted(events, key=lambda e: e[0]):
            if kind == "log":
                steps.append(step_name(item["template"]))
            else:
                visit(*item["target"].split("#"), depth + 1, steps)

    result = {}
    for ep in index["endpoints"]:
        steps: list[str] = []
        visit(*ep["handler"].split("#"), 0, steps)
        result[ep["http"]] = steps
    return result
