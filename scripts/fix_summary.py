#!/usr/bin/env python3
"""Postprocess simulator outputs to fix timestamp mapping and adaptation cycles.

Writes a corrected `data/streams/scenario_summary.json` and a CSV alongside a backup.

Usage: python3 scripts/fix_summary.py
Optional: pass a run log path as first arg to extract cycles from logs.
"""
import json
import glob
import os
import sys
from statistics import median


WORKDIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVENTS_PATH = os.path.join(WORKDIR, "data/streams/events.jsonl")
ADAPT_PATH = os.path.join(WORKDIR, "data/streams/adaptations.jsonl")
RESULTS_GLOB = os.path.join(WORKDIR, "tests/scenario_outputs/*.result.json")
SUMMARY_PATH = os.path.join(WORKDIR, "data/streams/scenario_summary.json")


def load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                # ignore malformed lines
                continue
    return out


def load_results():
    results = []
    for p in sorted(glob.glob(RESULTS_GLOB)):
        try:
            with open(p, "r", encoding="utf-8") as f:
                results.append(json.load(f))
        except Exception:
            continue
    return results


def extract_numeric_ts(ev):
    vals = []
    # common keys that contain relative timestamps in events.jsonl
    for k in ("created_at", "timestamp", "started_at", "completed_at", "decision_time", "ts"):
        v = ev.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                vals.append(float(v))
        except Exception:
            pass
    return vals


def find_disturbance_events(events):
    # map disturbance_name -> list of (index, event)
    dd = {}
    for i, ev in enumerate(events):
        if ev.get("event_type") == "Disturbance":
            name = ev.get("disturbance")
            dd.setdefault(name, []).append((i, ev))
    return dd


def compute_window_median(events, idx, window=25):
    lo = max(0, idx - window)
    hi = min(len(events), idx + window + 1)
    nums = []
    for j in range(lo, hi):
        nums.extend([v for v in extract_numeric_ts(events[j]) if v < 1e9])
    if not nums:
        return None
    return median(nums)


def find_nonstub_cycles(adaptations, injection_ms):
    # find adaptation entries near injection_ms (converted to ms)
    candidates = []
    for a in adaptations:
        # adapt 'ts' can be epoch seconds (float) or epoch ms
        ts = a.get("ts")
        if ts is None:
            continue
        ts_ms = float(ts) * 1000 if ts < 1e12 else float(ts)
        if abs(ts_ms - injection_ms) <= 300000:  # within 5 minutes
            if not a.get("stub", False) and isinstance(a.get("cycle_count"), (int, float)):
                candidates.append((ts_ms, int(a.get("cycle_count"))))
    if not candidates:
        return None
    # return the max cycle_count observed
    return max(c for _, c in candidates)


def main():
    events = load_jsonl(EVENTS_PATH)
    adaptations = load_jsonl(ADAPT_PATH)
    results = load_results()

    if not results:
        print("No per-scenario result files found (tests/scenario_outputs). Exiting.")
        sys.exit(1)

    disturbance_map = find_disturbance_events(events)

    # optional run log (pass path as first arg) to extract breach wallclock times
    run_log_path = sys.argv[1] if len(sys.argv) > 1 else None
    runlog_lines = None
    if run_log_path and os.path.exists(run_log_path):
        try:
            with open(run_log_path, "r", encoding="utf-8") as rf:
                runlog_lines = [l.rstrip("\n") for l in rf]
        except Exception:
            runlog_lines = None

    changes = []
    corrected = []

    for r in results:
        scenario = r.get("scenario")
        name = r.get("disturbance")
        inj_ms = r.get("injection_ts_ms")
        breach = r.get("breach_ts_ms")
        time_to_breach = r.get("time_to_breach_ms")

        # find the disturbance event index if possible
        idx = None
        if name in disturbance_map:
            # pick the last occurrence
            idx = disturbance_map[name][-1][0]

        updated = False

        # If we have the run log, try to extract wallclock breach time (preferred)
        if runlog_lines:
            try:
                marker = f"[disturbance] applied {name}"
                marker_idx = None
                for li, line in enumerate(runlog_lines):
                    if marker in line:
                        marker_idx = li
                # fallback: look for running header
                if marker_idx is None:
                    header = f"=== RUNNING: {scenario}"
                    for li, line in enumerate(runlog_lines):
                        if header in line:
                            marker_idx = li
                            break

                if marker_idx is not None:
                    import re, datetime
                    breach_line = None
                    # scan a short window after the marker for the first breach log
                    for line in runlog_lines[marker_idx: marker_idx + 300]:
                        if "breach=True" in line:
                            breach_line = line
                            break
                    if breach_line:
                        m = re.match(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*breach=True", breach_line)
                        if m:
                            dt = datetime.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
                            epoch_ms = int(dt.timestamp() * 1000)
                            if inj_ms and isinstance(inj_ms, (int, float)):
                                ttb = int(epoch_ms - inj_ms)
                                if 0 <= ttb <= 600000:
                                    r["breach_ts_ms"] = epoch_ms
                                    r["time_to_breach_ms"] = ttb
                                    updated = True
                                    changes.append((scenario, "breach from runlog", epoch_ms, ttb))
                    # also try to extract adaptation cycle count from nearby engine logs
                    try:
                        cycles_line = None
                        for line2 in runlog_lines[marker_idx: marker_idx + 600]:
                            if "adaptation done" in line2 and "cycles=" in line2:
                                cycles_line = line2
                                break
                        if cycles_line:
                            m2 = re.search(r"cycles=(?P<cycles>\d+)", cycles_line)
                            if m2:
                                cycles_val = int(m2.group("cycles"))
                                # only set when we don't already have a non-null value
                                if r.get("adaptation_cycles") in (None, "null"):
                                    r["adaptation_cycles"] = cycles_val
                                    updated = True
                                    changes.append((scenario, "adaptation_cycles from runlog", cycles_val))
                    except Exception:
                        pass
            except Exception:
                pass

        # fix negative or small breach timestamps
        if breach is not None:
            try:
                breach_val = float(breach)
            except Exception:
                breach_val = None
        else:
            breach_val = None

        if inj_ms and isinstance(inj_ms, (int, float)) and breach_val is not None:
            if breach_val < 1e12 and inj_ms >= 1e12 and idx is not None:
                med_rel = compute_window_median(events, idx, window=25)
                if med_rel is not None:
                    offset = inj_ms - med_rel
                    new_breach_ms = int(round(breach_val + offset))
                    new_ttb = int(round(new_breach_ms - inj_ms))
                    # sanity checks: ttb should be non-negative and not absurd
                    if 0 <= new_ttb <= 600000:
                        r["breach_ts_ms"] = new_breach_ms
                        r["time_to_breach_ms"] = new_ttb
                        updated = True
                        changes.append((scenario, "mapped relative breach to epoch", breach_val, new_breach_ms, new_ttb))
                    else:
                        # mapping produced implausible numbers -> clear instead
                        r["breach_ts_ms"] = None
                        r["time_to_breach_ms"] = None
                        updated = True
                        changes.append((scenario, "cleared implausible mapped breach", breach_val))

        # if time_to_breach negative (old buggy state) try to repair similarly
        if r.get("time_to_breach_ms") is not None and isinstance(r.get("time_to_breach_ms"), (int, float)):
            if r["time_to_breach_ms"] < 0 and r.get("breach_ts_ms") is not None and idx is not None:
                # attempt same mapping using the stored breach value (which may be relative)
                b = r["breach_ts_ms"]
                if b < 1e12:
                    med_rel = compute_window_median(events, idx, window=25)
                    if med_rel is not None:
                        offset = inj_ms - med_rel
                        new_breach_ms = int(round(b + offset))
                        new_ttb = int(round(new_breach_ms - inj_ms))
                        if 0 <= new_ttb <= 600000:
                            r["breach_ts_ms"] = new_breach_ms
                            r["time_to_breach_ms"] = new_ttb
                            updated = True
                            changes.append((scenario, "fixed negative ttb", new_ttb))
                        else:
                            r["breach_ts_ms"] = None
                            r["time_to_breach_ms"] = None
                            updated = True
                            changes.append((scenario, "cleared negative ttb"))

        # fill adaptation_cycles from adaptations stream if missing
        if (r.get("adaptation_cycles") is None or r.get("adaptation_cycles") == "null") and inj_ms and idx is not None:
            # try to infer from adaptations.jsonl near injection
            cycles = find_nonstub_cycles(adaptations, inj_ms)
            if cycles is not None:
                r["adaptation_cycles"] = cycles
                updated = True
                changes.append((scenario, "filled adaptation_cycles from adaptations.jsonl", cycles))

        # fallback: if still null, try to infer recovered_confirmed from adaptations
        if (r.get("recovered_confirmed") is False or r.get("recovered_confirmed") is None) and inj_ms and idx is not None:
            # look for any adaptation entry with recovery_confirmed True near injection
            for a in adaptations:
                ts = a.get("ts")
                if ts is None:
                    continue
                ts_ms = float(ts) * 1000 if ts < 1e12 else float(ts)
                if abs(ts_ms - inj_ms) <= 300000 and a.get("recovery_confirmed"):
                    r["recovered_confirmed"] = True
                    updated = True
                    changes.append((scenario, "marked recovered_confirmed from adaptations"))
                    break

        if updated:
            corrected.append(scenario)

    # write outputs (backup then overwrite)
    if os.path.exists(SUMMARY_PATH):
        bak = SUMMARY_PATH + ".bak"
        try:
            if not os.path.exists(bak):
                os.replace(SUMMARY_PATH, bak)
        except Exception:
            pass

    # write corrected JSON (preserve ordering)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, sort_keys=False)

    # write CSV
    csv_path = os.path.join(WORKDIR, "data/streams/scenario_summary.csv")
    import csv
    with open(csv_path, "w", newline="", encoding="utf-8") as cf:
        w = csv.writer(cf)
        w.writerow(["scenario","disturbance","injection_ts_ms","breach_ts_ms","time_to_breach_ms","adaptation_cycles","recovered_confirmed","approval_before","approval_after"])
        for r in results:
            w.writerow([
                r.get("scenario"),
                r.get("disturbance"),
                r.get("injection_ts_ms"),
                r.get("breach_ts_ms"),
                r.get("time_to_breach_ms"),
                r.get("adaptation_cycles"),
                r.get("recovered_confirmed"),
                r.get("approval_before"),
                r.get("approval_after"),
            ])

    # summary
    print("Postprocess complete.")
    if corrected:
        print("Corrected scenarios:")
        for s in corrected:
            print(" -", s)
    else:
        print("No changes applied.")
    if changes:
        print("Changes detail:")
        for c in changes:
            print(" -", c)


if __name__ == "__main__":
    main()
