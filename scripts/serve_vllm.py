#!/usr/bin/env python3
"""
Starts the three vLLM servers the engine uses (vision, text, OCR) on one machine: Kaggle, RunPod or AWS.

    python scripts/serve_vllm.py                      # run in the foreground, Ctrl+C stops all
    python scripts/serve_vllm.py --detach             # return once all are ready (notebooks)
    python scripts/serve_vllm.py --stop               # stop servers started with --detach
    python scripts/serve_vllm.py --dry-run            # only print the commands

Layout: with two or more GPUs the vision model gets GPU 0 and the other two share GPU 1; with one GPU all
three share it. Servers start one at a time, because vLLM sizes its memory from what is free at start-up.
The memory fractions are starting points that have NOT been measured; override them if a server fails.

VLLM_API_KEY, when set, is handed to the servers through the environment (never on the command line).
Without it the servers only listen on 127.0.0.1.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from typing import List, NamedTuple, Optional
import urllib.request


class Server(NamedTuple):
    role: str
    env_name: str
    model: str
    port: int
    gpu: int
    memory: float
    max_model_len: int
    quantize: bool


class Gpu(NamedTuple):
    name: str
    memory_mib: int


def detect_gpus() -> List[Gpu]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    rows = (line.rsplit(",", 1) for line in out.splitlines() if line.strip())
    return [Gpu(name.strip(), int(memory)) for name, memory in rows]


def lacks_bfloat16(name: str) -> bool:
    """NVIDIA cards older than Ampere (compute capability below 8.0)."""
    upper = name.upper()
    return any(tag in upper.split() or f"TESLA {tag}" in upper for tag in ("T4", "V100", "P100")) \
        or "RTX 20" in upper or "QUADRO RTX" in upper


def plan(args: argparse.Namespace, gpu_count: int) -> List[Server]:
    two = gpu_count >= 2
    # With a GPU to itself the vision model does not need quantisation (bitsandbytes support for
    # Qwen3-VL in vLLM is the least certain part).
    vision_quantize = not (two or args.no_quantize)
    if args.vision_quantize is not None:
        vision_quantize = args.vision_quantize
    return [
        Server("vision", "VLLM_API_BASE", args.vision_model, args.port, 0,
               args.vision_memory or (0.90 if two else 0.40), args.max_model_len, vision_quantize),
        Server("text", "VLLM_TEXT_API_BASE", args.text_model, args.port + 1, 1 if two else 0,
               args.text_memory or (0.60 if two else 0.35), args.text_max_model_len, not args.no_quantize),
        Server("ocr", "VLLM_OCR_API_BASE", args.ocr_model, args.port + 2, 1 if two else 0,
               args.ocr_memory or (0.30 if two else 0.20), min(args.max_model_len, 8192), False),
    ]


def command(server: Server, args: argparse.Namespace, float16: bool) -> List[str]:
    cmd = shlex.split(args.vllm_bin) + [
        "serve", server.model,
        "--host", args.host,
        "--port", str(server.port),
        "--max-model-len", str(server.max_model_len),
        "--gpu-memory-utilization", f"{server.memory:.2f}",
    ]
    if float16:
        cmd += ["--dtype", "float16"]
    if server.quantize:
        cmd += ["--quantization", "bitsandbytes"]
    return cmd + shlex.split(args.extra)


def is_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def tail(path: Path, lines: int = 40) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def stop(pid_file: Path) -> None:
    if not pid_file.is_file():
        print("No running servers recorded.")
        return
    for pid in json.loads(pid_file.read_text()):
        _kill(pid)
    pid_file.unlink()
    print("Stopped.")


def _kill(pid: int) -> None:
    """Stops a server and the worker processes it started."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--vision-model", default=os.environ.get("DOC_DEFAULT_MODEL", "Qwen/Qwen3-VL-4B-Instruct"))
    parser.add_argument("--text-model", default="Qwen/Qwen3-4B-Instruct-2507")
    parser.add_argument("--ocr-model", default="zai-org/GLM-OCR")
    parser.add_argument("--port", type=int, default=8000, help="vision port; text and OCR use the next two")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to accept remote clients (needs VLLM_API_KEY)")
    parser.add_argument("--vllm-bin", default="vllm", help="path to the vllm command, e.g. a separate venv")
    parser.add_argument("--max-model-len", type=int, default=8192, help="vision and OCR context length")
    parser.add_argument("--text-max-model-len", type=int, default=16384,
                        help="text context length: OCR text and prompt plus up to 6144 output tokens")
    parser.add_argument("--vision-memory", type=float)
    parser.add_argument("--text-memory", type=float)
    parser.add_argument("--ocr-memory", type=float)
    parser.add_argument("--no-quantize", action="store_true", help="no 4-bit loading for any model")
    parser.add_argument("--vision-quantize", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--dtype", choices=("auto", "float16"), default="auto",
                        help="auto picks float16 on GPUs without bfloat16 (T4)")
    parser.add_argument("--extra", default="", help="extra flags added to every `vllm serve`")
    parser.add_argument("--log-dir", type=Path, default=Path("vllm-logs"))
    parser.add_argument("--ready-timeout", type=int, default=1800, help="seconds per server, download included")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    pid_file = args.log_dir / "pids.json"
    if args.stop:
        stop(pid_file)
        return 0

    key = os.environ.get("VLLM_API_KEY")
    if args.host not in ("127.0.0.1", "localhost") and not key:
        print("Refusing to listen on a network address without VLLM_API_KEY.", file=sys.stderr)
        return 2

    gpus = detect_gpus()
    if not gpus and not args.dry_run:
        print("No NVIDIA GPU found (nvidia-smi).", file=sys.stderr)
        return 2
    float16 = args.dtype == "float16" or any(lacks_bfloat16(g.name) for g in gpus)
    servers = plan(args, len(gpus))
    print(f"GPUs: {', '.join(f'{g.name} ({g.memory_mib} MiB)' for g in gpus) or 'none detected'}")

    if args.dry_run:
        for server in servers:
            print(f"CUDA_VISIBLE_DEVICES={server.gpu} " + shlex.join(command(server, args, float16)))
        return 0

    if pid_file.is_file():
        print(f"{pid_file} exists: run with --stop first.", file=sys.stderr)
        return 2
    busy = [s.port for s in servers if is_ready(s.port)]
    if busy:
        print(f"Ports already serving: {busy}", file=sys.stderr)
        return 2

    args.log_dir.mkdir(parents=True, exist_ok=True)
    processes: List[subprocess.Popen] = []

    def shut_down() -> None:
        for process in processes:
            _kill(process.pid)
        pid_file.unlink(missing_ok=True)

    try:
        for server in servers:
            log = args.log_dir / f"{server.role}.log"
            print(f"Starting {server.role}: {server.model} on port {server.port}, GPU {server.gpu} (log: {log})")
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(server.gpu))
            with open(log, "wb") as stream:
                processes.append(subprocess.Popen(
                    command(server, args, float16), env=env, stdout=stream, stderr=subprocess.STDOUT,
                    start_new_session=True,
                ))
            pid_file.write_text(json.dumps([p.pid for p in processes]))
            deadline = time.monotonic() + args.ready_timeout
            while not is_ready(server.port):
                if processes[-1].poll() is not None or time.monotonic() > deadline:
                    print(f"\n{server.role} did not start. End of {log}:\n{tail(log)}", file=sys.stderr)
                    shut_down()
                    return 1
                time.sleep(5)
            print(f"  ready")

        print("\nAll servers are ready. Point the engine at them:")
        for server in servers:
            print(f"export {server.env_name}=http://127.0.0.1:{server.port}/v1")
        if args.detach:
            return 0
        while all(p.poll() is None for p in processes):
            time.sleep(5)
        print("A server stopped; shutting the others down.", file=sys.stderr)
        shut_down()
        return 1
    except KeyboardInterrupt:
        shut_down()
        return 130


if __name__ == "__main__":
    sys.exit(main())
