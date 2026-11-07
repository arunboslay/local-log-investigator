"""Sanity check for the local training environment: torch, CUDA, GPU, bf16 throughput, free VRAM."""

import sys
import time

import torch


def bf16_matmul_tflops(n: int = 4096, iters: int = 50) -> float:
    a = torch.randn(n, n, device="cuda", dtype=torch.bfloat16)
    b = torch.randn(n, n, device="cuda", dtype=torch.bfloat16)
    for _ in range(10):  # warmup
        a @ b
    torch.cuda.synchronize()
    start = time.perf_counter()
    for _ in range(iters):
        a @ b
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start
    return 2 * n**3 * iters / elapsed / 1e12


def main() -> int:
    print(f"python            : {sys.version.split()[0]}")
    print(f"torch             : {torch.__version__}")
    print(f"torch CUDA build  : {torch.version.cuda}")
    available = torch.cuda.is_available()
    print(f"CUDA available    : {available}")
    if not available:
        return 1

    props = torch.cuda.get_device_properties(0)
    cap = torch.cuda.get_device_capability(0)
    print(f"device            : {props.name}")
    print(f"compute capability: {cap}" + ("" if cap == (12, 0) else "  (expected (12, 0))"))
    print(f"total VRAM        : {props.total_memory / 2**30:.1f} GiB")
    print(f"bf16 supported    : {torch.cuda.is_bf16_supported()}")

    tflops = bf16_matmul_tflops()
    print(f"bf16 4096^2 matmul: {tflops:.1f} TFLOPS")

    free, total = torch.cuda.mem_get_info()
    print(f"free VRAM         : {free / 2**30:.1f} / {total / 2**30:.1f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
