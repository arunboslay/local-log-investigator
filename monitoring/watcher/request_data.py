"""Turn log lines into one record per finished request, for the anomaly models."""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from watcher.detector import parse_time, shape


@dataclass
class RequestRecord:
    t: datetime
    request_id: str
    endpoint: str
    status: int
    latency_ms: float
    db_queries: float
    templates: list[str]  # the message shapes in order, without "request received/completed"
    warns: int
    errors: int


class RequestGrouper:
    """Feed log lines in order; get a RequestRecord back when a request finishes."""

    def __init__(self):
        self.open: dict[str, list[dict]] = {}

    def feed(self, row: dict) -> RequestRecord | None:
        request_id = row.get("requestId")
        if not request_id:
            return None
        rows = self.open.setdefault(request_id, [])
        rows.append(row)
        if row.get("message") != "request completed":
            return None
        del self.open[request_id]
        if not row.get("endpoint"):
            return None  # a URL the app doesn't have
        levels = [r.get("level") for r in rows if r.get("message") != "request completed"]
        return RequestRecord(
            t=parse_time(row), request_id=request_id, endpoint=f"{row.get('httpMethod')} {row['endpoint']}",
            status=int(row.get("status", 0)), latency_ms=float(row.get("latencyMs", 0)), db_queries=float(row.get("dbQueries", 0)),
            templates=[shape(r.get("message", "")) for r in rows if r.get("message") not in ("request received", "request completed")],
            warns=levels.count("WARN"), errors=levels.count("ERROR"))


def read_rows(folder: Path):
    rows = []
    for line in (Path(folder) / "logs.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "@timestamp" in row:
            rows.append(row)
    return sorted(rows, key=parse_time)


def read_requests(folder: Path) -> list[RequestRecord]:
    grouper = RequestGrouper()
    return [rec for rec in map(grouper.feed, read_rows(folder)) if rec]
