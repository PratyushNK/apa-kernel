"""Run all 8 simulator disturbance modes with the kernel and collect metrics.

This lightweight harness sequentially runs each disturbance mode by
starting the simulator (with a disturbance injection) and the kernel
engine concurrently. After each run it parses `data/streams/events.jsonl`
and `data/streams/adaptations.jsonl` to compute the requested metrics.

Keep this file short and focused for repeatable experiments.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import time

ROOT = pathlib.Path(__file__).parent.parent
STREAMS = ROOT / "data" / "streams"
ADAPTATIONS = STREAMS / "adaptations.jsonl"
EVENTS = STREAMS / "events.jsonl"

# default per-scenario runtime (seconds)
# Set default to 60s to match experiment runs.
RUN_SECONDS = int(os.getenv("DEMO_PERF_RUNTIME_S", "60"))
# Run the kernel inline in the harness by default for simplicity/reliability.
# Set INLINE_ENGINE=0 to spawn the kernel in a subprocess instead.
INLINE_ENGINE = os.getenv("INLINE_ENGINE", "1") == "1"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "simulator"))

# We'll spawn the simulator and kernel in subprocesses to avoid import-time
# coupling and to isolate failures. This keeps experiments repeatable and
# ensures we don't modify other modules during this harness run.
import asyncio.subprocess
import subprocess


def _find_python_executable(min_version: tuple[int, int] = (3, 9)) -> str:
    """Return a suitable python executable path (prefers newer versions)."""
    env = os.getenv("PYTHON_CMD") or os.getenv("PYTHON_EXEC")
    if env:
        return env

    candidates = ["python3.11", "python3.10", "python3.9", "python3", "python"]
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.check_output([path, "--version"], stderr=subprocess.STDOUT, text=True).strip()
            parts = out.split()
            if len(parts) >= 2:
                ver = parts[1]
                major, minor = (int(x) for x in ver.split(".")[:2])
                if (major, minor) >= min_version:
                    return path
        except Exception:
            return path

    return sys.executable or "python3"


async def _spawn_simulator_process(disturbance: str, debug_eval_ms: int | None = None):
    python = _find_python_executable(min_version=(3, 9))
    dbg = (str(int(debug_eval_ms)) if debug_eval_ms is not None else "None")
    code = (
        "import sys, pathlib, asyncio, os, json, time\n"
        "ROOT = pathlib.Path.cwd()\n"
        "sys.path.insert(0, str(ROOT))\n"
        "sys.path.insert(0, str(ROOT / 'simulator'))\n"
        f"debug_ms = {dbg}\n"
        f"disturbance = {disturbance!r}\n"
        "from simulator.runner import simulation_runner\n"
        "async def _main():\n"
        "    await simulation_runner(debug_eval_ms=debug_ms, disturbance_type=disturbance)\n"
        "asyncio.run(_main())\n"
    )
    proc = await asyncio.create_subprocess_exec(
        python, "-u", "-c", code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
        env=os.environ.copy(),
    )
    return proc


async def _spawn_engine_process():
    python = _find_python_executable(min_version=(3, 10))
    # check whether selected python is >= 3.10
    try:
        ver_out = subprocess.check_output([python, "--version"], stderr=subprocess.STDOUT, text=True).strip()
        ver_parts = ver_out.split()[1].split(".")
        major, minor = int(ver_parts[0]), int(ver_parts[1])
    except Exception:
        major, minor = 0, 0

    # allow forcing the lightweight stub for controlled experiments
    force_stub = os.getenv("FORCE_USE_STUB", "0") == "1"
    if not force_stub and (major, minor) >= (3, 10):
        code = (
            "import sys, pathlib, asyncio, os\n"
            "ROOT = pathlib.Path.cwd()\n"
            "# ensure repository root and kernel package are available\n"
            "sys.path.insert(0, str(ROOT))\n"
            "sys.path.insert(0, str(ROOT / 'kernel'))\n"
            "# allow an optional shim path (PYTHON_SHIM_PATH) to override specific modules\n"
            "shim = os.getenv('PYTHON_SHIM_PATH')\n"
            "if shim:\n"
            "    try:\n"
            "        sys.path.insert(0, shim)\n"
            "    except Exception:\n"
            "        pass\n"
            "from kernel.engine.runner import engine_runner\n"
            "asyncio.run(engine_runner())\n"
        )
        proc = await asyncio.create_subprocess_exec(
            python, "-u", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT),
            env=os.environ.copy(),
        )
        return proc

    # Fallback: minimal kernel stub that only observes events and writes
    # the lightweight adaptation records requested by the harness. Do not
    # attempt to import or run any kernel modules; keep behavior strictly
    # limited to detection+observation records.
    # Softened stub: mark stub records and optionally avoid emitting a
    # recovered record so harness can detect and ignore synthetic output.
    stub_code = (
        "import time, json, pathlib, sys, os\n"
        "ROOT = pathlib.Path.cwd()\n"
        "EV = ROOT / 'data' / 'streams' / 'events.jsonl'\n"
        "AD = ROOT / 'data' / 'streams' / 'adaptations.jsonl'\n"
        "STUB_WRITE_RECOVERY = os.getenv('STUB_WRITE_RECOVERY','0') == '1'\n"
        "last_count = 0\n"
        "while True:\n"
        "    try:\n"
        "        time.sleep(0.2)\n"
        "        if not EV.exists():\n"
        "            continue\n"
        "        with EV.open('r', encoding='utf-8') as fh:\n"
        "            lines = [l for l in fh.read().splitlines() if l.strip()]\n"
        "        new = lines[last_count:]\n"
        "        for line in new:\n"
        "            try:\n"
        "                rec = json.loads(line)\n"
        "            except Exception:\n"
        "                continue\n"
        "            if rec.get('event_type') == 'Disturbance':\n"
        "                # observed record: report observation immediately (marked as stub)\n"
        "                obs = {'stage': 'observed', 'ts': time.time(), 'cycle_count': 1, 'stub': True}\n"
        "                with AD.open('a', encoding='utf-8') as fh:\n"
        "                    fh.write(json.dumps(obs) + '\\n')\n"
        "                # optionally emit a recovered record (only if enabled)\n"
        "                if STUB_WRITE_RECOVERY:\n"
        "                    time.sleep(1.0)\n"
        "                    recov = {'stage': 'recovered', 'ts': time.time(), 'cycle_count': 2, 'recovery_confirmed': True, 'stub': True}\n"
        "                    with AD.open('a', encoding='utf-8') as fh:\n"
        "                        fh.write(json.dumps(recov) + '\\n')\n"
        "        last_count = len(lines)\n"
        "    except KeyboardInterrupt:\n"
        "        break\n"
        "    except Exception:\n"
        "        time.sleep(0.5)\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable or 'python3', "-u", "-c", stub_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(ROOT),
        env=os.environ.copy(),
    )
    return proc


async def _shutdown_and_collect(proc, name: str, timeout: float = 10.0) -> tuple[str, str]:
    if proc is None:
        return "", ""
    try:
        if proc.returncode is None:
            proc.terminate()
    except Exception:
        pass
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        out, err = await proc.communicate()

    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    if out:
        print(f"[{name} stdout]\n{out}")
    if err:
        print(f"[{name} stderr]\n{err}")
    return out, err


SCENARIOS = [
    ("Healthy Baseline", "healthy_baseline"),
    ("Gateway Degradation", "gateway_degradation"),
    ("Full Outage", "full_outage"),
    ("Circuit Breaker Trigger", "circuit_breaker_trigger"),
    ("Retry Amplification", "retry_amplification"),
    ("SLA Breach", "sla_breach"),
    ("Burst Traffic", "burst_traffic"),
    ("Everything Breaks", "everything_breaks"),
]


def _reset_environment():
    policy_path = ROOT / "data" / "policies" / "policy.json"
    gateway_path = ROOT / "data" / "gateway_commands.json"
    streams_path = ROOT / "data" / "streams"

    default_policy_text = json.dumps({
        "provider_priority": ["G1", "G2"],
        "provider_weights": {"G1": 0.5, "G2": 0.5},
        "weight_learning_rate": 0.1,
        "max_retry": 3,
        "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
        "base_backoff_ms": 100,
        "backoff_multiplier": 2.0,
        "retry_budget_window_ms": 60000,
        "max_retries_per_window": 200,
        "timeout_ms": {"G1": 300, "G2": 300}
    }, indent=2)

    default_gateway_text = json.dumps({
        "regimes": {"G1": "HEALTHY", "G2": "HEALTHY"},
        "commands": [],
        "updated_at": int(time.time() * 1000)
    }, indent=2)

    try:
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(default_policy_text, encoding="utf-8")
    except Exception as e:
        print(f"[harness] failed writing policy file {policy_path}: {e}", file=sys.stderr)
    try:
        gateway_path.parent.mkdir(parents=True, exist_ok=True)
        gateway_path.write_text(default_gateway_text, encoding="utf-8")
    except Exception as e:
        print(f"[harness] failed writing gateway file {gateway_path}: {e}", file=sys.stderr)
    try:
        streams_path.mkdir(parents=True, exist_ok=True)
        ev = streams_path / "events.jsonl"
        ev.write_text("", encoding="utf-8")
        ad = streams_path / "adaptations.jsonl"
        ad.write_text("", encoding="utf-8")
    except Exception as e:
        print(f"[harness] failed resetting streams in {streams_path}: {e}", file=sys.stderr)


def read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def compute_approval(events: list[dict], start_idx: int, end_idx: int) -> float:
    window = events[start_idx:end_idx]
    attempts = [e for e in window if e.get("event_type") == "AttemptResult"]
    if not attempts:
        return 0.0
    # case-insensitive success check and Laplace smoothing to reduce
    # quantization when sample sizes are small
    succ = sum(1 for a in attempts if str(a.get("status") or "").upper() == "SUCCESS")
    return (succ + 1) / (len(attempts) + 2)


async def run_one(name: str, disturbance: str) -> dict:
    # reset environment: policy, gateway commands, and streams
    _reset_environment()

    print(f"\n=== RUNNING: {name} ({disturbance}) ===\n")

    # start simulator (inject disturbance)
    # allow optional controlled debug injection timing via `SIM_DEBUG_MS`
    debug_env = os.getenv("SIM_DEBUG_MS")
    try:
        debug_eval = int(debug_env) if debug_env is not None else None
    except Exception:
        debug_eval = None
    sim_proc = await _spawn_simulator_process(disturbance, debug_eval_ms=debug_eval)
    await asyncio.sleep(1.0)

    # warn if a PYTHON_SHIM_PATH is in use; shimmed modules can produce
    # deterministic/synthetic outputs that should be considered suspect
    if os.getenv("PYTHON_SHIM_PATH"):
        print(f"[harness] WARNING: PYTHON_SHIM_PATH set -> shimmed modules may affect results ({os.getenv('PYTHON_SHIM_PATH')})", file=sys.stderr)

    # start kernel: inline task (default) or subprocess (when INLINE_ENGINE=0)
    engine_task = None
    eng_proc = None
    if INLINE_ENGINE:
        try:
            # run engine within this process to ensure deterministic behaviour
            from kernel.engine.runner import engine_runner
            engine_task = asyncio.create_task(engine_runner())
        except Exception as e:
            print(f"[kernel inline] failed to start inline engine: {e}")
            eng_proc = await _spawn_engine_process()
    else:
        eng_proc = await _spawn_engine_process()

    # let simulator + kernel run for a fixed wall-time then terminate
    # Wait briefly for the simulator to emit the disturbance (non-fatal)
    wait_for_injection_s = float(os.getenv("WAIT_FOR_INJECTION_S", "4.0"))
    injected = False
    start_wait = time.time()
    while time.time() - start_wait < wait_for_injection_s:
        evs = read_jsonl(EVENTS)
        if any(e.get("event_type") == "Disturbance" and e.get("disturbance") == disturbance for e in evs):
            injected = True
            break
        await asyncio.sleep(0.2)
    if not injected:
        print(f"[harness] warning: no disturbance event found within {wait_for_injection_s}s for {disturbance}", file=sys.stderr)
    try:
        await asyncio.sleep(RUN_SECONDS)
    except asyncio.CancelledError:
        pass

    # shut down processes / tasks and collect logs
    await _shutdown_and_collect(sim_proc, "simulator")
    if INLINE_ENGINE:
        if engine_task is not None:
            engine_task.cancel()
            try:
                await engine_task
            except asyncio.CancelledError:
                pass
    else:
        await _shutdown_and_collect(eng_proc, "kernel")
    # give kernel a short moment to flush adaptation records
    await asyncio.sleep(1.0)

    events = read_jsonl(EVENTS)
    adaptations = read_jsonl(ADAPTATIONS)

    # find disturbance index and ts (ms)
    inj_idx = None
    inj_ts = None
    for i, e in enumerate(events):
        if e.get("event_type") == "Disturbance" and e.get("disturbance") == disturbance:
            inj_idx = i
            try:
                v = e.get("ts")
                if v is None:
                    inj_ts = None
                else:
                    fv = float(v)
                    if fv >= 1e12:
                        inj_ts = int(fv)
                    elif fv >= 1e9:
                        inj_ts = int(fv * 1000)
                    else:
                        # small numeric values are treated as relative-ms
                        inj_ts = int(fv)
            except Exception:
                inj_ts = None
            break

    def _rec_ts_ms(r: dict) -> int | None:
        v = r.get("ts")
        if v is None:
            return None
        # robust conversion: handle epoch-ms, epoch-s, and relative-ms
        try:
            fv = float(v)
        except Exception:
            try:
                iv = int(v)
                fv = float(iv)
            except Exception:
                return None
        # heuristics:
        # - epoch milliseconds are large (~1e12+)
        # - epoch seconds are ~1e9 -> convert to ms
        # - small numbers are likely relative milliseconds (do not scale)
        if fv >= 1e12:
            return int(fv)
        if fv >= 1e9:
            return int(fv * 1000)
        return int(fv)

    def _raw_event_val(e: dict) -> float | None:
        # return the raw numeric timestamp value (no scaling)
        for k in ("completed_at", "timestamp", "created_at", "ts", "started_at"):
            if k in e:
                try:
                    return float(e[k])
                except Exception:
                    continue
        return None

    def _event_ts_ms(e: dict) -> int | None:
        # convert an event's timestamp to milliseconds since epoch when possible.
        raw = _raw_event_val(e)
        if raw is None:
            return None
        fv = raw
        if fv >= 1e12:
            return int(fv)
        if fv >= 1e9:
            return int(fv * 1000)
        # small values are treated as relative-ms (returned as-is)
        return int(fv)

    def _find_nearest_rel_event_value(events_list: list[dict], idx: int) -> tuple[int | None, float | None]:
        # Search backwards from idx for the nearest event with a raw numeric timestamp
        for k in range(idx, -1, -1):
            rv = _raw_event_val(events_list[k])
            if rv is not None:
                return k, rv
        return None, None

    # Collect adaptation records that occur at/after injection
    adapts_after = []
    for r in adaptations:
        # ignore synthetic stub records emitted by the lightweight test stub
        if r.get("stub"):
            continue
        r_ts = _rec_ts_ms(r)
        if inj_ts is None or r_ts is None or r_ts >= inj_ts:
            adapts_after.append((r_ts or 0, r))
    adapts_after.sort(key=lambda x: x[0])

    # Identify observed / recovered records
    observed_records = [(ts, r) for ts, r in adapts_after if str(r.get("stage") or "").lower() == "observed"]
    recovered_records = [
        (ts, r) for ts, r in adapts_after
        if (
            str(r.get("stage") or "").lower() in ("recovered", "recovery", "recovery_confirmed")
            or bool(r.get("recovery_confirmed"))
            or str(r.get("status") or "").lower() == "success"
        )
    ]

    breach_ts = None
    breach_rec = None

    if observed_records:
        breach_ts, breach_rec = observed_records[0]
    elif recovered_records:
        breach_ts, breach_rec = recovered_records[0]

    # Healthy baseline is intentionally a no-op disturbance; do not
    # consider it a breach even if transient event noise exists.
    if disturbance == "healthy_baseline":
        breach_ts = None
        breach_rec = None
        cycles = 0
        recovered = False
        # compute approval windows around injection for reporting
        approval_at_breach = None
        approval_after = None
        if inj_idx is not None:
            pre_start = max(0, inj_idx - 100)
            pre_end = inj_idx
            post_start = inj_idx + 1
            post_end = min(len(events), inj_idx + 101)
            approval_at_breach = compute_approval(events, pre_start, pre_end)
            approval_after = compute_approval(events, post_start, post_end)
        time_to_breach_ms = None
        # Persist result using same variable names below
        result = {
            "scenario": name,
            "disturbance": disturbance,
            "injection_index": inj_idx,
            "injection_ts_ms": inj_ts,
            "breach_ts_ms": None,
            "time_to_breach_ms": None,
            "adaptation_cycles": cycles,
            "recovered_confirmed": recovered,
            "approval_before": approval_at_breach,
            "approval_after": approval_after,
        }

        outdir = pathlib.Path("tests") / "scenario_outputs"
        outdir.mkdir(parents=True, exist_ok=True)
        outpath = outdir / f"{disturbance}.result.json"
        outpath.write_text(json.dumps(result, indent=2))

        print(f"Finished {name}: wrote {outpath}")
        return result

    # Note: `_event_ts_ms` and related helpers are defined above

    # If no explicit adaptation observation, detect breach from events (time-windowed sliding)
    if breach_ts is None and inj_idx is not None:
        # Build index of attempt events
        attempt_indices = [i for i, ev in enumerate(events) if ev.get("event_type") == "AttemptResult"]
        # find first attempt index on/after injection
        pos = 0
        while pos < len(attempt_indices) and attempt_indices[pos] < inj_idx:
            pos += 1

        breach_found = False
        THRESHOLD = 0.85
        WINDOW_SECONDS = float(os.getenv("BREACH_WINDOW_S", "5.0"))
        MIN_ATTEMPTS = int(os.getenv("BREACH_MIN_ATTEMPTS", "10"))
        # iterate through attempts and evaluate a time-based sliding window ending at each attempt
        for j in range(pos, len(attempt_indices)):
            idx_j = attempt_indices[j]
            ts_j = _event_ts_ms(events[idx_j])
            if ts_j is None:
                # fallback to count-based window
                start_attempt_idx = max(0, j - 20 + 1)
                start_event_idx = attempt_indices[start_attempt_idx]
            else:
                window_start_ts = ts_j - int(WINDOW_SECONDS * 1000)
                # find the earliest attempt event index within the time window
                start_event_idx = None
                search_start = max(0, j - 50)
                for k in range(search_start, j + 1):
                    idx_k = attempt_indices[k]
                    ts_k = _event_ts_ms(events[idx_k])
                    if ts_k is not None and ts_k >= window_start_ts:
                        start_event_idx = idx_k
                        break
                if start_event_idx is None:
                    start_event_idx = attempt_indices[max(0, j - 20 + 1)]
            end_event_idx = idx_j + 1
            attempts_window = [ev for ev in events[start_event_idx:end_event_idx] if ev.get("event_type") == "AttemptResult"]
            if len(attempts_window) < MIN_ATTEMPTS:
                continue
            approval = compute_approval(events, start_event_idx, end_event_idx)
            if approval < THRESHOLD:
                breach_idx_event = idx_j
                # attempt to compute a consistent time delta between the
                # breach event and the injection using raw event timestamps
                raw_breach = _raw_event_val(events[breach_idx_event])
                breach_ts = None
                computed_delta_ms = None
                if raw_breach is not None and inj_idx is not None:
                    # find a nearby event with a relative timestamp for the injection
                    inj_near_idx, inj_near_raw = _find_nearest_rel_event_value(events, inj_idx)
                    if inj_near_raw is not None:
                        def _raw_to_ms(rv: float) -> int:
                            if rv >= 1e12:
                                return int(rv)
                            if rv >= 1e9:
                                return int(rv * 1000)
                            return int(rv)

                        breach_raw_ms = _raw_to_ms(raw_breach)
                        inj_near_raw_ms = _raw_to_ms(inj_near_raw)
                        computed_delta_ms = int(breach_raw_ms - inj_near_raw_ms)
                        if inj_ts is not None:
                            breach_ts = int(inj_ts + computed_delta_ms)
                # Only accept a computed delta when we can map relative event
                # timestamps to an injection reference. Do not fall back to
                # arbitrary absolute conversions (they are error-prone).
                if computed_delta_ms is not None:
                    time_to_breach_ms = computed_delta_ms
                    if inj_ts is not None:
                        breach_ts = int(inj_ts + computed_delta_ms)
                else:
                    # leave breach_ts/time_to_breach as None when mapping fails
                    breach_ts = None
                    time_to_breach_ms = None
                breach_rec = None
                breach_found = True
                break

    # If we computed a delta earlier during event-based detection, preserve it.
    if "time_to_breach_ms" in locals() and time_to_breach_ms is not None:
        # ensure breach_ts is set when inj_ts exists
        if inj_ts is not None and breach_ts is None:
            breach_ts = int(inj_ts + time_to_breach_ms)
    else:
        # If breach_ts looks like a small relative-ms value, treat it as a delta
        if breach_ts is not None and inj_ts is not None and breach_ts < 1e11:
            time_to_breach_ms = int(breach_ts)
            breach_ts = int(inj_ts + time_to_breach_ms)
        else:
            time_to_breach_ms = (breach_ts - inj_ts) if (breach_ts is not None and inj_ts is not None) else None

    # Heuristic: when breach_ts is a small number it often represents a relative-ms
    # timestamp (not epoch ms). If inj_ts is available, convert to absolute.
    if breach_ts is not None and inj_ts is not None:
        if breach_ts < 1e11:
            # treat `breach_ts` as delta in ms when no explicit delta was computed
            if time_to_breach_ms is None:
                time_to_breach_ms = int(breach_ts)
            # map to absolute epoch ms
            breach_ts = int(inj_ts + int(time_to_breach_ms))

    # Determine adaptation cycles: prefer explicit cycle_count/cycles fields
    cycles = None
    cycle_vals = []
    for ts, r in adapts_after:
        if "cycle_count" in r:
            try:
                cycle_vals.append(int(r.get("cycle_count")))
            except Exception:
                pass
        elif "cycles" in r:
            try:
                cycle_vals.append(int(r.get("cycles")))
            except Exception:
                pass
    if cycle_vals:
        unique_vals = sorted(set(cycle_vals))
        # detect suspicious uniform small counts (likely stub/shim) and try to infer instead
        if len(unique_vals) == 1 and unique_vals[0] in (1, 2) and len(adapts_after) >= 3:
            inferred = sum(1 for _, r in adapts_after if str(r.get("stage") or "").lower() == "recovered" or bool(r.get("recovery_confirmed")))
            if inferred:
                cycles = inferred
            else:
                cycles = None
                print("[harness] suspicious uniform cycle_count values detected; reporting null cycles", file=sys.stderr)
        else:
            cycles = max(cycle_vals)
    else:
        inferred = sum(1 for _, r in adapts_after if str(r.get("stage") or "").lower() == "recovered" or bool(r.get("recovery_confirmed")))
        if inferred:
            cycles = inferred
        else:
            cycles = None

    # Compute approval rates at breach and after recovery (fallback to injection window)
    approval_at_breach = None
    approval_after = None
    if breach_ts is not None:
        # find nearest event index for breach_ts
        breach_event_idx = None
        for i, ev in enumerate(events):
            ets = _event_ts_ms(ev)
            if ets is not None and breach_ts is not None and ets >= breach_ts:
                breach_event_idx = i
                break
        if breach_event_idx is None:
            breach_event_idx = inj_idx
        pre_start = max(0, breach_event_idx - 100)
        pre_end = breach_event_idx
        post_start = breach_event_idx + 1
        post_end = min(len(events), breach_event_idx + 101)
        approval_at_breach = compute_approval(events, pre_start, pre_end)
        approval_after = compute_approval(events, post_start, post_end)
    elif inj_idx is not None:
        pre_start = max(0, inj_idx - 100)
        pre_end = inj_idx
        post_start = inj_idx + 1
        post_end = min(len(events), inj_idx + 101)
        approval_at_breach = compute_approval(events, pre_start, pre_end)
        approval_after = compute_approval(events, post_start, post_end)

    # Determine recovery confirmation (optionally require post-recovery approval)
    recovered = False
    if adapts_after:
        recovered = any(
            bool(r.get("recovery_confirmed"))
            or str(r.get("status") or "").lower() == "success"
            or str(r.get("stage") or "").lower() == "recovered"
            for _, r in adapts_after
        )

    # default to strict approval gating to avoid reporting recovery when
    # post-recovery approval has not measurably improved
    harness_strict = os.getenv("HARNESS_STRICT_APPROVAL", "1") == "1"
    try:
        harness_min_approval = float(os.getenv("HARNESS_MIN_APPROVAL", "0.85"))
    except Exception:
        harness_min_approval = 0.85

    if recovered and harness_strict:
        # require a measurable approval after recovery and enforce threshold
        if approval_after is None or approval_after < harness_min_approval:
            recovered = False

    result = {
        "scenario": name,
        "disturbance": disturbance,
        "injection_index": inj_idx,
        "injection_ts_ms": inj_ts,
        "breach_ts_ms": breach_ts,
        "time_to_breach_ms": time_to_breach_ms,
        "adaptation_cycles": cycles,
        "recovered_confirmed": recovered,
        "approval_before": approval_at_breach,
        "approval_after": approval_after,
    }

    # persist per-scenario results
    outdir = pathlib.Path("tests") / "scenario_outputs"
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / f"{disturbance}.result.json"
    outpath.write_text(json.dumps(result, indent=2))

    print(f"Finished {name}: wrote {outpath}")
    return result


async def main():
    results = []
    for name, disturbance in SCENARIOS:
        r = await run_one(name, disturbance)
        results.append(r)

    # write summary
    SUMMARY = STREAMS / "scenario_summary.json"
    SUMMARY.write_text(json.dumps(results, indent=2))
    print(f"\nWrote summary to {SUMMARY}\n")


if __name__ == "__main__":
    asyncio.run(main())
