#!/usr/bin/env python3
import urllib.request
import urllib.error
import json
import time
import sys

BASE = "http://127.0.0.1:8000"


def post(path):
    try:
        req = urllib.request.Request(BASE + path, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            try:
                return data.decode("utf-8")
            except Exception:
                return None
    except Exception as e:
        print("POST error", path, e, file=sys.stderr)
        return None


def run_once(timeout_s: int = 50) -> int:
    print("[test] POST /start")
    post("/start")
    # give the processes a moment to start
    time.sleep(2.0)

    print("[test] POST /gateway/G1/outage")
    post("/gateway/G1/outage")

    stream_url = BASE + "/stream"
    print(f"[test] connecting to {stream_url}")

    start = time.time()
    deadline = start + timeout_s
    success = False

    try:
        req = urllib.request.Request(stream_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            # Read lines until timeout or success
            while time.time() < deadline:
                line = r.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                try:
                    s = line.decode("utf-8").strip()
                except Exception:
                    continue
                if not s:
                    continue
                # SSE data lines are prefixed with 'data:'
                if s.startswith("data:"):
                    data = s[len("data:"):].strip()
                    try:
                        payload = json.loads(data)
                    except Exception as e:
                        print("[test] failed to parse JSON from data line:", e, file=sys.stderr)
                        continue
                    ast = payload.get("adaptation_status")
                    print("[stream] adaptation_status=", ast)
                    # Also print a short adaptation log snippet for diagnostics
                    try:
                        al = payload.get("adaptation_log", [])
                        if al:
                            msg = al[-1].get("message") if isinstance(al[-1], dict) else str(al[-1])
                            print("[stream] last adaptation msg=", msg)
                    except Exception:
                        pass
                    if ast == "success":
                        print("[test] Recovery confirmed in stream")
                        success = True
                        break
                # else ignore other SSE control lines
    except Exception as e:
        print("[test] stream connection error:", e, file=sys.stderr)

    print("[test] POST /stop")
    post("/stop")
    print(f"[test] finished, success={success}")
    return 0 if success else 2


if __name__ == '__main__':
    code = run_once(50)
    sys.exit(code)
