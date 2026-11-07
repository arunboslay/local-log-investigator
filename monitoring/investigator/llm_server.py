"""Start and stop a local llama.cpp server (OpenAI-compatible API) for one model.

Everything runs on this machine. By default llama-server.exe is expected in ~/llm/llama.cpp and
model files in ~/llm/models. Set LLAMA_DIR or LLM_MODELS to use other folders.
"""

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

LLAMA_DIR = Path(os.environ.get("LLAMA_DIR", Path.home() / "llm" / "llama.cpp"))
MODELS_DIR = Path(os.environ.get("LLM_MODELS", Path.home() / "llm" / "models"))
RUN_DIR = Path(__file__).resolve().parent.parent / "run"
PORT = 8200
BASE_URL = f"http://127.0.0.1:{PORT}"


@dataclass
class ModelProfile:
    name: str
    gguf: str
    sampling: dict  # sent with every request
    server_args: list[str] = field(default_factory=list)
    fits_in_vram: bool = True  # False: let llama.cpp split layers between GPU and CPU (--fit)


PROFILES = {
    # OpenAI's open-weight model (Apache-2.0). Mixture of experts: 21B total, ~3.6B used per token.
    "gpt-oss-20b": ModelProfile(
        "gpt-oss-20b", "gpt-oss-20b-MXFP4.gguf",
        {"temperature": 1.0, "top_p": 1.0},
        ["--reasoning-effort", "medium"],
    ),
}


class LlamaServer:
    def __init__(self, profile: ModelProfile, ctx: int = 32768):
        self.profile, self.ctx = profile, ctx
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        RUN_DIR.mkdir(exist_ok=True)
        log = open(RUN_DIR / f"llama-{self.profile.name}.out", "a", encoding="utf-8")
        args = [str(LLAMA_DIR / "llama-server.exe"), "-m", str(MODELS_DIR / self.profile.gguf),
                "--alias", self.profile.name, "--port", str(PORT), "--host", "127.0.0.1",
                "-c", str(self.ctx), "-fa", "on", "-np", "1", "--jinja", "--no-webui",
                *(["-ngl", "99"] if self.profile.fits_in_vram else ["--fit", "on", "--fit-target", "768"]),
                *self.profile.server_args]
        self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited; see {RUN_DIR / f'llama-{self.profile.name}.out'}")
            try:
                if httpx.get(f"{BASE_URL}/health", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("llama-server did not become ready in 5 minutes")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=30)


class VramMonitor(threading.Thread):
    """Samples GPU memory in use (whole card, MiB) and llama-server RAM (MiB) once a second."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples: list[int] = []
        self.ram_samples: list[int] = []
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True)
            try:
                self.samples.append(int(out.stdout.strip().splitlines()[0]))
            except (ValueError, IndexError):
                pass
            ram = sum(p.info["memory_info"].rss for p in psutil.process_iter(["name", "memory_info"])
                      if p.info["name"] == "llama-server.exe" and p.info["memory_info"])
            self.ram_samples.append(ram // 2**20)
            self.stopped.wait(1)

    def stop(self) -> tuple[int, int]:
        """Peak VRAM (whole card) and peak llama-server RAM, in MiB."""
        self.stopped.set()
        self.join(timeout=5)
        return max(self.samples, default=0), max(self.ram_samples, default=0)
