from __future__ import annotations

import asyncio
import json
import signal
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from app.runtime import (
    DEFAULT_POLICY,
    FRONTEND_DIR,
    GATEWAY_CMD_PATH,
    POLICY_PATH,
    ROOT,
    STREAM_PATH,
    capture_process_output,
    compute_metrics,
    ensure_gateway_file,
    extract_engine_state,
    heartbeat,
    python_env,
    safe_read_json,
    state,
    parse_max_cycles,
    parse_health_thresholds,
)

app = FastAPI(title="APA Kernel Demo API")


async def start_processes() -> None:
    if state.system_status == "RUNNING":
        return
    STREAM_PATH.parent.mkdir(parents=True, exist_ok=True)
    STREAM_PATH.write_text("", encoding="utf-8")
    ensure_gateway_file()
    state.system_status = "RUNNING"
    state.baseline_metrics = None
    state.last_trigger, state.last_status, state.cycles = "-", "-", 0
    state.simulator_log.clear()
    state.kernel_log.clear()
    state.adaptation_log.clear()
    state.simulator_proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "simulator/runner.py",
        cwd=str(ROOT),
        env=python_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    state.kernel_proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "kernel/engine/runner.py",
        cwd=str(ROOT),
        env=python_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    state.sim_reader_task = asyncio.create_task(capture_process_output(state.simulator_proc, state.simulator_log, "simulator"))
    state.kernel_reader_task = asyncio.create_task(capture_process_output(state.kernel_proc, state.kernel_log, "kernel"))
    state.heartbeat_task = asyncio.create_task(heartbeat())


async def stop_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "system_status": state.system_status}


@app.post("/start")
async def start() -> JSONResponse:
    await start_processes()
    return JSONResponse({"status": "started"})


@app.post("/stop")
async def stop() -> JSONResponse:
    await stop_process(state.simulator_proc)
    await stop_process(state.kernel_proc)
    state.system_status = "STOPPED"
    # Clear runtime/baseline state so subsequent runs start fresh
    state.baseline_metrics = None
    state.adaptation_status = None
    state.stream_events_cache.clear()
    state.last_trigger = "-"
    state.last_status = "-"
    state.cycles = 0
    state.simulator_log.clear()
    state.kernel_log.clear()
    # also clear persistent event stream so aggregator restarts without prior history
    try:
        STREAM_PATH.parent.mkdir(parents=True, exist_ok=True)
        STREAM_PATH.write_text("", encoding="utf-8")
    except Exception:
        pass
    # reset policy and gateway file to healthy defaults
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(DEFAULT_POLICY, indent=2), encoding="utf-8")
    # explicitly write healthy gateway regimes so UI and simulator reset
    try:
        payload = ensure_gateway_file()
        payload.setdefault("regimes", {})["G1"] = "HEALTHY"
        payload.setdefault("regimes", {})["G2"] = "HEALTHY"
        payload.setdefault("commands", [])
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        GATEWAY_CMD_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        ensure_gateway_file()
    return JSONResponse({"status": "stopped"})


@app.post("/gateway/{provider}/outage")
async def gateway_outage(provider: str) -> JSONResponse:
    p = provider.upper()
    if p not in {"G1", "G2"}:
        raise HTTPException(status_code=400, detail="provider must be G1 or G2")
    payload = ensure_gateway_file()
    payload.setdefault("regimes", {})[p] = "OUTAGE"
    payload.setdefault("commands", []).append({"provider": p, "action": "OUTAGE", "at": datetime.now(timezone.utc).isoformat()})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    GATEWAY_CMD_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return JSONResponse({"status": "ok", "provider": p, "regime": "OUTAGE"})


@app.post("/gateway/{provider}/recover")
async def gateway_recover(provider: str) -> JSONResponse:
    p = provider.upper()
    if p not in {"G1", "G2"}:
        raise HTTPException(status_code=400, detail="provider must be G1 or G2")
    payload = ensure_gateway_file()
    payload.setdefault("regimes", {})[p] = "HEALTHY"
    payload.setdefault("commands", []).append({"provider": p, "action": "HEALTHY", "at": datetime.now(timezone.utc).isoformat()})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    GATEWAY_CMD_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return JSONResponse({"status": "ok", "provider": p, "regime": "HEALTHY"})


@app.get("/stream")
async def stream() -> EventSourceResponse:
    async def event_generator():
        while True:
            events = list(state.stream_events_cache)
            metrics = compute_metrics(events)
            gateway = ensure_gateway_file().get("regimes", {"G1": "HEALTHY", "G2": "HEALTHY"})
            policy = safe_read_json(POLICY_PATH, DEFAULT_POLICY)
            if state.baseline_metrics is None and metrics["approval_rate"] > 0:
                state.baseline_metrics = dict(metrics)
            base = state.baseline_metrics or {k: 0.0 for k in metrics.keys()}
            # Prefer runtime adaptation status when reporting engine state
            if state.adaptation_status == "running":
                engine_state = "adapting"
            else:
                engine_state = extract_engine_state(list(state.kernel_log))
            def _compute_semantic_reason(metrics: dict[str, float], engine_state: str) -> str:
                thresh = parse_health_thresholds()
                # explicit running / cycle outcomes
                if state.adaptation_status == "running":
                    return "Adaptation running — reasoning, policy synthesis or verification in progress"
                if state.adaptation_status == "success":
                    return f"Recovered — approval_rate={metrics.get('approval_rate',0):.3f} meets threshold {thresh.get('min_approval_rate'):.2f}"
                if state.adaptation_status == "max_cycles":
                    return "Adaptation ended — reached max cycles without recovery"
                # cooldown explanatory message
                if engine_state == "cooldown":
                    return "Cooldown — suppression active after a recent adaptation to avoid oscillation"
                # detect first semantic violation
                checks = [
                    ("approval_rate", "min_approval_rate", lambda v, t: v < t, "Approval rate below threshold"),
                    ("p95_latency_ms", "max_p95_latency_ms", lambda v, t: v > t, "P95 latency too high"),
                    ("timeout_rate", "max_timeout_rate", lambda v, t: v > t, "Timeout rate too high"),
                    ("sla_breach_rate", "max_sla_breach_rate", lambda v, t: v > t, "SLA breach rate too high"),
                    ("retry_amplification_factor", "max_retry_amplification", lambda v, t: v > t, "Retry amplification too high"),
                ]
                for key, thkey, cond, label in checks:
                    v = metrics.get(key)
                    t = thresh.get(thkey)
                    if v is None or t is None:
                        continue
                    try:
                        if cond(v, t):
                            return f"Degraded — {label}: {v:.3f} vs threshold {t:.3f}"
                    except Exception:
                        continue
                return "Monitoring — metrics within configured health thresholds"

            semantic_reason = _compute_semantic_reason(metrics, engine_state)

            payload = {
                "metrics": metrics,
                "deltas": {
                    "approval_rate_delta": metrics["approval_rate"] - base.get("approval_rate", 0.0),
                    "rolling_success_rate_delta": metrics["rolling_success_rate"] - base.get("rolling_success_rate", 0.0),
                    "retry_amplification_delta": metrics["retry_amplification_factor"] - base.get("retry_amplification_factor", 0.0),
                    "sla_breach_rate_delta": metrics["sla_breach_rate"] - base.get("sla_breach_rate", 0.0),
                    "circuit_open_rate_delta": metrics["circuit_open_rate"] - base.get("circuit_open_rate", 0.0),
                },
                "gateway_regimes": gateway,
                "policy": {
                    "provider_weights": policy.get("provider_weights", {"G1": 0.5, "G2": 0.5}),
                    "max_retry": policy.get("max_retry", 3),
                    "base_backoff_ms": policy.get("base_backoff_ms", 100),
                    "max_retries_per_window": policy.get("max_retries_per_window", 200),
                },
                "engine_state": engine_state,
                "adaptation_status": state.adaptation_status,
                "adaptation_status_reason": semantic_reason,
                "tlc": {
                    "status": state.tlc_status,
                    "violations": state.tlc_violations,
                    "output_path": state.tlc_output_path,
                },
                "simulator_log": list(state.simulator_log)[-30:],
                "kernel_log": list(state.kernel_log)[-30:],
                "adaptation_log": list(state.adaptation_log)[-20:],
                "event_tail": events[-20:],
                "system_status": state.system_status,
                "engine_meta": {"last_trigger": state.last_trigger, "last_status": state.last_status, "cycles": state.cycles, "max_cycles": parse_max_cycles()},
                "thresholds": parse_health_thresholds(),
            }
            yield {"event": "state", "data": json.dumps(payload)}
            await asyncio.sleep(2)

    return EventSourceResponse(event_generator())


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
