"""Expected steps per endpoint at any commit, read from the app's git history."""

import io
import sys
import subprocess
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from code_index import build_index  # noqa: E402
from watcher.detector import expected_steps  # noqa: E402


class StepMaps:
    def __init__(self, repo: Path):
        self.repo, self.cache = Path(repo), {}

    def __call__(self, commit: str) -> dict[str, list[str]]:
        if commit not in self.cache:
            self.cache[commit] = self._build(commit)
        return self.cache[commit]

    def _build(self, commit: str) -> dict[str, list[str]]:
        out = subprocess.run(["git", "archive", "--format=tar", commit, "src/main/java"], cwd=self.repo, capture_output=True)
        if out.returncode != 0:
            print(f"warning: no code for commit {commit[:10]}; missing-step check is off for it", file=sys.stderr)
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(fileobj=io.BytesIO(out.stdout)) as tar:
                tar.extractall(tmp, filter="data")
            return expected_steps(build_index(Path(tmp)))
