#!/usr/bin/env python3
"""Run a single scenario end-to-end (simulator + kernel) and print outputs.

This script is intentionally minimal and uses subprocesses with a
Python >= 3.10 executable for the kernel to avoid inline-import issues.
It runs the `everything_breaks` disturbance for a fixed time then
terminates processes and prints the `adaptations.jsonl` and
`events.jsonl` contents for inspection.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STREAMS = ROOT / "data" / "streams"
EVENTS = STREAMS / "events.jsonl"
ADAPT = STREAMS / "adaptations.jsonl"


def find_python(min_version=(3, 10)):
    candidates = ["python3.11", "python3.10", "python3.9", "python3", "python"]
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.check_output([path, "--version"], stderr=subprocess.STDOUT, text=True).strip()
            ver = out.split()[1]
            major, minor = (int(x) for x in ver.split(".")[:2])
            if (major, minor) >= min_version:
                return path
        except Exception:
            continue
    return sys.executable or "python3"


def reset_streams():
    STREAMS.mkdir(parents=True, exist_ok=True)
    EVENTS.write_text("", encoding="utf-8")
    ADAPT.write_text("", encoding="utf-8")


def prepare_compat_shim():
    # Create a small compatibility shim to shadow `interfaces.llm` for
    # older Python interpreters (3.9) so we can run the kernel process
    # without editing repository files.
    compat_dir = ROOT / "tests" / "_compat" / "interfaces"
    compat_dir.mkdir(parents=True, exist_ok=True)
    shim = compat_dir / "llm.py"
    if shim.exists():
        return str(shim.parent.parent)
    shim.write_text(
        "from typing import Protocol, runtime_checkable, List, Dict, Type, TypeVar, Optional\n"
        "from pydantic import BaseModel\n"
        "T = TypeVar('T', bound=BaseModel)\n\n"
        "@runtime_checkable\n"
        "class EmbeddingModel(Protocol):\n"
        "    def embed(self, input: List) -> List: ...\n\n"
        "@runtime_checkable\n"
        "class LLM(Protocol):\n"
        "    def generate(self, prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 4000) -> str: ...\n\n"
        "    def generate_structured(self, schema: Type[T], prompt: str, system_prompt: Optional[str] = None, max_tokens: int = 4000) -> T: ...\n\n"
        "    def chat(self, message: List[Dict]) -> str: ...\n"
    , encoding="utf-8")
    return str(shim.parent.parent)


def spawn_simulator(python):
    code = (
        "import sys, pathlib, asyncio\n"
        "ROOT = pathlib.Path.cwd()\n"
        "sys.path.insert(0, str(ROOT))\n"
        "sys.path.insert(0, str(ROOT / 'simulator'))\n"
        "from simulator.runner import simulation_runner\n"
        "asyncio.run(simulation_runner(debug_eval_ms=500, disturbance_type='everything_breaks'))\n"
    )
    return subprocess.Popen([python, "-u", "-c", code], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())


def spawn_kernel(python, env=None):
    code = (
        "import sys, pathlib, asyncio, os\n"
        "ROOT = pathlib.Path.cwd()\n"
        "# Ensure our compatibility shim is first on sys.path so it can\n"
        "# shadow project modules that use Python 3.10+ syntax when\n"
        "# running under older interpreters.\n"
        "compat = ROOT / 'tests' / '_compat'\n"
        "sys.path.insert(0, str(compat))\n"
        "sys.path.insert(0, str(ROOT))\n"
        "sys.path.insert(0, str(ROOT / 'kernel'))\n"
        "from kernel.engine.runner import engine_runner\n"
        "asyncio.run(engine_runner())\n"
    )
    return subprocess.Popen([python, "-u", "-c", code], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or os.environ.copy())


def read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def main():
    python = find_python()
    print(f"Using python: {python}")
    reset_streams()

    sim = spawn_simulator(python)
    time.sleep(1.0)
    # ensure compatibility shim is present and added to PYTHONPATH so the
    # kernel can import `interfaces.llm` even on Python < 3.10
    compat_root = prepare_compat_shim()
    env = os.environ.copy()
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = compat_root + (os.pathsep + prev if prev else "")
    kern = spawn_kernel(python, env=env)

    # run for a fixed duration then terminate
    DURATION_S = int(os.getenv("SIMPLE_TEST_S", "45"))
    print(f"Running simulator+kernel for {DURATION_S}s...")
    try:
        time.sleep(DURATION_S)
    except KeyboardInterrupt:
        pass

    for p, name in ((sim, "simulator"), (kern, "kernel")):
        try:
            p.terminate()
        except Exception:
            pass

    # collect outputs
    for p, name in ((sim, "simulator"), (kern, "kernel")):
        try:
            out, err = p.communicate(timeout=5)
        except Exception:
            try:
                p.kill()
                out, err = p.communicate(timeout=5)
            except Exception:
                out, err = b"", b""
        print(f"--- {name} stdout ---")
        try:
            print(out.decode("utf-8", errors="replace"))
        except Exception:
            print(out)
        print(f"--- {name} stderr ---")
        try:
            print(err.decode("utf-8", errors="replace"))
        except Exception:
            print(err)

    print("\nAdaptations records:")
    for r in read_jsonl(ADAPT):
        print(json.dumps(r, indent=2))

    print("\nEvent summary (first 40 events):")
    events = read_jsonl(EVENTS)
    for e in events[:40]:
        print(json.dumps(e, indent=2))


if __name__ == "__main__":
    main()
