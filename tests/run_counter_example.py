from pathlib import Path
import json

from kernel.verification import verify as v


def run():
    jar = Path("kernel/verification/tla_specs/tla2tools.jar")
    pv = v.PolicyVerifier(jar_path=jar if jar.exists() else None)

    # Construct a params object with an unknown provider in weights to try
    # to trigger I5_WeightDomainValid in TLC.
    params = v.PolicyParams(provider_priority=["G1", "G2"], provider_weights={"G1": 0.5, "X": 0.5})
    print("Running verify_custom with params:", params)
    res = pv.verify_custom("counter_example", params)
    print(res.summary())
    if res.tlc_output:
        out_path = Path("kernel/verification/tla_specs/states/counter_example_tlc.txt")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(res.tlc_output)
        print(f"Wrote TLC output to {out_path}")
    print(json.dumps({
        "suite": res.suite_name,
        "status": res.status.value,
        "error": res.error,
    }, indent=2))


if __name__ == "__main__":
    run()
