"""Shared helpers for running the demo shop: build, deploy, start and stop processes, make commits.

Processes (all local):
    h2       database server on port 9092 (separate process, so it can be taken down)
    payment  fake outside payment service on port 8101 (fake_payment.py)
    app      the Spring Boot shop on port 8100
    load     background traffic (load.py)
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "demo_app"
RUN_DIR = ROOT / "run"
DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"
APP_URL = "http://127.0.0.1:8100"
BASE_RPS = 10
JAVA = shutil.which("java") or "java"


def retry(fn, attempts: int = 50, delay: float = 0.1):
    """Windows won't rename or delete a file another process has open; try again for a few seconds."""
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def h2_jar() -> Path:
    jars = sorted((Path.home() / ".m2" / "repository" / "com" / "h2database" / "h2").glob("*/h2-*.jar"))
    if not jars:
        raise RuntimeError("H2 jar not found; build demo_app once with mvnw.cmd package")
    return jars[-1]


class Lab:
    def __init__(self):
        # Faults are real commits, so they must go to the demo app's own repo, never this one.
        if not (APP_DIR / ".git").exists():
            raise RuntimeError("monitoring/demo_app is not set up; run: uv run python monitoring/setup_demo_app.py")
        RUN_DIR.mkdir(exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}

    # ---- git in the demo app repo ----

    def git(self, *args: str) -> str:
        out = subprocess.run(["git", *args], cwd=APP_DIR, check=True, capture_output=True, text=True, encoding="utf-8")
        return out.stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def ensure_clean(self):
        if self.git("status", "--porcelain"):
            raise RuntimeError(f"{APP_DIR} has uncommitted changes; commit or discard them first")

    def commit_edits(self, edits: list[tuple[str, str, str]], message: str) -> str:
        """Apply (file, old, new) text edits and commit them. Each `old` must match exactly once."""
        for file, old, new in edits:
            path = APP_DIR / file
            # Bytes, not text mode: on Windows text mode rewrites every line ending, and the
            # commit would then look like the whole file changed.
            text = path.read_bytes().decode("utf-8")
            if text.count(old) != 1:
                raise RuntimeError(f"expected exactly one match in {file} for:\n{old}")
            path.write_bytes(text.replace(old, new).encode("utf-8"))
        self.git("commit", "-q", "-am", message)
        return self.head()

    def revert_commit(self, sha: str):
        self.git("revert", "--no-edit", sha)

    # ---- processes ----

    def start(self, name: str, args: list, cwd: Path = ROOT, env: dict | None = None):
        out = open(RUN_DIR / f"{name}.out", "a", encoding="utf-8")
        self.procs[name] = subprocess.Popen(
            [str(a) for a in args], cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )

    def stop(self, name: str):
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=15)

    def start_db(self):
        self.start("h2", [JAVA, "-cp", h2_jar(), "org.h2.tools.Server", "-tcp", "-tcpPort", "9092",
                          "-ifNotExists", "-baseDir", DATA_DIR])
        time.sleep(1.5)

    def reset_data(self):
        """Fresh database for each run; the app seeds it on startup."""
        DATA_DIR.mkdir(exist_ok=True)
        for f in DATA_DIR.glob("shop*.db"):
            retry(f.unlink)
        for jar in (RUN_DIR / "deploys").glob("*.jar"):
            try:
                jar.unlink()
            except PermissionError:
                pass  # still held by a process from an earlier run

    def build(self):
        result = subprocess.run(["cmd", "/c", r".\mvnw.cmd", "-q", "-B", "-o", "package", "-DskipTests"],
                                cwd=APP_DIR, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"build failed:\n{result.stdout[-3000:]}")

    def start_app(self):
        # Run a copy of the jar: Windows locks a running jar, and the next build must replace target/shop.jar.
        sha = self.head()
        deploy_dir = RUN_DIR / "deploys"
        deploy_dir.mkdir(exist_ok=True)
        jar = deploy_dir / f"shop-{sha[:8]}-{int(time.time())}.jar"
        shutil.copyfile(APP_DIR / "target" / "shop.jar", jar)
        self.start("app", [JAVA, "-Xmx256m", "-jar", jar, f"--app.commit={sha}"], cwd=APP_DIR)
        if not self.wait_healthy(timeout_s=90):
            raise RuntimeError("demo app did not become healthy; see monitoring/run/app.out")

    def deploy(self):
        """Build the current commit, then restart the app on it, like a deploy."""
        self.build()
        self.stop("app")
        self.start_app()

    def start_payment(self):
        self.start("payment", [sys.executable, ROOT / "fake_payment.py"])

    def start_load(self, record_to: Path | None = None):
        self.start("load", [sys.executable, ROOT / "load.py"],
                   env={"LOAD_RECORD": str(record_to)} if record_to else None)

    def query(self, sql: str) -> list[list[str]]:
        """Run SQL against the shop database with H2's command-line shell; rows as lists of strings."""
        out = subprocess.run([JAVA, "-cp", str(h2_jar()), "org.h2.tools.Shell", "-url", "jdbc:h2:tcp://localhost:9092/shop",
                              "-user", "sa", "-password", "", "-sql", sql], capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            raise RuntimeError(f"query failed: {out.stderr or out.stdout}")
        lines = [l for l in out.stdout.splitlines() if " | " in l or (l.strip() and not l.startswith("("))]
        return [[c.strip() for c in l.split(" | ")] for l in lines[1:]]  # first line is the header

    def write_control(self, name: str, data: dict):
        (RUN_DIR / f"{name}_control.json").write_text(json.dumps(data))

    def wait_healthy(self, timeout_s: float = 30) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if httpx.get(f"{APP_URL}/actuator/health", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def shutdown(self):
        for name in ["load", "app", "payment", "lock", "h2"] + list(self.procs):
            self.stop(name)
