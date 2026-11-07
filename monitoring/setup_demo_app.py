"""One-time setup: create monitoring/demo_app from the template, as its own git repo, and build it.

The fault injector makes real commits, so the demo app must be a separate repo inside this one
(it is ignored by this repo's .gitignore). Needs JDK 21 and network access for the first Maven build.

Usage: uv run python monitoring/setup_demo_app.py
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "demo_app"

if APP.exists():
    raise SystemExit(f"{APP} already exists; delete it first to start again")
shutil.copytree(ROOT / "demo_app_template", APP)
for args in (["init", "-q", "-b", "main"], ["config", "core.autocrlf", "false"], ["config", "user.name", "Shop Dev"],
             ["config", "user.email", "dev@shop.example"], ["add", "-A"], ["commit", "-q", "-m", "Initial shop service"]):
    subprocess.run(["git", *args], cwd=APP, check=True)
subprocess.run(["cmd", "/c", r".\mvnw.cmd", "-q", "-B", "package", "-DskipTests"], cwd=APP, check=True)
print(f"ready: {APP}")
