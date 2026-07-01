from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STREAM_PATH = ROOT / "data" / "streams" / "events.jsonl"
POLICY_PATH = ROOT / "data" / "policies" / "policy.json"
GATEWAY_CMD_PATH = ROOT / "data" / "gateway_commands.json"
FRONTEND_DIR = ROOT / "frontend"

DEFAULT_POLICY = {
    "provider_priority": ["G1", "G2"],
    "provider_weights": {"G1": 0.5, "G2": 0.5},
    "weight_learning_rate": 0.1,
    "max_retry": 3,
    "retryable_statuses": ["SOFT_DECLINE", "TIMEOUT"],
    "base_backoff_ms": 100,
    "backoff_multiplier": 2.0,
    "retry_budget_window_ms": 60000,
    "max_retries_per_window": 200,
}


@dataclass
class RuntimeState:
    simulator_proc: asyncio.subprocess.Process | None = None
    kernel_proc: asyncio.subprocess.Process | None = None
    simulator_log: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    kernel_log: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    adaptation_log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=30))
    agent_log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=100))
    stream_events_cache: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=4000))
    heartbeat_task: asyncio.Task[None] | None = None
    sim_reader_task: asyncio.Task[None] | None = None
    kernel_reader_task: asyncio.Task[None] | None = None
    baseline_metrics: dict[str, float] | None = None
    system_status: str = "IDLE"
    last_trigger: str = "-"
    last_status: str = "-"
    adaptation_status: str | None = None
    cycles: int = 0
    tlc_status: str | None = None
    tlc_violations: list[str] | None = None
    tlc_output_path: str | None = None
    # New fields to disambiguate TLC model-checker vs Python fallback
    tlc_ran: bool = False
    tlc_result: str | None = None  # one of 'passed','failed','timed_out','error', or None
    verification_status: str | None = None  # overall verifier outcome: 'passed'|'failed'|None


state = RuntimeState()


def classify_adaptation_type(message: str) -> str:
    lowered = message.lower()
    if "policy deployed" in lowered or "policy vector received" in lowered:
        return "deployed"
    if "recovery" in lowered or "success" in lowered:
        return "recovery"
    return "reasoning"


class AdaptationBufferHandler(logging.Handler):
    def __init__(self, buffer: deque[dict[str, Any]]):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "type": classify_adaptation_type(record.getMessage()),
            }
        )


adaptation_logger = logging.getLogger("adaptation")
adaptation_logger.setLevel(logging.INFO)
adaptation_logger.propagate = False
adaptation_logger.handlers.clear()
adaptation_logger.addHandler(AdaptationBufferHandler(state.adaptation_log))


def safe_read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        if not path.exists():
            return dict(fallback)
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(fallback)


def ensure_gateway_file() -> dict[str, Any]:
    payload = safe_read_json(
        GATEWAY_CMD_PATH,
        {
            "regimes": {"G1": "HEALTHY", "G2": "HEALTHY"},
            "commands": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    GATEWAY_CMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    GATEWAY_CMD_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


async def read_stream_tail(max_lines: int = 3000) -> list[dict[str, Any]]:
    if not STREAM_PATH.exists():
        return []
    lines = STREAM_PATH.read_text(encoding="utf-8").splitlines()
    parsed: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed


def _pct(numer: float, denom: float) -> float:
    return numer / denom if denom > 0 else 0.0


def compute_metrics(events: list[dict[str, Any]]) -> dict[str, float]:
    attempts = [e for e in events if e.get("event_type") == "AttemptResult"]
    routes = [e for e in events if e.get("event_type") == "RouteDecision"]
    txns = [e for e in events if e.get("event_type") == "NewTransaction"]
    total_attempts, total_txns = len(attempts), max(len(txns), 1)
    successes = sum(1 for e in attempts if e.get("status") == "SUCCESS")
    timeouts = sum(1 for e in attempts if e.get("status") == "TIMEOUT")
    failed = sum(1 for e in attempts if e.get("status") in {"FAILED", "SOFT_DECLINE", "HARD_DECLINE"})
    total_cost = sum(float(e.get("provider_cost", 0.0)) for e in attempts)
    p95_vals = sorted(float(e.get("processing_latency_ms", 0.0)) for e in attempts)
    p95_latency = p95_vals[int(0.95 * (len(p95_vals) - 1))] if p95_vals else 0.0
    avg_decision_latency = _pct(sum(float(e.get("decision_latency_ms", 0.0)) for e in routes), max(len(routes), 1))
    return {
        "approval_rate": _pct(successes, max(successes + failed + timeouts, 1)),
        "rolling_success_rate": _pct(successes, max(total_attempts, 1)),
        "retry_amplification_factor": _pct(total_attempts, total_txns),
        "sla_breach_rate": _pct(failed + timeouts, max(total_attempts, 1)),
        "circuit_open_rate": 0.0,
        "average_attempts_per_txn": _pct(total_attempts, total_txns),
        "timeout_rate": _pct(timeouts, max(total_attempts, 1)),
        "cost_per_successful_txn": _pct(total_cost, max(successes, 1)),
        "average_decision_latency": avg_decision_latency,
        "p95_latency_ms": p95_latency,
    }


def extract_engine_state(kernel_lines: list[str]) -> str:
    joined = "\n".join(kernel_lines[-20:]).lower()
    if "[adaptation] starting loop" in joined:
        return "adapting"
    if "entering cooldown" in joined or "backoff" in joined:
        return "cooldown"
    return "monitoring"


async def capture_process_output(proc: asyncio.subprocess.Process, buffer: deque[str], source: str) -> None:
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        buffer.append(text)
        # Detect structured agent events emitted by the kernel process.
        # Kernel logs a single-line JSON payload prefixed with the marker
        # "[adaptation][agent]" so we can parse and surface it to the UI.
        if "[adaptation][agent]" in text:
            try:
                idx = text.index("[adaptation][agent]")
                raw = text[idx + len("[adaptation][agent]"):].strip()
                parsed = json.loads(raw)
                state.agent_log.append(parsed)
            except Exception:
                # best-effort fallback: try to extract JSON object from the line
                import re as _re_json
                m = _re_json.search(r"(\{.*\})", text)
                if m:
                    try:
                        parsed = json.loads(m.group(1))
                        state.agent_log.append(parsed)
                    except Exception:
                        pass
        if "[adaptation]" in text:
            adaptation_logger.info(text)
            lower = text.lower()
            # Reset TLC markers at the start of a new adaptation loop
            if "starting loop" in text:
                state.last_trigger = text
                state.adaptation_status = "running"
                state.tlc_ran = False
                state.tlc_result = None
                state.verification_status = None
                state.tlc_violations = None
                state.tlc_output_path = None
                state.tlc_status = None

            if "loop ended" in text or "policy deployed" in text:
                state.last_status = text
                # try to extract explicit loop-ended status
                import re as _re2
                mstat = _re2.search(r"loop ended\s*—\s*status:\s*(\w+)", text)
                if mstat:
                    state.adaptation_status = mstat.group(1)
                # also map other phrases
                if "recovery confirmed" in text:
                    state.adaptation_status = "success"
                if "max cycles reached" in text:
                    state.adaptation_status = "max_cycles"

            # Detect explicit TLC run start/result messages emitted by the verifier
            if "running tlc for spec" in lower:
                state.tlc_ran = True
                state.tlc_result = None
                state.tlc_status = "running"

            if "tlc passed for spec" in lower or "tlc passed" in lower:
                state.tlc_ran = True
                state.tlc_result = "passed"
                state.tlc_violations = None
                state.tlc_status = "passed"

            if "tlc run timed out or errored" in lower or "tlc timed out after" in lower:
                state.tlc_ran = True
                state.tlc_result = "timed_out"
                state.tlc_status = "timed_out"

            # parse overall verifier outcome (Python fallback or TLC) — do not
            # overwrite tlc_result, keep separate verification_status
            if "verification passed" in lower:
                state.verification_status = "passed"
                # Map an overall verifier pass to tlc_status if no explicit
                # model-checker result was recorded (Python fallback case).
                if not state.tlc_result:
                    state.tlc_status = "passed"
            if "verification failed" in lower:
                state.verification_status = "failed"
                # try to extract violations text after a dash or colon
                import re as _re2
                mviol = _re2.search(r"verification failed.*?[-:\u2014]\s*(.*)", text)
                if mviol:
                    vals = mviol.group(1)
                    state.tlc_violations = [v.strip() for v in re.split(r"[;,:]", vals) if v.strip()]
                else:
                    state.tlc_violations = [text]
                if not state.tlc_result:
                    state.tlc_status = "failed"

            # Parse explicit cycle counts from adaptation logs where possible
            import re
            m = re.search(r"cycle\s+(\d+)(?:\s*/\s*(\d+))?", text)
            if m:
                try:
                    state.cycles = int(m.group(1))
                except Exception:
                    state.cycles += 1
        if source == "kernel" and "[engine] cure trigger" in text:
            state.last_trigger = text


async def heartbeat() -> None:
    while state.system_status == "RUNNING":
        state.stream_events_cache.clear()
        state.stream_events_cache.extend(await read_stream_tail())
        await asyncio.sleep(2)


def python_env() -> dict[str, str]:
    env = os.environ.copy()
    root, sim = str(ROOT), str(ROOT / "simulator")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([p for p in [root, sim, existing] if p])
    return env


def parse_max_cycles() -> int | None:
    """Read kernel/adaptation/loop.py and extract MAX_CYCLES if present."""
    try:
        p = ROOT / "kernel" / "adaptation" / "loop.py"
        txt = p.read_text(encoding="utf-8")
        import re
        m = re.search(r"MAX_CYCLES\s*=\s*(\d+)", txt)
        if m:
            return int(m.group(1))
    except Exception:
        return None


def parse_health_thresholds() -> dict[str, float]:
    """Parse HealthThresholds attributes from kernel/aggregator/aggregator.py.
    Returns a dict suitable for sending to the frontend.
    """
    defaults = {
        "min_approval_rate": 0.85,
        "max_p95_latency_ms": 500.0,
        "max_timeout_rate": 0.05,
        "max_sla_breach_rate": 0.10,
        "max_retry_amplification": 2.0,
        "max_circuit_open_rate": 0.20,
    }
    try:
        p = ROOT / "kernel" / "aggregator" / "aggregator.py"
        txt = p.read_text(encoding="utf-8")
        import re
        found = {}
        for k in defaults.keys():
            # allow both with or without type annotation
            pattern = rf"{k}\s*[:=].*?=\s*([0-9.eE+-]+)"
            m = re.search(pattern, txt)
            if m:
                try:
                    found[k] = float(m.group(1))
                except Exception:
                    pass
        if found:
            for k, v in defaults.items():
                defaults[k] = found.get(k, v)
    except Exception:
        pass
    return defaults
