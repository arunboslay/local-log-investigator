"""Score an investigator report against the incident's truth.json.

Checks:
- category: same category as the truth
- commit: for code/config faults, the reported guilty commit matches; for other faults, no commit is blamed
- file: for code/config faults, the reported file is one of the guilty files
- top1: category, commit and file all right (the overall "got it" score)
"""

import json
from pathlib import Path


def _same_sha(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.strip().lower(), b.strip().lower()
    return len(a) >= 6 and (a.startswith(b[:len(a)]) or b.startswith(a))


def score(report: dict | None, truth: dict) -> dict:
    if not report:
        return {"category": False, "commit": False, "file": False, "top1": False, "note": "no report"}
    code_fault = truth["guilty_commit"] is not None
    category = report.get("category") == truth["category"]
    reported_commit = report.get("guilty_commit") or None
    if isinstance(reported_commit, str) and reported_commit.lower() in ("null", "none", ""):
        reported_commit = None
    if code_fault:
        commit = _same_sha(reported_commit, truth["guilty_commit"])
        reported_file = (report.get("guilty_file") or "").replace("\\", "/")
        file = bool(reported_file) and any(reported_file.endswith(f) or f.endswith(reported_file.split("/")[-1])
                                           for f in truth["guilty_files"])
    else:
        commit = reported_commit is None
        file = not report.get("guilty_file")
    return {"category": category, "commit": commit, "file": file, "top1": category and commit and file}


def score_incident(incident_dir: Path, report: dict | None) -> dict:
    truth = json.loads((Path(incident_dir) / "truth.json").read_text(encoding="utf-8"))
    return {"fault_id": truth["fault_id"], **score(report, truth)}
