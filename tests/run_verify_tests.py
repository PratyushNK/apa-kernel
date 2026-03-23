import inspect
import importlib
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory


def run_tests():
    mod = importlib.import_module("tests_verify")
    funcs = [getattr(mod, n) for n in dir(mod) if n.startswith("test_")]
    total = len(funcs)
    passed = 0
    results = []

    for fn in funcs:
        name = fn.__name__
        try:
            sig = inspect.signature(fn)
            if "tmp_path" in sig.parameters:
                with TemporaryDirectory() as td:
                    tmp = Path(td)
                    fn(tmp)
            else:
                fn()
            results.append((name, True, ""))
            passed += 1
            print(f"PASS: {name}")
        except AssertionError as ae:
            tb = traceback.format_exc()
            results.append((name, False, tb))
            print(f"FAIL: {name}\n{tb}")
        except Exception as e:
            tb = traceback.format_exc()
            results.append((name, False, tb))
            print(f"ERROR: {name}\n{tb}")

    print(f"\nSummary: {passed}/{total} tests passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
