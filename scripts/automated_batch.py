#!/usr/bin/env python3
import subprocess, time, sys, os

N = int(os.environ.get("BATCH_RUNS", "10"))
script = os.path.join(os.path.dirname(__file__), "automated_run_test.py")
successes = 0

for i in range(1, N+1):
    print(f"[batch] Run {i}/{N}", flush=True)
    try:
        p = subprocess.run([sys.executable, script], cwd=os.getcwd(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=80)
        print(p.stdout)
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        print("[batch] run timed out", flush=True)
        if getattr(e, 'stdout', None):
            print(e.stdout)
        rc = 124
    if rc == 0:
        successes += 1
    else:
        print(f"[batch] Run {i} failed (exit={rc})", flush=True)
    time.sleep(1)

print(f"[batch] Completed {N} runs: successes={successes}/{N}", flush=True)
# exit 0 only if all succeeded
sys.exit(0 if successes == N else 1)
