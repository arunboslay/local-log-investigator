"""Build a map of the Spring source, so an investigator can go from a log line to the code.

Usage:
    uv run python monitoring/code_index.py [--src monitoring/demo_app] [--out monitoring/code_index.json]

The index has:
- endpoints: HTTP method + path -> controller method (file, line), with the tree of service and
  repository methods it calls
- classes: every class and method with file and line range, and the calls each method makes
- log_statements: every log.info/warn/error template with its class, method, file and line, so a log
  message like "order saved orderId=..." can be traced to the exact line that wrote it

It is a plain-text scan (regular expressions plus brace matching), not a full Java parser.
That is enough for typical Spring code; unusual syntax may be missed.
"""

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "try", "synchronized",
            "super", "this"}
MODIFIERS = r"(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*"
METHOD_START = re.compile(r"(?m)^[ \t]+" + MODIFIERS + r"(?:<[^>]+>\s+)?([\w.<>\[\],? ]+?)\s+(\w+)\s*\(")
CONSTRUCTOR_START = re.compile(r"(?m)^[ \t]+(?:public|protected|private)\s+(\w+)\s*\(")
FIELD = re.compile(r"(?m)^[ \t]+(?:(?:private|protected|public|static|final)\s+)+([\w.]+)(?:<[^;=]*>)?\s+(\w+)\s*[;=]")
TYPE_DECL = re.compile(r"\b(class|interface|record|enum)\s+(\w+)")
MAPPING = re.compile(r'@(Get|Post|Put|Delete|Patch|Request)Mapping(?:\(([^)]*)\))?')
CALL_ON_FIELD = re.compile(r"\b(\w+)\.(\w+)\s*\(")
SELF_CALL = re.compile(r"(?<![\w.])(\w+)\s*\(")
LOG_CALL = re.compile(r'\blog\.(trace|debug|info|warn|error)\(\s*"((?:[^"\\]|\\.)*)"')
JPA_INHERITED = {"findById", "findAll", "save", "saveAndFlush", "saveAll", "delete", "deleteById", "count",
                 "existsById", "flush", "getReferenceById"}


def depth_map(text: str) -> list[int]:
    """Brace depth at every character, ignoring braces inside strings, text blocks and comments."""
    depths, depth, i, n = [0] * (len(text) + 1), 0, 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
        elif text[i] in "\"'":
            quote, j = text[i], i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            j += 1
        else:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            depths[i + 1] = depth
            i += 1
            continue
        for k in range(i, min(j, n)):
            depths[k + 1] = depth
        i = j
    return depths


def matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def matching_brace(depths: list[int], open_idx: int) -> int:
    target = depths[open_idx]  # depth just before the opening brace
    for i in range(open_idx + 1, len(depths) - 1):
        if depths[i + 1] == target:
            return i
    return len(depths) - 2


def line_of(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


def annotations_before(text: str, idx: int) -> list[str]:
    """Annotation lines directly above a declaration."""
    lines = text[:idx].rstrip().split("\n")
    found = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("@") or (found and stripped and not stripped.endswith((";", "{", "}")) and
                                        not stripped.startswith("//")):
            found.append(stripped)
        else:
            break
    return list(reversed(found))


def mapping_of(annotations: list[str]) -> tuple[str, str] | None:
    for ann in annotations:
        m = MAPPING.search(ann)
        if not m:
            continue
        kind, args = m.group(1), m.group(2) or ""
        path = re.search(r'"([^"]*)"', args)
        method = kind.upper() if kind != "Request" else (re.search(r"RequestMethod\.(\w+)", args) or [None, "ANY"])[1]
        return method, path.group(1) if path else ""
    return None


def scan_file(path: Path, src_root: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    depths = depth_map(text)
    decl = TYPE_DECL.search(text)
    if not decl:
        return None
    kind, name = decl.groups()
    rel = path.relative_to(src_root).as_posix()
    class_ann = annotations_before(text, text.rfind("\n", 0, decl.start()) + 1)
    base = mapping_of(class_ann)
    cls = {"name": name, "kind": kind, "file": rel, "line": line_of(text, decl.start()),
           "annotations": [a for a in class_ann if a.startswith("@")],
           "base_path": base[1] if base else "", "fields": {}, "methods": {}}

    for m in FIELD.finditer(text):
        if depths[m.start()] == 1:
            cls["fields"][m.group(2)] = m.group(1).split(".")[-1]

    starts = [(m, m.group(2)) for m in METHOD_START.finditer(text)]
    starts += [(m, m.group(1)) for m in CONSTRUCTOR_START.finditer(text) if m.group(1) == name]
    for m, method_name in starts:
        if method_name in KEYWORDS or depths[m.start()] != 1:
            continue
        close = matching_paren(text, m.end() - 1)
        if close < 0:
            continue
        rest = text[close + 1:close + 200]
        after = re.match(r"\s*(?:throws\s+[\w., ]+)?\s*([{;])", rest)
        if not after:
            continue
        ann = annotations_before(text, text.rfind("\n", 0, m.start()) + 1)
        entry = {"line": line_of(text, m.start()), "annotations": [a for a in ann if a.startswith("@")],
                 "signature": " ".join(text[m.start():close + 1].split())}
        if after.group(1) == "{":
            open_idx = close + 1 + after.start(1)
            end = matching_brace(depths, open_idx)
            body = text[open_idx:end + 1]
            entry["end_line"] = line_of(text, end)
            entry["_body"], entry["_body_offset"] = body, open_idx
        else:
            entry["end_line"] = entry["line"]
        mapping = mapping_of(ann)
        if mapping:
            entry["http"] = f"{mapping[0]} {(cls['base_path'] + mapping[1]) or '/'}"
        cls["methods"][method_name] = entry

    method_names = set(cls["methods"])
    for method_name, entry in cls["methods"].items():
        body = entry.pop("_body", "")
        offset = entry.pop("_body_offset", 0)
        calls, logs = [], []
        for c in CALL_ON_FIELD.finditer(body):
            receiver, called = c.groups()
            if receiver in cls["fields"] and cls["fields"][receiver] != "Logger":  # logs are listed separately
                calls.append({"target": f"{cls['fields'][receiver]}#{called}", "line": line_of(text, offset + c.start())})
        for c in SELF_CALL.finditer(body):
            called = c.group(1)
            if called in method_names and called != method_name:
                calls.append({"target": f"{name}#{called}", "line": line_of(text, offset + c.start())})
        for lg in LOG_CALL.finditer(body):
            logs.append({"level": lg.group(1).upper(), "template": lg.group(2), "line": line_of(text, offset + lg.start())})
        entry["calls"] = sorted(calls, key=lambda c: c["line"])
        entry["logs"] = logs
    return cls


def call_tree(classes: dict, target: str, depth: int = 0, seen: frozenset = frozenset()) -> dict:
    cls_name, method = target.split("#")
    node = {"call": target}
    cls = classes.get(cls_name)
    if cls is None:
        return node
    entry = cls["methods"].get(method)
    if entry is None:
        if cls["kind"] == "interface" and method in JPA_INHERITED:
            node["note"] = "inherited from Spring Data JpaRepository"
        node["file"] = cls["file"]
        return node
    node.update(file=cls["file"], line=entry["line"])
    if entry["annotations"]:
        node["annotations"] = entry["annotations"]
    if depth < 4 and target not in seen:
        children = [call_tree(classes, c["target"], depth + 1, seen | {target}) | {"called_at_line": c["line"]}
                    for c in entry["calls"]]
        if children:
            node["calls"] = children
    return node


def build_index(app_dir: Path) -> dict:
    src_root = app_dir
    classes = {}
    for path in sorted((app_dir / "src" / "main" / "java").rglob("*.java")):
        cls = scan_file(path, src_root)
        if cls:
            classes[cls["name"]] = cls
    endpoints, log_statements = [], []
    for cls in classes.values():
        for method, entry in cls["methods"].items():
            if "http" in entry:
                endpoints.append({"http": entry["http"], "handler": f"{cls['name']}#{method}", "file": cls["file"],
                                  "line": entry["line"], "calls": call_tree(classes, f"{cls['name']}#{method}").get("calls", [])})
            for lg in entry["logs"]:
                log_statements.append({**lg, "class": cls["name"], "method": method, "file": cls["file"]})
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=app_dir, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = None
    return {"commit": commit, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoints": sorted(endpoints, key=lambda e: e["http"]), "log_statements": log_statements,
            "classes": classes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=ROOT / "demo_app")
    parser.add_argument("--out", type=Path, default=ROOT / "code_index.json")
    args = parser.parse_args()
    index = build_index(args.src)
    args.out.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"{len(index['endpoints'])} endpoints, {len(index['classes'])} classes, "
          f"{len(index['log_statements'])} log statements -> {args.out}")


if __name__ == "__main__":
    main()
