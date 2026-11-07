import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from watcher.detector import Detector, expected_steps, shape, step_name  # noqa: E402

START = datetime(2026, 11, 7, 0, 0, 0, tzinfo=timezone.utc)
SIGNUP = "POST /api/customers"
STEPS = {SIGNUP: ["signup validated", "customer saved", "signup completed"]}
SERVICE = "com.example.shop.customer.CustomerService"


def row(seconds, message, level="INFO", logger=SERVICE, **extra):
    return {"@timestamp": (START + timedelta(seconds=seconds)).isoformat(), "message": message, "level": level,
            "logger_name": logger, **extra}


def request(seconds, rid, steps, status=201, latency=5, queries=1, endpoint="/api/customers", method="POST", warn=None):
    common = {"requestId": rid, "httpMethod": method, "endpoint": endpoint}
    rows = [row(seconds, s + " id=1", **common) for s in steps]
    if warn:
        rows.append(row(seconds, warn, level="WARN", **common))
    rows.append(row(seconds, "request completed", logger="com.example.shop.web.RequestLoggingFilter",
                    level="ERROR" if status >= 500 else "INFO", status=str(status), latencyMs=str(latency),
                    dbQueries=str(queries), **common))
    return rows


def run(detector, rows):
    alerts = []
    for r in rows:
        alerts += detector.feed(r)
    return alerts


def started_detector(**kwargs):
    d = Detector(steps_for_commit=lambda commit: STEPS, **kwargs)
    d.feed(row(-100, "service started commit=abc123 pid=1"))
    return d


def test_normal_requests_raise_nothing():
    d = started_detector()
    rows = [r for i in range(50) for r in request(i, f"r{i}", STEPS[SIGNUP])]
    assert run(d, rows) == []


def test_silent_failure_is_caught_even_with_201():
    d = started_detector()
    alerts = run(d, request(1, "r1", ["signup validated", "signup completed"], status=201))
    assert [a.check for a in alerts] == ["missing_step"]
    assert "customer saved" in alerts[0].summary and alerts[0].request_id == "r1"


def test_failed_requests_are_not_reported_as_missing_steps():
    d = started_detector()
    assert not [a for a in run(d, request(1, "r1", ["signup validated"], status=500)) if a.check == "missing_step"]


def test_new_error_pattern_fires_once_and_masks_personal_data():
    d = started_detector()
    first = run(d, request(1, "r1", STEPS[SIGNUP], warn="could not save email=priya.nair@example.com: too long (19)"))
    again = run(d, request(2, "r2", STEPS[SIGNUP], warn="could not save email=kavya.rao@example.com: too long (21)"))
    assert [a.check for a in first] == ["new_error_pattern"]
    assert again == []
    assert "example.com" not in first[0].summary and "<EMAIL>" in first[0].summary


def test_warnings_during_startup_are_ignored():
    d = Detector()
    d.feed(row(0, "Starting ShopApplication using Java 21"))
    assert d.feed(row(1, "open-in-view is enabled", level="WARN")) == []
    d.feed(row(2, "service started commit=abc pid=1"))
    assert [a.check for a in d.feed(row(3, "something new broke", level="WARN"))] == ["new_error_pattern"]


def test_error_rate_jump():
    d = started_detector()
    rows = [r for i in range(4) for r in request(i, f"r{i}", ["signup validated"], status=500)]
    assert "error_rate" in [a.check for a in run(d, rows)]


def test_latency_jump_after_a_healthy_baseline():
    d = started_detector()
    healthy = [r for i in range(60) for r in request(i, f"h{i}", STEPS[SIGNUP], latency=5)]
    slow = [r for i in range(60, 70) for r in request(i, f"s{i}", STEPS[SIGNUP], latency=80)]
    assert run(d, healthy) == []
    assert "latency" in [a.check for a in run(d, slow)]


def test_small_latency_rise_is_not_an_alert():
    d = started_detector()
    healthy = [r for i in range(60) for r in request(i, f"h{i}", STEPS[SIGNUP], latency=5)]
    slightly = [r for i in range(60, 70) for r in request(i, f"s{i}", STEPS[SIGNUP], latency=20)]
    run(d, healthy)
    assert run(d, slightly) == []


def test_slow_requests_right_after_a_restart_are_ignored():
    d = started_detector()
    run(d, [r for i in range(60) for r in request(i, f"h{i}", STEPS[SIGNUP], latency=5)])
    d.feed(row(61, "service started commit=abc123 pid=2"))
    warming = [r for i in range(62, 70) for r in request(i, f"w{i}", STEPS[SIGNUP], latency=300)]
    assert run(d, warming) == []


def test_query_count_jump():
    d = started_detector()
    run(d, [r for i in range(60) for r in request(i, f"h{i}", STEPS[SIGNUP], queries=1)])
    many = [r for i in range(60, 70) for r in request(i, f"n{i}", STEPS[SIGNUP], queries=51)]
    assert "query_count" in [a.check for a in run(d, many)]


def test_helpers():
    assert step_name("signup validated name=x email=y") == "signup validated"
    assert shape("took 512 ms for id deadbeef99") == "took N ms for id N"
    index = {"endpoints": [{"http": SIGNUP, "handler": "C#a"}],
             "classes": {"C": {"methods": {"a": {"logs": [{"level": "INFO", "line": 9, "template": "last step"}],
                                                 "calls": [{"line": 5, "target": "S#b"}]}}},
                         "S": {"methods": {"b": {"logs": [{"level": "INFO", "line": 2, "template": "first step id={}"},
                                                          {"level": "WARN", "line": 3, "template": "not a step"}],
                                                 "calls": []}}}}}
    assert expected_steps(index) == {SIGNUP: ["first step", "last step"]}
