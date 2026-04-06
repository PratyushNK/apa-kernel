"""Run a single TLC verification without a timeout to measure duration.

Usage:
  python -u scripts/tlc_timeout_test.py

This script uses `MockLLM.generate_structured(PolicyParams, ...)` to
construct a candidate policy and runs the TLA+ verifier with no
subprocess timeout so we can observe how long a full TLC run takes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
import os

from services.llms.mock import MockLLM

try:
	from kernel.verification.verify import PolicyVerifier, PolicyParams
except Exception as e:
	print("Failed to import verifier:", e)
	raise


def main() -> int:
	repo_root = Path(__file__).resolve().parent.parent
	jar_path = repo_root / "kernel" / "verification" / "tla_specs" / "tla2tools.jar"

	mock = MockLLM()
	print("Generating policy proposal from MockLLM...")
	policy = mock.generate_structured(PolicyParams, "tlc timeout test")

	if not jar_path.exists():
		print(f"tla2tools.jar not found at {jar_path}; cannot run TLC integration test")
		return 2

	verifier = PolicyVerifier(jar_path=jar_path)

	# Configure TLCRunner timeout for test runs. Use
	# VERIFIER_TLC_TEST_UNBOUNDED=1 to disable timeout (not recommended),
	# otherwise set VERIFIER_TLC_TEST_TIMEOUT (seconds, default 600).
	try:
		if os.getenv("VERIFIER_TLC_TEST_UNBOUNDED", "0") == "1":
			verifier._tlc.timeout = None
			print("[tlc_timeout_test] running with UNBOUNDED TLCRunner timeout")
		else:
			verifier._tlc.timeout = int(os.getenv("VERIFIER_TLC_TEST_TIMEOUT", "600"))
			print(f"[tlc_timeout_test] TLCRunner timeout set to {verifier._tlc.timeout}s")
	except Exception:
		print("Could not set TLCRunner timeout; continuing with configured timeout")

	print("Starting TLC run (this may take a while)...")
	start = time.perf_counter()
	try:
		result = verifier.verify_custom("mock_tlc_test", policy)
	except Exception as e:
		print("Verifier raised exception:", e)
		return 3
	elapsed = time.perf_counter() - start

	print(f"\nVerification finished in {elapsed:.2f}s")
	print(f"Overall status: {result.status}  error={result.error!r}")

	if result.tlc_output:
		print("\n--- TLC output (tail) ---")
		for ln in result.tlc_output.strip().splitlines()[-200:]:
			print(ln)
		print("--- end TLC output ---\n")
	else:
		print("No TLC output captured (clean pass suppressed or SANY skipped)")

	# Parse a compact summary from TLC output if present
	import re
	gen = dist = depth = left = None
	if result.tlc_output:
		m = re.search(r"(?P<generated>[0-9,]+) states generated, (?P<distinct>[0-9,]+) distinct states found(?:, (?P<left>[0-9,]+) states left on queue)?", result.tlc_output)
		if m:
			gen = int(m.group("generated").replace(",", ""))
			dist = int(m.group("distinct").replace(",", ""))
			left = int(m.group("left").replace(",", "")) if m.group("left") else None
		md = re.search(r"The depth of the complete state graph search is (?P<depth>\d+)", result.tlc_output)
		if md:
			depth = int(md.group("depth"))

	max_steps_env = os.getenv("VERIFIER_TLC_MAX_STEPS") or "(unset)"
	print(f"SUMMARY: max_steps={max_steps_env} elapsed={elapsed:.2f} generated={gen} distinct={dist} depth={depth} left={left}")

	return 0 if result.passed() else 1


if __name__ == "__main__":
	sys.exit(main())

